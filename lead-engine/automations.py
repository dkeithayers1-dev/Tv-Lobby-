"""The seven automation systems, wired together.

1. Speed-to-lead       new_lead()            instant reply, collect details, offer slots
2. Appointment reminders book_appointment()  confirmations, reminders, reschedule links
3. CRM intelligence     dashboard_stats()     pipeline, inquiries, overdue follow-ups
4. AI sales agent       handle_inbound()     qualify, answer, book (all channels + phone)
5. Follow-up/nurture    enroll()/run_due_jobs() multi-step SMS/WhatsApp/email sequences
6. WhatsApp Q&A + text-back  handle_inbound() + missed_call()
"""
import json
import logging
import re
import secrets
from datetime import timedelta

import ai
import business
import db
import messaging

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Leads
# ---------------------------------------------------------------------------

def normalize_phone(raw):
    if not raw:
        return None
    raw = str(raw).replace("whatsapp:", "").strip()
    digits = re.sub(r"\D", "", raw)
    if not digits:
        return None
    if raw.startswith("+"):
        return "+" + digits
    if len(digits) == 10:
        return "+1" + digits
    if len(digits) == 11 and digits.startswith("1"):
        return "+" + digits
    return "+" + digits


def get_lead(lead_id):
    return db.one("SELECT * FROM leads WHERE id = ?", (lead_id,))


def find_lead(phone=None, email=None):
    phone = normalize_phone(phone)
    if phone:
        lead = db.one("SELECT * FROM leads WHERE phone = ? ORDER BY id DESC LIMIT 1", (phone,))
        if lead:
            return lead
    if email:
        return db.one("SELECT * FROM leads WHERE lower(email) = lower(?) ORDER BY id DESC LIMIT 1", (email.strip(),))
    return None


def create_lead(data, source):
    now = business.ts()
    record = {
        "name": (data.get("name") or "").strip() or None,
        "phone": normalize_phone(data.get("phone")),
        "email": (data.get("email") or "").strip() or None,
        "service": (data.get("service") or "").strip() or None,
        "timeline": (data.get("timeline") or "").strip() or None,
        "budget": (data.get("budget") or "").strip() or None,
        "details": (data.get("details") or data.get("message") or "").strip() or None,
        "value": float(data.get("value") or 0),
        "source": source,
        "stage": "new",
        "preferred_channel": data.get("preferred_channel") or ("sms" if data.get("phone") else "email"),
        "created_at": now,
        "updated_at": now,
    }
    lead_id = db.insert("leads", record)
    return get_lead(lead_id)


def find_or_create_lead(phone=None, email=None, source="inbound", channel=None):
    lead = find_lead(phone=phone, email=email)
    if lead:
        return lead, False
    data = {"phone": phone, "email": email}
    if channel in ("sms", "whatsapp", "email"):
        data["preferred_channel"] = channel
    lead = create_lead(data, source)
    add_alert(lead["id"], "new_lead", f"New {source.replace('_', ' ')} inquiry from {lead['phone'] or lead['email'] or 'website visitor'}")
    return lead, True


def add_alert(lead_id, kind, body, notify=False):
    db.insert("alerts", {"lead_id": lead_id, "kind": kind, "body": body, "created_at": business.ts()})
    if notify:
        messaging.notify_team(f"[{business.setting('business_name')}] {kind.replace('_', ' ').title()}", body)


def add_note(lead_id, body):
    db.insert("notes", {"lead_id": lead_id, "body": body, "created_at": business.ts()})


def upcoming_appointment(lead_id):
    return db.one(
        "SELECT * FROM appointments WHERE lead_id = ? AND status IN ('scheduled','confirmed') "
        "AND starts_at >= ? ORDER BY starts_at LIMIT 1",
        (lead_id, business.ts()),
    )


def _schedule_follow_up(lead_id, hours=None):
    hours = hours if hours is not None else float(business.setting("follow_up_after_hours") or 48)
    db.update("leads", lead_id, {"next_follow_up_at": business.ts(business.now() + timedelta(hours=hours))})


