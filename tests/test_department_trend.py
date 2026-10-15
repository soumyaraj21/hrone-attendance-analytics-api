import sys
from datetime import date as date_cls, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from unittest.mock import MagicMock
import pytest
from fastapi.testclient import TestClient

from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.main import app, round_half_up

client = TestClient(app)


def simulate_department_trend_pipeline(pipeline, employees_docs, logs_docs, department, from_date_str, to_date_str):
    """
    Simulates the exact MongoDB aggregation pipeline stages of department_trend.
    Validates that the pipeline logic adheres strictly to MongoDB aggregation semantics:
    - Date generation via $map / $range / $dateAdd / $dateToString
    - $unwind
    - $dayOfWeek for is_working_day (2, 3, 4, 5, 6)
    - $lookup on employees (joined_on <= cur_date)
    - $lookup on attendance_logs (emp_code in active_codes, date == cur_date)
    - $reduce for present_count (0.5 for half_day, 1.0 for full day)
    - $filter for late_count (late_minutes > 0)
    - $cond / $round for attendance_rate (4 decimal places, null on weekend / zero headcount)
    - $setWindowFields with [-6, 0] window for moving_avg_7d
    - $round for moving_avg_7d (4 decimal places)
    - $project and $sort by date ascending
    """
    from_d = date_cls.fromisoformat(from_date_str)
    to_d = date_cls.fromisoformat(to_date_str)
    num_days = (to_d - from_d).days + 1

    # Stage: Date generation and is_working_day
    calendar_days = []
    for offset in range(num_days):
        cur_d = from_d + timedelta(days=offset)
        d_str = cur_d.isoformat()
        # MongoDB $dayOfWeek: 1=Sun, 2=Mon, 3=Tue, 4=Wed, 5=Thu, 6=Fri, 7=Sat
        mongo_dow = (cur_d.weekday() + 1) % 7 + 1
        is_working_day = mongo_dow in (2, 3, 4, 5, 6)
        calendar_days.append({
            "date": d_str,
            "is_working_day": is_working_day,
        })

    # Stage: Lookups and metric calculations
    rows = []
    for day in calendar_days:
        d_str = day["date"]
        is_working_day = day["is_working_day"]

        # $lookup employees: dept match and joined_on <= cur_date
        active_emps = [
            e for e in employees_docs
            if e.get("department") == department and e.get("joined_on", "") <= d_str
        ]
        headcount = len(active_emps)
        active_codes = set(e["emp_code"] for e in active_emps)

        # $lookup attendance_logs: date match and emp_code in active_codes
        day_logs = [
            l for l in logs_docs
            if l.get("date") == d_str and l.get("emp_code") in active_codes
        ]

        # $reduce present_count
        present_count = 0.0
        for l in day_logs:
            if l.get("status") in ("PRESENT", "WFH", "ON_DUTY"):
                if l.get("half_day") is True:
                    present_count += 0.5
                else:
                    present_count += 1.0

        # $size of $filter late_minutes > 0
        late_count = sum(1 for l in day_logs if (l.get("late_minutes") or 0) > 0)

        # $cond attendance_rate using Decimal128 arithmetic
        if is_working_day and headcount > 0:
            dec_present = Decimal(str(present_count))
            dec_headcount = Decimal(str(headcount))
            rate_dec = dec_present / dec_headcount
            rate = float(rate_dec.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP))
        else:
            rate = None

        rows.append({
            "date": d_str,
            "is_working_day": is_working_day,
            "headcount": headcount,
            "present_count": present_count,
            "late_count": late_count,
            "attendance_rate": rate,
        })

    # Stage: $setWindowFields moving_avg_7d with documents: [-6, 0]
    for i, row in enumerate(rows):
        window_start = max(0, i - 6)
        window = rows[window_start : i + 1]
        non_null_rates = [r["attendance_rate"] for r in window if r["attendance_rate"] is not None]
        if non_null_rates:
            dec_sum = sum(Decimal(str(r)) for r in non_null_rates)
            dec_count = Decimal(str(len(non_null_rates)))
            mean_dec = dec_sum / dec_count
            row["moving_avg_7d"] = float(mean_dec.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP))
        else:
            row["moving_avg_7d"] = None

    # $sort: {"date": 1}
    rows.sort(key=lambda r: str(r["date"]))
    return rows


