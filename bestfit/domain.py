"""Placement rules from the Best Fit CRM Studio blueprint v1.3.

Stage indexes are 0–9. Prose in the blueprint that says “stage 9” and
“stage 10” counts from 1: Invoice Sent is 8, Invoice Paid is 9.
"""

from __future__ import annotations

import re
import statistics
from datetime import date, datetime, timedelta


STAGES = (
    "Prospect",
    "Sent Contract",
    "Contract Signed",
    "Tours Scheduled",
    "Decision Time",
    "Community Chosen",
    "Move-in Date Scheduled",
    "Actual Move-in Date",
    "Invoice Sent to Community",
    "Invoice Paid",
)

MOVE_IN_INDEX = 7
INVOICE_SENT_INDEX = 8
INVOICE_PAID_INDEX = 9

# Days in the current stage after which the prospect is stalled.
# Invoice Paid never stalls.
DEFAULT_STALL_DAYS = {
    0: 7,
    1: 7,
    2: 7,
    3: 10,
    4: 10,
    5: 10,
    6: 45,
    7: 7,
    8: 30,
}

# stage index -> (task title, days until due)
# Stage 4 is included on purpose. The prototype highlighted Decision Time
# as stalled and never created a reminder, which is where families go quiet.
PLACEMENT_TASKS = {
    1: ("Follow up on contract signature", 2),
    2: ("Schedule community tours", 1),
    3: ("Confirm tours and follow up with family", 1),
    4: ("Check in with the family during decision time", 2),
    5: ("Confirm preferred community and move-in plan", 1),
    6: ("Confirm upcoming move-in arrangements", 1),
    7: ("Prepare and send community referral invoice", 1),
    8: ("Follow up on community invoice payment", 7),
}

INACTIVE_AFTER_DAYS = 90
DIRECTORY_RADIUS_MILES = 120
AUSTIN = (30.2672, -97.7431)


def normalize_email(value: str | None) -> str | None:
    text = (value or "").strip().lower()
    return text or None


def normalize_phone(value: str | None) -> str | None:
    digits = re.sub(r"\D", "", value or "")
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return digits or None


def engagement_status(anchor: date, today: date) -> str:
    if (today - anchor).days >= INACTIVE_AFTER_DAYS:
        return "Inactive"
    return "Active"


def parse_iso(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def days_in_stage(milestone: date | None, today: date) -> int | None:
    if milestone is None:
        return None
    return (today - milestone).days


def is_stalled(stage_index: int, days: int | None, limits: dict[int, int]) -> bool:
    if stage_index >= INVOICE_PAID_INDEX or days is None:
        return False
    limit = limits.get(stage_index)
    if limit is None:
        return False
    return days > limit


def placement_task_for(stage_index: int) -> tuple[str, int] | None:
    spec = PLACEMENT_TASKS.get(stage_index)
    if spec is None:
        return None
    return spec


def task_key(contact_id: str, stage_index: int) -> str:
    return f"{contact_id}:{stage_index}"


def duration_days(start: date | None, end: date | None) -> int | None:
    if start is None or end is None:
        return None
    return (end - start).days


def dates_out_of_order(milestones: dict[int, date]) -> bool:
    previous = None
    for _index, milestone in sorted(milestones.items()):
        if previous is not None and milestone < previous:
            return True
        previous = milestone
    return False


def _median(values: list[int]) -> float | None:
    if not values:
        return None
    return float(statistics.median(values))


def summarize(values: list[int]) -> dict[str, float | int | None]:
    if not values:
        return {"count": 0, "average": None, "median": None, "fastest": None, "slowest": None}
    return {
        "count": len(values),
        "average": round(sum(values) / len(values), 1),
        "median": _median(values),
        "fastest": min(values),
        "slowest": max(values),
    }


def prospect_start(milestones: dict[int, date], created_on: date | None) -> date | None:
    return milestones.get(0) or created_on


def duration_rows(milestones: dict[int, date], created_on: date | None) -> dict[str, int | None]:
    start = prospect_start(milestones, created_on)
    return {
        "prospect_to_signed": duration_days(start, milestones.get(2)),
        "signed_to_move_in": duration_days(milestones.get(2), milestones.get(MOVE_IN_INDEX)),
        "move_in_to_paid": duration_days(milestones.get(MOVE_IN_INDEX), milestones.get(INVOICE_PAID_INDEX)),
        "prospect_to_move_in": duration_days(start, milestones.get(MOVE_IN_INDEX)),
    }


def funnel(reached_counts: list[int]) -> list[dict[str, int | float | None]]:
    """reached_counts aligns with STAGES. Percent is of the prior stage."""
    rows = []
    for index, count in enumerate(reached_counts):
        prior = reached_counts[index - 1] if index else None
        percent = None
        if prior:
            percent = round(100 * count / prior, 1)
        rows.append({"stage": STAGES[index], "count": count, "percent_of_prior": percent})
    return rows


def validate_stall_limits(raw: dict) -> dict[int, int]:
    limits = {}
    for index in range(INVOICE_PAID_INDEX):
        value = raw.get(index, raw.get(str(index)))
        if value is None:
            raise ValueError(f"Missing stall limit for {STAGES[index]}")
        days = int(value)
        if days < 1 or days > 3650:
            raise ValueError(f"Stall limit for {STAGES[index]} must be between 1 and 3650 days")
        limits[index] = days
    return limits


def due_on(today: date, days: int) -> date:
    return today + timedelta(days=days)


def local_today(now: datetime, zone_name: str = "America/Chicago") -> date:
    from zoneinfo import ZoneInfo

    if now.tzinfo is None:
        now = now.replace(tzinfo=ZoneInfo("UTC"))
    return now.astimezone(ZoneInfo(zone_name)).date()
