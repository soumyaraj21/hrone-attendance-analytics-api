"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)
"""
import calendar
import os
from contextlib import asynccontextmanager
from datetime import date as date_cls, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, List, Optional

from bson import Decimal128, ObjectId
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field, field_validator, model_validator
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError
from pymongo.uri_parser import parse_uri

load_dotenv()

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")
DEFAULT_SERVER_SELECTION_TIMEOUT_MS = 5000
MAX_SERVER_SELECTION_TIMEOUT_MS = 20000


def create_mongo_client(uri: str) -> MongoClient:
    """Create a MongoClient with tz_aware and bounded serverSelectionTimeoutMS (<20s)."""
    kwargs: Dict[str, Any] = {"tz_aware": True}
    try:
        parsed = parse_uri(uri)
        uri_opts = {k.lower(): v for k, v in parsed.get("options", {}).items()}
        # Note: parse_uri normalizes serverSelectionTimeoutMS to seconds (float)
        timeout_sec = uri_opts.get("serverselectiontimeoutms")
        if timeout_sec is None:
            kwargs["serverSelectionTimeoutMS"] = DEFAULT_SERVER_SELECTION_TIMEOUT_MS
        else:
            timeout_ms = int(round(timeout_sec * 1000))
            if timeout_ms >= MAX_SERVER_SELECTION_TIMEOUT_MS:
                kwargs["serverSelectionTimeoutMS"] = DEFAULT_SERVER_SELECTION_TIMEOUT_MS
            else:
                kwargs["serverSelectionTimeoutMS"] = timeout_ms
    except Exception:
        kwargs["serverSelectionTimeoutMS"] = DEFAULT_SERVER_SELECTION_TIMEOUT_MS
    return MongoClient(uri, **kwargs)


client = create_mongo_client(MONGO_URI)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))


REQUIRED_INDEXES: Dict[str, List[tuple]] = {
    "employees": [
        ([("emp_code", 1)], {"unique": True}),
        ([("department", 1), ("emp_code", 1)], {}),
        ([("department", 1), ("joined_on", 1)], {}),
    ],
    "attendance_logs": [
        ([("emp_code", 1), ("date", 1)], {"unique": True}),
        ([("date", -1), ("emp_code", 1)], {}),
        ([("emp_code", 1), ("punch_in", -1)], {}),
        ([("date", 1), ("status", 1)], {}),
        ([("date", 1), ("late_minutes", 1)], {}),
    ],
}


def ensure_indexes(database=None) -> Dict[str, List[str]]:
    """Idempotently create and verify all required MongoDB indexes at startup.

    Raises RuntimeError if any required index cannot be created, ensuring startup
    fails clearly on database outages or index specification conflicts.
    """
    target_db = db if database is None else database
    created: Dict[str, List[str]] = {}
    for col_name, indexes in REQUIRED_INDEXES.items():
        col = target_db[col_name]
        created[col_name] = []
        for keys, options in indexes:
            try:
                idx_name = col.create_index(keys, **options)
                created[col_name].append(idx_name)
            except Exception as exc:
                raise RuntimeError(
                    f"Failed to create index {keys} with options {options} on collection '{col_name}': {exc}"
                ) from exc
    return created


@asynccontextmanager
async def lifespan(app: FastAPI):
    ensure_indexes()
    yield


app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    lifespan=lifespan,
)


# --------------------------------------------------------------------------- #
# Helpers & Domain Logic (Rules R1 - R8)
# --------------------------------------------------------------------------- #
def round_half_up(value: float, decimals: int) -> float:
    """Standard half-up rounding (R4, R8)."""
    d = Decimal(str(value))
    target = Decimal("10") ** -decimals
    return float(d.quantize(target, rounding=ROUND_HALF_UP))


def to_epoch_ms(dt: Optional[datetime]) -> Optional[int]:
    """Convert a UTC or naive datetime to epoch milliseconds."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def determine_attendance_date(punch_in_utc: datetime, shift_start: str, shift_end: str) -> str:
    """
    R1 Time zone and attendance date.
    Shift times and attendance dates are IST (UTC+05:30).
    A record's date is the IST calendar date of its punch_in instant.
    Exception - overnight shifts (shift_end <= shift_start):
    A punch-in whose IST time is strictly earlier than shift_end belongs to the shift that started the previous day.
    """
    punch_in_ist = punch_in_utc.astimezone(IST)
    if shift_end <= shift_start:  # overnight shift
        end_h, end_m = map(int, shift_end.split(":"))
        if punch_in_ist.time() < time(end_h, end_m):
            return (punch_in_ist.date() - timedelta(days=1)).isoformat()
    return punch_in_ist.date().isoformat()


