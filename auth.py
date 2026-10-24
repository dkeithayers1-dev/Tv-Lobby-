"""Administrator login for the schedule.

One admin account, configured through ADMIN_USERNAME / ADMIN_PASSWORD in
.env. Anyone can view the schedule; only a logged-in admin can change it
or text it out. The login is remembered in Flask's signed session cookie.
"""
import hmac
import os
from functools import wraps

from flask import jsonify, session

SESSION_KEY = "is_admin"


def _get_config():
    username = os.environ.get("ADMIN_USERNAME", "admin")
    password = os.environ.get("ADMIN_PASSWORD")
    if not password:
        return None
    return {"username": username, "password": password}


def is_configured() -> bool:
    return _get_config() is not None


def check_credentials(username: str, password: str) -> bool:
    config = _get_config()
    if config is None:
        return False
    user_ok = hmac.compare_digest((username or "").encode(), config["username"].encode())
    pass_ok = hmac.compare_digest((password or "").encode(), config["password"].encode())
    return user_ok and pass_ok


def log_in():
    session.clear()
    session[SESSION_KEY] = True


def log_out():
    session.clear()


def is_admin() -> bool:
    return bool(session.get(SESSION_KEY))


def admin_required(view):
    """For JSON API routes: reject the request unless an admin is logged in."""
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not is_admin():
            return jsonify({"error": "Administrator login required."}), 403
        return view(*args, **kwargs)
    return wrapped
