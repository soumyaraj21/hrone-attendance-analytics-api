import sys
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock
import pytest
from bson import ObjectId

from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient
from app.main import (
    app,
    get_month_boundaries,
    count_working_days,
    round_half_up,
)

client = TestClient(app)


class MockCollection:
    def __init__(self, name):
        self.name = name
        self.find_one = MagicMock()
        self.find = MagicMock()
        self.find_one_and_update = MagicMock()
        self.insert_one = MagicMock()
        self.count_documents = MagicMock()
        self.aggregate = MagicMock()


class MockDatabase:
    def __init__(self):
        self.employees = MockCollection("employees")
        self.attendance_logs = MockCollection("attendance_logs")
        self.command = MagicMock()

    def __getitem__(self, name):
        return getattr(self, name)


@pytest.fixture
def mock_db(monkeypatch):
    mdb = MockDatabase()
    monkeypatch.setattr("app.main.db", mdb)
    return mdb


# --------------------------------------------------------------------------- #
# Unit Tests for Helper Functions
# --------------------------------------------------------------------------- #
def test_month_boundaries():
    start, end, days = get_month_boundaries("2026-07")
    assert start == "2026-07-01"
    assert end == "2026-07-31"
    assert days == 31

    start2, end2, days2 = get_month_boundaries("2026-02")
    assert start2 == "2026-02-01"
    assert end2 == "2026-02-28"
    assert days2 == 28


def test_count_working_days():
    # July 2026: July 1 (Wed) to July 31 (Fri) has 23 weekdays
    assert count_working_days("2026-07-01", "2026-07-31") == 23

    # Mid-month joiner: July 15 (Wed) to July 31 (Fri) has 13 weekdays
    assert count_working_days("2026-07-15", "2026-07-31") == 13

    # Inverted dates
    assert count_working_days("2026-07-10", "2026-07-01") == 0


# --------------------------------------------------------------------------- #
# Endpoint 1: GET /analytics/employees/{emp_code}/monthly
# --------------------------------------------------------------------------- #
def test_employee_monthly_bad_month():
    res = client.get("/analytics/employees/EMP0001/monthly?month=2026-13")
    assert res.status_code == 422


def test_employee_monthly_not_found(mock_db):
    mock_db.employees.find_one.return_value = None
    res = client.get("/analytics/employees/EMP9999/monthly?month=2026-07")
    assert res.status_code == 404
    assert res.json() == {"detail": "Employee not found"}


def test_employee_monthly_zero_logs_joined_before_month(mock_db):
    emp = {"emp_code": "EMP0001", "joined_on": "2026-01-01"}
    mock_db.employees.find_one.return_value = emp
    mock_db.attendance_logs.aggregate.return_value = []

    res = client.get("/analytics/employees/EMP0001/monthly?month=2026-07")
    assert res.status_code == 200
    data = res.json()
    assert data["emp_code"] == "EMP0001"
    assert data["month"] == "2026-07"
    assert data["working_days"] == 23
    assert data["present_days"] == 0.0
    assert data["attendance_pct"] == 0.00
    assert data["leave_days"] == 0
    assert data["late_count"] == 0


def test_employee_monthly_joined_after_month_end(mock_db):
    emp = {"emp_code": "EMP0002", "joined_on": "2026-08-01"}
    mock_db.employees.find_one.return_value = emp
    mock_db.attendance_logs.aggregate.return_value = []

    res = client.get("/analytics/employees/EMP0002/monthly?month=2026-07")
    assert res.status_code == 200
    data = res.json()
    assert data["working_days"] == 0
    assert data["present_days"] == 0.0
    assert data["attendance_pct"] is None


def test_employee_monthly_with_records(mock_db):
    emp = {"emp_code": "EMP0001", "joined_on": "2026-01-01"}
    mock_db.employees.find_one.return_value = emp
    mock_db.attendance_logs.aggregate.return_value = [{
        "present_days": 21.5,
        "leave_days": 1,
        "late_count": 3,
        "total_late_minutes": 45,
        "total_overtime_minutes": 60,
    }]

    res = client.get("/analytics/employees/EMP0001/monthly?month=2026-07")
    assert res.status_code == 200
    data = res.json()
    assert data["working_days"] == 23
    assert data["present_days"] == 21.5
    assert data["leave_days"] == 1
    assert data["late_count"] == 3
    assert data["total_late_minutes"] == 45
    assert data["total_overtime_minutes"] == 60
    # 21.5 / 23 * 100 = 93.47826... -> 93.48
    assert data["attendance_pct"] == 93.48


