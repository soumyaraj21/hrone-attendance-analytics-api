import sys
import pytest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from unittest.mock import MagicMock
from pymongo.errors import ConnectionFailure

from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)

def test_health_when_db_down(monkeypatch):
    mock_client = MagicMock()
    mock_client.admin.command.side_effect = ConnectionFailure("Database unavailable")
    monkeypatch.setattr("app.main.client", mock_client)

    response = client.get("/health")
    assert response.status_code == 503
    assert response.json() == {"detail": "Database unavailable"}
    mock_client.admin.command.assert_called_once_with("ping")

def test_employee_validation_bad_emp_code():
    # emp_code regex ^EMP\d{4,6}$
    res = client.post("/employees", json={
        "emp_code": "INVALID",
        "name": "Test User",
        "email": "test@example.com",
        "department": "Engineering",
        "joined_on": "2026-01-01"
    })
    assert res.status_code == 422

def test_employee_validation_short_emp_code():
    res = client.post("/employees", json={
        "emp_code": "EMP12",  # only 2 digits, requires 4-6
        "name": "Test User",
        "email": "test@example.com",
        "department": "Engineering",
        "joined_on": "2026-01-01"
    })
    assert res.status_code == 422

def test_employee_validation_bad_email():
    res = client.post("/employees", json={
        "emp_code": "EMP0001",
        "name": "Test User",
        "email": "not-an-email",
        "department": "Engineering",
        "joined_on": "2026-01-01"
    })
    assert res.status_code == 422

def test_employee_validation_identical_shifts():
    # shift_start must differ from shift_end
    res = client.post("/employees", json={
        "emp_code": "EMP0001",
        "name": "Test User",
        "email": "test@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "09:30",
        "joined_on": "2026-01-01"
    })
    assert res.status_code == 422

def test_employee_validation_bad_joined_on():
    res = client.post("/employees", json={
        "emp_code": "EMP0001",
        "name": "Test User",
        "email": "test@example.com",
        "department": "Engineering",
        "joined_on": "2026-13-45"
    })
    assert res.status_code == 422

def test_list_employees_pagination_bounds():
    # page < 1
    res = client.get("/employees?page=0")
    assert res.status_code == 422

    # page_size > 100
    res = client.get("/employees?page_size=101")
    assert res.status_code == 422

def test_punch_in_validation_invalid_status():
    # status must be PRESENT, WFH, or ON_DUTY
    res = client.post("/attendance/punch-in", json={
        "emp_code": "EMP0001",
        "status": "LEAVE"
    })
    assert res.status_code == 422

    res2 = client.post("/attendance/punch-in", json={
        "emp_code": "EMP0001",
        "status": "ABSENT"
    })
    assert res2.status_code == 422

def test_punch_in_validation_seconds_timestamp():
    # punched_at < 100000000000 (looks like seconds)
    res = client.post("/attendance/punch-in", json={
        "emp_code": "EMP0001",
        "punched_at": 1783312500
    })
    assert res.status_code == 422

def test_punch_in_validation_float_timestamp():
    res = client.post("/attendance/punch-in", json={
        "emp_code": "EMP0001",
        "punched_at": 1783312500000.5
    })
    assert res.status_code == 422

def test_punch_in_validation_string_timestamp():
    res = client.post("/attendance/punch-in", json={
        "emp_code": "EMP0001",
        "punched_at": "1783312500000"
    })
    assert res.status_code == 422

def test_attendance_list_validation():
    # date_from > date_to
    res = client.get("/attendance?date_from=2026-07-10&date_to=2026-07-01")
    assert res.status_code == 422

    # invalid status
    res2 = client.get("/attendance?status=INVALID_STATUS")
    assert res2.status_code == 422

    # page < 1
    res3 = client.get("/attendance?page=0")
    assert res3.status_code == 422

    # page_size > 100
    res4 = client.get("/attendance?page_size=150")
    assert res4.status_code == 422
