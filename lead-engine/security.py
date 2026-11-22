"""Signed links and webhook verification."""
import base64
import hashlib
import hmac
import os

from itsdangerous import BadSignature, URLSafeSerializer


def _secret():
    return os.environ.get("FLASK_SECRET_KEY", "dev-only-secret-change-me")


def _serializer():
    return URLSafeSerializer(_secret(), salt="lead-booking")


def sign_lead(lead_id):
    return _serializer().dumps(int(lead_id))


def unsign_lead(token):
    try:
        return int(_serializer().loads(token))
    except (BadSignature, ValueError, TypeError):
        return None


def twilio_signature_valid(url, params, signature):
    """Validate X-Twilio-Signature (HMAC-SHA1 over URL + sorted POST params).

    Returns True when TWILIO_AUTH_TOKEN is unset so local development and the
    in-app simulator keep working; set the token in production.
    """
    token = os.environ.get("TWILIO_AUTH_TOKEN")
    if not token or os.environ.get("TWILIO_SKIP_SIGNATURE") == "1":
        return True
    data = url + "".join(f"{k}{params[k]}" for k in sorted(params))
    digest = hmac.new(token.encode(), data.encode(), hashlib.sha1).digest()
    expected = base64.b64encode(digest).decode()
    return hmac.compare_digest(expected, signature or "")
