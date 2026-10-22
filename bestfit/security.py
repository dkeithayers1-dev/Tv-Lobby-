"""Password hashing, field encryption, and CSRF tokens."""

from __future__ import annotations

import hmac
import json
import secrets
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from werkzeug.security import check_password_hash, generate_password_hash


MIN_PASSWORD = 12


class Crypto:
    def __init__(self, key: str):
        try:
            self._fernet = Fernet(key.encode() if isinstance(key, str) else key)
        except (ValueError, TypeError) as exc:
            raise RuntimeError("BESTFIT_DATA_KEY must be a Fernet key") from exc

    def encrypt(self, value: str | None) -> str | None:
        if value is None or value == "":
            return None
        return self._fernet.encrypt(value.encode()).decode()

    def decrypt(self, token: str | None) -> str:
        if not token:
            return ""
        try:
            return self._fernet.decrypt(token.encode()).decode()
        except InvalidToken as exc:
            raise RuntimeError("Could not decrypt data. The data key does not match this database.") from exc

    def encrypt_json(self, payload: dict) -> str:
        return self._fernet.encrypt(json.dumps(payload).encode()).decode()

    def decrypt_json(self, token: str | None) -> dict:
        if not token:
            return {}
        raw = self.decrypt(token)
        data = json.loads(raw or "{}")
        if not isinstance(data, dict):
            raise RuntimeError("Encrypted profile was not an object")
        return data

    def seal(self, payload: bytes) -> bytes:
        return b"BESTFIT1\n" + self._fernet.encrypt(payload)

    def open_sealed(self, blob: bytes) -> bytes:
        if not blob.startswith(b"BESTFIT1\n"):
            raise ValueError("This file is not an encrypted Best Fit backup")
        try:
            return self._fernet.decrypt(blob.split(b"\n", 1)[1])
        except InvalidToken as exc:
            raise ValueError("The backup could not be opened with this server's data key") from exc


def hash_password(password: str) -> str:
    if len(password) < MIN_PASSWORD:
        raise ValueError(f"Password must be at least {MIN_PASSWORD} characters")
    return generate_password_hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    return check_password_hash(password_hash, password)


def new_csrf() -> str:
    return secrets.token_urlsafe(32)


def csrf_matches(expected: str | None, provided: str | None) -> bool:
    if not expected or not provided:
        return False
    return hmac.compare_digest(expected, provided)


def public_user(row: Any) -> dict:
    return {
        "id": row["id"],
        "display_name": row["display_name"],
        "email": row["email"],
        "role": row["role"],
        "active": bool(row["active"]),
    }
