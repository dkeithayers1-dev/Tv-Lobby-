"""Server-side email. A missing SMTP setup raises; it does not pretend to send."""

from __future__ import annotations

import os
import smtplib
from email.message import EmailMessage


class EmailNotConfigured(Exception):
    pass


class ConsentRequired(Exception):
    pass


def smtp_config() -> dict | None:
    host = os.environ.get("SMTP_HOST")
    username = os.environ.get("SMTP_USERNAME")
    password = os.environ.get("SMTP_PASSWORD")
    sender = os.environ.get("MAIL_FROM") or username
    if not all([host, username, password, sender]):
        return None
    return {
        "host": host,
        "port": int(os.environ.get("SMTP_PORT", "587")),
        "use_tls": os.environ.get("SMTP_USE_TLS", "true").lower() != "false",
        "username": username,
        "password": password,
        "mail_from": sender,
    }


def smtp_ready(app) -> bool:
    if app.config.get("MAIL_OUTBOX") is not None:
        return True
    return smtp_config() is not None


def send_message(app, *, to: str, subject: str, body: str, attachment_name: str, attachment: bytes, cc: str = "") -> str:
    message = EmailMessage()
    message["Subject"] = subject
    message["To"] = to
    if cc:
        message["Cc"] = cc
    message.set_content(body)
    message.add_attachment(attachment, maintype="application", subtype="pdf", filename=attachment_name)

    outbox = app.config.get("MAIL_OUTBOX")
    if outbox is not None:
        outbox.append({"to": to, "cc": cc, "subject": subject, "body": body, "filename": attachment_name, "attachment": attachment})
        return "test"

    config = smtp_config()
    if config is None:
        raise EmailNotConfigured(
            "Email is not configured. Set SMTP_HOST, SMTP_USERNAME, SMTP_PASSWORD, and MAIL_FROM."
        )
    message["From"] = config["mail_from"]
    with smtplib.SMTP(config["host"], config["port"], timeout=30) as server:
        if config["use_tls"]:
            server.starttls()
        server.login(config["username"], config["password"])
        server.send_message(message)
    return "smtp"
