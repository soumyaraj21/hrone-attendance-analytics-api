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
    compute_work_hours,
    compute_overtime,
    compute_half_day,
    determine_attendance_date,
    IST
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


class MockDatabase:
    def __init__(self):
        self.employees = MockCollection("employees")
        self.attendance_logs = MockCollection("attendance_logs")

    def __getitem__(self, name):
        return getattr(self, name)


@pytest.fixture
def mock_db(monkeypatch):
    mdb = MockDatabase()
    monkeypatch.setattr("app.main.db", mdb)
    return mdb


# --------------------------------------------------------------------------- #
# Schema Validation Tests (API Boundary)
# --------------------------------------------------------------------------- #
def test_punch_out_validation_missing_emp_code():
    res = client.post("/attendance/punch-out", json={})
    assert res.status_code == 422


def test_punch_out_validation_seconds_timestamp():
    res = client.post("/attendance/punch-out", json={
        "emp_code": "EMP0001",
        "punched_at": 1783312500  # seconds instead of ms (< 100000000000)
    })
    assert res.status_code == 422


def test_punch_out_validation_string_timestamp():
    res = client.post("/attendance/punch-out", json={
        "emp_code": "EMP0001",
        "punched_at": "1783312500000"
    })
    assert res.status_code == 422


def test_regularize_validation_short_reason():
    res = client.patch("/attendance/EMP0001/2026-07-06", json={
        "reason": "bad",  # min length 5
        "regularized_by": "admin"
    })
    assert res.status_code == 422


def test_regularize_validation_invalid_status():
    res = client.patch("/attendance/EMP0001/2026-07-06", json={
        "status": "INVALID_STATUS",
        "reason": "Valid reason for test",
        "regularized_by": "admin"
    })
    assert res.status_code == 422


def test_regularize_validation_invalid_date():
    res = client.patch("/attendance/EMP0001/2026-13-45", json={
        "reason": "Valid reason for test",
        "regularized_by": "admin"
    })
    assert res.status_code == 422


# --------------------------------------------------------------------------- #
# Punch-In Nonexistent Employee Test (Explicitly answers user question)
# --------------------------------------------------------------------------- #
def test_punch_in_nonexistent_employee_returns_404(mock_db):
    """Explicitly verifies that punch-in for a nonexistent employee returns 404."""
    mock_db.employees.find_one.return_value = None
    res = client.post("/attendance/punch-in", json={"emp_code": "EMP9999"})
    assert res.status_code == 404
    assert res.json() == {"detail": "Employee not found"}


# --------------------------------------------------------------------------- #
# Punch-Out Logic & Error Tests
# --------------------------------------------------------------------------- #
def test_punch_out_nonexistent_employee(mock_db):
    mock_db.employees.find_one.return_value = None
    res = client.post("/attendance/punch-out", json={"emp_code": "EMP9999"})
    assert res.status_code == 404
    assert res.json() == {"detail": "Employee not found"}


def test_punch_out_no_punch_in_record(mock_db):
    emp = {"emp_code": "EMP0001", "shift_start": "09:30", "shift_end": "18:30"}
    mock_db.employees.find_one.return_value = emp
    mock_db.attendance_logs.find_one.return_value = None

    res = client.post("/attendance/punch-out", json={"emp_code": "EMP0001"})
    assert res.status_code == 404
    assert res.json() == {"detail": "No punch-in found"}


def test_punch_out_no_eligible_open_record_returns_404(mock_db):
    """When all records are already closed or no open record exists, returns 404."""
    emp = {"emp_code": "EMP0001", "shift_start": "09:30", "shift_end": "18:30"}
    mock_db.employees.find_one.return_value = emp
    # Simulates that find_one with punch_out: None finds no matching open record
    mock_db.attendance_logs.find_one.return_value = None

    res = client.post("/attendance/punch-out", json={
        "emp_code": "EMP0001",
        "punched_at": 1783344600000
    })
    assert res.status_code == 404
    assert res.json() == {"detail": "No punch-in found"}


