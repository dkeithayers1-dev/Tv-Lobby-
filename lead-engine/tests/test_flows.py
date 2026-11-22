import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ["LEADPILOT_NO_AUTOAPP"] = "1"
os.environ["AI_PROVIDER"] = "rules"
for var in ("ADMIN_PASSWORD", "TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "SMTP_HOST", "LEAD_API_KEY",
            "TEAM_FORWARD_NUMBER"):
    os.environ.pop(var, None)

import app as app_module  # noqa: E402
import automations as auto  # noqa: E402
import business  # noqa: E402
import db  # noqa: E402

MONDAY_10AM = datetime(2026, 11, 23, 10, 0, 0)


@pytest.fixture
def client(tmp_path):
    flask_app = app_module.create_app({"TESTING": True, "SYNC_TASKS": True,
                                       "DATABASE": str(tmp_path / "test.db")})
    business.freeze(MONDAY_10AM)
    yield flask_app.test_client()
    business.freeze(None)
    db.close()


def messages(lead_id):
    return db.query("SELECT * FROM messages WHERE lead_id = ? ORDER BY id", (lead_id,))


def sms(client, text, phone="+15555550123"):
    return client.post("/webhooks/twilio/message", data={"From": phone, "Body": text})


def lead_by_phone(phone="+15555550123"):
    return auto.find_lead(phone=phone)


# 1. Speed-to-lead --------------------------------------------------------

def test_web_form_lead_gets_instant_reply_with_slots(client):
    r = client.post("/f/contact", data={"name": "Jane Doe", "phone": "555-111-2222",
                                        "service": "AC repair", "timeline": "ASAP"})
    assert r.status_code == 200
    lead = auto.find_lead(phone="+15551112222")
    assert lead["first_response_at"] == lead["created_at"]  # replied in the same second
    assert lead["stage"] == "qualified"
    out = [m for m in messages(lead["id"]) if m["direction"] == "out"]
    assert "Reply with a number" in out[0]["body"]
    assert out[0]["status"] == "simulated"
    assert len(json.loads(lead["offered_slots"])) == 3
    assert db.scalar("SELECT COUNT(*) FROM alerts WHERE kind='new_lead'") == 1


def test_form_requires_contact(client):
    client.post("/f/contact", data={"name": "No Contact"})
    assert db.scalar("SELECT COUNT(*) FROM leads") == 0


def test_api_intake_requires_key(client, monkeypatch):
    assert client.post("/api/v1/leads", json={"phone": "5551234567"}).status_code == 401
    monkeypatch.setenv("LEAD_API_KEY", "secret")
    r = client.post("/api/v1/leads", json={"phone": "5551234567", "name": "Ad Lead"},
                    headers={"X-API-Key": "secret"})
    assert r.status_code == 201


# 4. AI agent: qualify -> offer slots -> book ------------------------------

def test_sms_conversation_qualifies_and_books(client):
    sms(client, "Hi there")
    lead = lead_by_phone()
    assert lead["stage"] == "contacted"
    sms(client, "My name is Sam Lee")
    sms(client, "my furnace is broken")
    sms(client, "asap please")
    lead = lead_by_phone()
    assert lead["name"] == "Sam Lee"
    assert lead["service"] == "Furnace repair"
    assert lead["timeline"] == "ASAP"
    assert lead["stage"] == "qualified"
    assert "Reply with a number" in messages(lead["id"])[-1]["body"]

    sms(client, "2")
    lead = lead_by_phone()
    appt = auto.upcoming_appointment(lead["id"])
    assert appt and lead["stage"] == "booked"
    assert "You're booked" in messages(lead["id"])[-1]["body"]
    # 2. reminders scheduled at 24h and 2h before
    reminders = db.query("SELECT * FROM jobs WHERE appointment_id = ? AND status='pending'", (appt["id"],))
    assert len(reminders) >= 1


def test_faq_answer(client):
    sms(client, "Do you offer financing?")
    body = messages(lead_by_phone()["id"])[-1]["body"]
    assert "0% financing" in body