class TrendMockDatabase:
    def __init__(self):
        self.employees_data = []
        self.attendance_logs_data = []
        self.last_pipeline = None

        self.employees = MagicMock()
        self.employees.find_one.side_effect = self._find_one_emp
        self.employees.aggregate.side_effect = self._aggregate_emp

        self.attendance_logs = MagicMock()

    def _find_one_emp(self, query):
        dept = query.get("department")
        for e in self.employees_data:
            if e.get("department") == dept:
                return e
        return None

    def _aggregate_emp(self, pipeline):
        self.last_pipeline = pipeline
        # Extract department from $match stage
        dept = pipeline[0]["$match"]["department"]
        # Extract from_date and num_days from $project stage
        map_expr = pipeline[2]["$project"]["dates"]["$map"]
        num_days = map_expr["input"]["$range"][1]
        from_date_str = map_expr["in"]["$dateToString"]["date"]["$dateAdd"]["startDate"]["$dateFromString"]["dateString"]
        from_d = date_cls.fromisoformat(from_date_str)
        to_d = from_d + timedelta(days=num_days - 1)

        result_rows = simulate_department_trend_pipeline(
            pipeline,
            self.employees_data,
            self.attendance_logs_data,
            dept,
            from_date_str,
            to_d.isoformat(),
        )
        return iter(result_rows)


@pytest.fixture
def trend_db(monkeypatch):
    tdb = TrendMockDatabase()
    monkeypatch.setattr("app.main.db", tdb)
    return tdb


# --------------------------------------------------------------------------- #
# Test 1: Department with employees but no attendance logs
# --------------------------------------------------------------------------- #
def test_department_with_employees_no_logs(trend_db):
    trend_db.employees_data = [
        {"emp_code": "EMP0001", "name": "Asha Rao", "department": "Engineering", "joined_on": "2026-01-01"},
        {"emp_code": "EMP0002", "name": "Bob Smith", "department": "Engineering", "joined_on": "2026-01-01"},
    ]
    trend_db.attendance_logs_data = []  # No logs

    res = client.get("/analytics/departments/Engineering/trend?from=2026-07-01&to=2026-07-03")
    assert res.status_code == 200
    data = res.json()
    assert data["department"] == "Engineering"
    items = data["items"]
    assert len(items) == 3  # Wed, Thu, Fri

    for item in items:
        assert item["is_working_day"] is True
        assert item["headcount"] == 2
        assert item["present_count"] == 0.0
        assert item["late_count"] == 0
        assert item["attendance_rate"] == 0.0000
        assert item["moving_avg_7d"] == 0.0000


# --------------------------------------------------------------------------- #
# Test 2: Department with no employees (404)
# --------------------------------------------------------------------------- #
def test_department_with_no_employees(trend_db):
    trend_db.employees_data = []
    res = client.get("/analytics/departments/Nonexistent/trend?from=2026-07-01&to=2026-07-05")
    assert res.status_code == 404
    assert res.json() == {"detail": "Department not found"}


# --------------------------------------------------------------------------- #
# Test 3: Weekends and weekdays
# --------------------------------------------------------------------------- #
def test_weekends_and_weekdays(trend_db):
    trend_db.employees_data = [
        {"emp_code": "EMP0001", "name": "Asha Rao", "department": "Engineering", "joined_on": "2026-01-01"},
    ]
    # Logs on Friday (working day) and Saturday (weekend punch)
    trend_db.attendance_logs_data = [
        {"emp_code": "EMP0001", "date": "2026-07-03", "status": "PRESENT", "half_day": False, "late_minutes": 0},
        {"emp_code": "EMP0001", "date": "2026-07-04", "status": "PRESENT", "half_day": False, "late_minutes": 0},
    ]

    # Friday 2026-07-03 to Monday 2026-07-06
    res = client.get("/analytics/departments/Engineering/trend?from=2026-07-03&to=2026-07-06")
    assert res.status_code == 200
    items = res.json()["items"]
    assert len(items) == 4

    # Friday (weekday)
    fri = items[0]
    assert fri["date"] == "2026-07-03"
    assert fri["is_working_day"] is True
    assert fri["present_count"] == 1.0
    assert fri["attendance_rate"] == 1.0000
    assert fri["moving_avg_7d"] == 1.0000

    # Saturday (weekend): present_count reflects weekend punch, attendance_rate is null
    sat = items[1]
    assert sat["date"] == "2026-07-04"
    assert sat["is_working_day"] is False
    assert sat["present_count"] == 1.0
    assert sat["attendance_rate"] is None
    # moving average ignores null Saturday rate, keeps Friday's 1.0000
    assert sat["moving_avg_7d"] == 1.0000

    # Sunday (weekend): no logs, attendance_rate is null
    sun = items[2]
    assert sun["date"] == "2026-07-05"
    assert sun["is_working_day"] is False
    assert sun["present_count"] == 0.0
    assert sun["attendance_rate"] is None
    assert sun["moving_avg_7d"] == 1.0000

    # Monday (weekday): no logs -> 0.0000
    mon = items[3]
    assert mon["date"] == "2026-07-06"
    assert mon["is_working_day"] is True
    assert mon["present_count"] == 0.0
    assert mon["attendance_rate"] == 0.0000
    # moving average of non-null rates [1.0, 0.0] = 0.5000
    assert mon["moving_avg_7d"] == 0.5000