def compute_late_minutes(punch_in_utc: datetime, shift_start: str, attendance_date_str: str) -> int:
    """
    R2 Late.
    Grace period is 10 minutes. A punch-in is late only if it is strictly more than 10:00 after shift_start.
    When late, late_minutes = whole minutes (floor) elapsed since shift_start - not since the end of the grace period.
    """
    punch_in_ist = punch_in_utc.astimezone(IST)
    d = date_cls.fromisoformat(attendance_date_str)
    start_h, start_m = map(int, shift_start.split(":"))
    shift_start_dt = datetime.combine(d, time(start_h, start_m), tzinfo=IST)
    elapsed_seconds = (punch_in_ist - shift_start_dt).total_seconds()
    if elapsed_seconds > 600:  # strictly more than 10m 00s
        return int(elapsed_seconds // 60)
    return 0


def compute_work_hours(punch_in_utc: datetime, punch_out_utc: datetime) -> float:
    """
    R4 Work hours.
    (punch_out - punch_in) in seconds / 3600, rounded to 2 decimals half-up.
    """
    diff_seconds = (punch_out_utc - punch_in_utc).total_seconds()
    hours = Decimal(str(diff_seconds)) / Decimal("3600")
    return float(hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_overtime(
    punch_out_utc: datetime, shift_start: str, shift_end: str, attendance_date_str: str
) -> int:
    """
    R3 Overtime.
    Whole minutes (floor) between shift_end and punch_out, counted only when >= 30; otherwise 0.
    """
    punch_out_ist = punch_out_utc.astimezone(IST)
    d = date_cls.fromisoformat(attendance_date_str)
    if shift_end <= shift_start:
        d = d + timedelta(days=1)  # overnight shift ends on next calendar day
    end_h, end_m = map(int, shift_end.split(":"))
    shift_end_dt = datetime.combine(d, time(end_h, end_m), tzinfo=IST)
    elapsed_seconds = (punch_out_ist - shift_end_dt).total_seconds()
    elapsed_minutes = int(elapsed_seconds // 60)
    if elapsed_minutes >= 30:
        return elapsed_minutes
    return 0


def compute_half_day(work_hours: Optional[float]) -> bool:
    """
    R5 Half day.
    After punch-out, half_day is true when the rounded work_hours < 4.50.
    """
    return work_hours is not None and work_hours < 4.50


def get_month_boundaries(month_str: str) -> tuple[str, str, int]:
    """Given YYYY-MM, return (YYYY-MM-01, YYYY-MM-last_day, num_days_in_month)."""
    year, month_num = map(int, month_str.split("-"))
    num_days = calendar.monthrange(year, month_num)[1]
    return f"{month_str}-01", f"{month_str}-{num_days:02d}", num_days


def count_working_days(start_date_str: str, end_date_str: str) -> int:
    """R7: Working days are Monday-Friday (no holiday calendar)."""
    d_start = date_cls.fromisoformat(start_date_str)
    d_end = date_cls.fromisoformat(end_date_str)
    if d_start > d_end:
        return 0
    cur = d_start
    count = 0
    one_day = timedelta(days=1)
    while cur <= d_end:
        if cur.weekday() < 5:
            count += 1
        cur += one_day
    return count


def format_employee(doc: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "emp_code": doc.get("emp_code"),
        "name": doc.get("name"),
        "email": doc.get("email"),
        "department": doc.get("department"),
        "shift_start": doc.get("shift_start"),
        "shift_end": doc.get("shift_end"),
        "joined_on": doc.get("joined_on"),
        "created_at": to_epoch_ms(doc.get("created_at")),
    }


def format_attendance_record(doc: Dict[str, Any]) -> Dict[str, Any]:
    history_out = []
    for h in doc.get("history") or []:
        entry = {
            "at": to_epoch_ms(h.get("at")),
            "by": h.get("by"),
            "reason": h.get("reason"),
            "changes": {},
        }
        for field, change in (h.get("changes") or {}).items():
            if field in ("punch_in", "punch_out"):
                from_val = change.get("from")
                to_val = change.get("to")
                entry["changes"][field] = {
                    "from": to_epoch_ms(from_val) if isinstance(from_val, datetime) else from_val,
                    "to": to_epoch_ms(to_val) if isinstance(to_val, datetime) else to_val,
                }
            else:
                entry["changes"][field] = {
                    "from": change.get("from"),
                    "to": change.get("to"),
                }
        history_out.append(entry)

    return {
        "emp_code": doc.get("emp_code"),
        "date": doc.get("date"),
        "status": doc.get("status"),
        "punch_in": to_epoch_ms(doc.get("punch_in")),
        "punch_out": to_epoch_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": bool(doc.get("half_day", False)),
        "history": history_out,
    }


# --------------------------------------------------------------------------- #
# Request / Response Schemas
# --------------------------------------------------------------------------- #
def validate_epoch_ms(v: Any) -> int:
    if isinstance(v, bool) or not isinstance(v, int):
        raise ValueError("Epoch milliseconds must be an integer")
    if v < 100_000_000_000 or v > 4_102_444_800_000:
        raise ValueError("Epoch milliseconds out of bounds (100000000000 to 4102444800000)")
    return v


class EmployeeIn(BaseModel):
    emp_code: str = Field(..., pattern=r"^EMP\d{4,6}$")
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$", max_length=120)
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field("18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str = Field(...)

    @model_validator(mode="after")
    def validate_model(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        try:
            date_cls.fromisoformat(self.joined_on)
        except Exception:
            raise ValueError("joined_on must be a valid date in YYYY-MM-DD format")
        return self


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None
    status: str = "PRESENT"

    @field_validator("punched_at", mode="before")
    @classmethod
    def check_punched_at(cls, v):
        if v is not None:
            return validate_epoch_ms(v)
        return v

    @field_validator("status")
    @classmethod
    def check_status(cls, v):
        if v not in ("PRESENT", "WFH", "ON_DUTY"):
            raise ValueError("status must be PRESENT, WFH, or ON_DUTY")
        return v


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None

    @field_validator("punched_at", mode="before")
    @classmethod
    def check_punched_at(cls, v):
        if v is not None:
            return validate_epoch_ms(v)
        return v


class RegularizeIn(BaseModel):
    status: Optional[str] = None
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)

    @field_validator("punch_in", "punch_out", mode="before")
    @classmethod
    def check_instants(cls, v):
        if v is not None:
            return validate_epoch_ms(v)
        return v

    @field_validator("status")
    @classmethod
    def check_status(cls, v):
        if v is not None and v not in ("PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"):
            raise ValueError("status must be PRESENT, ABSENT, LEAVE, WFH, or ON_DUTY")
        return v


# --------------------------------------------------------------------------- #
# Endpoints (Milestones 1 & 2)
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(503, "Database unavailable")


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    now_utc = datetime.now(timezone.utc).replace(microsecond=0)
    doc = body.model_dump()
    doc["created_at"] = now_utc
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return format_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {}
    if department:
        q["department"] = department
    total = db.employees.count_documents(q)
    skip = (page - 1) * page_size
    cursor = db.employees.find(q).sort("emp_code", 1).skip(skip).limit(page_size)
    items = [format_employee(d) for d in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    if body.punched_at is not None:
        truncated_sec = body.punched_at // 1000
        punch_in_utc = datetime.fromtimestamp(truncated_sec, tz=timezone.utc)
    else:
        now_sec = int(datetime.now(timezone.utc).timestamp())
        punch_in_utc = datetime.fromtimestamp(now_sec, tz=timezone.utc)

    att_date = determine_attendance_date(
        punch_in_utc, emp.get("shift_start", "09:30"), emp.get("shift_end", "18:30")
    )
    late_minutes = compute_late_minutes(
        punch_in_utc, emp.get("shift_start", "09:30"), att_date
    )

    doc = {
        "emp_code": body.emp_code,
        "date": att_date,
        "status": body.status,
        "punch_in": punch_in_utc,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_minutes,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return format_attendance_record(doc)


def build_attendance_list_query(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
) -> dict:
    q: Dict[str, Any] = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status
    return q


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if status is not None:
        if status not in ("PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"):
            raise HTTPException(422, "Invalid status value")
    if date_from and date_to:
        if date_from > date_to:
            raise HTTPException(422, "date_from cannot be greater than date_to")

    q = build_attendance_list_query(emp_code, date_from, date_to, status)
    total = db.attendance_logs.count_documents(q)
    skip = (page - 1) * page_size
    cursor = (
        db.attendance_logs.find(q)
        .sort([("date", -1), ("emp_code", 1)])
        .skip(skip)
        .limit(page_size)
    )
    items = [format_attendance_record(d) for d in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    if body.punched_at is not None:
        truncated_sec = body.punched_at // 1000
        punch_out_utc = datetime.fromtimestamp(truncated_sec, tz=timezone.utc)
    else:
        now_sec = int(datetime.now(timezone.utc).timestamp())
        punch_out_utc = datetime.fromtimestamp(now_sec, tz=timezone.utc)

    # Closes the employee's most recent open record whose punch_in <= punched_at
    record = db.attendance_logs.find_one(
        {
            "emp_code": body.emp_code,
            "punch_in": {"$lte": punch_out_utc},
            "punch_out": None,
        },
        sort=[("punch_in", -1)],
    )
    if not record:
        raise HTTPException(404, "No punch-in found")

    punch_in_utc = record["punch_in"]
    if punch_in_utc.tzinfo is None:
        punch_in_utc = punch_in_utc.replace(tzinfo=timezone.utc)

    delta_sec = (punch_out_utc - punch_in_utc).total_seconds()
    if delta_sec <= 0:
        raise HTTPException(422, "punched_at must be strictly after punch_in")
    if delta_sec > 24 * 3600:
        raise HTTPException(422, "punched_at cannot be more than 24 hours after punch_in")

    work_hours = compute_work_hours(punch_in_utc, punch_out_utc)
    overtime_minutes = compute_overtime(
        punch_out_utc,
        emp.get("shift_start", "09:30"),
        emp.get("shift_end", "18:30"),
        record["date"],
    )
    half_day = compute_half_day(work_hours)

    updated = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},
        {
            "$set": {
                "punch_out": punch_out_utc,
                "work_hours": work_hours,
                "overtime_minutes": overtime_minutes,
                "half_day": half_day,
            }
        },
        return_document=ReturnDocument.AFTER,
    )
    if not updated:
        raise HTTPException(409, "Already punched out")
    return format_attendance_record(updated)


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeIn):
    try:
        date_cls.fromisoformat(date)
    except Exception:
        raise HTTPException(422, "Invalid date format, expected YYYY-MM-DD")

    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not record:
        raise HTTPException(404, "Attendance record not found for date")

    # Determine target status
    target_status = body.status if body.status is not None else record.get("status")

    if target_status in ("ABSENT", "LEAVE"):
        # Setting status ABSENT or LEAVE clears both punch times, so supplying punch_in or punch_out with it is 422
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(422, "Cannot supply punch_in or punch_out when status is ABSENT or LEAVE")
        target_punch_in = None
        target_punch_out = None
        target_work_hours = None
        target_late_minutes = 0
        target_overtime_minutes = 0
        target_half_day = False
    else:
        # Presence status: PRESENT, WFH, ON_DUTY
        if body.punch_in is not None:
            truncated_in_sec = body.punch_in // 1000
            target_punch_in = datetime.fromtimestamp(truncated_in_sec, tz=timezone.utc)
        else:
            target_punch_in = record.get("punch_in")
            if target_punch_in is not None and target_punch_in.tzinfo is None:
                target_punch_in = target_punch_in.replace(tzinfo=timezone.utc)

        if target_punch_in is None:
            raise HTTPException(422, "Presence status requires a punch_in")

        # punch_in must stay on the record's attendance date (R1)
        att_date = determine_attendance_date(
            target_punch_in, emp.get("shift_start", "09:30"), emp.get("shift_end", "18:30")
        )
        if att_date != date:
            raise HTTPException(422, "punch_in must belong to the record's attendance date per R1")

        if body.punch_out is not None:
            truncated_out_sec = body.punch_out // 1000
            target_punch_out = datetime.fromtimestamp(truncated_out_sec, tz=timezone.utc)
        else:
            target_punch_out = record.get("punch_out")
            if target_punch_out is not None and target_punch_out.tzinfo is None:
                target_punch_out = target_punch_out.replace(tzinfo=timezone.utc)

        if target_punch_out is not None:
            diff_sec = (target_punch_out - target_punch_in).total_seconds()
            if diff_sec <= 0:
                raise HTTPException(422, "punch_out must be strictly after punch_in")
            if diff_sec > 24 * 3600:
                raise HTTPException(422, "punch_out cannot be more than 24 hours after punch_in")
            target_work_hours = compute_work_hours(target_punch_in, target_punch_out)
            target_overtime_minutes = compute_overtime(
                target_punch_out,
                emp.get("shift_start", "09:30"),
                emp.get("shift_end", "18:30"),
                date,
            )
            target_half_day = compute_half_day(target_work_hours)
        else:
            target_work_hours = None
            target_overtime_minutes = 0
            target_half_day = False

        target_late_minutes = compute_late_minutes(
            target_punch_in, emp.get("shift_start", "09:30"), date
        )

    # Detect actual changes
    changes = {}
    existing_status = record.get("status")
    if existing_status != target_status:
        changes["status"] = {"from": existing_status, "to": target_status}

    existing_in = record.get("punch_in")
    if to_epoch_ms(existing_in) != to_epoch_ms(target_punch_in):
        changes["punch_in"] = {"from": existing_in, "to": target_punch_in}

    existing_out = record.get("punch_out")
    if to_epoch_ms(existing_out) != to_epoch_ms(target_punch_out):
        changes["punch_out"] = {"from": existing_out, "to": target_punch_out}

    existing_wh = record.get("work_hours")
    if existing_wh != target_work_hours:
        changes["work_hours"] = {"from": existing_wh, "to": target_work_hours}

    existing_late = record.get("late_minutes", 0)
    if existing_late != target_late_minutes:
        changes["late_minutes"] = {"from": existing_late, "to": target_late_minutes}

    existing_ot = record.get("overtime_minutes", 0)
    if existing_ot != target_overtime_minutes:
        changes["overtime_minutes"] = {"from": existing_ot, "to": target_overtime_minutes}

    existing_hd = bool(record.get("half_day", False))
    if existing_hd != target_half_day:
        changes["half_day"] = {"from": existing_hd, "to": target_half_day}

    # A request that changes nothing is 422
    if not changes:
        raise HTTPException(422, "Request produces no changes")

    now_utc = datetime.now(timezone.utc).replace(microsecond=0)
    history_entry = {
        "at": now_utc,
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    existing_history = record.get("history") or []
    hist_len = len(existing_history)

    # Atomic update ensuring concurrent edits do not silently lose history entries
    updated = db.attendance_logs.find_one_and_update(
        {
            "_id": record["_id"],
            f"history.{hist_len}": {"$exists": False},
        },
        {
            "$set": {
                "status": target_status,
                "punch_in": target_punch_in,
                "punch_out": target_punch_out,
                "work_hours": target_work_hours,
                "late_minutes": target_late_minutes,
                "overtime_minutes": target_overtime_minutes,
                "half_day": target_half_day,
            },
            "$push": {"history": history_entry},
        },
        return_document=ReturnDocument.AFTER,
    )
    if not updated:
        raise HTTPException(409, "Concurrent regularization conflict")
    return format_attendance_record(updated)


# --------------------------------------------------------------------------- #
# Analytics Endpoints (Milestone 3)
# --------------------------------------------------------------------------- #
def build_employee_monthly_pipeline(
    emp_code: str,
    month_start: str,
    month_end: str,
) -> list:
    return [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {"$gte": month_start, "$lte": month_end},
            }
        },
        {
            "$project": {
                "date": 1,
                "status": 1,
                "half_day": 1,
                "late_minutes": 1,
                "overtime_minutes": 1,
                "day_of_week": {
                    "$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}
                },
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$day_of_week", [2, 3, 4, 5, 6]]},
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                ]
                            },
                            {"$cond": ["$half_day", 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {
                        "$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]
                    }
                },
                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$late_minutes", 0]}
                },
                "total_overtime_minutes": {
                    "$sum": {"$ifNull": ["$overtime_minutes", 0]}
                },
            }
        },
    ]


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(
    emp_code: str,
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")

    month_start, month_end, _ = get_month_boundaries(month)
    joined_on = emp.get("joined_on", "1970-01-01")

    if joined_on > month_end:
        working_days = 0
    else:
        effective_start = max(month_start, joined_on)
        working_days = count_working_days(effective_start, month_end)

    pipeline = build_employee_monthly_pipeline(emp_code, month_start, month_end)
    agg = list(db.attendance_logs.aggregate(pipeline))
    if agg:
        res = agg[0]
        present_days = float(res.get("present_days", 0.0))
        leave_days = int(res.get("leave_days", 0))
        late_count = int(res.get("late_count", 0))
        total_late_minutes = int(res.get("total_late_minutes", 0))
        total_overtime_minutes = int(res.get("total_overtime_minutes", 0))
    else:
        present_days = 0.0
        leave_days = 0
        late_count = 0
        total_late_minutes = 0
        total_overtime_minutes = 0

    if working_days > 0:
        attendance_pct = round_half_up((present_days / working_days) * 100, 2)
    else:
        attendance_pct = None

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late_minutes,
        "total_overtime_minutes": total_overtime_minutes,
        "attendance_pct": attendance_pct,
    }


def build_department_summary_pipeline(
    month_start: str,
    month_end: str,
    department: Optional[str] = None,
) -> list:
    match_stage: Dict[str, Any] = {"joined_on": {"$lte": month_end}}
    if department:
        match_stage["department"] = department

    return [
        {"$match": match_stage},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"emp": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$emp"]},
                                    {"$gte": ["$date", month_start]},
                                    {"$lte": ["$date", month_end]},
                                ]
                            }
                        }
                    }
                ],
                "as": "logs",
            }
        },
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {
            "$project": {
                "department": 1,
                "emp_code": 1,
                "log_date": "$logs.date",
                "status": "$logs.status",
                "half_day": "$logs.half_day",
                "work_hours": "$logs.work_hours",
                "late_minutes": "$logs.late_minutes",
                "day_of_week": {
                    "$cond": [
                        {"$ifNull": ["$logs.date", False]},
                        {"$dayOfWeek": {"$dateFromString": {"dateString": "$logs.date"}}},
                        None,
                    ]
                },
            }
        },
        {
            "$group": {
                "_id": "$department",
                "all_employees": {"$addToSet": "$emp_code"},
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$day_of_week", [2, 3, 4, 5, 6]]},
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                ]
                            },
                            {"$cond": ["$half_day", 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "total_work_hours": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$ne": ["$work_hours", None]},
                                ]
                            },
                            "$work_hours",
                            0.0,
                        ]
                    }
                },
                "work_hours_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$ne": ["$work_hours", None]},
                                ]
                            },
                            1,
                            0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]
                    }
                },
                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$late_minutes", 0]}
                },
                "leave_count": {
                    "$sum": {
                        "$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]
                    }
                },
                "on_duty_count": {
                    "$sum": {
                        "$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]
                    }
                },
            }
        },
        {"$sort": {"_id": 1}},
    ]


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    month_start, month_end, _ = get_month_boundaries(month)
    pipeline = build_department_summary_pipeline(month_start, month_end, department)
    cursor = db.employees.aggregate(pipeline)
    items = []
    for row in cursor:
        headcount = len(row.get("all_employees") or [])
        if headcount == 0:
            continue
        wh_count = row.get("work_hours_count", 0)
        if wh_count > 0:
            avg_wh = round_half_up(row.get("total_work_hours", 0.0) / wh_count, 2)
        else:
            avg_wh = None

        items.append({
            "department": row["_id"],
            "headcount": headcount,
            "present_days": float(row.get("present_days", 0.0)),
            "avg_work_hours": avg_wh,
            "late_count": int(row.get("late_count", 0)),
            "total_late_minutes": int(row.get("total_late_minutes", 0)),
            "leave_count": int(row.get("leave_count", 0)),
            "on_duty_count": int(row.get("on_duty_count", 0)),
        })

    return {"month": month, "items": items}


