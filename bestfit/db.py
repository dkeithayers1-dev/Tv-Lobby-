"""SQLite storage. Sensitive profile JSON is encrypted by the caller."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from bestfit.domain import DEFAULT_STALL_DAYS
import json


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS organizations (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL,
    display_name TEXT NOT NULL,
    email TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('super_admin', 'advisor', 'finance')),
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    FOREIGN KEY (organization_id) REFERENCES organizations(id)
);

CREATE TABLE IF NOT EXISTS contacts (
    id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL,
    owner_id TEXT,
    name TEXT NOT NULL,
    email TEXT,
    phone TEXT,
    email_norm TEXT,
    phone_norm TEXT,
    last_engagement TEXT,
    email_consent INTEGER NOT NULL DEFAULT 0,
    sms_consent INTEGER NOT NULL DEFAULT 0,
    tags TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    FOREIGN KEY (organization_id) REFERENCES organizations(id),
    FOREIGN KEY (owner_id) REFERENCES users(id)
);

CREATE UNIQUE INDEX IF NOT EXISTS contacts_email_unique
    ON contacts(organization_id, email_norm) WHERE email_norm IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS contacts_phone_unique
    ON contacts(organization_id, phone_norm) WHERE phone_norm IS NOT NULL;

CREATE TABLE IF NOT EXISTS profiles (
    contact_id TEXT PRIMARY KEY,
    data_enc TEXT,
    FOREIGN KEY (contact_id) REFERENCES contacts(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS advisor_fields (
    contact_id TEXT PRIMARY KEY,
    data_enc TEXT,
    FOREIGN KEY (contact_id) REFERENCES contacts(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS stages (
    contact_id TEXT PRIMARY KEY,
    stage_index INTEGER NOT NULL,
    FOREIGN KEY (contact_id) REFERENCES contacts(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS milestones (
    contact_id TEXT NOT NULL,
    stage_index INTEGER NOT NULL,
    milestone_date TEXT NOT NULL,
    PRIMARY KEY (contact_id, stage_index),
    FOREIGN KEY (contact_id) REFERENCES contacts(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    contact_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('intake', 'followup', 'placement')),
    task_key TEXT,
    title TEXT NOT NULL,
    stage_index INTEGER,
    due_on TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('open', 'done')),
    test INTEGER NOT NULL DEFAULT 0,
    auto_closed INTEGER NOT NULL DEFAULT 0,
    completed_at TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (contact_id) REFERENCES contacts(id) ON DELETE CASCADE
);
CREATE UNIQUE INDEX IF NOT EXISTS tasks_key_unique ON tasks(task_key) WHERE task_key IS NOT NULL;

CREATE TABLE IF NOT EXISTS tours (
    id TEXT PRIMARY KEY,
    contact_id TEXT NOT NULL,
    community_id TEXT,
    title TEXT NOT NULL,
    tour_date TEXT NOT NULL,
    tour_time TEXT,
    status TEXT NOT NULL CHECK (status IN ('scheduled', 'completed', 'cancelled')),
    feedback TEXT NOT NULL DEFAULT '',
    FOREIGN KEY (contact_id) REFERENCES contacts(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS communities (
    id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL,
    name TEXT NOT NULL,
    care_types TEXT NOT NULL DEFAULT '',
    address TEXT NOT NULL DEFAULT '',
    city TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL DEFAULT 'TX',
    zip TEXT NOT NULL DEFAULT '',
    license TEXT NOT NULL CHECK (license IN ('yes', 'no', 'unknown')) DEFAULT 'unknown',
    note TEXT NOT NULL DEFAULT '',
    lat REAL,
    lng REAL,
    geo_source TEXT,
    FOREIGN KEY (organization_id) REFERENCES organizations(id)
);

CREATE TABLE IF NOT EXISTS billing_contacts (
    community_id TEXT PRIMARY KEY,
    attention TEXT NOT NULL DEFAULT '',
    street TEXT NOT NULL DEFAULT '',
    city TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL DEFAULT '',
    zip TEXT NOT NULL DEFAULT '',
    billing_email TEXT NOT NULL DEFAULT '',
    cc_email TEXT NOT NULL DEFAULT '',
    FOREIGN KEY (community_id) REFERENCES communities(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS invoices (
    id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL,
    number TEXT NOT NULL UNIQUE,
    contact_id TEXT NOT NULL,
    community_id TEXT NOT NULL,
    resident TEXT NOT NULL,
    attention TEXT NOT NULL DEFAULT '',
    street TEXT NOT NULL DEFAULT '',
    city TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL DEFAULT '',
    zip TEXT NOT NULL DEFAULT '',
    billing_email TEXT NOT NULL DEFAULT '',
    cc_email TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL,
    move_in TEXT,
    amount_cents INTEGER NOT NULL,
    fee_rule TEXT NOT NULL,
    invoice_date TEXT NOT NULL,
    due_on TEXT NOT NULL,
    notes TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL CHECK (status IN ('Unpaid', 'Sent', 'Paid')),
    sent_at TEXT,
    sent_to TEXT,
    send_method TEXT,
    paid_at TEXT,
    payment_reference TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (contact_id) REFERENCES contacts(id),
    FOREIGN KEY (community_id) REFERENCES communities(id)
);

CREATE TABLE IF NOT EXISTS settings (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    business_name TEXT NOT NULL,
    street TEXT NOT NULL,
    city_line TEXT NOT NULL,
    phone TEXT NOT NULL DEFAULT '',
    email TEXT NOT NULL DEFAULT '',
    website TEXT NOT NULL DEFAULT '',
    terms TEXT NOT NULL DEFAULT 'Net 30',
    referral_fee_cents INTEGER,
    retention_days INTEGER,
    stall_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_events (
    id TEXT PRIMARY KEY,
    actor_id TEXT,
    contact_id TEXT,
    event_type TEXT NOT NULL,
    before_json TEXT,
    after_json TEXT,
    occurred_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS email_log (
    id TEXT PRIMARY KEY,
    actor_id TEXT,
    contact_id TEXT,
    recipient TEXT NOT NULL,
    cc TEXT NOT NULL DEFAULT '',
    subject TEXT NOT NULL,
    status TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS geocode_cache (
    query TEXT PRIMARY KEY,
    lat REAL NOT NULL,
    lng REAL NOT NULL,
    cached_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS invoice_seq (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    next_number INTEGER NOT NULL
);
"""


def connect(path: str) -> sqlite3.Connection:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    existing = conn.execute("SELECT id FROM organizations").fetchone()
    if existing is None:
        conn.execute(
            "INSERT INTO organizations (id, name) VALUES (?, ?)",
            ("org-bestfit", "Best Fit Senior Placement"),
        )
        conn.execute(
            """
            INSERT INTO settings (
                id, business_name, street, city_line, phone, email, website, terms,
                referral_fee_cents, retention_days, stall_json
            ) VALUES (1, ?, ?, ?, '', '', '', 'Net 30', NULL, NULL, ?)
            """,
            (
                "Best Fit Senior Placement",
                "4102 Constitution Dr.",
                "Lago Vista, TX 78645",
                json.dumps(DEFAULT_STALL_DAYS),
            ),
        )
        conn.execute("INSERT INTO invoice_seq (id, next_number) VALUES (1, 1)")
    conn.commit()


def next_invoice_number(conn: sqlite3.Connection) -> str:
    row = conn.execute("SELECT next_number FROM invoice_seq WHERE id = 1").fetchone()
    number = int(row["next_number"])
    conn.execute("UPDATE invoice_seq SET next_number = ? WHERE id = 1", (number + 1,))
    return f"INV-{number:07d}"