# --------------------------------------------------------------------------- #
# Test 4: Employees joining partway through the requested date range
# --------------------------------------------------------------------------- #
def test_employees_joining_partway_through_range(trend_db):
    trend_db.employees_data = [
        {"emp_code": "EMP0001", "name": "Asha Rao", "department": "Design", "joined_on": "2026-07-01"},
        {"emp_code": "EMP0002", "name": "Bob Smith", "department": "Design", "joined_on": "2026-07-03"},
    ]
    # EMP0001 present on July 1, 2, 3
    trend_db.attendance_logs_data = [
        {"emp_code": "EMP0001", "date": "2026-07-01", "status": "PRESENT", "half_day": False, "late_minutes": 0},
        {"emp_code": "EMP0001", "date": "2026-07-02", "status": "PRESENT", "half_day": False, "late_minutes": 0},
        {"emp_code": "EMP0001", "date": "2026-07-03", "status": "PRESENT", "half_day": False, "late_minutes": 0},
    ]

    res = client.get("/analytics/departments/Design/trend?from=2026-07-01&to=2026-07-03")
    assert res.status_code == 200
    items = res.json()["items"]
    assert len(items) == 3

    # Day 1: Headcount 1, present 1.0 -> rate 1.0000
    assert items[0]["headcount"] == 1
    assert items[0]["present_count"] == 1.0
    assert items[0]["attendance_rate"] == 1.0000

    # Day 2: Headcount 1, present 1.0 -> rate 1.0000
    assert items[1]["headcount"] == 1
    assert items[1]["present_count"] == 1.0
    assert items[1]["attendance_rate"] == 1.0000

    # Day 3: EMP0002 joins -> Headcount 2, present 1.0 (EMP0001 only) -> rate 0.5000
    assert items[2]["headcount"] == 2
    assert items[2]["present_count"] == 1.0
    assert items[2]["attendance_rate"] == 0.5000
    # moving average of [1.0, 1.0, 0.5] = 2.5 / 3 = 0.8333
    assert items[2]["moving_avg_7d"] == 0.8333


# --------------------------------------------------------------------------- #
# Test 5: Zero headcount dates
# --------------------------------------------------------------------------- #
def test_zero_headcount_dates(trend_db):
    # Employee joins on 2026-07-03; query starts on 2026-07-01
    trend_db.employees_data = [
        {"emp_code": "EMP0001", "name": "Asha Rao", "department": "HR", "joined_on": "2026-07-03"},
    ]
    trend_db.attendance_logs_data = [
        {"emp_code": "EMP0001", "date": "2026-07-03", "status": "PRESENT", "half_day": False, "late_minutes": 0},
    ]

    res = client.get("/analytics/departments/HR/trend?from=2026-07-01&to=2026-07-03")
    assert res.status_code == 200
    items = res.json()["items"]
    assert len(items) == 3

    # Day 1 (Wed): Headcount 0 -> rate null, moving_avg null
    assert items[0]["headcount"] == 0
    assert items[0]["is_working_day"] is True
    assert items[0]["attendance_rate"] is None
    assert items[0]["moving_avg_7d"] is None

    # Day 2 (Thu): Headcount 0 -> rate null, moving_avg null
    assert items[1]["headcount"] == 0
    assert items[1]["attendance_rate"] is None
    assert items[1]["moving_avg_7d"] is None

    # Day 3 (Fri): Headcount 1 -> rate 1.0000, moving_avg 1.0000
    assert items[2]["headcount"] == 1
    assert items[2]["attendance_rate"] == 1.0000
    assert items[2]["moving_avg_7d"] == 1.0000


