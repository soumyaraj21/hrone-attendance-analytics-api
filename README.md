# Employee Attendance & Analytics API

A robust, spec-compliant FastAPI and MongoDB backend for employee attendance tracking, manual regularization, and department analytics. Built strictly according to the assignment's official OpenAPI contract (`openapi.yaml`) and data architecture specification (`DATA_MODEL.md`).

---

## 1. Architecture & Design Principles

* **Single Production File**: All production code is self-contained in [`app/main.py`](app/main.py).
* **Deterministic Precision**: Implements Rule R8 strict Half-Up rounding via Python's `decimal.Decimal` (`ROUND_HALF_UP`), eliminating floating-point and Banker's rounding discrepancies.
* **Natural Business Key**: Uses customer-assigned `emp_code` (e.g. `EMP0001`) as the unique identifier. Internal MongoDB `_id` values are never exposed in requests or responses.
* **Epoch Millisecond Instants**: All point-in-time instants (`punched_at`, `punch_in`, `punch_out`, `created_at`, `history[].at`) are integer epoch milliseconds ($10^{11} \le t \le 4.1 \times 10^{12}$). Seconds-scale values, floats, and strings are rejected with HTTP 422.
* **Native Analytics**: Analytics endpoints run via MongoDB aggregation pipelines (`$lookup`, `$unwind`, `$group`, `$setWindowFields`) without in-memory Python looping.

---

## 2. Configuration & Environment

The application dynamically reads configuration from environment variables or a local `.env` file:

| Variable | Default Value | Description |
|---|---|---|
| `MONGO_URI` | `mongodb://localhost:27017` | MongoDB connection URI (Atlas or local instance) |
| `MONGO_DB` | `attendance_db` | Target MongoDB database name |

---

## 3. Local Setup & Startup

### Prerequisites
* Python 3.11+ (tested on Python 3.13)
* MongoDB 6.0+ (the grader uses 7.0; MongoDB Atlas free tier is supported)

### Installation
```bash
# 1. Create and activate a virtual environment
python -m venv .venv
# Windows (PowerShell):
.venv\Scripts\Activate.ps1
# Linux / macOS:
source .venv/bin/activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. Configure environment
# Copy .env.example if present, or set MONGO_URI in your environment:
$env:MONGO_URI="mongodb://localhost:27017"
$env:MONGO_DB="attendance_db"
```

### Running the Server
```bash
uvicorn app.main:app --port 8000
```
On startup, FastAPI's `lifespan` handler automatically and idempotently ensures all required indexes exist on `employees` and `attendance_logs`.

---

## 4. API Endpoints

### System
* `GET /health` — Returns `200 {"status": "ok"}` if MongoDB responds to `ping`; otherwise returns `503 {"detail": "Database unavailable"}`.

### Employees
* `POST /employees` — Creates a new employee with schema validation (`emp_code` regex `^EMP\d{4,6}$`, shift formats, distinct shift times). Returns 409 on duplicate.
* `GET /employees` — Paginated list of employees sorted by `emp_code` ascending, with optional `department` filter.

### Attendance
* `POST /attendance/punch-in` — Records daily punch-in. Resolves attendance date in IST (handling overnight shifts per R1), computes `late_minutes` (R2), and enforces unique daily punches via compound index (409 on duplicate).
* `POST /attendance/punch-out` — Closes the employee's most recent open record where `punch_in <= punched_at`. Validates durations ($0 < \Delta \le 24$h), recomputes `work_hours` (R4), `overtime_minutes` (R3), and `half_day` (R5). Uses atomic `find_one_and_update` to prevent concurrent punch-out races (409 for concurrent losers).
* `GET /attendance` — Paginated attendance logs sorted by `date` descending, then `emp_code` ascending. Supports filters: `emp_code`, `date_from`, `date_to`, `status`.
* `PATCH /attendance/{emp_code}/{date}` — Manually regularizes an attendance record. Enforces status rules (clears punch times for `ABSENT`/`LEAVE`; requires `punch_in` for presence statuses; validates R1 date alignment). Detects effective changes (422 if no net change), recomputes derived metrics, and atomically appends an immutable audit entry to `history`.

