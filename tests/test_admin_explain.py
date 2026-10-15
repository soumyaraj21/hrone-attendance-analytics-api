import sys
from unittest.mock import MagicMock
import pytest
from fastapi.testclient import TestClient

from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.main import (
    app,
    build_attendance_list_query,
    build_employee_monthly_pipeline,
    build_department_summary_pipeline,
    build_late_leaderboard_pipeline,
    build_department_trend_pipeline,
)

client = TestClient(app)


@pytest.fixture
def mock_db(monkeypatch):
    mdb = MagicMock()
    monkeypatch.setattr("app.main.db", mdb)
    return mdb


# --------------------------------------------------------------------------- #
# Test 1: Unknown endpoint handling
# --------------------------------------------------------------------------- #
def test_explain_unknown_endpoint():
    res = client.get("/admin/explain/nonexistent_endpoint")
    assert res.status_code == 422
    assert "Unknown endpoint for explain" in res.json()["detail"]


# --------------------------------------------------------------------------- #
# Test 2: Missing required query parameters per endpoint
# --------------------------------------------------------------------------- #
def test_explain_missing_parameters():
    # employee_monthly requires emp_code and month
    res1 = client.get("/admin/explain/employee_monthly")
    assert res1.status_code == 422
    res2 = client.get("/admin/explain/employee_monthly?emp_code=EMP0001")
    assert res2.status_code == 422
    res3 = client.get("/admin/explain/employee_monthly?month=2026-07")
    assert res3.status_code == 422

    # department_summary requires month
    res4 = client.get("/admin/explain/department_summary")
    assert res4.status_code == 422

    # late_leaderboard requires month
    res5 = client.get("/admin/explain/late_leaderboard")
    assert res5.status_code == 422

    # department_trend requires department, from, and to
    res6 = client.get("/admin/explain/department_trend")
    assert res6.status_code == 422
    res7 = client.get("/admin/explain/department_trend?department=Engineering")
    assert res7.status_code == 422
    res8 = client.get("/admin/explain/department_trend?department=Engineering&from=2026-07-01")
    assert res8.status_code == 422


# --------------------------------------------------------------------------- #
# Test 3: Validation rules in explain
# --------------------------------------------------------------------------- #
def test_explain_validation_rules():
    # attendance_list invalid status
    res1 = client.get("/admin/explain/attendance_list?status=INVALID_STATUS")
    assert res1.status_code == 422

    # attendance_list date_from > date_to
    res2 = client.get("/admin/explain/attendance_list?date_from=2026-07-10&date_to=2026-07-01")
    assert res2.status_code == 422

    # department_trend invalid date format
    res3 = client.get("/admin/explain/department_trend?department=Engineering&from=bad-date&to=2026-07-05")
    assert res3.status_code == 422

    # department_trend to < from
    res4 = client.get("/admin/explain/department_trend?department=Engineering&from=2026-07-10&to=2026-07-01")
    assert res4.status_code == 422

    # department_trend range > 92 days
    res5 = client.get("/admin/explain/department_trend?department=Engineering&from=2026-01-01&to=2026-05-01")
    assert res5.status_code == 422


# --------------------------------------------------------------------------- #
# Test 4: attendance_list explain query shape and executionStats
# --------------------------------------------------------------------------- #
def test_explain_attendance_list_command_structure(mock_db):
    mock_explain = {"queryPlanner": {"winningPlan": {"stage": "IXSCAN"}}, "executionStats": {"nReturned": 5}}
    mock_db.command.return_value = mock_explain

    res = client.get("/admin/explain/attendance_list?emp_code=EMP0001&date_from=2026-07-01&date_to=2026-07-10&status=PRESENT&page=2&page_size=25")
    assert res.status_code == 200
    data = res.json()
    assert data["endpoint"] == "attendance_list"
    assert data["collection"] == "attendance_logs"
    assert data["explain"] == mock_explain

    mock_db.command.assert_called_once()
    args, kwargs = mock_db.command.call_args
    assert args[0] == "explain"
    assert kwargs.get("verbosity") == "executionStats"

    cmd_payload = args[1]
    assert cmd_payload["find"] == "attendance_logs"
    expected_filter = build_attendance_list_query(
        emp_code="EMP0001",
        date_from="2026-07-01",
        date_to="2026-07-10",
        status="PRESENT",
    )
    assert cmd_payload["filter"] == expected_filter
    assert cmd_payload["sort"] == {"date": -1, "emp_code": 1}
    assert cmd_payload["skip"] == 25
    assert cmd_payload["limit"] == 25