def _offer_slots(lead):
    """Slots to show this lead: keep previously offered ones if still free."""
    free = {business.ts(t) for t in business.available_slots(limit=50, spread=False)}
    previous = json.loads(lead.get("offered_slots") or "[]")
    kept = [s for s in previous if s in free]
    if len(kept) == len(previous) and kept:
        return [business.parse_ts(s) for s in kept]
    return business.available_slots(limit=3)


def _remember_slots(lead_id, slots):
    db.update("leads", lead_id, {"offered_slots": json.dumps([business.ts(s) for s in slots])})


# ---------------------------------------------------------------------------
# 1. Speed-to-lead
# ---------------------------------------------------------------------------

def new_lead(data, source="web_form"):
    """Create a lead and reply within seconds on their best channel."""
    existing = find_lead(phone=data.get("phone"), email=data.get("email"))
    if existing and existing["stage"] not in ("won", "lost"):
        lead = existing
        updates = {k: v for k, v in {
            "name": data.get("name"), "service": data.get("service"), "timeline": data.get("timeline"),
            "details": data.get("details") or data.get("message"),
        }.items() if v and not lead.get(k)}
        if updates:
            updates["updated_at"] = business.ts()
            db.update("leads", lead["id"], updates)
        add_alert(lead["id"], "new_lead", f"Repeat inquiry via {source.replace('_', ' ')} from {lead['name'] or lead['phone'] or lead['email']}")
        lead = get_lead(lead["id"])
    else:
        lead = create_lead(data, source)
        add_alert(lead["id"], "new_lead",
                  f"New lead via {source.replace('_', ' ')}: {lead['name'] or 'Unknown'}"
                  f"{' — ' + lead['service'] if lead['service'] else ''}", notify=True)

    if data.get("details") or data.get("message"):
        db.insert("messages", {
            "lead_id": lead["id"], "direction": "in", "channel": "web", "sender": "lead",
            "body": (data.get("details") or data.get("message")).strip(), "status": "received",
            "created_at": business.ts(),
        })

    slots = business.available_slots(limit=3)
    body = ai.opening_message(lead, slots)
    messaging.send(lead, body, channel=lead.get("preferred_channel"), sender="ai",
                   subject=f"Thanks for contacting {business.setting('business_name')}")
    if "Reply with a number" in body:
        _remember_slots(lead["id"], slots)
    stage = "qualified" if lead.get("service") and lead.get("timeline") else "contacted"
    if lead["stage"] == "new":
        db.update("leads", lead["id"], {"stage": stage})
    _schedule_follow_up(lead["id"])
    return get_lead(lead["id"])


# ---------------------------------------------------------------------------
# 4/6. Conversational agent for every inbound message
# ---------------------------------------------------------------------------