def test_punch_out_latest_closed_older_eligible_open(mock_db):
    """The latest record is already closed, but an older eligible record remains open."""
    emp = {"emp_code": "EMP0001", "shift_start": "09:30", "shift_end": "18:30"}
    mock_db.employees.find_one.return_value = emp

    older_open_id = ObjectId()
    older_in_dt = datetime(2026, 7, 5, 4, 0, 0, tzinfo=timezone.utc)
    older_open_rec = {
        "_id": older_open_id,
        "emp_code": "EMP0001",
        "date": "2026-07-05",
        "status": "PRESENT",
        "punch_in": older_in_dt,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": 0,
        "overtime_minutes": 0,
        "half_day": False,
        "history": []
    }

    # find_one query contains {"emp_code": "EMP0001", "punch_in": {"$lte": ...}, "punch_out": None}
    # It selects the older open record rather than a newer closed record
    def mock_find_one(q, sort=None):
        assert q.get("punch_out") is None
        assert "punch_in" in q
        return older_open_rec

    mock_db.attendance_logs.find_one.side_effect = mock_find_one

    out_dt = datetime(2026, 7, 5, 13, 5, 0, tzinfo=timezone.utc)
    out_ms = int(out_dt.timestamp() * 1000)

    def mock_find_one_and_update(q, update, return_document=None):
        assert q.get("_id") == older_open_id
        assert q.get("punch_out") is None
        updated = dict(older_open_rec)
        updated.update(update["$set"])
        return updated

    mock_db.attendance_logs.find_one_and_update.side_effect = mock_find_one_and_update

    res = client.post("/attendance/punch-out", json={
        "emp_code": "EMP0001",
        "punched_at": out_ms
    })
    assert res.status_code == 200
    data = res.json()
    assert data["date"] == "2026-07-05"
    assert data["punch_out"] == out_ms


def test_punch_out_duration_errors(mock_db):
    emp = {"emp_code": "EMP0001", "shift_start": "09:30", "shift_end": "18:30"}
    punch_in_dt = datetime(2026, 7, 6, 4, 0, 0, tzinfo=timezone.utc)  # 09:30 IST
    existing_rec = {
        "_id": ObjectId(),
        "emp_code": "EMP0001",
        "date": "2026-07-06",
        "punch_in": punch_in_dt,
        "punch_out": None,
    }
    mock_db.employees.find_one.return_value = emp
    mock_db.attendance_logs.find_one.return_value = existing_rec

    # 1. punched_at <= punch_in
    in_ms = int(punch_in_dt.timestamp() * 1000)
    res = client.post("/attendance/punch-out", json={
        "emp_code": "EMP0001",
        "punched_at": in_ms  # exact same instant
    })
    assert res.status_code == 422
    assert "strictly after" in res.json()["detail"]

    # 2. punched_at > 24 hours after punch_in
    out_too_late_ms = in_ms + (25 * 3600 * 1000)
    res2 = client.post("/attendance/punch-out", json={
        "emp_code": "EMP0001",
        "punched_at": out_too_late_ms
    })
    assert res2.status_code == 422
    assert "24 hours" in res2.json()["detail"]


def test_punch_out_success_and_concurrency(mock_db):
    emp = {"emp_code": "EMP0001", "shift_start": "09:30", "shift_end": "18:30"}
    p_in_dt = datetime(2026, 7, 6, 3, 58, 0, tzinfo=timezone.utc)   # 09:28 IST
    p_out_dt = datetime(2026, 7, 6, 13, 5, 0, tzinfo=timezone.utc)  # 18:35 IST
    rec_id = ObjectId()
    rec = {
        "_id": rec_id,
        "emp_code": "EMP0001",
        "date": "2026-07-06",
        "status": "PRESENT",
        "punch_in": p_in_dt,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": 0,
        "overtime_minutes": 0,
        "half_day": False,
        "history": []
    }
    mock_db.employees.find_one.return_value = emp
    mock_db.attendance_logs.find_one.return_value = rec

    def mock_find_one_and_update(q, update, return_document=None):
        if q.get("punch_out") is None:
            updated = dict(rec)
            updated.update(update["$set"])
            return updated
        return None

    mock_db.attendance_logs.find_one_and_update.side_effect = mock_find_one_and_update

    out_ms = int(p_out_dt.timestamp() * 1000)
    res = client.post("/attendance/punch-out", json={
        "emp_code": "EMP0001",
        "punched_at": out_ms
    })
    assert res.status_code == 200
    data = res.json()
    assert data["work_hours"] == 9.12
    assert data["overtime_minutes"] == 0
    assert data["half_day"] is False
    assert data["punch_out"] == out_ms
    assert "id" not in data
    assert "_id" not in data

    # Concurrent loser test: when another request updated it first, find_one_and_update returns None -> 409
    mock_db.attendance_logs.find_one_and_update.side_effect = None
    mock_db.attendance_logs.find_one_and_update.return_value = None
    res_loser = client.post("/attendance/punch-out", json={
        "emp_code": "EMP0001",
        "punched_at": out_ms
    })
    assert res_loser.status_code == 409
    assert res_loser.json() == {"detail": "Already punched out"}


