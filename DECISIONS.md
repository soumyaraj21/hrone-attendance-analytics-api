# Architectural Decisions

### 1. Indexes
* **`employees`**:
  * `emp_code` (unique): Enforces primary business key uniqueness and prevents duplicate creations under concurrency.
  * `(department, emp_code)`: Optimizes department lookups and trend scans.
  * `(department, joined_on)`: Accelerates department summary filtering (`joined_on <= month_end`).
* **`attendance_logs`**:
  * `(emp_code, date)` (unique): Enforces rule R1 (one log per employee per day) and atomic natural-key addressing.
  * `(date: -1, emp_code: 1)`: Powers list attendance pagination without in-memory sorting.
  * `(emp_code: 1, punch_in: -1)`: Optimizes resolving the latest open punch-in during punch-out.
  * `(date: 1, status: 1)` & `(date: 1, late_minutes: 1)`: Support date-scoped aggregations and leaderboard scans.
* **Considered & Rejected**: An index on `punch_out` alone was rejected; open-record lookups always filter by `emp_code` and sort by `punch_in`, which `(emp_code, punch_in)` already covers with higher selectivity.

### 2. Punch-in Race
Two concurrent punch-in requests validate the employee and attempt `insert_one`. WiredTiger resolves the race on the unique compound index `(emp_code, date)`. One write succeeds (201 Created); the second triggers a PyMongo `DuplicateKeyError`, which our handler intercepts to return HTTP 409 (`already punched in for this date`).

### 3. Ties
Using standard competition ranking (`$rank`), ties share rank and subsequent ranks skip (e.g. 1, 2, 2, 4). The `limit` condition is applied post-ranking (`rank <= limit`). When employees tie at or below the cutoff rank, all are returned, legitimately exceeding `limit` rows per contract.

### 4. Headcount
The pipeline starts from `employees` matching `joined_on <= month_end`, joins `attendance_logs` via `$lookup`, and unwinds with `preserveNullAndEmptyArrays: True`. Employees with zero monthly logs remain in the stream and contribute to `$addToSet: "$emp_code"`, guaranteeing headcount reflects all active staff.

### 5. 100x Scale
Maintain pre-aggregated daily and monthly rollup collections updated via MongoDB Change Streams upon punch-out/regularization. This eliminates cross-collection `$lookup` and `$setWindowFields` on millions of raw logs during read requests.
