"""Placement operations. Routes call these so every path follows the same rules."""

from __future__ import annotations

import csv
import io
import sqlite3
import uuid
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from bestfit.domain import (
    INVOICE_PAID_INDEX,
    MOVE_IN_INDEX,
    STAGES,
    engagement_status,
    is_stalled,
    parse_iso,
    placement_task_for,
    task_key,
    validate_stall_limits,
)
from bestfit.security import Crypto, hash_password


class DuplicateContact(Exception):
    def __init__(self, contact_id: str, name: str, field: str):
        self.contact_id = contact_id
        self.name = name
        self.field = field
        super().__init__(f"A contact already uses this {field}: {name}")


class NeedsBackwardConfirm(Exception):
    pass


class RuleError(Exception):
    pass


def new_id() -> str:
    return uuid.uuid4().hex


def clock(today: date | None = None) -> datetime:
    if today is not None:
        return datetime(today.year, today.month, today.day, 12, 0, tzinfo=ZoneInfo("America/Chicago"))
    return datetime.now(ZoneInfo("America/Chicago"))


def audit(conn, actor_id: str | None, event_type: str, contact_id: str | None, before, after, today: date | None) -> None:
    conn.execute(
        """
        INSERT INTO audit_events (id, actor_id, contact_id, event_type, before_json, after_json, occurred_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            new_id(),
            actor_id,
            contact_id,
            event_type,
            None if before is None else __import__("json").dumps(before),
            None if after is None else __import__("json").dumps(after),
            clock(today).isoformat(),
        ),
    )


def find_duplicate(conn, organization_id: str, email_norm: str | None, phone_norm: str | None, except_id: str | None = None):
    if email_norm:
        row = conn.execute(
            "SELECT id, name FROM contacts WHERE organization_id = ? AND email_norm = ? AND id != ?",
            (organization_id, email_norm, except_id or ""),
        ).fetchone()
        if row:
            return row, "email"
    if phone_norm:
        row = conn.execute(
            "SELECT id, name FROM contacts WHERE organization_id = ? AND phone_norm = ? AND id != ?",
            (organization_id, phone_norm, except_id or ""),
        ).fetchone()
        if row:
            return row, "phone"
    return None, None


def create_contact(conn, crypto: Crypto, actor: dict, fields: dict, today: date) -> str:
    from bestfit.domain import normalize_email, normalize_phone

    email_norm = normalize_email(fields.get("email"))
    phone_norm = normalize_phone(fields.get("phone"))
    existing, field = find_duplicate(conn, actor["organization_id"], email_norm, phone_norm)
    if existing:
        raise DuplicateContact(existing["id"], existing["name"], field)
    contact_id = new_id()
    owner_id = fields.get("owner_id") or actor["id"]
    conn.execute(
        """
        INSERT INTO contacts (
            id, organization_id, owner_id, name, email, phone, email_norm, phone_norm,
            last_engagement, email_consent, sms_consent, tags, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            contact_id,
            actor["organization_id"],
            owner_id,
            fields["name"].strip(),
            (fields.get("email") or "").strip() or None,
            (fields.get("phone") or "").strip() or None,
            email_norm,
            phone_norm,
            today.isoformat(),
            1 if fields.get("email_consent") else 0,
            1 if fields.get("sms_consent") else 0,
            (fields.get("tags") or "").strip(),
            clock(today).isoformat(),
        ),
    )
    conn.execute(
        "INSERT INTO profiles (contact_id, data_enc) VALUES (?, ?)",
        (contact_id, crypto.encrypt_json({})),
    )
    conn.execute(
        "INSERT INTO advisor_fields (contact_id, data_enc) VALUES (?, ?)",
        (contact_id, crypto.encrypt_json({})),
    )
    conn.execute("INSERT INTO stages (contact_id, stage_index) VALUES (?, 0)", (contact_id,))
    conn.execute(
        "INSERT INTO milestones (contact_id, stage_index, milestone_date) VALUES (?, 0, ?)",
        (contact_id, today.isoformat()),
    )
    conn.execute(
        """
        INSERT INTO tasks (
            id, contact_id, kind, task_key, title, stage_index, due_on, status, test, created_at
        ) VALUES (?, ?, 'intake', NULL, ?, NULL, ?, 'open', 0, ?)
        """,
        (new_id(), contact_id, "Send prospect intake form", today.isoformat(), clock(today).isoformat()),
    )
    audit(conn, actor["id"], "contact.create", contact_id, None, {"name": fields["name"].strip()}, today)
    return contact_id