def handle_inbound(lead, channel, body, reply_channel=None):
    """Process a customer's message and respond. Returns the reply text (or None)."""
    body = (body or "").strip()
    now = business.ts()
    reply_channel = reply_channel or channel
    if not body:
        return None

    db.insert("messages", {
        "lead_id": lead["id"], "direction": "in", "channel": channel, "sender": "lead",
        "body": body, "status": "received", "created_at": now,
    })
    upd = {"last_inbound_at": now, "updated_at": now}
    if channel in ("sms", "whatsapp", "email"):
        upd["preferred_channel"] = channel
    db.update("leads", lead["id"], upd)
    lead = get_lead(lead["id"])

    # A reply always halts nurture sequences and tells the team.
    stopped = stop_enrollments(lead["id"], "replied")
    if stopped or lead["stage"] == "quoted":
        add_alert(lead["id"], "reply",
                  f"{lead['name'] or lead['phone'] or 'A prospect'} replied: “{body[:140]}”", notify=True)

    if lead["opted_out"]:
        if re.fullmatch(r"\s*(start|unstop|yes)\s*", body, re.I):
            db.update("leads", lead["id"], {"opted_out": 0})
            lead = get_lead(lead["id"])
            text = "You're re-subscribed. How can we help?"
            messaging.send(lead, text, channel=reply_channel, sender="system")
            return text
        return None

    if lead["ai_paused"]:
        add_alert(lead["id"], "reply", f"Message for team (AI paused): “{body[:140]}”")
        return None

    slots = _offer_slots(lead)
    appointment = upcoming_appointment(lead["id"])
    history = db.query(
        "SELECT * FROM (SELECT * FROM messages WHERE lead_id = ? ORDER BY id DESC LIMIT 21) ORDER BY id",
        (lead["id"],),
    )[:-1]
    decision = ai.decide(lead, channel, slots, appointment, history, body)

    # Persist anything new we learned.
    learned = {}
    for field in ("name", "email", "phone", "service", "timeline", "budget"):
        val = getattr(decision, field)
        if val and val.strip() and val.strip().lower() not in ("null", "none", "unknown"):
            if field == "phone":
                if lead.get("phone"):
                    continue
                val = normalize_phone(val)
                if not val:
                    continue
            if field in ("name", "email") and lead.get(field) and channel != "web":
                continue  # don't overwrite contact details we already trust
            learned[field] = val.strip()
    if decision.details:
        learned["details"] = ((lead.get("details") or "") + ("\n" if lead.get("details") else "") + decision.details.strip())[:2000]
    learned["score"] = decision.lead_score
    if learned:
        learned["updated_at"] = business.ts()
        db.update("leads", lead["id"], learned)
        lead = get_lead(lead["id"])
    if lead["stage"] in ("new", "contacted") and lead.get("service") and lead.get("timeline"):
        set_stage(lead, "qualified")
        lead = get_lead(lead["id"])
    elif lead["stage"] == "new":
        db.update("leads", lead["id"], {"stage": "contacted"})

    intent = decision.intent
    reply = decision.reply.strip()

    if intent == "stop":
        stop_enrollments(lead["id"], "opted out")
        messaging.send(lead, reply, channel=reply_channel, sender="system")
        db.update("leads", lead["id"], {"opted_out": 1, "next_follow_up_at": None})
        return reply

    if decision.handoff or intent == "human":
        db.update("leads", lead["id"], {"ai_paused": 1, "next_follow_up_at": business.ts()})
        add_alert(lead["id"], "handoff",
                  f"Handoff needed for {lead['name'] or lead['phone'] or 'lead'}: "
                  f"{decision.handoff_reason or 'complex request'} — “{body[:140]}”", notify=True)
        messaging.send(lead, reply, channel=reply_channel, sender="ai")
        return reply

    if intent == "pick_slot" and decision.slot_choice:
        start = slots[decision.slot_choice - 1]
        # On a phone call the written confirmation (with link) goes out by SMS.
        confirm_channel = "sms" if reply_channel == "voice" else reply_channel
        if appointment:
            appt = reschedule_appointment(appointment, start, channel=confirm_channel)
        else:
            appt = book_appointment(lead, start, channel=confirm_channel)
        if reply_channel == "voice":
            spoken = (f"You're all set for {business.human_time(appt['starts_at'])}. "
                      f"I've texted you a confirmation. Is there anything else I can help with?")
            messaging.send(get_lead(lead["id"]), spoken, channel="voice", sender="ai")
            return spoken
        return _confirmation_text(appt)

    if intent == "confirm_appointment" and appointment:
        db.update("appointments", appointment["id"], {"status": "confirmed", "updated_at": business.ts()})
    elif intent == "cancel" and appointment:
        cancel_appointment(appointment, by="customer", notify_customer=False)

    messaging.send(lead, reply, channel=reply_channel, sender="ai")
    _remember_slots(lead["id"], slots)
    lead = get_lead(lead["id"])
    if lead["stage"] in ("new", "contacted", "qualified", "quoted"):
        _schedule_follow_up(lead["id"])
    return reply


def missed_call(phone, source="missed_call"):
    """Text back a caller we couldn't answer (deduplicated for 10 minutes)."""
    lead, created = find_or_create_lead(phone=phone, source=source, channel="sms")
    recent = db.scalar(
        "SELECT COUNT(*) FROM alerts WHERE lead_id = ? AND kind = 'missed_call' AND created_at >= ?",
        (lead["id"], business.ts(business.now() - timedelta(minutes=10))),
    )
    if recent:
        return lead
    add_alert(lead["id"], "missed_call", f"Missed call from {lead['name'] or lead['phone']} — auto text-back sent")
    text = business.render(business.setting("missed_call_text"), lead)
    messaging.send(lead, text, channel="sms", sender="ai")
    if lead["stage"] == "new":
        db.update("leads", lead["id"], {"stage": "contacted"})
    _schedule_follow_up(lead["id"], hours=4)
    return get_lead(lead["id"])