# --------------------------------------------------------------------------- #
# Test 5: employee_monthly explain pipeline and executionStats
# --------------------------------------------------------------------------- #
def test_explain_employee_monthly_full_pipeline(mock_db):
    mock_explain = {"stages": [{"$cursor": {"queryPlanner": {"winningPlan": {"stage": "IXSCAN"}}}}, {"$group": {}}]}
    mock_db.command.return_value = mock_explain

    res = client.get("/admin/explain/employee_monthly?emp_code=EMP0002&month=2026-07")
    assert res.status_code == 200
    data = res.json()
    assert data["endpoint"] == "employee_monthly"
    assert data["collection"] == "attendance_logs"
    assert data["explain"] == mock_explain

    mock_db.command.assert_called_once()
    args, kwargs = mock_db.command.call_args
    assert args[0] == "explain"
    assert kwargs.get("verbosity") == "executionStats"

    cmd_payload = args[1]
    assert cmd_payload["aggregate"] == "attendance_logs"
    expected_pipeline = build_employee_monthly_pipeline("EMP0002", "2026-07-01", "2026-07-31")
    assert cmd_payload["pipeline"] == expected_pipeline

    # Ensure material stages are present
    stage_keys = [list(s.keys())[0] for s in cmd_payload["pipeline"]]
    assert stage_keys == ["$match", "$project", "$group"]


# --------------------------------------------------------------------------- #
# Test 6: department_summary explain pipeline and executionStats
# --------------------------------------------------------------------------- #
def test_explain_department_summary_full_pipeline(mock_db):
    mock_explain = {"stages": [{"$cursor": {"queryPlanner": {"winningPlan": {"stage": "IXSCAN"}}}}]}
    mock_db.command.return_value = mock_explain

    res = client.get("/admin/explain/department_summary?month=2026-07&department=Engineering")
    assert res.status_code == 200
    data = res.json()
    assert data["endpoint"] == "department_summary"
    assert data["collection"] == "employees"

    mock_db.command.assert_called_once()
    args, kwargs = mock_db.command.call_args
    assert args[0] == "explain"
    assert kwargs.get("verbosity") == "executionStats"

    cmd_payload = args[1]
    assert cmd_payload["aggregate"] == "employees"
    expected_pipeline = build_department_summary_pipeline("2026-07-01", "2026-07-31", "Engineering")
    assert cmd_payload["pipeline"] == expected_pipeline

    # Verify lookup with date range and grouping stages are preserved
    stage_keys = [list(s.keys())[0] for s in cmd_payload["pipeline"]]
    assert stage_keys == ["$match", "$lookup", "$unwind", "$project", "$group", "$sort"]

    lookup_stage = cmd_payload["pipeline"][1]["$lookup"]
    assert lookup_stage["from"] == "attendance_logs"
    assert "pipeline" in lookup_stage
    assert "$expr" in lookup_stage["pipeline"][0]["$match"]


# --------------------------------------------------------------------------- #
# Test 7: late_leaderboard explain includes joins, sorting, and ranking
# --------------------------------------------------------------------------- #
def test_explain_late_leaderboard_includes_joins_and_ranking(mock_db):
    mock_explain = {"stages": [{"$cursor": {"queryPlanner": {"winningPlan": {"stage": "IXSCAN"}}}}]}
    mock_db.command.return_value = mock_explain

    res = client.get("/admin/explain/late_leaderboard?month=2026-07&limit=5&department=Sales")
    assert res.status_code == 200
    data = res.json()
    assert data["endpoint"] == "late_leaderboard"
    assert data["collection"] == "attendance_logs"

    mock_db.command.assert_called_once()
    args, kwargs = mock_db.command.call_args
    assert args[0] == "explain"
    assert kwargs.get("verbosity") == "executionStats"

    cmd_payload = args[1]
    assert cmd_payload["aggregate"] == "attendance_logs"
    expected_pipeline = build_late_leaderboard_pipeline("2026-07-01", "2026-07-31", 5, "Sales")
    assert cmd_payload["pipeline"] == expected_pipeline

    # Verify joins ($lookup on employees), department match, $sort, $setWindowFields with $rank, and rank limit
    pipeline_str = str(cmd_payload["pipeline"])
    assert "$lookup" in pipeline_str
    assert "employees" in pipeline_str
    assert "$setWindowFields" in pipeline_str
    assert "$rank" in pipeline_str
    assert "{'emp.department': 'Sales'}" in pipeline_str