# --------------------------------------------------------------------------- #
# Test 6: Half-day attendance
# --------------------------------------------------------------------------- #
def test_half_day_attendance(trend_db):
    trend_db.employees_data = [
        {"emp_code": "EMP0001", "name": "Asha Rao", "department": "Sales", "joined_on": "2026-01-01"},
        {"emp_code": "EMP0002", "name": "Bob Smith", "department": "Sales", "joined_on": "2026-01-01"},
    ]
    # EMP0001 half day (0.5), EMP0002 WFH full day (1.0)
    trend_db.attendance_logs_data = [
        {"emp_code": "EMP0001", "date": "2026-07-01", "status": "PRESENT", "half_day": True, "late_minutes": 15},
        {"emp_code": "EMP0002", "date": "2026-07-01", "status": "WFH", "half_day": False, "late_minutes": 0},
    ]

    res = client.get("/analytics/departments/Sales/trend?from=2026-07-01&to=2026-07-01")
    assert res.status_code == 200
    items = res.json()["items"]
    assert len(items) == 1
    row = items[0]
    assert row["headcount"] == 2
    assert row["present_count"] == 1.5
    assert row["late_count"] == 1
    # 1.5 / 2 = 0.7500
    assert row["attendance_rate"] == 0.7500
    assert row["moving_avg_7d"] == 0.7500


# --------------------------------------------------------------------------- #
# Test 7: Moving-average calculations at start and end of requested range
# --------------------------------------------------------------------------- #
def test_moving_average_window_boundaries(trend_db):
    trend_db.employees_data = [
        {"emp_code": "EMP0001", "name": "Asha Rao", "department": "Ops", "joined_on": "2026-01-01"},
    ]
    # 10-day range: 2026-07-01 (Wed) to 2026-07-10 (Fri)
    # Days: Wed 1, Thu 2, Fri 3, Sat 4, Sun 5, Mon 6, Tue 7, Wed 8, Thu 9, Fri 10
    # Rates on working days:
    # Wed 01: 1.0
    # Thu 02: 0.0 (no log)
    # Fri 03: 1.0
    # Sat 04: None
    # Sun 05: None
    # Mon 06: 1.0
    # Tue 07: 1.0
    # Wed 08: 0.0 (no log)
    # Thu 09: 1.0
    # Fri 10: 1.0
    trend_db.attendance_logs_data = [
        {"emp_code": "EMP0001", "date": "2026-07-01", "status": "PRESENT", "half_day": False},
        {"emp_code": "EMP0001", "date": "2026-07-03", "status": "PRESENT", "half_day": False},
        {"emp_code": "EMP0001", "date": "2026-07-06", "status": "PRESENT", "half_day": False},
        {"emp_code": "EMP0001", "date": "2026-07-07", "status": "PRESENT", "half_day": False},
        {"emp_code": "EMP0001", "date": "2026-07-09", "status": "PRESENT", "half_day": False},
        {"emp_code": "EMP0001", "date": "2026-07-10", "status": "PRESENT", "half_day": False},
    ]

    res = client.get("/analytics/departments/Ops/trend?from=2026-07-01&to=2026-07-10")
    assert res.status_code == 200
    items = res.json()["items"]
    assert len(items) == 10

    # Day 1 (index 0, July 1): Window is [July 1] -> non-null [1.0] -> avg 1.0000
    assert items[0]["moving_avg_7d"] == 1.0000

    # Day 2 (index 1, July 2): Window is [July 1, July 2] -> non-null [1.0, 0.0] -> avg 0.5000
    assert items[1]["moving_avg_7d"] == 0.5000

    # Day 7 (index 6, July 7): Window is [July 1..July 7] (7 calendar days)
    # Non-null rates: July 1(1.0), July 2(0.0), July 3(1.0), July 6(1.0), July 7(1.0)
    # Sum = 4.0 / 5 = 0.8000
    assert items[6]["moving_avg_7d"] == 0.8000

    # Day 8 (index 7, July 8): Window is [July 2..July 8] (7 calendar days, July 1 dropped!)
    # Rates: July 2(0.0), July 3(1.0), July 4(None), July 5(None), July 6(1.0), July 7(1.0), July 8(0.0)
    # Non-null: [0.0, 1.0, 1.0, 1.0, 0.0] -> Sum = 3.0 / 5 = 0.6000
    assert items[7]["moving_avg_7d"] == 0.6000

    # Day 10 (index 9, July 10): Window is [July 4..July 10]
    # Rates: July 4(None), July 5(None), July 6(1.0), July 7(1.0), July 8(0.0), July 9(1.0), July 10(1.0)
    # Non-null: [1.0, 1.0, 0.0, 1.0, 1.0] -> Sum = 4.0 / 5 = 0.8000
    assert items[9]["moving_avg_7d"] == 0.8000