# ---------------------------------------------------------------------------
# 2. Appointments + reminders
# ---------------------------------------------------------------------------

def _confirmation_text(appt):
    link = f"{business.base_url()}/a/{appt['token']}"
    return (f"You're booked with {business.setting('business_name')} for "
            f"{business.human_time(appt['starts_at'])}. Reply C to confirm or R to reschedule, "
            f"or manage it here: {link}")


def book_appointment(lead, start, channel=None, duration=None, notes=None, notify_customer=True):
    now = business.ts()
    appt_id = db.insert("appointments", {
        "lead_id": lead["id"], "starts_at": business.ts(start),
        "duration": duration or int(business.setting("slot_minutes") or 30),
        "status": "scheduled", "token": secrets.token_urlsafe(16), "notes": notes,
        "created_at": now, "updated_at": now,
    })
    appt = db.one("SELECT * FROM appointments WHERE id = ?", (appt_id,))
    db.update("leads", lead["id"], {"offered_slots": None, "next_follow_up_at": None})
    if lead["stage"] in ("new", "contacted", "qualified"):
        set_stage(lead, "booked")
    stop_enrollments(lead["id"], "booked")
    schedule_reminders(appt)
    add_alert(lead["id"], "booking", f"{lead['name'] or lead['phone'] or 'Lead'} booked for {business.human_time(appt['starts_at'])}")
    if notify_customer:
        messaging.send(get_lead(lead["id"]), _confirmation_text(appt), channel=channel, sender="ai",
                       subject="Your appointment is booked")
    return appt


def reschedule_appointment(appt, start, channel=None, notify_customer=True):
    db.update("appointments", appt["id"], {"starts_at": business.ts(start), "status": "scheduled",
                                           "updated_at": business.ts()})
    appt = db.one("SELECT * FROM appointments WHERE id = ?", (appt["id"],))
    lead = get_lead(appt["lead_id"])
    db.update("leads", lead["id"], {"offered_slots": None})
    schedule_reminders(appt)
    add_alert(lead["id"], "booking", f"{lead['name'] or lead['phone'] or 'Lead'} rescheduled to {business.human_time(appt['starts_at'])}")
    if notify_customer:
        messaging.send(lead, _confirmation_text(appt), channel=channel, sender="ai",
                       subject="Your appointment was rescheduled")
    return appt


def cancel_appointment(appt, by="team", notify_customer=True):
    db.update("appointments", appt["id"], {"status": "cancelled", "updated_at": business.ts()})
    db.execute("UPDATE jobs SET status = 'cancelled' WHERE appointment_id = ? AND status = 'pending'", (appt["id"],))
    lead = get_lead(appt["lead_id"])
    if lead["stage"] == "booked":
        set_stage(lead, "qualified")
    _schedule_follow_up(lead["id"], hours=24)
    add_alert(lead["id"], "cancellation",
              f"{lead['name'] or lead['phone'] or 'Lead'} cancelled ({by}) the {business.human_time(appt['starts_at'])} appointment",
              notify=(by == "customer"))
    if notify_customer:
        messaging.send(lead, f"Your appointment for {business.human_time(appt['starts_at'])} has been cancelled. "
                             f"Reply anytime to rebook.", sender="system", subject="Appointment cancelled")


def schedule_reminders(appt):
    db.execute("UPDATE jobs SET status = 'cancelled' WHERE appointment_id = ? AND kind = 'reminder' AND status = 'pending'",
               (appt["id"],))
    start = business.parse_ts(appt["starts_at"])
    for hours in business.setting("reminder_offsets_hours") or []:
        run_at = start - timedelta(hours=float(hours))
        if run_at > business.now():
            db.insert("jobs", {"kind": "reminder", "run_at": business.ts(run_at), "lead_id": appt["lead_id"],
                               "appointment_id": appt["id"], "payload": json.dumps({"hours": hours})})


