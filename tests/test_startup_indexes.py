import sys
from unittest.mock import MagicMock
import pytest
from pymongo.errors import PyMongoError, OperationFailure, ServerSelectionTimeoutError
from fastapi.testclient import TestClient

from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.main import (
    app,
    ensure_indexes,
    REQUIRED_INDEXES,
    lifespan,
    create_mongo_client,
    DEFAULT_SERVER_SELECTION_TIMEOUT_MS,
    client as main_client,
)


class MockCollection:
    def __init__(self, name):
        self.name = name
        self.created_indexes = []

    def create_index(self, keys, **kwargs):
        name = "_".join(f"{k}_{d}" for k, d in keys)
        self.created_indexes.append((keys, kwargs, name))
        return name


class MockDatabase:
    def __init__(self):
        self.employees = MockCollection("employees")
        self.attendance_logs = MockCollection("attendance_logs")

    def __getitem__(self, name):
        return getattr(self, name)


# --------------------------------------------------------------------------- #
# Test 1: Required index definitions match assignment specifications
# --------------------------------------------------------------------------- #
def test_required_indexes_definitions():
    """Verify exact keys, ordering, and uniqueness options of all required indexes."""
    assert set(REQUIRED_INDEXES.keys()) == {"employees", "attendance_logs"}

    emp_indexes = REQUIRED_INDEXES["employees"]
    assert len(emp_indexes) == 3
    # 1. Unique primary business key
    assert emp_indexes[0] == ([("emp_code", 1)], {"unique": True})
    # 2. Department lookups and employee sorting
    assert emp_indexes[1] == ([("department", 1), ("emp_code", 1)], {})
    # 3. Department summary filtering (joined_on <= month_end)
    assert emp_indexes[2] == ([("department", 1), ("joined_on", 1)], {})

    att_indexes = REQUIRED_INDEXES["attendance_logs"]
    assert len(att_indexes) == 5
    # 1. Compound natural key (R1: one log per employee per day)
    assert att_indexes[0] == ([("emp_code", 1), ("date", 1)], {"unique": True})
    # 2. List attendance sorting (date desc, emp_code asc)
    assert att_indexes[1] == ([("date", -1), ("emp_code", 1)], {})
    # 3. Punch-out lookup for open records (emp_code asc, punch_in desc)
    assert att_indexes[2] == ([("emp_code", 1), ("punch_in", -1)], {})
    # 4. Date-scoped presence aggregations
    assert att_indexes[3] == ([("date", 1), ("status", 1)], {})
    # 5. Date-scoped late minutes aggregations (leaderboard)
    assert att_indexes[4] == ([("date", 1), ("late_minutes", 1)], {})


# --------------------------------------------------------------------------- #
# Test 2: ensure_indexes requests all indexes with exact definitions
# --------------------------------------------------------------------------- #
def test_ensure_indexes_creates_all_indexes():
    """Verify that ensure_indexes issues create_index calls for all required indexes."""
    mock_db = MockDatabase()
    result = ensure_indexes(mock_db)

    assert "employees" in result
    assert "attendance_logs" in result
    assert len(result["employees"]) == 3
    assert len(result["attendance_logs"]) == 5

    # Check employees collection calls
    emp_calls = [(keys, opts) for keys, opts, _ in mock_db.employees.created_indexes]
    assert emp_calls == [
        ([("emp_code", 1)], {"unique": True}),
        ([("department", 1), ("emp_code", 1)], {}),
        ([("department", 1), ("joined_on", 1)], {}),
    ]

    # Check attendance_logs collection calls
    att_calls = [(keys, opts) for keys, opts, _ in mock_db.attendance_logs.created_indexes]
    assert att_calls == [
        ([("emp_code", 1), ("date", 1)], {"unique": True}),
        ([("date", -1), ("emp_code", 1)], {}),
        ([("emp_code", 1), ("punch_in", -1)], {}),
        ([("date", 1), ("status", 1)], {}),
        ([("date", 1), ("late_minutes", 1)], {}),
    ]


# --------------------------------------------------------------------------- #
# Test 3: ensure_indexes is idempotent upon repeated execution
# --------------------------------------------------------------------------- #
def test_ensure_indexes_idempotency():
    """Verify that repeatedly running ensure_indexes is safe and idempotent."""
    mock_db = MockDatabase()

    first_run = ensure_indexes(mock_db)
    second_run = ensure_indexes(mock_db)

    assert first_run == second_run
    assert len(mock_db.employees.created_indexes) == 6
    assert len(mock_db.attendance_logs.created_indexes) == 10


# --------------------------------------------------------------------------- #
# Test 4: Index creation failure on employees is surfaced, not swallowed
# --------------------------------------------------------------------------- #
def test_ensure_indexes_surfaces_employees_failure():
    """Verify that an error creating an employees index is not swallowed."""
    mock_db = MockDatabase()
    mock_db.employees.create_index = MagicMock(
        side_effect=OperationFailure("Index with name: emp_code_1 already exists with different options")
    )

    with pytest.raises(RuntimeError) as exc_info:
        ensure_indexes(mock_db)

    err_msg = str(exc_info.value)
    assert "Failed to create index" in err_msg
    assert "employees" in err_msg
    assert "emp_code" in err_msg
    assert isinstance(exc_info.value.__cause__, OperationFailure)