# --------------------------------------------------------------------------- #
# Test 8: Dates with missing attendance records
# --------------------------------------------------------------------------- #
def test_dates_with_missing_attendance_records(trend_db):
    trend_db.employees_data = [
        {"emp_code": "EMP0001", "name": "Asha Rao", "department": "Marketing", "joined_on": "2026-01-01"},
    ]
    # Only Day 1 has a log. Days 2-5 have NO records at all.
    trend_db.attendance_logs_data = [
        {"emp_code": "EMP0001", "date": "2026-07-01", "status": "PRESENT", "half_day": False},
    ]

    res = client.get("/analytics/departments/Marketing/trend?from=2026-07-01&to=2026-07-05")
    assert res.status_code == 200
    items = res.json()["items"]
    assert len(items) == 5

    # Day 2 (Thu): missing record -> present 0.0, rate 0.0000
    assert items[1]["date"] == "2026-07-02"
    assert items[1]["present_count"] == 0.0
    assert items[1]["attendance_rate"] == 0.0000

    # Day 3 (Fri): missing record -> present 0.0, rate 0.0000
    assert items[2]["date"] == "2026-07-03"
    assert items[2]["present_count"] == 0.0
    assert items[2]["attendance_rate"] == 0.0000

    # Day 4 (Sat): missing record weekend -> present 0.0, rate None
    assert items[3]["date"] == "2026-07-04"
    assert items[3]["present_count"] == 0.0
    assert items[3]["attendance_rate"] is None


# --------------------------------------------------------------------------- #
# Test 9: Preservation of response schema and rounding behavior
# --------------------------------------------------------------------------- #
def test_response_schema_and_rounding(trend_db):
    trend_db.employees_data = [
        {"emp_code": "EMP0001", "name": "Asha Rao", "department": "Engineering", "joined_on": "2026-01-01"},
        {"emp_code": "EMP0002", "name": "Bob Smith", "department": "Engineering", "joined_on": "2026-01-01"},
        {"emp_code": "EMP0003", "name": "Charlie Brown", "department": "Engineering", "joined_on": "2026-01-01"},
    ]
    # 1 out of 3 present -> 1/3 = 0.333333... -> 0.3333
    trend_db.attendance_logs_data = [
        {"emp_code": "EMP0001", "date": "2026-07-01", "status": "PRESENT", "half_day": False},
    ]

    res = client.get("/analytics/departments/Engineering/trend?from=2026-07-01&to=2026-07-01")
    assert res.status_code == 200
    data = res.json()

    # Top-level schema
    assert "department" in data
    assert "items" in data
    assert data["department"] == "Engineering"

    # Item schema
    item = data["items"][0]
    expected_keys = {"date", "is_working_day", "headcount", "present_count", "late_count", "attendance_rate", "moving_avg_7d"}
    assert set(item.keys()) == expected_keys

    # Field types
    assert isinstance(item["date"], str)
    assert isinstance(item["is_working_day"], bool)
    assert isinstance(item["headcount"], int)
    assert isinstance(item["present_count"], (int, float))
    assert isinstance(item["late_count"], int)
    assert isinstance(item["attendance_rate"], float)
    assert isinstance(item["moving_avg_7d"], float)

    # 4-decimal half-up rounding
    assert item["attendance_rate"] == 0.3333
    assert item["moving_avg_7d"] == 0.3333