def test_punch_out_overnight_shift(mock_db):
    emp = {"emp_code": "EMP0005", "shift_start": "22:00", "shift_end": "06:00"}
    p_in_dt = datetime(2026, 7, 6, 16, 25, 0, tzinfo=timezone.utc)  # 21:55 IST
    p_out_dt = datetime(2026, 7, 7, 1, 10, 0, tzinfo=timezone.utc)  # 06:40 IST (next day, 40 min overtime)
    rec = {
        "_id": ObjectId(),
        "emp_code": "EMP0005",
        "date": "2026-07-06",
        "status": "PRESENT",
        "punch_in": p_in_dt,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": 0,
        "overtime_minutes": 0,
        "half_day": False,
        "history": []
    }
    mock_db.employees.find_one.return_value = emp
    mock_db.attendance_logs.find_one.return_value = rec
    mock_db.attendance_logs.find_one_and_update.side_effect = lambda q, u, return_document=None: {**rec, **u["$set"]}

    out_ms = int(p_out_dt.timestamp() * 1000)
    res = client.post("/attendance/punch-out", json={"emp_code": "EMP0005", "punched_at": out_ms})
    assert res.status_code == 200
    data = res.json()
    assert data["overtime_minutes"] == 40
    assert data["work_hours"] == 8.75
    assert data["half_day"] is False


# --------------------------------------------------------------------------- #
# Regularization (PATCH) Tests
# --------------------------------------------------------------------------- #
def test_regularize_not_found_employee(mock_db):
    mock_db.employees.find_one.return_value = None
    res = client.patch("/attendance/EMP9999/2026-07-06", json={
        "status": "PRESENT",
        "reason": "Correction reason here",
        "regularized_by": "manager"
    })
    assert res.status_code == 404
    assert res.json() == {"detail": "Employee not found"}


def test_regularize_not_found_attendance_record(mock_db):
    emp = {"emp_code": "EMP0001", "shift_start": "09:30", "shift_end": "18:30"}
    mock_db.employees.find_one.return_value = emp
    mock_db.attendance_logs.find_one.return_value = None

    res = client.patch("/attendance/EMP0001/2026-07-06", json={
        "status": "PRESENT",
        "reason": "Correction reason here",
        "regularized_by": "manager"
    })
    assert res.status_code == 404
    assert res.json() == {"detail": "Attendance record not found for date"}


def test_regularize_absent_with_punch_times_raises_422(mock_db):
    emp = {"emp_code": "EMP0001", "shift_start": "09:30", "shift_end": "18:30"}
    rec = {"_id": ObjectId(), "emp_code": "EMP0001", "date": "2026-07-06", "status": "PRESENT"}
    mock_db.employees.find_one.return_value = emp
    mock_db.attendance_logs.find_one.return_value = rec

    res = client.patch("/attendance/EMP0001/2026-07-06", json={
        "status": "ABSENT",
        "punch_in": 1783312500000,
        "reason": "Correction reason here",
        "regularized_by": "manager"
    })
    assert res.status_code == 422
    assert "Cannot supply punch_in or punch_out" in res.json()["detail"]


