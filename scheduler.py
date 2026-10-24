"""Monthly employee shift schedule: a roster of employees (name + phone) and,
per month, which shift each employee works on each day.

Three 8-hour shifts cover the day: 7a-3p, 3p-11p and 11p-7a. Data is stored
as JSON under instance/ (never committed), one file for the roster and one
per month for the shifts.
"""
import calendar
import json
import re
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
SCHEDULE_DIR = BASE_DIR / "instance" / "schedule"
ROSTER_PATH = SCHEDULE_DIR / "employees.json"

DEFAULT_EMPLOYEE_COUNT = 35

# code -> label and paid hours. "OFF" is an explicit day off (0 hours).
SHIFTS = {
    "D": {"label": "7a-3p", "hours": 8},
    "E": {"label": "3p-11p", "hours": 8},
    "N": {"label": "11p-7a", "hours": 8},
    "OFF": {"label": "Off", "hours": 0},
}

MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


def _parse_month(month: str):
    if not MONTH_RE.match(month or ""):
        raise ValueError(f"Month must look like YYYY-MM, got {month!r}")
    year, mon = month.split("-")
    return int(year), int(mon)


def days_in_month(month: str) -> int:
    year, mon = _parse_month(month)
    return calendar.monthrange(year, mon)[1]


def day_headers(month: str):
    """[{day: 1, weekday: 'Thu'}, ...] for the top row of the grid."""
    year, mon = _parse_month(month)
    return [
        {"day": d, "weekday": calendar.day_abbr[calendar.weekday(year, mon, d)]}
        for d in range(1, days_in_month(month) + 1)
    ]


def _read_json(path: Path, default):
    if not path.exists():
        return default
    with open(path) as f:
        return json.load(f)


def _write_json(path: Path, data):
    SCHEDULE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    tmp.replace(path)


def default_roster():
    return [
        {"id": f"e{i}", "name": f"Employee {i}", "phone": ""}
        for i in range(1, DEFAULT_EMPLOYEE_COUNT + 1)
    ]


def load_roster():
    return _read_json(ROSTER_PATH, None) or default_roster()


def save_roster(employees):
    cleaned = []
    seen = set()
    for i, emp in enumerate(employees, start=1):
        emp_id = str(emp.get("id") or f"e{i}")
        if emp_id in seen:
            raise ValueError(f"Duplicate employee id {emp_id}")
        seen.add(emp_id)
        cleaned.append({
            "id": emp_id,
            "name": (emp.get("name") or "").strip() or f"Employee {i}",
            "phone": (emp.get("phone") or "").strip(),
        })
    _write_json(ROSTER_PATH, cleaned)
    return cleaned


def _month_path(month: str) -> Path:
    _parse_month(month)
    return SCHEDULE_DIR / f"{month}.json"


def load_shifts(month: str):
    """{employee_id: {"1": "D", "2": "OFF", ...}} for the given month."""
    return _read_json(_month_path(month), {})


def save_shifts(month: str, shifts):
    max_day = days_in_month(month)
    cleaned = {}
    for emp_id, days in (shifts or {}).items():
        row = {}
        for day, code in (days or {}).items():
            if not code:
                continue
            if code not in SHIFTS:
                raise ValueError(f"Unknown shift {code!r}")
            if not 1 <= int(day) <= max_day:
                raise ValueError(f"Day {day} is outside {month}")
            row[str(int(day))] = code
        if row:
            cleaned[str(emp_id)] = row
    _write_json(_month_path(month), cleaned)
    return cleaned


def daily_totals(month: str, employees, shifts):
    """Per-day scheduled hours and headcount per shift, for the bottom rows."""
    totals = []
    ids = {e["id"] for e in employees}
    for day in range(1, days_in_month(month) + 1):
        counts = {code: 0 for code in SHIFTS}
        for emp_id, days in shifts.items():
            if emp_id not in ids:
                continue
            code = days.get(str(day))
            if code:
                counts[code] += 1
        hours = sum(SHIFTS[code]["hours"] * n for code, n in counts.items())
        totals.append({"day": day, "hours": hours, "counts": counts})
    return totals


def employee_hours(shifts_for_employee):
    return sum(SHIFTS[code]["hours"] for code in (shifts_for_employee or {}).values())


def schedule_text(month: str, employee, shifts_for_employee) -> str:
    """The SMS body for one employee's month."""
    year, mon = _parse_month(month)
    worked = sorted(
        (int(day), code)
        for day, code in (shifts_for_employee or {}).items()
        if code != "OFF"
    )
    header = f"{employee['name']}, your {calendar.month_name[mon]} {year} schedule"
    if not worked:
        return f"{header}: no shifts scheduled."
    lines = [f"{mon}/{day} {SHIFTS[code]['label']}" for day, code in worked]
    hours = employee_hours(shifts_for_employee)
    return f"{header} ({hours} hrs):\n" + "\n".join(lines)