def update_contact(conn, actor, contact_id: str, fields: dict, today: date) -> None:
    from bestfit.domain import normalize_email, normalize_phone

    email_norm = normalize_email(fields.get("email"))
    phone_norm = normalize_phone(fields.get("phone"))
    existing, field = find_duplicate(conn, actor["organization_id"], email_norm, phone_norm, contact_id)
    if existing:
        raise DuplicateContact(existing["id"], existing["name"], field)
    last = fields.get("last_engagement") or None
    conn.execute(
        """
        UPDATE contacts
        SET name = ?, email = ?, phone = ?, email_norm = ?, phone_norm = ?,
            last_engagement = ?, email_consent = ?, sms_consent = ?, tags = ?, owner_id = ?
        WHERE id = ?
        """,
        (
            fields["name"].strip(),
            (fields.get("email") or "").strip() or None,
            (fields.get("phone") or "").strip() or None,
            email_norm,
            phone_norm,
            last,
            1 if fields.get("email_consent") else 0,
            1 if fields.get("sms_consent") else 0,
            (fields.get("tags") or "").strip(),
            fields.get("owner_id"),
            contact_id,
        ),
    )
    audit(conn, actor["id"], "contact.update", contact_id, None, {"name": fields["name"].strip()}, today)


def save_profile(conn, crypto: Crypto, actor_id: str, contact_id: str, data: dict, today: date) -> None:
    current = crypto.decrypt_json(
        conn.execute("SELECT data_enc FROM profiles WHERE contact_id = ?", (contact_id,)).fetchone()["data_enc"]
    )
    current.update(data)
    conn.execute("UPDATE profiles SET data_enc = ? WHERE contact_id = ?", (crypto.encrypt_json(current), contact_id))
    audit(conn, actor_id, "profile.update", contact_id, None, {"fields": sorted(data.keys())}, today)


def save_advisor(conn, crypto: Crypto, actor_id: str, contact_id: str, data: dict, today: date) -> None:
    current = crypto.decrypt_json(
        conn.execute("SELECT data_enc FROM advisor_fields WHERE contact_id = ?", (contact_id,)).fetchone()["data_enc"]
    )
    current.update(data)
    current["updated_at"] = clock(today).isoformat()
    conn.execute(
        "UPDATE advisor_fields SET data_enc = ? WHERE contact_id = ?",
        (crypto.encrypt_json(current), contact_id),
    )
    audit(conn, actor_id, "advisor.update", contact_id, None, {"fields": sorted(k for k in data)}, today)


def load_profile(conn, crypto: Crypto, contact_id: str) -> dict:
    row = conn.execute("SELECT data_enc FROM profiles WHERE contact_id = ?", (contact_id,)).fetchone()
    return crypto.decrypt_json(row["data_enc"] if row else None)


def load_advisor(conn, crypto: Crypto, contact_id: str) -> dict:
    row = conn.execute("SELECT data_enc FROM advisor_fields WHERE contact_id = ?", (contact_id,)).fetchone()
    return crypto.decrypt_json(row["data_enc"] if row else None)


def milestone_map(conn, contact_id: str) -> dict[int, date]:
    rows = conn.execute(
        "SELECT stage_index, milestone_date FROM milestones WHERE contact_id = ?",
        (contact_id,),
    ).fetchall()
    found = {}
    for row in rows:
        parsed = parse_iso(row["milestone_date"])
        if parsed:
            found[int(row["stage_index"])] = parsed
    return found