def test_regularize_presence_without_punch_in_raises_422(mock_db):
    emp = {"emp_code": "EMP0001", "shift_start": "09:30", "shift_end": "18:30"}
    rec = {
        "_id": ObjectId(),
        "emp_code": "EMP0001",
        "date": "2026-07-06",
        "status": "ABSENT",
        "punch_in": None,
        "punch_out": None
    }
    mock_db.employees.find_one.return_value = emp
    mock_db.attendance_logs.find_one.return_value = rec

    res = client.patch("/attendance/EMP0001/2026-07-06", json={
        "status": "PRESENT",
        # no punch_in supplied and existing is None
        "reason": "Correction reason here",
        "regularized_by": "manager"
    })
    assert res.status_code == 422
    assert "requires a punch_in" in res.json()["detail"]


def test_regularize_punch_in_wrong_date_raises_422(mock_db):
    emp = {"emp_code": "EMP0001", "shift_start": "09:30", "shift_end": "18:30"}
    rec = {
        "_id": ObjectId(),
        "emp_code": "EMP0001",
        "date": "2026-07-06",
        "status": "PRESENT",
        "punch_in": datetime(2026, 7, 6, 4, 0, 0, tzinfo=timezone.utc),
        "punch_out": None
    }
    mock_db.employees.find_one.return_value = emp
    mock_db.attendance_logs.find_one.return_value = rec

    # punch_in on 2026-07-07 instead of path date 2026-07-06
    wrong_day_dt = datetime(2026, 7, 7, 4, 0, 0, tzinfo=timezone.utc)
    res = client.patch("/attendance/EMP0001/2026-07-06", json={
        "punch_in": int(wrong_day_dt.timestamp() * 1000),
        "reason": "Correction reason here",
        "regularized_by": "manager"
    })
    assert res.status_code == 422
    assert "must belong to the record's attendance date" in res.json()["detail"]


def test_regularize_zero_changes_raises_422(mock_db):
    emp = {"emp_code": "EMP0001", "shift_start": "09:30", "shift_end": "18:30"}
    p_in = datetime(2026, 7, 6, 3, 58, 0, tzinfo=timezone.utc)
    p_out = datetime(2026, 7, 6, 13, 0, 0, tzinfo=timezone.utc)
    rec = {
        "_id": ObjectId(),
        "emp_code": "EMP0001",
        "date": "2026-07-06",
        "status": "PRESENT",
        "punch_in": p_in,
        "punch_out": p_out,
        "work_hours": 9.03,
        "late_minutes": 0,
        "overtime_minutes": 0,
        "half_day": False,
        "history": []
    }
    mock_db.employees.find_one.return_value = emp
    mock_db.attendance_logs.find_one.return_value = rec

    # Request identical to existing stored values
    res = client.patch("/attendance/EMP0001/2026-07-06", json={
        "status": "PRESENT",
        "punch_in": int(p_in.timestamp() * 1000),
        "punch_out": int(p_out.timestamp() * 1000),
        "reason": "No changes made here",
        "regularized_by": "manager"
    })
    assert res.status_code == 422
    assert "Request produces no changes" in res.json()["detail"]


def test_regularize_success_and_audit_history(mock_db):
    emp = {"emp_code": "EMP0001", "shift_start": "09:30", "shift_end": "18:30"}
    old_in = datetime(2026, 7, 7, 4, 35, 0, tzinfo=timezone.utc)  # 10:05 IST -> 35 min late
    p_out = datetime(2026, 7, 7, 13, 0, 0, tzinfo=timezone.utc)
    rec = {
        "_id": ObjectId(),
        "emp_code": "EMP0001",
        "date": "2026-07-07",
        "status": "PRESENT",
        "punch_in": old_in,
        "punch_out": p_out,
        "work_hours": 8.42,
        "late_minutes": 35,
        "overtime_minutes": 0,
        "half_day": False,
        "history": []
    }
    mock_db.employees.find_one.return_value = emp
    mock_db.attendance_logs.find_one.return_value = rec

    # New punch in: 09:28 IST (03:58 UTC) -> late 0 min, work_hours 9.03
    new_in = datetime(2026, 7, 7, 3, 58, 0, tzinfo=timezone.utc)
    new_in_ms = int(new_in.timestamp() * 1000)

    captured_update = {}
    def mock_find_one_and_update(q, update, return_document=None):
        captured_update.update(update)
        updated = dict(rec)
        updated.update(update["$set"])
        updated["history"] = list(rec["history"]) + [update["$push"]["history"]]
        return updated

    mock_db.attendance_logs.find_one_and_update.side_effect = mock_find_one_and_update

    res = client.patch("/attendance/EMP0001/2026-07-07", json={
        "punch_in": new_in_ms,
        "reason": "biometric glitch",
        "regularized_by": "hr.admin"
    })
    assert res.status_code == 200
    data = res.json()
    assert data["late_minutes"] == 0
    assert data["work_hours"] == 9.03
    assert len(data["history"]) == 1
    h = data["history"][0]
    assert h["by"] == "hr.admin"
    assert h["reason"] == "biometric glitch"
    # changes should only record fields that actually changed
    changes = h["changes"]
    assert "punch_in" in changes
    assert "late_minutes" in changes
    assert "work_hours" in changes
    assert "status" not in changes  # status didn't change
    assert "overtime_minutes" not in changes
    assert "half_day" not in changes

    assert changes["late_minutes"] == {"from": 35, "to": 0}
    assert changes["work_hours"] == {"from": 8.42, "to": 9.03}
    assert changes["punch_in"]["from"] == int(old_in.timestamp() * 1000)
    assert changes["punch_in"]["to"] == new_in_ms