def test_confirm_and_reschedule_by_text(client):
    sms(client, "I'm Pat, need AC repair this week")
    sms(client, "1")
    lead = lead_by_phone()
    appt = auto.upcoming_appointment(lead["id"])
    sms(client, "C")
    assert db.one("SELECT status FROM appointments WHERE id=?", (appt["id"],))["status"] == "confirmed"
    sms(client, "R")
    assert "new times" in messages(lead["id"])[-1]["body"]
    sms(client, "3")
    moved = db.one("SELECT * FROM appointments WHERE id=?", (appt["id"],))
    assert moved["starts_at"] != appt["starts_at"]
    assert moved["status"] == "scheduled"


def test_handoff_pauses_ai_and_alerts(client):
    sms(client, "I want to speak to a manager about my refund")
    lead = lead_by_phone()
    assert lead["ai_paused"] == 1
    assert db.scalar("SELECT COUNT(*) FROM alerts WHERE kind='handoff'") == 1
    n = len(messages(lead["id"]))
    sms(client, "hello?")
    assert len(messages(lead["id"])) == n + 1  # inbound logged, AI silent


def test_stop_opts_out(client):
    sms(client, "STOP")
    lead = lead_by_phone()
    assert lead["opted_out"] == 1
    auto.enroll(lead, 1)
    assert db.scalar("SELECT COUNT(*) FROM enrollments") == 0


# 7. WhatsApp + missed-call text-back -------------------------------------

def test_whatsapp_routes_to_whatsapp(client):
    client.post("/webhooks/twilio/message", data={"From": "whatsapp:+15555550999", "Body": "what are your hours?",
                                                  "ProfileName": "Maria"})
    lead = auto.find_lead(phone="+15555550999")
    assert lead["name"] == "Maria"
    last = messages(lead["id"])[-1]
    assert last["channel"] == "whatsapp" and "Monday to Friday" in last["body"]


def test_missed_call_text_back_dedupes(client):
    auto.missed_call("+15555550777")
    auto.missed_call("+15555550777")
    lead = auto.find_lead(phone="+15555550777")
    outs = [m for m in messages(lead["id"]) if m["direction"] == "out"]
    assert len(outs) == 1 and "missed your call" in outs[0]["body"]


def test_voice_after_hours_ai_answers_and_books(client):
    r = client.post("/webhooks/twilio/voice", data={"From": "+15555550444"})
    assert b"<Gather" in r.data
    client.post("/webhooks/twilio/voice/turn", data={"From": "+15555550444", "SpeechResult": "this is Alex"})
    client.post("/webhooks/twilio/voice/turn", data={"From": "+15555550444", "SpeechResult": "I need a new system install next week"})
    r = client.post("/webhooks/twilio/voice/turn", data={"From": "+15555550444", "SpeechResult": "option 1"})
    assert b"all set" in r.data
    lead = auto.find_lead(phone="+15555550444")
    assert auto.upcoming_appointment(lead["id"])
    assert any(m["channel"] == "sms" and "You're booked" in m["body"] for m in messages(lead["id"]))


def test_voice_dial_no_answer_triggers_text_back(client):
    r = client.post("/webhooks/twilio/voice/dial-status", data={"From": "+15555550555", "DialCallStatus": "no-answer"})
    assert b"<Gather" in r.data
    lead = auto.find_lead(phone="+15555550555")
    assert db.scalar("SELECT COUNT(*) FROM alerts WHERE kind='missed_call' AND lead_id=?", (lead["id"],)) == 1


def test_twilio_signature_enforced(client, monkeypatch):
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "tok")
    assert sms(client, "hi").status_code == 403


# 5/6. Nurture sequences ----------------------------------------------------

def test_quoted_stage_autostarts_sequence_and_reply_stops_it(client):
    sms(client, "I'm Kim, need a new system install next month")
    lead = lead_by_phone()
    client.post(f"/api/leads/{lead['id']}/stage", json={"stage": "quoted"})
    enr = db.one("SELECT * FROM enrollments WHERE lead_id=?", (lead["id"],))
    assert enr["status"] == "active"

    business.freeze(MONDAY_10AM + timedelta(hours=25))
    auto.run_due_jobs()
    out = messages(lead["id"])[-1]
    assert out["direction"] == "out" and "quote" in out["body"].lower()
    assert db.one("SELECT step_index FROM enrollments WHERE id=?", (enr["id"],))["step_index"] == 1

    sms(client, "yes let's do it")
    enr = db.one("SELECT * FROM enrollments WHERE id=?", (enr["id"],))
    assert enr["status"] == "stopped" and enr["stop_reason"] == "replied"
    assert db.scalar("SELECT COUNT(*) FROM jobs WHERE enrollment_id=? AND status='pending'", (enr["id"],)) == 0
    assert db.scalar("SELECT COUNT(*) FROM alerts WHERE kind='reply'") >= 1