def _send_reminder(job):
    appt = db.one("SELECT * FROM appointments WHERE id = ?", (job["appointment_id"],))
    if not appt or appt["status"] not in ("scheduled", "confirmed"):
        return "skipped: appointment not active"
    lead = get_lead(appt["lead_id"])
    link = f"{business.base_url()}/a/{appt['token']}"
    if appt["status"] == "confirmed":
        text = (f"Reminder: your appointment with {business.setting('business_name')} is "
                f"{business.human_time(appt['starts_at'])}. Need to change it? {link}")
    else:
        text = (f"Reminder: you're booked with {business.setting('business_name')} "
                f"{business.human_time(appt['starts_at'])}. Reply C to confirm, R to reschedule, or manage here: {link}")
    messaging.send(lead, text, sender="system", subject="Appointment reminder")
    return None


# ---------------------------------------------------------------------------
# 3. Pipeline / CRM
# ---------------------------------------------------------------------------

def set_stage(lead, stage):
    if stage not in business.STAGE_KEYS or lead["stage"] == stage:
        return
    updates = {"stage": stage, "updated_at": business.ts()}
    if stage in ("won", "lost"):
        updates["next_follow_up_at"] = None
        stop_enrollments(lead["id"], f"marked {stage}")
    db.update("leads", lead["id"], updates)
    for seq in db.query("SELECT * FROM sequences WHERE active = 1 AND trigger = ?", (stage,)):
        enroll(get_lead(lead["id"]), seq["id"])


def dashboard_stats():
    now = business.now()
    week_ago = business.ts(now - timedelta(days=7))
    resp = db.query(
        "SELECT created_at, first_response_at FROM leads WHERE first_response_at IS NOT NULL AND created_at >= ?",
        (business.ts(now - timedelta(days=30)),),
    )
    secs = [(business.parse_ts(r["first_response_at"]) - business.parse_ts(r["created_at"])).total_seconds() for r in resp]
    secs = [max(0, s) for s in secs]
    past = db.query(
        "SELECT status FROM appointments WHERE starts_at < ? AND starts_at >= ? AND status IN ('completed','no_show','confirmed','scheduled')",
        (business.ts(now), business.ts(now - timedelta(days=30))),
    )
    showed = sum(1 for a in past if a["status"] == "completed")
    no_show = sum(1 for a in past if a["status"] == "no_show")
    open_value = db.scalar(
        f"SELECT COALESCE(SUM(value),0) FROM leads WHERE stage IN ({','.join('?' * len(business.OPEN_STAGES))})",
        business.OPEN_STAGES,
    )
    return {
        "new_7d": db.scalar("SELECT COUNT(*) FROM leads WHERE created_at >= ?", (week_ago,)),
        "avg_response_secs": (sum(secs) / len(secs)) if secs else None,
        "upcoming": db.scalar("SELECT COUNT(*) FROM appointments WHERE status IN ('scheduled','confirmed') AND starts_at >= ?", (business.ts(now),)),
        "show_rate": (showed / (showed + no_show) * 100) if (showed + no_show) else None,
        "open_value": open_value or 0,
        "won_7d": db.scalar("SELECT COUNT(*) FROM leads WHERE stage = 'won' AND updated_at >= ?", (week_ago,)),
        "active_enrollments": db.scalar("SELECT COUNT(*) FROM enrollments WHERE status = 'active'"),
        "unread_alerts": db.scalar("SELECT COUNT(*) FROM alerts WHERE read_at IS NULL"),
    }


def overdue_leads(limit=50):
    now = business.ts()
    return db.query(
        f"""SELECT * FROM leads
            WHERE stage IN ({','.join('?' * len(business.OPEN_STAGES))}) AND opted_out = 0
              AND ((next_follow_up_at IS NOT NULL AND next_follow_up_at <= ?)
                   OR (stage = 'new' AND first_response_at IS NULL))
            ORDER BY COALESCE(next_follow_up_at, created_at) LIMIT ?""",
        (*business.OPEN_STAGES, now, limit),
    )


