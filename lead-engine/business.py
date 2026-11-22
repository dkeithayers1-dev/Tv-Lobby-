"""Business profile, clock, availability, and seed data."""
import json
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import db

STAGES = [
    ("new", "New inquiry"),
    ("contacted", "Contacted"),
    ("qualified", "Qualified"),
    ("booked", "Call booked"),
    ("quoted", "Quote sent"),
    ("won", "Won"),
    ("lost", "Lost"),
]
STAGE_KEYS = [s[0] for s in STAGES]
OPEN_STAGES = ["new", "contacted", "qualified", "booked", "quoted"]
CHANNELS = ["sms", "whatsapp", "email"]
DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]

DEFAULT_SETTINGS = {
    "business_name": "Acme Home Services",
    "business_description": (
        "Local home-services company offering HVAC repair, installation, and "
        "maintenance plans. Free estimates on new installs."
    ),
    "services": "AC repair, Furnace repair, New system install, Maintenance plan",
    "service_area": "Greater Springfield area",
    "agent_name": "Ava",
    "timezone": "America/New_York",
    "hours": {
        "mon": ["09:00", "17:00"], "tue": ["09:00", "17:00"], "wed": ["09:00", "17:00"],
        "thu": ["09:00", "17:00"], "fri": ["09:00", "17:00"], "sat": None, "sun": None,
    },
    "slot_minutes": 30,
    "min_notice_hours": 2,
    "booking_days_ahead": 10,
    "reminder_offsets_hours": [24, 2],
    "follow_up_after_hours": 48,
    "missed_call_text": (
        "Hi, this is {agent_name} from {business_name}. Sorry we missed your call! "
        "How can we help? Reply here and we'll get you sorted."
    ),
    "public_base_url": "",
}

DEFAULT_FAQS = [
    ("What are your hours?", "We're open Monday to Friday, 9am to 5pm. Messages are answered 24/7.",
     "hours,open,close,closing,weekend,saturday,sunday"),
    ("Do you offer free estimates?", "Yes — estimates for new system installs are free. Repair visits have a $89 diagnostic fee that's credited toward the repair.",
     "estimate,quote,free,cost,price,diagnostic,fee"),
    ("What areas do you serve?", "We serve the Greater Springfield area, including surrounding suburbs within about 30 miles.",
     "area,serve,location,where,city,town,miles"),
    ("Do you offer financing?", "Yes, we offer 0% financing for 12 months on new installs for approved customers.",
     "financing,finance,payment,plan,monthly,credit"),
    ("Are you licensed and insured?", "Yes, we're fully licensed, bonded, and insured.",
     "license,licensed,insured,insurance,bonded"),
]

DEFAULT_SEQUENCES = [
    {
        "name": "Quote follow-up",
        "description": "Checks in after a quote is sent until the prospect replies.",
        "trigger": "quoted",
        "steps": [
            {"delay_hours": 24, "channel": "sms",
             "template": "Hi {first_name}, it's {agent_name} from {business_name}. Just checking you received the quote for {service}. Any questions I can answer?"},
            {"delay_hours": 72, "channel": "email",
             "template": "Hi {first_name},\n\nFollowing up on your {service} quote. If it helps, we offer 0% financing and can usually start within the week.\n\nReply to this message or book a quick call: {booking_link}\n\n— {agent_name}, {business_name}"},
            {"delay_hours": 168, "channel": "whatsapp",
             "template": "Hi {first_name}! Still thinking over the {service} quote? Happy to adjust options to fit your budget — just reply here."},
            {"delay_hours": 336, "channel": "email",
             "template": "Hi {first_name},\n\nI don't want to keep filling your inbox — should I close out your {service} quote for now? Reply anytime and we'll pick it right back up.\n\n— {agent_name}, {business_name}"},
        ],
    },
    {
        "name": "Re-engage unresponsive lead",
        "description": "For new inquiries that went quiet before qualifying.",
        "trigger": None,
        "steps": [
            {"delay_hours": 4, "channel": "sms",
             "template": "Hi {first_name}, {agent_name} here from {business_name} — still need help with {service}? I can get you booked in a couple of minutes."},
            {"delay_hours": 48, "channel": "email",
             "template": "Hi {first_name},\n\nJust making sure your request didn't slip through the cracks. Pick a time that suits you: {booking_link}\n\n— {agent_name}, {business_name}"},
        ],
    },
]


def seed():
    """Insert defaults that aren't present yet (idempotent)."""
    for key, value in DEFAULT_SETTINGS.items():
        if db.one("SELECT 1 FROM settings WHERE key = ?", (key,)) is None:
            db.set_setting(key, value)
    if not db.scalar("SELECT COUNT(*) FROM faqs"):
        for q, a, k in DEFAULT_FAQS:
            db.insert("faqs", {"question": q, "answer": a, "keywords": k})
    if not db.scalar("SELECT COUNT(*) FROM sequences"):
        for s in DEFAULT_SEQUENCES:
            db.insert("sequences", {
                "name": s["name"], "description": s["description"], "trigger": s["trigger"],
                "active": 1, "steps": json.dumps(s["steps"]),
            })