# --------------------------------------------------------------------------- #
# Endpoint 2: GET /analytics/departments/summary
# --------------------------------------------------------------------------- #
def test_department_summary_validation():
    res = client.get("/analytics/departments/summary")
    assert res.status_code == 422

    res_bad = client.get("/analytics/departments/summary?month=bad-month")
    assert res_bad.status_code == 422


def test_department_summary_includes_zero_log_employees(mock_db):
    # Department has 3 employees in headcount, 2 with logs, 1 without logs
    # All records across department: total work hours 16.5 across 2 records -> avg 8.25
    mock_db.employees.aggregate.return_value = [
        {
            "_id": "Engineering",
            "all_employees": ["EMP0001", "EMP0002", "EMP0003"],
            "present_days": 19.5,
            "total_work_hours": 16.5,
            "work_hours_count": 2,
            "late_count": 2,
            "total_late_minutes": 30,
            "leave_count": 1,
            "on_duty_count": 1,
        }
    ]

    res = client.get("/analytics/departments/summary?month=2026-07")
    assert res.status_code == 200
    data = res.json()
    assert data["month"] == "2026-07"
    assert len(data["items"]) == 1
    item = data["items"][0]
    assert item["department"] == "Engineering"
    assert item["headcount"] == 3  # All 3 employees included!
    assert item["present_days"] == 19.5
    assert item["avg_work_hours"] == 8.25
    assert item["late_count"] == 2
    assert item["total_late_minutes"] == 30
    assert item["leave_count"] == 1
    assert item["on_duty_count"] == 1


def test_department_summary_zero_records_returns_null_avg(mock_db):
    mock_db.employees.aggregate.return_value = [
        {
            "_id": "Marketing",
            "all_employees": ["EMP0010"],
            "present_days": 0.0,
            "total_work_hours": 0.0,
            "work_hours_count": 0,
            "late_count": 0,
            "total_late_minutes": 0,
            "leave_count": 0,
            "on_duty_count": 0,
        }
    ]

    res = client.get("/analytics/departments/summary?month=2026-07")
    assert res.status_code == 200
    data = res.json()
    item = data["items"][0]
    assert item["department"] == "Marketing"
    assert item["headcount"] == 1
    assert item["avg_work_hours"] is None


# --------------------------------------------------------------------------- #
# Endpoint 3: GET /analytics/leaderboard/late
# --------------------------------------------------------------------------- #
def test_late_leaderboard_validation():
    # missing month
    res = client.get("/analytics/leaderboard/late")
    assert res.status_code == 422

    # limit > 50
    res2 = client.get("/analytics/leaderboard/late?month=2026-07&limit=51")
    assert res2.status_code == 422


def test_late_leaderboard_competition_ranking_and_tie_cutoff(mock_db):
    # Simulated pipeline output with ties:
    # 1. EMP0001 (120 min) -> rank 1
    # 2. EMP0002 (90 min)  -> rank 2
    # 3. EMP0003 (90 min)  -> rank 2
    # 4. EMP0004 (60 min)  -> rank 4
    # With limit=2, cutoff rank is 2. Both EMP0002 and EMP0003 have rank 2 <= 2.
    # Therefore, 3 items are returned!
    mock_db.attendance_logs.aggregate.return_value = [
        {
            "rank": 1,
            "_id": "EMP0001",
            "emp": {"name": "Asha Rao", "department": "Engineering"},
            "total_late_minutes": 120,
            "late_count": 3,
        },
        {
            "rank": 2,
            "_id": "EMP0002",
            "emp": {"name": "Bob Smith", "department": "Engineering"},
            "total_late_minutes": 90,
            "late_count": 2,
        },
        {
            "rank": 2,
            "_id": "EMP0003",
            "emp": {"name": "Charlie Brown", "department": "Engineering"},
            "total_late_minutes": 90,
            "late_count": 2,
        },
    ]

    res = client.get("/analytics/leaderboard/late?month=2026-07&limit=2")
    assert res.status_code == 200
    data = res.json()
    assert data["month"] == "2026-07"
    assert len(data["items"]) == 3
    assert data["items"][0]["rank"] == 1
    assert data["items"][1]["rank"] == 2
    assert data["items"][2]["rank"] == 2
    assert data["items"][1]["name"] == "Bob Smith"
    assert data["items"][2]["name"] == "Charlie Brown"