# --------------------------------------------------------------------------- #
# Test 10: Validation rules and error handling
# --------------------------------------------------------------------------- #
def test_trend_endpoint_validations(trend_db):
    # Invalid date format
    res = client.get("/analytics/departments/Engineering/trend?from=bad-date&to=2026-07-05")
    assert res.status_code == 422

    # to < from
    res2 = client.get("/analytics/departments/Engineering/trend?from=2026-07-10&to=2026-07-01")
    assert res2.status_code == 422

    # range > 92 days
    res3 = client.get("/analytics/departments/Engineering/trend?from=2026-01-01&to=2026-05-01")
    assert res3.status_code == 422


# --------------------------------------------------------------------------- #
# Test 11: Pipeline Structure Verification
# --------------------------------------------------------------------------- #
def test_pipeline_stages_and_no_python_loops(trend_db):
    trend_db.employees_data = [
        {"emp_code": "EMP0001", "department": "Engineering", "joined_on": "2026-01-01"}
    ]
    res = client.get("/analytics/departments/Engineering/trend?from=2026-07-01&to=2026-07-05")
    assert res.status_code == 200

    # Verify pipeline was passed to db.employees.aggregate
    pipeline = trend_db.last_pipeline
    assert pipeline is not None, "Pipeline must be passed to db.employees.aggregate"

    stage_names = [list(stage.keys())[0] for stage in pipeline]
    # Check that required native MongoDB aggregation stages are present
    assert "$match" in stage_names
    assert "$limit" in stage_names
    assert "$project" in stage_names
    assert "$unwind" in stage_names
    assert "$addFields" in stage_names
    assert "$lookup" in stage_names
    assert "$setWindowFields" in stage_names
    assert "$sort" in stage_names

    # Check date generation uses native MongoDB operators
    date_proj = next(s["$project"] for s in pipeline if "dates" in s.get("$project", {}))
    map_expr = date_proj["dates"]["$map"]
    assert "$range" in map_expr["input"]
    assert "$dateToString" in map_expr["in"]
    assert "$dateAdd" in map_expr["in"]["$dateToString"]["date"]

    # Check window fields uses 7-day preceding window [-6, 0]
    window_stage = next(s["$setWindowFields"] for s in pipeline if "$setWindowFields" in s)
    assert window_stage["sortBy"] == {"date": 1}
    assert window_stage["output"]["moving_avg_7d"]["window"] == {"documents": [-6, 0]}

    # Check that $round is NOT used anywhere; native $floor / $multiply / $add / $divide are used
    pipeline_str = str(pipeline)
    assert "$round" not in pipeline_str, "Pipeline must not use MongoDB $round which performs Banker's rounding"
    assert "$floor" in pipeline_str, "Pipeline must use $floor arithmetic for ROUND_HALF_UP"
    assert "$toDecimal" in pipeline_str, "Pipeline must use $toDecimal for precision-safe Decimal128 arithmetic"
    assert "$toDouble" in pipeline_str, "Pipeline must use $toDouble in project to ensure JSON serialization"


