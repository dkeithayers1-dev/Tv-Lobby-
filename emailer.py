"""Sends the updated expense report as an email attachment via SMTP.

Configured entirely through environment variables (see .env.example) so no
credentials live in code. If SMTP isn't configured, sending is skipped
rather than crashing the request that just updated the report.
"""
import os
import smtplib
from email.message import EmailMessage
from pathlib import Path


class EmailNotConfigured(Exception):
    pass


def _get_config():
    host = os.environ.get("SMTP_HOST")
    username = os.environ.get("SMTP_USERNAME")
    password = os.environ.get("SMTP_PASSWORD")
    mail_to = os.environ.get("MAIL_TO")
    if not all([host, username, password, mail_to]):
        return None
    return {
        "host": host,
        "port": int(os.environ.get("SMTP_PORT", "587")),
        "use_tls": os.environ.get("SMTP_USE_TLS", "true").lower() != "false",
        "username": username,
        "password": password,
        "mail_from": os.environ.get("MAIL_FROM", username),
        "mail_to": [addr.strip() for addr in mail_to.split(",") if addr.strip()],
    }


def is_configured() -> bool:
    return _get_config() is not None


def send_report_email(attachment_path: Path, subject: str, body: str):
    config = _get_config()
    if config is None:
        raise EmailNotConfigured(
            "SMTP is not configured. Copy .env.example to .env and fill in "
            "SMTP_HOST, SMTP_USERNAME, SMTP_PASSWORD and MAIL_TO."
        )

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = config["mail_from"]
    msg["To"] = ", ".join(config["mail_to"])
    msg.set_content(body)

    attachment_path = Path(attachment_path)
    with open(attachment_path, "rb") as f:
        msg.add_attachment(
            f.read(),
            maintype="application",
            subtype="vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            filename=attachment_path.name,
        )

    with smtplib.SMTP(config["host"], config["port"], timeout=30) as server:
        if config["use_tls"]:
            server.starttls()
        server.login(config["username"], config["password"])
        server.send_message(msg)