# --------------------------------------------------------------------------- #
# Endpoint 4: GET /analytics/departments/{department}/trend
# --------------------------------------------------------------------------- #
def test_department_trend_validation_and_errors(mock_db):
    # 1. Invalid date format
    res = client.get("/analytics/departments/Engineering/trend?from=2026-13-01&to=2026-07-05")
    assert res.status_code == 422

    # 2. from > to
    res2 = client.get("/analytics/departments/Engineering/trend?from=2026-07-10&to=2026-07-01")
    assert res2.status_code == 422

    # 3. > 92 days
    res3 = client.get("/analytics/departments/Engineering/trend?from=2026-01-01&to=2026-05-01")
    assert res3.status_code == 422

    # 4. Unknown department -> 404
    mock_db.employees.find_one.return_value = None
    res4 = client.get("/analytics/departments/Nonexistent/trend?from=2026-07-01&to=2026-07-05")
    assert res4.status_code == 404
    assert res4.json() == {"detail": "Department not found"}


def test_department_trend_gap_filling_and_moving_average(mock_db):
    # Department exists with 2 employees
    mock_db.employees.find_one.return_value = {"department": "Engineering"}
    mock_db.employees.aggregate.return_value = [
        {
            "date": "2026-07-01",
            "is_working_day": True,
            "headcount": 2,
            "present_count": 2.0,
            "late_count": 0,
            "attendance_rate": 1.0000,
            "moving_avg_7d": 1.0000,
        },
        {
            "date": "2026-07-02",
            "is_working_day": True,
            "headcount": 2,
            "present_count": 0.0,
            "late_count": 0,
            "attendance_rate": 0.0000,
            "moving_avg_7d": 0.5000,
        },
        {
            "date": "2026-07-03",
            "is_working_day": True,
            "headcount": 2,
            "present_count": 0.5,
            "late_count": 1,
            "attendance_rate": 0.2500,
            "moving_avg_7d": 0.4167,
        },
        {
            "date": "2026-07-04",
            "is_working_day": False,
            "headcount": 2,
            "present_count": 0.0,
            "late_count": 0,
            "attendance_rate": None,
            "moving_avg_7d": 0.4167,
        },
        {
            "date": "2026-07-05",
            "is_working_day": False,
            "headcount": 2,
            "present_count": 0.0,
            "late_count": 0,
            "attendance_rate": None,
            "moving_avg_7d": 0.4167,
        },
    ]

    res = client.get("/analytics/departments/Engineering/trend?from=2026-07-01&to=2026-07-05")
    assert res.status_code == 200
    data = res.json()
    assert data["department"] == "Engineering"
    items = data["items"]
    assert len(items) == 5  # Every calendar day in range!

    # Day 1: 2026-07-01 (Wed)
    d1 = items[0]
    assert d1["date"] == "2026-07-01"
    assert d1["is_working_day"] is True
    assert d1["headcount"] == 2
    assert d1["present_count"] == 2.0
    assert d1["attendance_rate"] == 1.0000
    assert d1["moving_avg_7d"] == 1.0000

    # Day 2: 2026-07-02 (Thu) - Zero activity
    d2 = items[1]
    assert d2["date"] == "2026-07-02"
    assert d2["is_working_day"] is True
    assert d2["headcount"] == 2
    assert d2["present_count"] == 0.0
    assert d2["attendance_rate"] == 0.0000
    # moving average of [1.0, 0.0] = 0.5000
    assert d2["moving_avg_7d"] == 0.5000

    # Day 3: 2026-07-03 (Fri) - Half day
    d3 = items[2]
    assert d3["date"] == "2026-07-03"
    assert d3["is_working_day"] is True
    assert d3["headcount"] == 2
    assert d3["present_count"] == 0.5
    assert d3["late_count"] == 1
    assert d3["attendance_rate"] == 0.2500
    # moving avg of [1.0, 0.0, 0.25] = 1.25 / 3 = 0.41666... -> 0.4167
    assert d3["moving_avg_7d"] == 0.4167

    # Day 4: 2026-07-04 (Sat) - Weekend
    d4 = items[3]
    assert d4["date"] == "2026-07-04"
    assert d4["is_working_day"] is False
    assert d4["attendance_rate"] is None
    # moving average over non-null rates in window [1.0, 0.0, 0.25] remains 0.4167
    assert d4["moving_avg_7d"] == 0.4167

    # Day 5: 2026-07-05 (Sun) - Weekend
    d5 = items[4]
    assert d5["date"] == "2026-07-05"
    assert d5["is_working_day"] is False
    assert d5["attendance_rate"] is None
    assert d5["moving_avg_7d"] == 0.4167


