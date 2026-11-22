"""Outbound delivery: SMS / WhatsApp via Twilio, email via SMTP.

Every outbound message is written to the `messages` table. When a channel
isn't configured the message is recorded with status "simulated" so the whole
system can be exercised locally without any provider accounts.
"""
import base64
import json
import os
import smtplib
import urllib.error
import urllib.parse
import urllib.request
from email.message import EmailMessage

import business
import db


def twilio_configured():
    return bool(os.environ.get("TWILIO_ACCOUNT_SID") and os.environ.get("TWILIO_AUTH_TOKEN"))


def channel_status():
    return {
        "sms": twilio_configured() and bool(os.environ.get("TWILIO_SMS_FROM")),
        "whatsapp": twilio_configured() and bool(os.environ.get("TWILIO_WHATSAPP_FROM")),
        "voice": twilio_configured(),
        "email": bool(os.environ.get("SMTP_HOST") and os.environ.get("SMTP_FROM")),
    }


class DeliveryError(Exception):
    pass


def _twilio_send(to, body, from_):
    sid = os.environ["TWILIO_ACCOUNT_SID"]
    token = os.environ["TWILIO_AUTH_TOKEN"]
    url = f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json"
    data = urllib.parse.urlencode({"To": to, "From": from_, "Body": body}).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    auth = base64.b64encode(f"{sid}:{token}".encode()).decode()
    req.add_header("Authorization", f"Basic {auth}")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:300]
        raise DeliveryError(f"Twilio {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise DeliveryError(f"Twilio unreachable: {exc.reason}") from exc


def _send_email(to, subject, body):
    msg = EmailMessage()
    msg["From"] = os.environ["SMTP_FROM"]
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    host = os.environ["SMTP_HOST"]
    port = int(os.environ.get("SMTP_PORT", "587"))
    user = os.environ.get("SMTP_USER")
    password = os.environ.get("SMTP_PASSWORD")
    try:
        if port == 465:
            server = smtplib.SMTP_SSL(host, port, timeout=20)
        else:
            server = smtplib.SMTP(host, port, timeout=20)
            server.starttls()
        with server:
            if user:
                server.login(user, password or "")
            server.send_message(msg)
    except (smtplib.SMTPException, OSError) as exc:
        raise DeliveryError(f"SMTP error: {exc}") from exc


def pick_channel(lead, preferred=None):
    """Choose a channel the lead can actually be reached on."""
    order = [preferred, lead.get("preferred_channel"), "sms", "whatsapp", "email"]
    for ch in order:
        if ch in ("sms", "whatsapp") and lead.get("phone"):
            return ch
        if ch == "email" and lead.get("email"):
            return ch
        if ch in ("web", "voice"):
            return ch
    return None


def send(lead, body, channel=None, sender="ai", subject=None):
    """Send a message to a lead and log it. Returns the message row id.

    Never raises for delivery problems — failures are recorded on the message
    so the team can see and retry them from the lead's timeline.
    """
    channel = pick_channel(lead, channel)
    now = business.ts()
    if channel is None:
        return db.insert("messages", {
            "lead_id": lead["id"], "direction": "out", "channel": "none", "sender": sender,
            "body": body, "status": "failed", "error": "Lead has no phone or email", "created_at": now,
        })
    if lead.get("opted_out") and sender != "system":
        return db.insert("messages", {
            "lead_id": lead["id"], "direction": "out", "channel": channel, "sender": sender,
            "body": body, "status": "failed", "error": "Lead opted out (STOP)", "created_at": now,
        })

    status, error = "sent", None
    live = channel_status()
    try:
        if channel in ("web", "voice"):
            status = "sent"  # delivered in-band (chat widget poll / TwiML <Say>)
        elif not live.get(channel):
            status = "simulated"
        elif channel == "sms":
            _twilio_send(lead["phone"], body, os.environ["TWILIO_SMS_FROM"])
        elif channel == "whatsapp":
            _twilio_send(f"whatsapp:{lead['phone']}", body, f"whatsapp:{os.environ['TWILIO_WHATSAPP_FROM']}")
        elif channel == "email":
            _send_email(lead["email"], subject or f"{business.setting('business_name')}", body)
    except DeliveryError as exc:
        status, error = "failed", str(exc)

    msg_id = db.insert("messages", {
        "lead_id": lead["id"], "direction": "out", "channel": channel, "sender": sender,
        "body": body, "status": status, "error": error, "created_at": now,
    })
    updates = {"last_contact_at": now, "updated_at": now}
    if status != "failed" and not lead.get("first_response_at"):
        updates["first_response_at"] = now
        lead["first_response_at"] = now
    db.update("leads", lead["id"], updates)
    return msg_id


def notify_team(subject, body):
    """Best-effort ping to the team (SMS and/or email) for urgent alerts."""
    phone = os.environ.get("TEAM_ALERT_PHONE")
    email = os.environ.get("TEAM_ALERT_EMAIL")
    live = channel_status()
    try:
        if phone and live["sms"]:
            _twilio_send(phone, f"{subject}: {body}"[:1500], os.environ["TWILIO_SMS_FROM"])
        if email and live["email"]:
            _send_email(email, subject, body)
    except DeliveryError:
        pass
