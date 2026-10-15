import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.main import (
    round_half_up,
    determine_attendance_date,
    compute_late_minutes,
    compute_work_hours,
    compute_overtime,
    compute_half_day,
    EmployeeIn,
    PunchInIn,
    IST
)

def test_rounding():
    assert round_half_up(2.525, 2) == 2.53
    assert round_half_up(2.524, 2) == 2.52
    assert round_half_up(0.12345, 4) == 0.1235
    print("Rounding tests passed!")

def test_r1_attendance_date():
    # Standard shift 09:30 - 18:30 IST on 2026-07-06
    # 09:28 IST = 03:58 UTC
    p1 = datetime(2026, 7, 6, 3, 58, 0, tzinfo=timezone.utc)
    d1 = determine_attendance_date(p1, "09:30", "18:30")
    assert d1 == "2026-07-06"

    # Overnight shift 22:00 - 06:00
    # Punch-in at 21:55 IST (16:25 UTC) on 2026-07-06
    p2 = datetime(2026, 7, 6, 16, 25, 0, tzinfo=timezone.utc)
    d2 = determine_attendance_date(p2, "22:00", "06:00")
    assert d2 == "2026-07-06"

    # Punch-in at 01:10 IST (19:40 UTC previous day) on 2026-07-07
    p3 = datetime(2026, 7, 6, 19, 40, 0, tzinfo=timezone.utc)
    # in IST this is 2026-07-07 01:10:00
    assert p3.astimezone(IST).hour == 1 and p3.astimezone(IST).minute == 10
    d3 = determine_attendance_date(p3, "22:00", "06:00")
    assert d3 == "2026-07-06"
    print("R1 attendance date tests passed!")

def test_r2_late():
    # Shift 09:30: 09:40:00 gives 0; 09:40:01 gives 10; 10:15:59 gives 45
    p_exact = datetime(2026, 7, 6, 9, 40, 0, tzinfo=IST)
    assert compute_late_minutes(p_exact, "09:30", "2026-07-06") == 0

    p_late1 = datetime(2026, 7, 6, 9, 40, 1, tzinfo=IST)
    assert compute_late_minutes(p_late1, "09:30", "2026-07-06") == 10

    p_late2 = datetime(2026, 7, 6, 10, 15, 59, tzinfo=IST)
    assert compute_late_minutes(p_late2, "09:30", "2026-07-06") == 45
    print("R2 late tests passed!")

def test_r3_overtime():
    # Shift 09:30 - 18:30. Punch-out 18:35 -> 5 min < 30 -> 0
    p1 = datetime(2026, 7, 6, 18, 35, 0, tzinfo=IST)
    assert compute_overtime(p1, "09:30", "18:30", "2026-07-06") == 0

    # Punch-out 19:10 -> 40 min >= 30 -> 40
    p2 = datetime(2026, 7, 6, 19, 10, 0, tzinfo=IST)
    assert compute_overtime(p2, "09:30", "18:30", "2026-07-06") == 40

    # Overnight shift 22:00 - 06:00 on 2026-07-06. Out at 06:40 on 2026-07-07
    p3 = datetime(2026, 7, 7, 6, 40, 0, tzinfo=IST)
    assert compute_overtime(p3, "22:00", "06:00", "2026-07-06") == 40
    print("R3 overtime tests passed!")

def test_r4_work_hours():
    # 9h 7min 12s -> 9.12
    p_in = datetime(2026, 7, 6, 3, 58, 0, tzinfo=timezone.utc)
    p_out = datetime(2026, 7, 6, 13, 5, 0, tzinfo=timezone.utc)
    # (13*3600+5*60) - (3*3600+58*60) = 47100 - 14280 = 32820s / 3600 = 9.116666... -> 9.12
    assert compute_work_hours(p_in, p_out) == 9.12
    print("R4 work hours tests passed!")

def test_r5_half_day():
    assert compute_half_day(3.50) is True
    assert compute_half_day(4.49) is True
    assert compute_half_day(4.50) is False
    assert compute_half_day(9.12) is False
    assert compute_half_day(None) is False
    print("R5 half day tests passed!")

def test_models():
    # Valid employee
    emp = EmployeeIn(
        emp_code="EMP0001",
        name="Asha Rao",
        email="asha@example.com",
        department="Engineering",
        joined_on="2026-01-05"
    )
    assert emp.emp_code == "EMP0001"

    # Invalid shift
    try:
        EmployeeIn(
            emp_code="EMP0001",
            name="Asha Rao",
            email="asha@example.com",
            department="Engineering",
            shift_start="09:30",
            shift_end="09:30",
            joined_on="2026-01-05"
        )
        assert False, "Should have failed shift validation"
    except ValueError:
        pass

    # Invalid punch status
    try:
        PunchInIn(emp_code="EMP0001", status="LEAVE")
        assert False, "Should have failed status validation"
    except ValueError:
        pass

    # Invalid punched_at (seconds instead of ms)
    try:
        PunchInIn(emp_code="EMP0001", punched_at=1783312500)
        assert False, "Should have failed punched_at bounds"
    except ValueError:
        pass
    print("Model validation tests passed!")

if __name__ == "__main__":
    test_rounding()
    test_r1_attendance_date()
    test_r2_late()
    test_r3_overtime()
    test_r4_work_hours()
    test_r5_half_day()
    test_models()
    print("\nALL UNIT TESTS PASSED SUCCESSFULLY!")