def change_stage(conn, actor_id: str, contact_id: str, new_index: int, today: date, confirm_backward: bool) -> dict:
    if new_index < 0 or new_index > INVOICE_PAID_INDEX:
        raise RuleError("Unknown stage")
    current = conn.execute("SELECT stage_index FROM stages WHERE contact_id = ?", (contact_id,)).fetchone()
    old = int(current["stage_index"])
    if new_index == old:
        return {"changed": False, "forward": False, "move_in": False, "stage": old}
    if new_index < old and not confirm_backward:
        raise NeedsBackwardConfirm()
    existing = conn.execute(
        "SELECT milestone_date FROM milestones WHERE contact_id = ? AND stage_index = ?",
        (contact_id, new_index),
    ).fetchone()
    if existing is None:
        conn.execute(
            "INSERT INTO milestones (contact_id, stage_index, milestone_date) VALUES (?, ?, ?)",
            (contact_id, new_index, today.isoformat()),
        )
    conn.execute("UPDATE stages SET stage_index = ? WHERE contact_id = ?", (new_index, contact_id))
    conn.execute(
        """
        UPDATE tasks
        SET status = 'done', auto_closed = 1, completed_at = ?
        WHERE contact_id = ? AND kind = 'placement' AND status = 'open' AND stage_index < ?
        """,
        (clock(today).isoformat(), contact_id, new_index),
    )
    spec = placement_task_for(new_index)
    if spec:
        title, days = spec
        key = task_key(contact_id, new_index)
        found = conn.execute("SELECT id FROM tasks WHERE task_key = ?", (key,)).fetchone()
        if found is None:
            conn.execute(
                """
                INSERT INTO tasks (
                    id, contact_id, kind, task_key, title, stage_index, due_on, status, test, created_at
                ) VALUES (?, ?, 'placement', ?, ?, ?, ?, 'open', 0, ?)
                """,
                (
                    new_id(),
                    contact_id,
                    key,
                    title,
                    new_index,
                    (today + timedelta(days=days)).isoformat(),
                    clock(today).isoformat(),
                ),
            )
    audit(
        conn,
        actor_id,
        "stage.change",
        contact_id,
        {"stage": old, "name": STAGES[old]},
        {"stage": new_index, "name": STAGES[new_index]},
        today,
    )
    return {
        "changed": True,
        "forward": new_index > old,
        "move_in": new_index == MOVE_IN_INDEX and new_index > old,
        "stage": new_index,
    }


def set_milestone(conn, actor_id: str, contact_id: str, stage_index: int, milestone: date | None, today: date) -> None:
    if milestone is None:
        conn.execute(
            "DELETE FROM milestones WHERE contact_id = ? AND stage_index = ?",
            (contact_id, stage_index),
        )
    else:
        conn.execute(
            """
            INSERT INTO milestones (contact_id, stage_index, milestone_date) VALUES (?, ?, ?)
            ON CONFLICT(contact_id, stage_index) DO UPDATE SET milestone_date = excluded.milestone_date
            """,
            (contact_id, stage_index, milestone.isoformat()),
        )
    audit(conn, actor_id, "milestone.update", contact_id, None, {"stage": stage_index}, today)


def complete_task(conn, actor_id: str, task_id: str, today: date) -> None:
    task = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if task is None or task["status"] == "done":
        return
    conn.execute(
        "UPDATE tasks SET status = 'done', completed_at = ? WHERE id = ?",
        (clock(today).isoformat(), task_id),
    )
    audit(conn, actor_id, "task.done", task["contact_id"], None, {"title": task["title"]}, today)


def mark_intake_sent(conn, actor_id: str, contact_id: str, today: date, test: bool) -> None:
    if test:
        conn.execute(
            """
            INSERT INTO tasks (
                id, contact_id, kind, task_key, title, stage_index, due_on, status, test, completed_at, created_at
            ) VALUES (?, ?, 'intake', NULL, 'TEST intake email', NULL, ?, 'done', 1, ?, ?)
            """,
            (new_id(), contact_id, today.isoformat(), clock(today).isoformat(), clock(today).isoformat()),
        )
        audit(conn, actor_id, "intake.test", contact_id, None, {"test": True}, today)
        return
    conn.execute(
        """
        UPDATE tasks SET status = 'done', completed_at = ?
        WHERE contact_id = ? AND kind = 'intake' AND status = 'open' AND test = 0
        """,
        (clock(today).isoformat(), contact_id),
    )
    follow = conn.execute(
        "SELECT id FROM tasks WHERE contact_id = ? AND kind = 'followup'",
        (contact_id,),
    ).fetchone()
    if follow is None:
        conn.execute(
            """
            INSERT INTO tasks (
                id, contact_id, kind, task_key, title, stage_index, due_on, status, test, created_at
            ) VALUES (?, ?, 'followup', NULL, ?, NULL, ?, 'open', 0, ?)
            """,
            (
                new_id(),
                contact_id,
                "Follow up on the intake form",
                (today + timedelta(days=1)).isoformat(),
                clock(today).isoformat(),
            ),
        )
    audit(conn, actor_id, "intake.sent", contact_id, None, {"test": False}, today)


def contact_status(contact, today: date) -> str:
    anchor = parse_iso(contact["last_engagement"]) or parse_iso(contact["created_at"])
    if anchor is None:
        return "Active"
    return engagement_status(anchor, today)