### Analytics
* `GET /analytics/employees/{emp_code}/monthly` — Monthly summary for one employee. Dynamically computes `working_days` (Mon–Fri from `max(month_start, joined_on)` per R7), `present_days` (Mon–Fri only, half day = 0.5), `leave_days`, late/overtime totals, and `attendance_pct` (rounded 2 decimals half-up; null if 0 working days).
* `GET /analytics/departments/summary` — Cross-collection aggregation anchored on `employees` (`joined_on <= month_end`). Includes staff with zero logs in `headcount` (R9). Calculates record-level `avg_work_hours` across individual attendance records. Omits departments with zero headcount.
* `GET /analytics/leaderboard/late` — Ranks employees by total late minutes using **Standard Competition Ranking** (`$rank`: 1, 2, 2, 4). The `limit` parameter filters `rank <= limit` **after** ranking, ensuring all ties at the cutoff boundary are returned.
* `GET /analytics/departments/{department}/trend` — Daily trend across `[from, to]` inclusive (max 92 days). Guarantees exactly one row per calendar day (including weekends and zero-activity days). Computes daily `headcount`, `present_count`, `attendance_rate` (null on weekends/zero headcount; 0.0000 on zero-log weekdays), and a 7-day moving average (`moving_avg_7d`) over non-null rates strictly within the requested date range.

### Diagnostics
* `GET /admin/explain/{endpoint}` — Reconstructs the exact query or aggregation pipeline for the specified endpoint with `executionStats` verbosity to inspect index traversal and execution plans.

---

## 5. Automated Tests

The test suite covers schema validation, domain calculations, endpoint handlers, concurrency simulation, analytics aggregations, and startup/index lifecycle:

```bash
# Run all tests
python -m pytest tests/ -v
```

### Verification Status
* **Automated Test Suite**: **99 passed, 0 failed (100%)**
  * `test_api_validation.py` (12 tests): FastAPI/Pydantic validation, boundary errors, database outage 503 response.
  * `test_milestone1.py` (7 tests): Rounding precision (R8), shift dates (R1), late calculations (R2), overtime (R3), work hours (R4), half day (R5).
  * `test_milestone2.py` (23 tests): Punch-out duration validation, overnight shift overtime, atomic punch-out race handling, regularization transitions, zero-change 422 checks, and audit history diffing.
  * `test_milestone3.py` (21 tests): Working days for mid-month joiners, department summary zero-log headcount, record-level work hours mean, competition ranking ties, 5-day trend gap filling with moving averages, and explain endpoints.
  * `test_department_trend.py` (15 tests): Continuous date series, gap filling, 7-day moving averages, exact Decimal128 ROUND_HALF_UP halfway ties, and binary float precision-safe rounding.
  * `test_admin_explain.py` (11 tests): Diagnostic execution plan generation, shared pipeline builders, executionStats verbosity, and parameter validation.
  * `test_startup_indexes.py` (10 tests): Explicit index definitions, idempotent creation, failure surfacing on startup, lifespan failure on DB outages, 5000ms bounded timeout, and URI timeout clamping.
* **Database Verification Scope**:
  * The automated 99-test suite executes in an offline/mocked environment using mock database drivers and pipeline simulators to validate contracts, business logic, boundary conditions, and schema compliance deterministically without state leakage.
  * Live MongoDB connectivity was separately verified against a local instance (`mongodb://localhost:27017`) via `ping` command checks and client timeout enforcement. Full physical storage-engine executionStats timings and multi-node replication benchmarks were not part of the automated suite.

---

## 6. Submission Artifacts

* `app/main.py`: Production application code.
* `requirements.txt`: Python package dependencies.
* `REVIEW.md`: Itemized review of 22 starter defects with symptoms and fixes, plus non-defect architectural decisions.
* `DECISIONS.md`: Concise analysis of index design, concurrency handling, tie resolution, headcount logic, and 100x scale enhancements.
* `README.md`: System documentation, configuration, and endpoint reference.
* `tests/`: Complete 99-test automated test suite covering all milestones and reliability fixes.