# --------------------------------------------------------------------------- #
# Test 8: department_trend explain uses complete 16-stage pipeline
# --------------------------------------------------------------------------- #
def test_explain_department_trend_complete_pipeline(mock_db):
    mock_explain = {"stages": [{"$cursor": {"queryPlanner": {"winningPlan": {"stage": "IXSCAN"}}}}]}
    mock_db.command.return_value = mock_explain

    res = client.get("/admin/explain/department_trend?department=Engineering&from=2026-07-01&to=2026-07-05")
    assert res.status_code == 200
    data = res.json()
    assert data["endpoint"] == "department_trend"
    assert data["collection"] == "employees"

    mock_db.command.assert_called_once()
    args, kwargs = mock_db.command.call_args
    assert args[0] == "explain"
    assert kwargs.get("verbosity") == "executionStats"

    cmd_payload = args[1]
    assert cmd_payload["aggregate"] == "employees"
    expected_pipeline = build_department_trend_pipeline("Engineering", "2026-07-01", 5)
    assert cmd_payload["pipeline"] == expected_pipeline

    # Verify material stages in explained pipeline
    stage_names = [list(stage.keys())[0] for stage in cmd_payload["pipeline"]]
    assert "$match" in stage_names
    assert "$limit" in stage_names
    assert "$project" in stage_names
    assert "$unwind" in stage_names
    assert "$addFields" in stage_names
    assert "$lookup" in stage_names
    assert "$setWindowFields" in stage_names
    assert "$sort" in stage_names

    pipeline_str = str(cmd_payload["pipeline"])
    assert "$toDecimal" in pipeline_str, "Explained pipeline must include Decimal128 ROUND_HALF_UP"
    assert "$toDouble" in pipeline_str, "Explained pipeline must include $toDouble projection"
    assert "$round" not in pipeline_str, "Pipeline must not use Banker's $round"


# --------------------------------------------------------------------------- #
# Test 9: Database unavailable / error handling
# --------------------------------------------------------------------------- #
def test_explain_database_error_handling(mock_db):
    mock_db.command.side_effect = Exception("MongoDB connection pool exhausted")
    res = client.get("/admin/explain/attendance_list")
    assert res.status_code == 503
    assert "Database unavailable or explain error" in res.json()["detail"]


# --------------------------------------------------------------------------- #
# Test 10: Response schema integrity
# --------------------------------------------------------------------------- #
def test_explain_response_schema(mock_db):
    mock_explain = {
        "queryPlanner": {
            "plannerVersion": 1,
            "namespace": "attendance_db.attendance_logs",
            "winningPlan": {"stage": "IXSCAN"},
        },
        "executionStats": {
            "executionSuccess": True,
            "nReturned": 10,
            "executionTimeMillis": 2,
            "totalKeysExamined": 10,
            "totalDocsExamined": 10,
        },
    }
    mock_db.command.return_value = mock_explain

    res = client.get("/admin/explain/attendance_list")
    assert res.status_code == 200
    data = res.json()
    assert set(data.keys()) == {"endpoint", "collection", "explain"}
    assert isinstance(data["endpoint"], str)
    assert isinstance(data["collection"], str)
    assert isinstance(data["explain"], dict)
    assert data["explain"]["executionStats"]["totalDocsExamined"] == 10


# --------------------------------------------------------------------------- #
# Test 11: Regression test for BSON Decimal128 serialization in explain
# --------------------------------------------------------------------------- #
def test_explain_department_trend_decimal128_serialization(mock_db):
    from bson.decimal128 import Decimal128

    mock_explain = {
        "stages": [
            {
                "$addFields": {
                    "attendance_rate": {
                        "$const": Decimal128("10000"),
                        "half": Decimal128("0.5"),
                    }
                }
            }
        ],
        "ok": 1.0,
    }
    mock_db.command.return_value = mock_explain

    res = client.get("/admin/explain/department_trend?department=Engineering&from=2026-07-01&to=2026-07-05")
    assert res.status_code == 200
    data = res.json()
    assert data["endpoint"] == "department_trend"
    assert data["collection"] == "employees"
    assert data["explain"]["stages"][0]["$addFields"]["attendance_rate"]["$const"] == 10000
    assert data["explain"]["stages"][0]["$addFields"]["attendance_rate"]["half"] == 0.5