def setting(key):
    return db.get_setting(key, DEFAULT_SETTINGS.get(key))


# ---- clock ----------------------------------------------------------------

FMT = "%Y-%m-%d %H:%M:%S"
_frozen_now = None


def tz():
    try:
        return ZoneInfo(setting("timezone") or "UTC")
    except ZoneInfoNotFoundError:
        return ZoneInfo("UTC")


def now():
    """Current local business time as a naive datetime (overridable in tests)."""
    if _frozen_now is not None:
        return _frozen_now
    return datetime.now(tz()).replace(tzinfo=None, microsecond=0)


def freeze(dt):
    global _frozen_now
    _frozen_now = dt


def ts(dt=None):
    return (dt or now()).strftime(FMT)


def parse_ts(value):
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    for fmt in (FMT, "%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None


def human_time(value):
    dt = parse_ts(value)
    if not dt:
        return ""
    today = now().date()
    if dt.date() == today:
        day = "today"
    elif dt.date() == today + timedelta(days=1):
        day = "tomorrow"
    else:
        day = dt.strftime("%a %b ") + str(dt.day)
    return f"{day} at {dt.strftime('%I:%M %p').lstrip('0')}"


# ---- availability ---------------------------------------------------------

def is_open(at=None):
    at = at or now()
    hours = (setting("hours") or {}).get(DAYS[at.weekday()])
    if not hours:
        return False
    start, end = (datetime.strptime(h, "%H:%M").time() for h in hours)
    return start <= at.time() < end


def available_slots(limit=3, after=None, spread=True):
    """Next free appointment start times within business hours.

    With spread=True we prefer slots on different days / parts of the day so
    the three options offered to a lead are meaningfully different.
    """
    slot = int(setting("slot_minutes") or 30)
    earliest = (after or now()) + timedelta(hours=float(setting("min_notice_hours") or 0))
    horizon = now() + timedelta(days=int(setting("booking_days_ahead") or 10))
    hours = setting("hours") or {}

    taken = set()
    for row in db.query(
        "SELECT starts_at FROM appointments WHERE status IN ('scheduled','confirmed') AND starts_at >= ?",
        (ts(now() - timedelta(days=1)),),
    ):
        taken.add(row["starts_at"][:16])

    candidates = []
    day = earliest.replace(hour=0, minute=0, second=0)
    while day <= horizon and len(candidates) < 200:
        window = hours.get(DAYS[day.weekday()])
        if window:
            start = datetime.combine(day.date(), datetime.strptime(window[0], "%H:%M").time())
            end = datetime.combine(day.date(), datetime.strptime(window[1], "%H:%M").time())
            t = start
            while t + timedelta(minutes=slot) <= end:
                if t >= earliest and ts(t)[:16] not in taken:
                    candidates.append(t)
                t += timedelta(minutes=slot)
        day += timedelta(days=1)

    if not spread:
        return candidates[:limit]

    picked, used = [], set()
    for t in candidates:  # first pass: one per day / half-day
        key = (t.date(), t.hour < 12)
        if key not in used and not any(p.date() == t.date() for p in picked):
            picked.append(t)
            used.add(key)
        if len(picked) == limit:
            return picked
    for t in candidates:
        if t not in picked:
            picked.append(t)
        if len(picked) == limit:
            break
    return sorted(picked)


# ---- templating -----------------------------------------------------------

def first_name(name):
    return (name or "").strip().split(" ")[0] if name and name.strip() else "there"


def render(template, lead=None, **extra):
    lead = lead or {}
    ctx = {
        "business_name": setting("business_name"),
        "agent_name": setting("agent_name"),
        "first_name": first_name(lead.get("name")),
        "name": lead.get("name") or "",
        "service": lead.get("service") or "your project",
        "booking_link": extra.pop("booking_link", "") or booking_link(lead),
    }
    ctx.update(extra)
    return re.sub(r"\{(\w+)\}", lambda m: str(ctx.get(m.group(1), m.group(0))), template)


_detected_base = ""


def remember_host(url):
    """Called on each request so background jobs can build absolute links."""
    global _detected_base
    _detected_base = (url or "").rstrip("/")


def base_url():
    import os
    return (setting("public_base_url") or os.environ.get("PUBLIC_BASE_URL") or _detected_base or "").rstrip("/")


def booking_link(lead):
    if not lead or not lead.get("id"):
        return ""
    import security  # local import to avoid cycle
    return f"{base_url()}/book/{security.sign_lead(lead['id'])}"