# --------------------------------------------------------------------------- #
# Endpoint 5: GET /admin/explain/{endpoint}
# --------------------------------------------------------------------------- #
def test_admin_explain_unknown_endpoint():
    res = client.get("/admin/explain/unknown_route")
    assert res.status_code == 422


def test_admin_explain_missing_parameters():
    # employee_monthly requires emp_code and month
    res = client.get("/admin/explain/employee_monthly")
    assert res.status_code == 422

    # department_summary requires month
    res2 = client.get("/admin/explain/department_summary")
    assert res2.status_code == 422

    # department_trend requires department, from, and to
    res3 = client.get("/admin/explain/department_trend?department=Engineering")
    assert res3.status_code == 422


def test_admin_explain_attendance_list(mock_db):
    mock_explain_result = {"queryPlanner": {"winningPlan": {"stage": "IXSCAN"}}}
    mock_db.command.return_value = mock_explain_result

    res = client.get("/admin/explain/attendance_list?emp_code=EMP0001&date_from=2026-07-01")
    assert res.status_code == 200
    data = res.json()
    assert data["endpoint"] == "attendance_list"
    assert data["collection"] == "attendance_logs"
    assert data["explain"] == mock_explain_result


def test_admin_explain_employee_monthly(mock_db):
    mock_explain_result = {"stages": [{"$cursor": {"queryPlanner": {"winningPlan": {"stage": "IXSCAN"}}}}]}
    mock_db.command.return_value = mock_explain_result

    res = client.get("/admin/explain/employee_monthly?emp_code=EMP0001&month=2026-07")
    assert res.status_code == 200
    data = res.json()
    assert data["endpoint"] == "employee_monthly"
    assert data["collection"] == "attendance_logs"
    assert data["explain"] == mock_explain_result


def test_admin_explain_department_summary(mock_db):
    mock_explain_result = {"stages": [{"$cursor": {"queryPlanner": {"winningPlan": {"stage": "IXSCAN"}}}}]}
    mock_db.command.return_value = mock_explain_result

    res = client.get("/admin/explain/department_summary?month=2026-07&department=Engineering")
    assert res.status_code == 200
    data = res.json()
    assert data["endpoint"] == "department_summary"
    assert data["collection"] == "employees"


def test_admin_explain_late_leaderboard(mock_db):
    mock_explain_result = {"stages": [{"$cursor": {"queryPlanner": {"winningPlan": {"stage": "IXSCAN"}}}}]}
    mock_db.command.return_value = mock_explain_result

    res = client.get("/admin/explain/late_leaderboard?month=2026-07")
    assert res.status_code == 200
    data = res.json()
    assert data["endpoint"] == "late_leaderboard"
    assert data["collection"] == "attendance_logs"


def test_admin_explain_department_trend(mock_db):
    mock_explain_result = {"stages": [{"$cursor": {"queryPlanner": {"winningPlan": {"stage": "IXSCAN"}}}}]}
    mock_db.command.return_value = mock_explain_result

    res = client.get("/admin/explain/department_trend?department=Engineering&from=2026-07-01&to=2026-07-10")
    assert res.status_code == 200
    data = res.json()
    assert data["endpoint"] == "department_trend"
    assert data["collection"] == "employees"