def stalled_for(conn, contact_id: str, stage_index: int, today: date, limits: dict[int, int]) -> tuple[int | None, bool]:
    row = conn.execute(
        "SELECT milestone_date FROM milestones WHERE contact_id = ? AND stage_index = ?",
        (contact_id, stage_index),
    ).fetchone()
    milestone = parse_iso(row["milestone_date"]) if row else None
    if milestone is None and stage_index == 0:
        created = conn.execute("SELECT created_at FROM contacts WHERE id = ?", (contact_id,)).fetchone()
        milestone = parse_iso(created["created_at"]) if created else None
    from bestfit.domain import days_in_stage

    days = days_in_stage(milestone, today)
    return days, is_stalled(stage_index, days, limits)


def load_stall_limits(conn) -> dict[int, int]:
    raw = conn.execute("SELECT stall_json FROM settings WHERE id = 1").fetchone()["stall_json"]
    return validate_stall_limits(__import__("json").loads(raw))


def settings_row(conn) -> sqlite3.Row:
    return conn.execute("SELECT * FROM settings WHERE id = 1").fetchone()


def documents_ready(settings) -> bool:
    return bool(settings["phone"] and settings["email"] and settings["website"] and settings["referral_fee_cents"])


def business_dict(settings) -> dict:
    return {
        "business_name": settings["business_name"],
        "street": settings["street"],
        "city_line": settings["city_line"],
        "phone": settings["phone"],
        "email": settings["email"],
        "website": settings["website"],
        "terms": settings["terms"],
    }


def remember_billing(conn, community_id: str, fields: dict) -> None:
    conn.execute(
        """
        INSERT INTO billing_contacts (
            community_id, attention, street, city, state, zip, billing_email, cc_email
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(community_id) DO UPDATE SET
            attention = excluded.attention,
            street = excluded.street,
            city = excluded.city,
            state = excluded.state,
            zip = excluded.zip,
            billing_email = excluded.billing_email,
            cc_email = excluded.cc_email
        """,
        (
            community_id,
            fields.get("attention") or "",
            fields.get("street") or "",
            fields.get("city") or "",
            fields.get("state") or "",
            fields.get("zip") or "",
            fields.get("billing_email") or "",
            fields.get("cc_email") or "",
        ),
    )


def create_invoice(conn, actor, contact, community, fields: dict, today: date) -> str:
    settings = settings_row(conn)
    if not documents_ready(settings):
        raise RuleError("Add the business phone, email, website, and referral fee before creating an invoice.")
    stage = conn.execute("SELECT stage_index FROM stages WHERE contact_id = ?", (contact["id"],)).fetchone()
    if int(stage["stage_index"]) < MOVE_IN_INDEX:
        raise RuleError("Record the actual move-in before invoicing the community.")
    move = conn.execute(
        "SELECT milestone_date FROM milestones WHERE contact_id = ? AND stage_index = ?",
        (contact["id"], MOVE_IN_INDEX),
    ).fetchone()
    if move is None:
        raise RuleError("The actual move-in date is missing.")
    from bestfit.db import next_invoice_number

    invoice_id = new_id()
    amount = int(settings["referral_fee_cents"])
    due = today + timedelta(days=30)
    conn.execute(
        """
        INSERT INTO invoices (
            id, organization_id, number, contact_id, community_id, resident, attention,
            street, city, state, zip, billing_email, cc_email, description, move_in,
            amount_cents, fee_rule, invoice_date, due_on, notes, status, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'Unpaid', ?)
        """,
        (
            invoice_id,
            actor["organization_id"],
            next_invoice_number(conn),
            contact["id"],
            community["id"],
            contact["name"],
            fields.get("attention") or "",
            fields.get("street") or community["address"],
            fields.get("city") or community["city"],
            fields.get("state") or community["state"],
            fields.get("zip") or community["zip"],
            fields.get("billing_email") or "",
            fields.get("cc_email") or "",
            fields.get("description") or "Senior living placement referral",
            move["milestone_date"],
            amount,
            "Flat referral fee set in business settings",
            today.isoformat(),
            due.isoformat(),
            fields.get("notes") or "",
            clock(today).isoformat(),
        ),
    )
    remember_billing(conn, community["id"], fields | {
        "street": fields.get("street") or community["address"],
        "city": fields.get("city") or community["city"],
        "state": fields.get("state") or community["state"],
        "zip": fields.get("zip") or community["zip"],
    })
    audit(conn, actor["id"], "invoice.create", contact["id"], None, {"invoice": invoice_id}, today)
    return invoice_id