# --------------------------------------------------------------------------- #
# Test 5: Index creation failure on attendance_logs is surfaced, not swallowed
# --------------------------------------------------------------------------- #
def test_ensure_indexes_surfaces_attendance_logs_failure():
    """Verify that an error creating an attendance_logs index is not swallowed."""
    mock_db = MockDatabase()
    mock_db.attendance_logs.create_index = MagicMock(
        side_effect=PyMongoError("WiredTiger error: disk full")
    )

    with pytest.raises(RuntimeError) as exc_info:
        ensure_indexes(mock_db)

    err_msg = str(exc_info.value)
    assert "Failed to create index" in err_msg
    assert "attendance_logs" in err_msg
    assert isinstance(exc_info.value.__cause__, PyMongoError)


# --------------------------------------------------------------------------- #
# Test 6: Database connection outage during startup surfaces clear error
# --------------------------------------------------------------------------- #
def test_ensure_indexes_surfaces_connection_outage():
    """Verify that a database outage raises an actionable RuntimeError with root cause."""
    mock_db = MockDatabase()
    mock_db.employees.create_index = MagicMock(
        side_effect=ServerSelectionTimeoutError("No servers found yet at localhost:27017")
    )

    with pytest.raises(RuntimeError) as exc_info:
        ensure_indexes(mock_db)

    err_msg = str(exc_info.value)
    assert "Failed to create index" in err_msg
    assert "employees" in err_msg
    assert isinstance(exc_info.value.__cause__, ServerSelectionTimeoutError)


# --------------------------------------------------------------------------- #
# Test 7: Lifespan fails startup when ensure_indexes raises an error
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_lifespan_aborts_on_startup_failure(monkeypatch):
    """Verify that application startup fails when ensure_indexes fails in lifespan."""
    def failing_ensure_indexes():
        raise RuntimeError("Database connection unreachable at startup")

    monkeypatch.setattr("app.main.ensure_indexes", failing_ensure_indexes)

    with pytest.raises(RuntimeError) as exc_info:
        async with lifespan(app):
            pass

    assert "Database connection unreachable at startup" in str(exc_info.value)


# --------------------------------------------------------------------------- #
# Test 8: Health check behavior with responsive vs unavailable database
# --------------------------------------------------------------------------- #
def test_health_check_contract(monkeypatch):
    """Verify GET /health returns 200 when MongoDB ping answers, 503 when ping fails."""
    mock_client = MagicMock()
    monkeypatch.setattr("app.main.client", mock_client)

    client = TestClient(app)

    # 1. Responsive database
    mock_client.admin.command.return_value = {"ok": 1.0}
    res = client.get("/health")
    assert res.status_code == 200
    assert res.json() == {"status": "ok"}
    mock_client.admin.command.assert_called_with("ping")

    # 2. Database outage
    mock_client.admin.command.side_effect = ServerSelectionTimeoutError("Connection timed out")
    res = client.get("/health")
    assert res.status_code == 503
    assert res.json() == {"detail": "Database unavailable"}


# --------------------------------------------------------------------------- #
# Test 9: Client serverSelectionTimeoutMS is finite and strictly below 20s
# --------------------------------------------------------------------------- #
def test_client_server_selection_timeout_configured():
    """Verify that default MongoClient has a finite server_selection_timeout below 20s."""
    assert DEFAULT_SERVER_SELECTION_TIMEOUT_MS == 5000
    timeout_sec = main_client.options.server_selection_timeout
    assert timeout_sec is not None
    # Must be finite, positive, and strictly below 20 seconds
    assert 0 < timeout_sec < 20.0
    assert timeout_sec == 5.0  # 5,000 ms


# --------------------------------------------------------------------------- #
# Test 10: create_mongo_client preserves URI overrides and applies defaults
# --------------------------------------------------------------------------- #
def test_create_mongo_client_uri_override():
    """Verify that create_mongo_client clamps timeouts >= 20000ms to 5000ms and preserves <20s."""
    # 1. No timeout specified -> 5000 ms (5.0s)
    c_none = create_mongo_client("mongodb://localhost:27017")
    assert c_none.options.server_selection_timeout == 5.0
    assert c_none.options.codec_options.tz_aware is True

    # 2. Explicit 5000 ms -> 5000 ms (5.0s)
    c_5k = create_mongo_client("mongodb://localhost:27017/?serverSelectionTimeoutMS=5000")
    assert c_5k.options.server_selection_timeout == 5.0
    assert c_5k.options.codec_options.tz_aware is True

    # 3. Explicit 12000 ms -> 12000 ms (12.0s) preserved
    c_12k = create_mongo_client("mongodb://localhost:27017/?serverSelectionTimeoutMS=12000")
    assert c_12k.options.server_selection_timeout == 12.0
    assert c_12k.options.codec_options.tz_aware is True

    # 4. Explicit 20000 ms -> 5000 ms (5.0s, clamped)
    c_20k = create_mongo_client("mongodb://localhost:27017/?serverSelectionTimeoutMS=20000")
    assert c_20k.options.server_selection_timeout == 5.0
    assert c_20k.options.codec_options.tz_aware is True

    # 5. Explicit 30000 ms -> 5000 ms (5.0s, clamped)
    c_30k = create_mongo_client("mongodb://localhost:27017/?serverSelectionTimeoutMS=30000")
    assert c_30k.options.server_selection_timeout == 5.0
    assert c_30k.options.codec_options.tz_aware is True

    # 6. Case-insensitivity in URI query parameter
    c_lower_valid = create_mongo_client("mongodb://localhost:27017/?serverselectiontimeoutms=8000")
    assert c_lower_valid.options.server_selection_timeout == 8.0
    assert c_lower_valid.options.codec_options.tz_aware is True

    c_lower_clamp = create_mongo_client("mongodb://localhost:27017/?serverselectiontimeoutms=25000")
    assert c_lower_clamp.options.server_selection_timeout == 5.0
    assert c_lower_clamp.options.codec_options.tz_aware is True