def test_reminder_job_sends(client):
    lead = auto.create_lead({"name": "Rae", "phone": "5550001111"}, "manual")
    auto.book_appointment(lead, MONDAY_10AM + timedelta(days=2))
    business.freeze(MONDAY_10AM + timedelta(days=1, minutes=1))
    auto.run_due_jobs()
    assert "Reminder" in messages(lead["id"])[-1]["body"]


# 3. CRM / pages -----------------------------------------------------------

def test_dashboard_and_pages_render(client):
    client.post("/f/contact", data={"name": "Jo", "phone": "5552223333", "service": "AC repair", "timeline": "ASAP"})
    lead = auto.find_lead(phone="+15552223333")
    for url in ["/", "/pipeline", "/leads", f"/leads/{lead['id']}", "/appointments", "/appointments?show=past",
                "/sequences", "/sequences?edit=1", "/inbox", "/settings", "/simulator", "/chat", "/f/contact",
                f"/book/{business.booking_link(lead).rsplit('/', 1)[-1]}", "/healthz"]:
        r = client.get(url)
        assert r.status_code == 200, url
    stats = auto.dashboard_stats()
    assert stats["new_7d"] == 1 and stats["avg_response_secs"] == 0


def test_overdue_follow_ups(client):
    lead = auto.create_lead({"name": "Quiet", "phone": "5559990000"}, "manual")
    auto.set_stage(lead, "contacted")
    db.update("leads", lead["id"], {"next_follow_up_at": business.ts(MONDAY_10AM - timedelta(hours=1))})
    assert [l["id"] for l in auto.overdue_leads()] == [lead["id"]]


def test_public_booking_and_manage_links(client):
    lead = auto.create_lead({"name": "Web", "email": "w@example.com"}, "manual")
    token = business.booking_link(lead).rsplit("/", 1)[-1]
    slot = business.available_slots(limit=1, spread=False)[0]
    r = client.post(f"/book/{token}", data={"slot": business.ts(slot)})
    assert r.status_code == 302
    appt = auto.upcoming_appointment(lead["id"])
    r = client.post(f"/a/{appt['token']}", data={"action": "cancel"})
    assert r.status_code == 200
    assert db.one("SELECT status FROM appointments WHERE id=?", (appt["id"],))["status"] == "cancelled"
    assert db.scalar("SELECT COUNT(*) FROM jobs WHERE appointment_id=? AND status='pending'", (appt["id"],)) == 0


def test_web_chat_collects_phone(client):
    client.post("/chat/send", json={"text": "hi, my name is Dana"})
    r = client.post("/chat/send", json={"text": "you can reach me at 555-321-7654"})
    rows = r.get_json()
    lead = auto.get_lead(db.scalar("SELECT id FROM leads WHERE source='website_chat'"))
    assert lead["phone"] == "+15553217654"
    assert rows[-1]["direction"] == "out"


def test_slots_respect_hours_and_bookings(client):
    slots = business.available_slots(limit=50, spread=False)
    assert all(9 <= s.hour < 17 and s.weekday() < 5 for s in slots)
    assert slots[0] >= MONDAY_10AM + timedelta(hours=2)
    lead = auto.create_lead({"phone": "5550002222"}, "manual")
    auto.book_appointment(lead, slots[0])
    assert slots[0] not in business.available_slots(limit=50, spread=False)


def test_admin_password(client, monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "pw")
    assert client.get("/").status_code == 302
    client.post("/login", data={"password": "pw"})
    assert client.get("/").status_code == 200


def test_customer_links_are_absolute(client):
    client.post("/f/contact", data={"name": "Lu", "phone": "5554445555", "service": "AC repair", "timeline": "ASAP"},
                base_url="https://leads.example.com")
    lead = auto.find_lead(phone="+15554445555")
    appt = auto.book_appointment(lead, business.available_slots(limit=1)[0])
    assert f"https://leads.example.com/a/{appt['token']}" in messages(lead["id"])[-1]["body"]