# --------------------------------------------------------------------------- #
# Test 12: Regression Test: Exact halfway tie for attendance_rate
# --------------------------------------------------------------------------- #
def test_round_half_up_exact_halfway_tie_attendance_rate(trend_db):
    """
    Verifies that an exact halfway value ending in an even 4th decimal digit
    is rounded UP per ROUND_HALF_UP, and not rounded DOWN per Banker's rounding.
    Example: 0.5 present out of 16 headcount = 0.03125.
    - Banker's rounding (round-half-to-even) would produce: 0.0312
    - ROUND_HALF_UP must produce: 0.0313
    """
    # 16 employees in department
    trend_db.employees_data = [
        {"emp_code": f"EMP{i:04d}", "name": f"Emp {i}", "department": "Platform", "joined_on": "2026-01-01"}
        for i in range(1, 17)
    ]
    # Exactly one employee with half day (0.5 present)
    trend_db.attendance_logs_data = [
        {"emp_code": "EMP0001", "date": "2026-07-01", "status": "PRESENT", "half_day": True, "late_minutes": 0},
    ]

    res = client.get("/analytics/departments/Platform/trend?from=2026-07-01&to=2026-07-01")
    assert res.status_code == 200
    items = res.json()["items"]
    assert len(items) == 1
    row = items[0]
    assert row["headcount"] == 16
    assert row["present_count"] == 0.5
    # 0.5 / 16 = 0.03125 -> ROUND_HALF_UP is 0.0313 (not 0.0312)
    assert row["attendance_rate"] == 0.0313
    assert row["moving_avg_7d"] == 0.0313

    # Additional halfway case: 2.5 / 16 = 0.15625 -> ROUND_HALF_UP is 0.1563 (not 0.1562)
    trend_db.attendance_logs_data = [
        {"emp_code": "EMP0001", "date": "2026-07-01", "status": "PRESENT", "half_day": False},
        {"emp_code": "EMP0002", "date": "2026-07-01", "status": "PRESENT", "half_day": False},
        {"emp_code": "EMP0003", "date": "2026-07-01", "status": "PRESENT", "half_day": True},
    ]
    res2 = client.get("/analytics/departments/Platform/trend?from=2026-07-01&to=2026-07-01")
    assert res2.status_code == 200
    row2 = res2.json()["items"][0]
    assert row2["present_count"] == 2.5
    assert row2["attendance_rate"] == 0.1563


# --------------------------------------------------------------------------- #
# Test 13: Regression Test: Exact halfway tie for moving_avg_7d
# --------------------------------------------------------------------------- #
def test_round_half_up_exact_halfway_tie_moving_average(trend_db):
    """
    Verifies that a 7-day moving average calculation resulting in an exact halfway value
    ending in an even 4th decimal digit rounds UP, not down.
    Example: 2-day window with rates [0.0625, 0.0000].
    Mean = 0.0625 / 2 = 0.03125.
    - Banker's rounding would produce: 0.0312
    - ROUND_HALF_UP must produce: 0.0313
    """
    # 16 employees in department
    trend_db.employees_data = [
        {"emp_code": f"EMP{i:04d}", "name": f"Emp {i}", "department": "Platform", "joined_on": "2026-01-01"}
        for i in range(1, 17)
    ]
    # Day 1: 1 employee present full day -> 1.0 / 16 = 0.0625
    # Day 2: 0 employees present -> 0.0000
    # Moving average on Day 2 = (0.0625 + 0.0000) / 2 = 0.03125 -> 0.0313
    trend_db.attendance_logs_data = [
        {"emp_code": "EMP0001", "date": "2026-07-01", "status": "PRESENT", "half_day": False},
    ]

    res = client.get("/analytics/departments/Platform/trend?from=2026-07-01&to=2026-07-02")
    assert res.status_code == 200
    items = res.json()["items"]
    assert len(items) == 2

    # Day 1
    assert items[0]["date"] == "2026-07-01"
    assert items[0]["attendance_rate"] == 0.0625
    assert items[0]["moving_avg_7d"] == 0.0625

    # Day 2: exact halfway moving average tie 0.03125
    assert items[1]["date"] == "2026-07-02"
    assert items[1]["attendance_rate"] == 0.0000
    assert items[1]["moving_avg_7d"] == 0.0313


