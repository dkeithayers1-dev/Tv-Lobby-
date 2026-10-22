"""Encrypted backup and restore. Password hashes travel inside the sealed file
so a restore can sign in again. The file on disk is not readable JSON.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from bestfit.security import Crypto


BACKUP_VERSION = 1

TABLES = (
    "organizations",
    "users",
    "contacts",
    "profiles",
    "advisor_fields",
    "stages",
    "milestones",
    "tasks",
    "tours",
    "communities",
    "billing_contacts",
    "invoices",
    "settings",
    "email_log",
    "geocode_cache",
    "invoice_seq",
)


def export_backup(conn: sqlite3.Connection, crypto: Crypto) -> bytes:
    payload = {"version": BACKUP_VERSION, "exported_at": datetime.now(timezone.utc).isoformat(), "tables": {}}
    for table in TABLES:
        rows = conn.execute(f"SELECT * FROM {table}").fetchall()
        payload["tables"][table] = [dict(row) for row in rows]
    return crypto.seal(json.dumps(payload).encode())


def restore_backup(conn: sqlite3.Connection, crypto: Crypto, blob: bytes) -> dict:
    try:
        payload = json.loads(crypto.open_sealed(blob))
    except json.JSONDecodeError as exc:
        raise ValueError("The backup decrypted but was not valid JSON") from exc
    if payload.get("version") != BACKUP_VERSION:
        raise ValueError("This backup version is not supported")
    tables = payload.get("tables")
    if not isinstance(tables, dict) or "contacts" not in tables or "settings" not in tables:
        raise ValueError("The backup is missing contacts or settings")
    for name in TABLES:
        if name not in tables or not isinstance(tables[name], list):
            raise ValueError(f"The backup is missing {name}")

    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        for name in reversed(TABLES):
            conn.execute(f"DELETE FROM {name}")
        for name in TABLES:
            rows = tables[name]
            if not rows:
                continue
            columns = list(rows[0].keys())
            placeholders = ", ".join("?" for _ in columns)
            quoted = ", ".join(columns)
            conn.executemany(
                f"INSERT INTO {name} ({quoted}) VALUES ({placeholders})",
                [tuple(row[column] for column in columns) for row in rows],
            )
        conn.commit()
    finally:
        conn.execute("PRAGMA foreign_keys = ON")
    return {"contacts": len(tables["contacts"]), "users": len(tables["users"])}