def build_late_leaderboard_pipeline(
    month_start: str,
    month_end: str,
    limit: int = 10,
    department: Optional[str] = None,
) -> list:
    pipeline: List[Dict[str, Any]] = [
        {
            "$match": {
                "date": {"$gte": month_start, "$lte": month_end},
                "late_minutes": {"$gt": 0},
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1},
            }
        },
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
    ]

    if department:
        pipeline.append({"$match": {"emp.department": department}})

    pipeline.extend([
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {
                    "rank": {"$rank": {}}
                },
            }
        },
        {"$match": {"rank": {"$lte": limit}}},
    ])
    return pipeline


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    month_start, month_end, _ = get_month_boundaries(month)
    pipeline = build_late_leaderboard_pipeline(month_start, month_end, limit, department)
    cursor = db.attendance_logs.aggregate(pipeline)
    items = []
    for row in cursor:
        items.append({
            "rank": int(row["rank"]),
            "emp_code": row["_id"],
            "name": row["emp"]["name"],
            "department": row["emp"]["department"],
            "total_late_minutes": int(row["total_late_minutes"]),
            "late_count": int(row["late_count"]),
        })

    return {"month": month, "items": items}


def build_department_trend_pipeline(
    department: str,
    from_date: str,
    num_days: int,
) -> list:
    return [
        {"$match": {"department": department}},
        {"$limit": 1},
        {
            "$project": {
                "dates": {
                    "$map": {
                        "input": {"$range": [0, num_days]},
                        "as": "day_offset",
                        "in": {
                            "$dateToString": {
                                "format": "%Y-%m-%d",
                                "date": {
                                    "$dateAdd": {
                                        "startDate": {"$dateFromString": {"dateString": from_date}},
                                        "unit": "day",
                                        "amount": "$$day_offset",
                                    }
                                },
                            }
                        },
                    }
                }
            }
        },
        {"$unwind": "$dates"},
        {"$project": {"_id": 0, "date": "$dates"}},
        {
            "$addFields": {
                "day_of_week": {
                    "$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}
                }
            }
        },
        {
            "$addFields": {
                "is_working_day": {
                    "$in": ["$day_of_week", [2, 3, 4, 5, 6]]
                }
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "let": {"cur_date": "$date"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$department", department]},
                                    {"$lte": ["$joined_on", "$$cur_date"]},
                                ]
                            }
                        }
                    },
                    {"$project": {"_id": 0, "emp_code": 1}},
                ],
                "as": "active_employees",
            }
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {
                    "cur_date": "$date",
                    "active_codes": "$active_employees.emp_code",
                },
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$date", "$$cur_date"]},
                                    {"$in": ["$emp_code", "$$active_codes"]},
                                ]
                            }
                        }
                    },
                    {
                        "$project": {
                            "_id": 0,
                            "status": 1,
                            "half_day": 1,
                            "late_minutes": 1,
                        }
                    },
                ],
                "as": "day_logs",
            }
        },
        {
            "$addFields": {
                "headcount": {"$size": "$active_employees"},
                "present_count": {
                    "$reduce": {
                        "input": "$day_logs",
                        "initialValue": 0.0,
                        "in": {
                            "$add": [
                                "$$value",
                                {
                                    "$cond": [
                                        {"$in": ["$$this.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                        {"$cond": [{"$eq": ["$$this.half_day", True]}, 0.5, 1.0]},
                                        0.0,
                                    ]
                                },
                            ]
                        },
                    }
                },
                "late_count": {
                    "$size": {
                        "$filter": {
                            "input": "$day_logs",
                            "as": "log",
                            "cond": {"$gt": ["$$log.late_minutes", 0]},
                        }
                    }
                },
            }
        },
        {
            "$addFields": {
                "attendance_rate": {
                    "$cond": [
                        {
                            "$and": [
                                "$is_working_day",
                                {"$gt": ["$headcount", 0]},
                            ]
                        },
                        {
                            "$divide": [
                                {
                                    "$floor": {
                                        "$add": [
                                            {
                                                "$multiply": [
                                                    {
                                                        "$divide": [
                                                            {"$toDecimal": "$present_count"},
                                                            {"$toDecimal": "$headcount"},
                                                        ]
                                                    },
                                                    {"$toDecimal": "10000"},
                                                ]
                                            },
                                            {"$toDecimal": "0.5"},
                                        ]
                                    }
                                },
                                {"$toDecimal": "10000"},
                            ]
                        },
                        None,
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"date": 1},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {
                            "documents": [-6, 0]
                        },
                    }
                },
            }
        },
        {
            "$addFields": {
                "moving_avg_7d": {
                    "$cond": [
                        {"$ne": ["$moving_avg_7d", None]},
                        {
                            "$divide": [
                                {
                                    "$floor": {
                                        "$add": [
                                            {
                                                "$multiply": [
                                                    {"$toDecimal": "$moving_avg_7d"},
                                                    {"$toDecimal": "10000"},
                                                ]
                                            },
                                            {"$toDecimal": "0.5"},
                                        ]
                                    }
                                },
                                {"$toDecimal": "10000"},
                            ]
                        },
                        None,
                    ]
                }
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": {
                    "$cond": [
                        {"$ne": ["$attendance_rate", None]},
                        {"$toDouble": "$attendance_rate"},
                        None,
                    ]
                },
                "moving_avg_7d": {
                    "$cond": [
                        {"$ne": ["$moving_avg_7d", None]},
                        {"$toDouble": "$moving_avg_7d"},
                        None,
                    ]
                },
            }
        },
        {"$sort": {"date": 1}},
    ]


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_date: str = Query(..., alias="from"),
    to_date: str = Query(..., alias="to"),
):
    try:
        from_dt = date_cls.fromisoformat(from_date)
        to_dt = date_cls.fromisoformat(to_date)
    except Exception:
        raise HTTPException(422, "Invalid date format, expected YYYY-MM-DD")

    if to_dt < from_dt:
        raise HTTPException(422, "'to' date must be on or after 'from' date")

    num_days = (to_dt - from_dt).days
    if num_days > 92:
        raise HTTPException(422, "Date range cannot exceed 92 days")

    dept_exists = db.employees.find_one({"department": department})
    if not dept_exists:
        raise HTTPException(404, "Department not found")

    num_days = (to_dt - from_dt).days + 1
    pipeline = build_department_trend_pipeline(department, from_date, num_days)
    cursor = db.employees.aggregate(pipeline)
    return {"department": department, "items": list(cursor)}