def log_email(conn, actor_id, contact_id, recipient, cc, subject, status, detail, today) -> None:
    conn.execute(
        """
        INSERT INTO email_log (id, actor_id, contact_id, recipient, cc, subject, status, detail, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (new_id(), actor_id, contact_id, recipient, cc or "", subject, status, detail, clock(today).isoformat()),
    )


def mark_invoice_sent(conn, actor_id, invoice, method: str, today: date) -> None:
    conn.execute(
        """
        UPDATE invoices
        SET status = 'Sent', sent_at = ?, sent_to = ?, send_method = ?
        WHERE id = ?
        """,
        (clock(today).isoformat(), invoice["billing_email"], method, invoice["id"]),
    )
    current = conn.execute("SELECT stage_index FROM stages WHERE contact_id = ?", (invoice["contact_id"],)).fetchone()
    if int(current["stage_index"]) < 8:
        change_stage(conn, actor_id, invoice["contact_id"], 8, today, confirm_backward=False)
    audit(
        conn,
        actor_id,
        "invoice.sent",
        invoice["contact_id"],
        None,
        {"number": invoice["number"], "method": method, "to": invoice["billing_email"]},
        today,
    )


def mark_invoice_paid(conn, actor_id, invoice, reference: str, today: date) -> None:
    reference = (reference or "").strip()
    if not reference:
        raise RuleError("Enter a check number, wire id, or other payment reference.")
    conn.execute(
        """
        UPDATE invoices
        SET status = 'Paid', paid_at = ?, payment_reference = ?
        WHERE id = ?
        """,
        (clock(today).isoformat(), reference, invoice["id"]),
    )
    current = conn.execute("SELECT stage_index FROM stages WHERE contact_id = ?", (invoice["contact_id"],)).fetchone()
    if int(current["stage_index"]) < INVOICE_PAID_INDEX:
        change_stage(conn, actor_id, invoice["contact_id"], INVOICE_PAID_INDEX, today, confirm_backward=False)
    audit(
        conn,
        actor_id,
        "invoice.paid",
        invoice["contact_id"],
        None,
        {"number": invoice["number"], "reference": reference, "verified_by_bank": False},
        today,
    )


def parse_community_csv(text: str) -> list[dict]:
    reader = csv.DictReader(io.StringIO(text))
    required = {"name", "care_types", "address", "city", "zip", "license"}
    if reader.fieldnames is None or not required.issubset({(name or "").strip() for name in reader.fieldnames}):
        raise RuleError("CSV needs columns: name, care_types, address, city, zip, license")
    rows = []
    for line in reader:
        license_value = (line.get("license") or "unknown").strip().lower() or "unknown"
        if license_value not in {"yes", "no", "unknown"}:
            raise RuleError(f"License for {line.get('name') or 'a row'} must be yes, no, or unknown")
        if not (line.get("name") or "").strip():
            continue
        rows.append(
            {
                "name": line["name"].strip(),
                "care_types": (line.get("care_types") or "").strip(),
                "address": (line.get("address") or "").strip(),
                "city": (line.get("city") or "").strip(),
                "state": (line.get("state") or "TX").strip() or "TX",
                "zip": (line.get("zip") or "").strip(),
                "license": license_value,
                "note": (line.get("note") or "").strip(),
            }
        )
    return rows


def insert_community(conn, organization_id: str, fields: dict, lat: float | None, lng: float | None, source: str | None) -> str:
    community_id = new_id()
    conn.execute(
        """
        INSERT INTO communities (
            id, organization_id, name, care_types, address, city, state, zip, license, note, lat, lng, geo_source
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            community_id,
            organization_id,
            fields["name"],
            fields.get("care_types") or "",
            fields.get("address") or "",
            fields.get("city") or "",
            fields.get("state") or "TX",
            fields.get("zip") or "",
            fields.get("license") or "unknown",
            fields.get("note") or "",
            lat,
            lng,
            source,
        ),
    )
    return community_id


def create_user(conn, organization_id: str, display_name: str, email: str, password: str, role: str, today: date) -> str:
    if role not in {"super_admin", "advisor", "finance"}:
        raise RuleError("Unknown role")
    user_id = new_id()
    try:
        conn.execute(
            """
            INSERT INTO users (id, organization_id, display_name, email, password_hash, role, active, created_at)
            VALUES (?, ?, ?, ?, ?, ?, 1, ?)
            """,
            (user_id, organization_id, display_name.strip(), email.strip().lower(), hash_password(password), role, clock(today).isoformat()),
        )
    except sqlite3.IntegrityError as exc:
        raise RuleError("That email is already a user") from exc
    return user_id