# ---------------------------------------------------------------------------
# 5. Follow-up + nurture sequences
# ---------------------------------------------------------------------------

def enroll(lead, sequence_id):
    seq = db.one("SELECT * FROM sequences WHERE id = ?", (sequence_id,))
    if not seq or lead.get("opted_out"):
        return None
    if db.one("SELECT 1 FROM enrollments WHERE lead_id = ? AND sequence_id = ? AND status = 'active'",
              (lead["id"], sequence_id)):
        return None
    steps = json.loads(seq["steps"] or "[]")
    if not steps:
        return None
    now = business.ts()
    enr_id = db.insert("enrollments", {"lead_id": lead["id"], "sequence_id": sequence_id, "step_index": 0,
                                       "status": "active", "started_at": now, "updated_at": now})
    _schedule_step(enr_id, lead["id"], steps[0])
    return enr_id


def _schedule_step(enrollment_id, lead_id, step):
    run_at = business.now() + timedelta(hours=float(step.get("delay_hours") or 0))
    db.insert("jobs", {"kind": "sequence_step", "run_at": business.ts(run_at), "lead_id": lead_id,
                       "enrollment_id": enrollment_id})
    db.update("leads", lead_id, {"next_follow_up_at": business.ts(run_at)})


def stop_enrollments(lead_id, reason):
    active = db.query("SELECT id FROM enrollments WHERE lead_id = ? AND status = 'active'", (lead_id,))
    for e in active:
        db.update("enrollments", e["id"], {"status": "stopped", "stop_reason": reason, "updated_at": business.ts()})
        db.execute("UPDATE jobs SET status = 'cancelled' WHERE enrollment_id = ? AND status = 'pending'", (e["id"],))
    return len(active)


def _run_sequence_step(job):
    enr = db.one("SELECT * FROM enrollments WHERE id = ?", (job["enrollment_id"],))
    if not enr or enr["status"] != "active":
        return "skipped: enrollment not active"
    seq = db.one("SELECT * FROM sequences WHERE id = ?", (enr["sequence_id"],))
    steps = json.loads(seq["steps"] or "[]")
    lead = get_lead(enr["lead_id"])
    if enr["step_index"] >= len(steps) or lead["opted_out"]:
        db.update("enrollments", enr["id"], {"status": "completed", "updated_at": business.ts()})
        return "skipped"
    step = steps[enr["step_index"]]
    body = business.render(step["template"], lead)
    messaging.send(lead, body, channel=step.get("channel"), sender="ai",
                   subject=f"Following up — {business.setting('business_name')}")
    nxt = enr["step_index"] + 1
    if nxt < len(steps):
        db.update("enrollments", enr["id"], {"step_index": nxt, "updated_at": business.ts()})
        _schedule_step(enr["id"], lead["id"], steps[nxt])
    else:
        db.update("enrollments", enr["id"], {"step_index": nxt, "status": "completed", "updated_at": business.ts()})
        _schedule_follow_up(lead["id"], hours=72)
    return None


# ---------------------------------------------------------------------------
# Job runner (called by the background scheduler)
# ---------------------------------------------------------------------------

HANDLERS = {"reminder": _send_reminder, "sequence_step": _run_sequence_step}


def run_due_jobs(limit=100):
    jobs = db.query("SELECT * FROM jobs WHERE status = 'pending' AND run_at <= ? ORDER BY run_at LIMIT ?",
                    (business.ts(), limit))
    for job in jobs:
        # claim it so a second worker can't run it too
        claimed = db.get().execute("UPDATE jobs SET status = 'running' WHERE id = ? AND status = 'pending'", (job["id"],))
        db.get().commit()
        if claimed.rowcount != 1:
            continue
        try:
            note = HANDLERS[job["kind"]](job)
            db.update("jobs", job["id"], {"status": "done", "error": note, "done_at": business.ts()})
        except Exception as exc:  # keep the scheduler alive no matter what
            log.exception("Job %s failed", job["id"])
            db.update("jobs", job["id"], {"status": "failed", "error": str(exc)[:500], "done_at": business.ts()})
    return len(jobs)
