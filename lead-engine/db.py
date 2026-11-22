"""SQLite storage for LeadPilot.

All timestamps are stored as naive ISO strings in the business's local time
zone (see clock.now()), which keeps scheduling math and display simple for a
single-location business.
"""
import json
import sqlite3
import threading
from pathlib import Path

_local = threading.local()
_DB_PATH = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS leads (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    name              TEXT,
    phone             TEXT,
    email             TEXT,
    source            TEXT,
    service           TEXT,
    timeline          TEXT,
    budget            TEXT,
    details           TEXT,
    value             REAL DEFAULT 0,
    stage             TEXT NOT NULL DEFAULT 'new',
    score             INTEGER DEFAULT 0,
    preferred_channel TEXT DEFAULT 'sms',
    ai_paused         INTEGER DEFAULT 0,
    offered_slots     TEXT,
    opted_out         INTEGER DEFAULT 0,
    created_at        TEXT NOT NULL,
    first_response_at TEXT,
    last_inbound_at   TEXT,
    last_contact_at   TEXT,
    next_follow_up_at TEXT,
    updated_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_leads_phone ON leads(phone);
CREATE INDEX IF NOT EXISTS idx_leads_email ON leads(email);
CREATE INDEX IF NOT EXISTS idx_leads_stage ON leads(stage);

CREATE TABLE IF NOT EXISTS messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id    INTEGER NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
    direction  TEXT NOT NULL,          -- in | out
    channel    TEXT NOT NULL,          -- sms | whatsapp | email | voice | web
    sender     TEXT NOT NULL,          -- lead | ai | team | system
    body       TEXT NOT NULL,
    status     TEXT NOT NULL,          -- received | sent | simulated | failed
    error      TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_lead ON messages(lead_id, id);

CREATE TABLE IF NOT EXISTS appointments (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id    INTEGER NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
    starts_at  TEXT NOT NULL,
    duration   INTEGER NOT NULL DEFAULT 30,
    status     TEXT NOT NULL DEFAULT 'scheduled', -- scheduled | confirmed | cancelled | completed | no_show
    token      TEXT NOT NULL UNIQUE,
    notes      TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_appt_start ON appointments(starts_at);

CREATE TABLE IF NOT EXISTS jobs (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    kind           TEXT NOT NULL,      -- reminder | sequence_step
    run_at         TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'pending', -- pending | done | failed | cancelled
    lead_id        INTEGER REFERENCES leads(id) ON DELETE CASCADE,
    appointment_id INTEGER REFERENCES appointments(id) ON DELETE CASCADE,
    enrollment_id  INTEGER REFERENCES enrollments(id) ON DELETE CASCADE,
    payload        TEXT,
    error          TEXT,
    done_at        TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_due ON jobs(status, run_at);

CREATE TABLE IF NOT EXISTS sequences (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    description TEXT,
    trigger     TEXT,                  -- stage name that auto-enrolls, or NULL
    active      INTEGER NOT NULL DEFAULT 1,
    steps       TEXT NOT NULL          -- JSON list of {delay_hours, channel, template}
);

CREATE TABLE IF NOT EXISTS enrollments (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id     INTEGER NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
    sequence_id INTEGER NOT NULL REFERENCES sequences(id) ON DELETE CASCADE,
    step_index  INTEGER NOT NULL DEFAULT 0,
    status      TEXT NOT NULL DEFAULT 'active', -- active | completed | stopped
    stop_reason TEXT,
    started_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS faqs (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    question TEXT NOT NULL,
    answer   TEXT NOT NULL,
    keywords TEXT
);

CREATE TABLE IF NOT EXISTS alerts (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id    INTEGER REFERENCES leads(id) ON DELETE CASCADE,
    kind       TEXT NOT NULL,          -- new_lead | reply | handoff | missed_call | booking | cancellation
    body       TEXT NOT NULL,
    created_at TEXT NOT NULL,
    read_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_alerts_unread ON alerts(read_at);

CREATE TABLE IF NOT EXISTS notes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id    INTEGER NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
    body       TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


def init(path):
    """Point the module at a database file and create tables."""
    global _DB_PATH
    _DB_PATH = str(path)
    Path(_DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    close()
    conn = get()
    conn.executescript(SCHEMA)
    conn.commit()


def get():
    conn = getattr(_local, "conn", None)
    if conn is None or getattr(_local, "path", None) != _DB_PATH:
        conn = sqlite3.connect(_DB_PATH, timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        _local.conn = conn
        _local.path = _DB_PATH
    return conn


def close():
    conn = getattr(_local, "conn", None)
    if conn is not None:
        conn.close()
        _local.conn = None


def query(sql, params=()):
    return [dict(r) for r in get().execute(sql, params).fetchall()]


def one(sql, params=()):
    row = get().execute(sql, params).fetchone()
    return dict(row) if row else None


def scalar(sql, params=()):
    row = get().execute(sql, params).fetchone()
    return row[0] if row else None


def execute(sql, params=()):
    conn = get()
    cur = conn.execute(sql, params)
    conn.commit()
    return cur.lastrowid


def insert(table, data):
    cols = ", ".join(data)
    marks = ", ".join("?" for _ in data)
    return execute(f"INSERT INTO {table} ({cols}) VALUES ({marks})", tuple(data.values()))


def update(table, row_id, data):
    if not data:
        return
    sets = ", ".join(f"{k} = ?" for k in data)
    execute(f"UPDATE {table} SET {sets} WHERE id = ?", (*data.values(), row_id))


# ---- settings -------------------------------------------------------------

def get_setting(key, default=None):
    row = one("SELECT value FROM settings WHERE key = ?", (key,))
    if row is None:
        return default
    try:
        return json.loads(row["value"])
    except (TypeError, ValueError):
        return row["value"]


def set_setting(key, value):
    execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, json.dumps(value)),
    )