def test_regularize_concurrent_conflict(mock_db):
    emp = {"emp_code": "EMP0001", "shift_start": "09:30", "shift_end": "18:30"}
    rec = {
        "_id": ObjectId(),
        "emp_code": "EMP0001",
        "date": "2026-07-07",
        "status": "PRESENT",
        "punch_in": datetime(2026, 7, 7, 4, 35, 0, tzinfo=timezone.utc),
        "punch_out": None,
        "work_hours": None,
        "late_minutes": 35,
        "overtime_minutes": 0,
        "half_day": False,
        "history": []
    }
    mock_db.employees.find_one.return_value = emp
    mock_db.attendance_logs.find_one.return_value = rec

    # find_one_and_update returns None when concurrent edit raced and incremented history length
    mock_db.attendance_logs.find_one_and_update.return_value = None

    new_in = datetime(2026, 7, 7, 3, 58, 0, tzinfo=timezone.utc)
    res = client.patch("/attendance/EMP0001/2026-07-07", json={
        "punch_in": int(new_in.timestamp() * 1000),
        "reason": "biometric glitch",
        "regularized_by": "hr.admin"
    })
    assert res.status_code == 409
    assert res.json() == {"detail": "Concurrent regularization conflict"}


def test_regularize_present_to_leave(mock_db):
    emp = {"emp_code": "EMP0001", "shift_start": "09:30", "shift_end": "18:30"}
    old_in = datetime(2026, 7, 7, 4, 0, 0, tzinfo=timezone.utc)
    old_out = datetime(2026, 7, 7, 13, 0, 0, tzinfo=timezone.utc)
    rec = {
        "_id": ObjectId(),
        "emp_code": "EMP0001",
        "date": "2026-07-07",
        "status": "PRESENT",
        "punch_in": old_in,
        "punch_out": old_out,
        "work_hours": 9.0,
        "late_minutes": 0,
        "overtime_minutes": 0,
        "half_day": False,
        "history": []
    }
    mock_db.employees.find_one.return_value = emp
    mock_db.attendance_logs.find_one.return_value = rec

    def mock_find_one_and_update(q, update, return_document=None):
        updated = dict(rec)
        updated.update(update["$set"])
        updated["history"] = list(rec["history"]) + [update["$push"]["history"]]
        return updated

    mock_db.attendance_logs.find_one_and_update.side_effect = mock_find_one_and_update

    res = client.patch("/attendance/EMP0001/2026-07-07", json={
        "status": "LEAVE",
        "reason": "Approved retroactive medical leave",
        "regularized_by": "hr.admin"
    })
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "LEAVE"
    assert data["punch_in"] is None
    assert data["punch_out"] is None
    assert data["work_hours"] is None
    assert len(data["history"]) == 1
    ch = data["history"][0]["changes"]
    assert ch["status"] == {"from": "PRESENT", "to": "LEAVE"}
    assert ch["punch_in"] == {"from": int(old_in.timestamp() * 1000), "to": None}
    assert ch["punch_out"] == {"from": int(old_out.timestamp() * 1000), "to": None}
    assert ch["work_hours"] == {"from": 9.0, "to": None}
