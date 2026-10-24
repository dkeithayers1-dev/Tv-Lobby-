"""Sends text messages through Twilio.

Configured entirely through environment variables (see .env.example) so no
credentials live in code. If Twilio isn't configured, sending raises
SmsNotConfigured instead of crashing the request.
"""
import os
import re


class SmsNotConfigured(Exception):
    pass


def _get_config():
    sid = os.environ.get("TWILIO_ACCOUNT_SID")
    token = os.environ.get("TWILIO_AUTH_TOKEN")
    from_number = os.environ.get("TWILIO_FROM_NUMBER")
    if not all([sid, token, from_number]):
        return None
    return {"sid": sid, "token": token, "from": from_number}


def is_configured() -> bool:
    return _get_config() is not None


def normalize_phone(phone: str, default_country_code: str = "1") -> str:
    """Turn '(480) 555-1234' into '+14805551234' (E.164, which Twilio needs)."""
    phone = (phone or "").strip()
    if phone.startswith("+"):
        return "+" + re.sub(r"\D", "", phone)
    digits = re.sub(r"\D", "", phone)
    if len(digits) == 10:
        return f"+{default_country_code}{digits}"
    if len(digits) == 11 and digits.startswith(default_country_code):
        return f"+{digits}"
    raise ValueError(f"Can't read phone number {phone!r}")


def send_sms(to: str, body: str) -> str:
    """Send one text and return Twilio's message SID."""
    config = _get_config()
    if config is None:
        raise SmsNotConfigured(
            "Twilio is not configured. Set TWILIO_ACCOUNT_SID, "
            "TWILIO_AUTH_TOKEN and TWILIO_FROM_NUMBER in .env."
        )
    from twilio.rest import Client

    client = Client(config["sid"], config["token"])
    message = client.messages.create(to=normalize_phone(to), from_=config["from"], body=body)
    return message.sid