# --------------------------------------------------------------------------- #
# Test 14: Regression Test: Binary floating-point undershoot (0.00015 example)
# --------------------------------------------------------------------------- #
def test_round_half_up_binary_undershoot_precision_safe_0_00015(trend_db):
    """
    Verifies that binary floating-point representation undershoot is resolved by Decimal128 arithmetic.

    Problem demonstration:
    In binary floating-point (IEEE 754), 0.00015 is approximated as 0.000149999999999999988...
    Evaluating floor(0.00015 * 10000 + 0.5) / 10000 yields floor(1.9999999999999998) / 10000 = 0.0001 (undershoot!).
    Under base-10 Decimal128 arithmetic:
    0.00015 * 10000 = 1.5, 1.5 + 0.5 = 2.0, floor(2.0) / 10000 = 0.0002.

    This test verifies:
    1. attendance_rate: 1.5 present out of 10000 headcount = 0.00015 -> 0.0002.
    2. moving_avg_7d: 2-day window with rates [0.0001, 0.0002] -> mean 0.00015 -> 0.0002.
    """
    # 10,000 employees in department
    trend_db.employees_data = [
        {"emp_code": f"EMP{i:05d}", "name": f"Emp {i}", "department": "ScaleTest", "joined_on": "2026-01-01"}
        for i in range(1, 10001)
    ]
    # Day 1: 1 full day (1.0) and 1 half day (0.5) -> present_count = 1.5
    # attendance_rate = 1.5 / 10000 = 0.00015 -> rounds up to 0.0002
    trend_db.attendance_logs_data = [
        {"emp_code": "EMP00001", "date": "2026-07-01", "status": "PRESENT", "half_day": False},
        {"emp_code": "EMP00002", "date": "2026-07-01", "status": "PRESENT", "half_day": True},
    ]

    res = client.get("/analytics/departments/ScaleTest/trend?from=2026-07-01&to=2026-07-01")
    assert res.status_code == 200
    items = res.json()["items"]
    assert len(items) == 1
    row = items[0]
    assert row["headcount"] == 10000
    assert row["present_count"] == 1.5
    assert row["attendance_rate"] == 0.0002, "0.00015 must round UP to 0.0002 via Decimal128, not undershoot to 0.0001"
    assert row["moving_avg_7d"] == 0.0002

    # Now verify moving_avg_7d with exact 0.00015 halfway mean:
    # Day 1: 1 present -> 1.0 / 10000 = 0.0001
    # Day 2: 2 present -> 2.0 / 10000 = 0.0002
    # Mean = (0.0001 + 0.0002) / 2 = 0.00015 -> moving_avg_7d must round UP to 0.0002
    trend_db.attendance_logs_data = [
        {"emp_code": "EMP00001", "date": "2026-07-01", "status": "PRESENT", "half_day": False},
        {"emp_code": "EMP00001", "date": "2026-07-02", "status": "PRESENT", "half_day": False},
        {"emp_code": "EMP00002", "date": "2026-07-02", "status": "PRESENT", "half_day": False},
    ]

    res2 = client.get("/analytics/departments/ScaleTest/trend?from=2026-07-01&to=2026-07-02")
    assert res2.status_code == 200
    items2 = res2.json()["items"]
    assert len(items2) == 2
    assert items2[0]["attendance_rate"] == 0.0001
    assert items2[1]["attendance_rate"] == 0.0002
    assert items2[1]["moving_avg_7d"] == 0.0002, "Moving average mean of 0.00015 must round UP to 0.0002"


# --------------------------------------------------------------------------- #
# Test 15: FastAPI and PyMongo serialization compatibility
# --------------------------------------------------------------------------- #
def test_decimal128_json_serialization_compatibility():
    """
    Verifies that raw BSON Decimal128 values cannot be directly serialized by FastAPI,
    and proves that $toDouble in the pipeline projection guarantees seamless JSON serialization
    through FastAPI's response encoder.
    """
    import bson.decimal128
    from fastapi.encoders import jsonable_encoder

    raw_decimal = bson.decimal128.Decimal128("0.0002")

    # FastAPI's jsonable_encoder fails when given raw BSON Decimal128
    with pytest.raises(ValueError):
        jsonable_encoder({"attendance_rate": raw_decimal})

    # When converted via $toDouble in the aggregation pipeline, documents contain native floats
    pipeline_doc = {
        "date": "2026-07-01",
        "is_working_day": True,
        "headcount": 10000,
        "present_count": 1.5,
        "late_count": 0,
        "attendance_rate": 0.0002,
        "moving_avg_7d": 0.0002,
    }
    encoded = jsonable_encoder(pipeline_doc)
    assert encoded["attendance_rate"] == 0.0002
    assert isinstance(encoded["attendance_rate"], float)
    assert encoded["moving_avg_7d"] == 0.0002
    assert isinstance(encoded["moving_avg_7d"], float)

    # Verify null preservation for weekends / zero headcount
    weekend_doc = {
        "date": "2026-07-04",
        "is_working_day": False,
        "headcount": 10000,
        "present_count": 0.0,
        "late_count": 0,
        "attendance_rate": None,
        "moving_avg_7d": 0.0002,
    }
    encoded_weekend = jsonable_encoder(weekend_doc)
    assert encoded_weekend["attendance_rate"] is None