# --------------------------------------------------------------------------- #
# Admin Explain Diagnostic Endpoint
# --------------------------------------------------------------------------- #
def sanitize_explain(obj: Any) -> Any:
    """Recursively convert BSON types (e.g. Decimal128, ObjectId) to JSON-serializable Python types."""
    if isinstance(obj, dict):
        return {k: sanitize_explain(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [sanitize_explain(v) for v in obj]
    if isinstance(obj, Decimal128):
        return obj.to_decimal()
    if isinstance(obj, ObjectId):
        return str(obj)
    return obj


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str,
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if endpoint not in (
        "attendance_list",
        "employee_monthly",
        "department_summary",
        "late_leaderboard",
        "department_trend",
    ):
        raise HTTPException(422, f"Unknown endpoint for explain: {endpoint}")

    try:
        if endpoint == "attendance_list":
            col_name = "attendance_logs"
            if status is not None:
                if status not in ("PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"):
                    raise HTTPException(422, "Invalid status value")
            if date_from and date_to:
                if date_from > date_to:
                    raise HTTPException(422, "date_from cannot be greater than date_to")

            q = build_attendance_list_query(emp_code, date_from, date_to, status)
            skip = (page - 1) * page_size
            explain_doc = db.command(
                "explain",
                {
                    "find": col_name,
                    "filter": q,
                    "sort": {"date": -1, "emp_code": 1},
                    "skip": skip,
                    "limit": page_size,
                },
                verbosity="executionStats",
            )

        elif endpoint == "employee_monthly":
            if not emp_code or not month:
                raise HTTPException(422, "emp_code and month are required for employee_monthly")
            col_name = "attendance_logs"
            month_start, month_end, _ = get_month_boundaries(month)
            pipeline = build_employee_monthly_pipeline(emp_code, month_start, month_end)
            explain_doc = db.command(
                "explain",
                {"aggregate": col_name, "pipeline": pipeline, "cursor": {}},
                verbosity="executionStats",
            )

        elif endpoint == "department_summary":
            if not month:
                raise HTTPException(422, "month is required for department_summary")
            col_name = "employees"
            month_start, month_end, _ = get_month_boundaries(month)
            pipeline = build_department_summary_pipeline(month_start, month_end, department)
            explain_doc = db.command(
                "explain",
                {"aggregate": col_name, "pipeline": pipeline, "cursor": {}},
                verbosity="executionStats",
            )

        elif endpoint == "late_leaderboard":
            if not month:
                raise HTTPException(422, "month is required for late_leaderboard")
            col_name = "attendance_logs"
            month_start, month_end, _ = get_month_boundaries(month)
            pipeline = build_late_leaderboard_pipeline(month_start, month_end, limit, department)
            explain_doc = db.command(
                "explain",
                {"aggregate": col_name, "pipeline": pipeline, "cursor": {}},
                verbosity="executionStats",
            )

        elif endpoint == "department_trend":
            if not department or not from_date or not to_date:
                raise HTTPException(422, "department, from, and to are required for department_trend")
            try:
                from_dt = date_cls.fromisoformat(from_date)
                to_dt = date_cls.fromisoformat(to_date)
            except Exception:
                raise HTTPException(422, "Invalid date format, expected YYYY-MM-DD")

            if to_dt < from_dt:
                raise HTTPException(422, "'to' date must be on or after 'from' date")

            num_days = (to_dt - from_dt).days
            if num_days > 92:
                raise HTTPException(422, "Date range cannot exceed 92 days")

            col_name = "employees"
            num_days = (to_dt - from_dt).days + 1
            pipeline = build_department_trend_pipeline(department, from_date, num_days)
            explain_doc = db.command(
                "explain",
                {"aggregate": col_name, "pipeline": pipeline, "cursor": {}},
                verbosity="executionStats",
            )

        return {
            "endpoint": endpoint,
            "collection": col_name,
            "explain": sanitize_explain(explain_doc),
        }
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(503, f"Database unavailable or explain error: {str(exc)}")
