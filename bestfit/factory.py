"""Authenticated Best Fit web application."""

from __future__ import annotations

import csv
import io
import json
import os
import time
from datetime import date, datetime, timedelta
from functools import wraps
from zoneinfo import ZoneInfo

from flask import (
    Flask,
    abort,
    flash,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)
from bestfit.backup import export_backup, restore_backup
from bestfit.db import connect, init_db
from bestfit.documents import (
    INTAKE_TO_PROFILE,
    advisor_page_pdf,
    family_intake_pdf,
    invoice_pdf,
)
from bestfit.domain import (
    AUSTIN,
    INVOICE_PAID_INDEX,
    MOVE_IN_INDEX,
    STAGES,
    dates_out_of_order,
    duration_rows,
    funnel,
    parse_iso,
    prospect_start,
    summarize,
    validate_stall_limits,
)
from bestfit.geo import Geocoder, locate_offline, project, rank_communities
from bestfit.mail import EmailNotConfigured, send_message, smtp_ready
from bestfit.security import Crypto, csrf_matches, hash_password, new_csrf, verify_password
from bestfit.service import (
    DuplicateContact,
    NeedsBackwardConfirm,
    RuleError,
    audit,
    business_dict,
    change_stage,
    complete_task,
    contact_status,
    create_contact,
    create_invoice,
    create_user,
    documents_ready,
    insert_community,
    load_advisor,
    load_profile,
    load_stall_limits,
    log_email,
    mark_intake_sent,
    mark_invoice_paid,
    mark_invoice_sent,
    milestone_map,
    parse_community_csv,
    save_advisor,
    save_profile,
    set_milestone,
    settings_row,
    stalled_for,
    update_contact,
)


PROFILE_KEYS = (
    "care_type",
    "mobility",
    "cognition",
    "medication",
    "daily_living",
    "medical_notes",
    "primary_contact",
    "relationship",
    "family_phone",
    "family_email",
    "budget_min",
    "budget_max",
    "move_timeline",
    "preferred_areas",
    "must_haves",
    "tour_feedback",
    "other_notes",
)
ADVISOR_KEYS = (
    "lead_source",
    "intake_completed",
    "care_level",
    "urgency",
    "shortlisted",
    "referrals",
    "tours",
    "next_step",
    "outcome",
    "stop_date",
    "stop_notified",
)


def create_app(config: dict | None = None) -> Flask:
    app = Flask(__name__, template_folder="templates", static_folder="static", static_url_path="/assets")
    secret = os.environ.get("BESTFIT_SECRET_KEY")
    data_key = os.environ.get("BESTFIT_DATA_KEY")
    app.config.update(
        SECRET_KEY=secret,
        DATA_KEY=data_key,
        DATABASE=os.environ.get("BESTFIT_DATABASE", "instance/bestfit.sqlite"),
        ADMIN_EMAIL=(os.environ.get("BESTFIT_ADMIN_EMAIL") or "").strip().lower(),
        ADMIN_PASSWORD=os.environ.get("BESTFIT_ADMIN_PASSWORD") or "",
        ADMIN_NAME=os.environ.get("BESTFIT_ADMIN_NAME") or "Administrator",
        COOKIE_SECURE=os.environ.get("BESTFIT_COOKIE_SECURE") == "1",
        NOMINATIM=os.environ.get("BESTFIT_NOMINATIM") == "1",
        TODAY=None,
        MAIL_OUTBOX=None,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
    )
    if config:
        app.config.update(config)
    if not app.config["SECRET_KEY"] or not app.config["DATA_KEY"]:
        raise RuntimeError("Set BESTFIT_SECRET_KEY and BESTFIT_DATA_KEY before starting Best Fit.")
    app.config["SESSION_COOKIE_SECURE"] = bool(app.config.get("COOKIE_SECURE"))
    Crypto(app.config["DATA_KEY"])
    app.extensions["failures"] = {}
    _bootstrap(app)
    _register(app)
    return app


def _bootstrap(app: Flask) -> None:
    conn = connect(app.config["DATABASE"])
    try:
        init_db(conn)
        count = conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]
        email = app.config.get("ADMIN_EMAIL")
        password = app.config.get("ADMIN_PASSWORD")
        if count == 0 and email and password:
            create_user(
                conn,
                "org-bestfit",
                app.config.get("ADMIN_NAME") or "Administrator",
                email,
                password,
                "super_admin",
                _configured_today(app),
            )
            conn.commit()
    finally:
        conn.close()


def _configured_today(app: Flask) -> date:
    override = app.config.get("TODAY")
    if isinstance(override, date):
        return override
    return datetime.now(ZoneInfo("America/Chicago")).date()


def _register(app: Flask) -> None:
    def db():
        if "db" not in g:
            g.db = connect(app.config["DATABASE"])
        return g.db

    def crypto() -> Crypto:
        return Crypto(app.config["DATA_KEY"])

    def today() -> date:
        return _configured_today(app)

    def current():
        user_id = session.get("user_id")
        if not user_id:
            return None
        return db().execute("SELECT * FROM users WHERE id = ? AND active = 1", (user_id,)).fetchone()

    def money(cents: int | None) -> str:
        if cents is None:
            return "—"
        return f"${cents / 100:,.2f}"

    @app.context_processor
    def inject():
        actor = current()
        return {
            "csrf_token": session.get("csrf", ""),
            "current_user": actor,
            "stage_names": STAGES,
            "money": money,
            "smtp_ready": smtp_ready(app),
            "documents_ready": documents_ready(settings_row(db())) if actor else False,
        }

    @app.before_request
    def prepare():
        if "csrf" not in session:
            session["csrf"] = new_csrf()
        if request.endpoint in {"login", "health", "static"} or request.endpoint is None:
            return None
        actor = current()
        if actor is None:
            return redirect(url_for("login", next=request.path))
        if request.method == "POST":
            provided = request.form.get("csrf") or request.headers.get("X-CSRF-Token")
            if not csrf_matches(session.get("csrf"), provided):
                abort(400)
        return None

    @app.after_request
    def harden(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Robots-Tag"] = "noindex, nofollow"
        return response

    @app.teardown_appcontext
    def close_db(exc):
        conn = g.pop("db", None)
        if conn is None:
            return
        try:
            if exc is None:
                conn.commit()
            else:
                conn.rollback()
        finally:
            conn.close()

    def roles(*allowed):
        def decorator(func):
            @wraps(func)
            def wrapper(*args, **kwargs):
                actor = current()
                if actor["role"] not in allowed:
                    abort(403)
                return func(*args, **kwargs)

            return wrapper

        return decorator

    def get_contact(contact_id: str):
        actor = current()
        row = db().execute("SELECT * FROM contacts WHERE id = ?", (contact_id,)).fetchone()
        if row is None or row["organization_id"] != actor["organization_id"]:
            abort(404)
        if actor["role"] == "advisor" and row["owner_id"] != actor["id"]:
            abort(404)
        return row

    def can_edit_care(actor, contact) -> bool:
        if actor["role"] == "finance":
            return False
        if actor["role"] == "advisor" and contact["owner_id"] != actor["id"]:
            return False
        return True

    def safe_next(value: str | None) -> str:
        if value and value.startswith("/") and not value.startswith("//"):
            return value
        return url_for("dashboard")

    def locked(ip: str) -> bool:
        record = app.extensions["failures"].get(ip)
        if not record:
            return False
        count, until = record
        if count < 5:
            return False
        if time.monotonic() > until:
            app.extensions["failures"].pop(ip, None)
            return False
        return True

    def note_failure(ip: str) -> None:
        count, _until = app.extensions["failures"].get(ip, (0, 0))
        app.extensions["failures"][ip] = (count + 1, time.monotonic() + 15 * 60)

    def clear_failure(ip: str) -> None:
        app.extensions["failures"].pop(ip, None)

    def contact_rows(actor):
        if actor["role"] == "advisor":
            return db().execute(
                "SELECT * FROM contacts WHERE organization_id = ? AND owner_id = ? ORDER BY name",
                (actor["organization_id"], actor["id"]),
            ).fetchall()
        return db().execute(
            "SELECT * FROM contacts WHERE organization_id = ? ORDER BY name",
            (actor["organization_id"],),
        ).fetchall()

    def present_contact(contact, limits):
        stage = db().execute("SELECT stage_index FROM stages WHERE contact_id = ?", (contact["id"],)).fetchone()
        index = int(stage["stage_index"]) if stage else 0
        days, stalled = stalled_for(db(), contact["id"], index, today(), limits)
        return {
            "id": contact["id"],
            "name": contact["name"],
            "email": contact["email"],
            "phone": contact["phone"],
            "owner_id": contact["owner_id"],
            "status": contact_status(contact, today()),
            "email_consent": bool(contact["email_consent"]),
            "sms_consent": bool(contact["sms_consent"]),
            "stage_index": index,
            "stage_name": STAGES[index],
            "days": days,
            "stalled": stalled,
            "last": contact["last_engagement"],
            "created_at": contact["created_at"],
        }

    @app.get("/health")
    def health():
        db().execute("SELECT 1").fetchone()
        return {"ok": True}

    @app.route("/login", methods=["GET", "POST"])
    def login():
        notice = None
        if request.method == "POST":
            if not csrf_matches(session.get("csrf"), request.form.get("csrf")):
                abort(400)
            ip = request.remote_addr or "local"
            if locked(ip):
                notice = "Too many sign-in attempts. Wait and try again."
            else:
                email = (request.form.get("email") or "").strip().lower()
                password = request.form.get("password") or ""
                row = db().execute("SELECT * FROM users WHERE email = ? AND active = 1", (email,)).fetchone()
                if row and verify_password(password, row["password_hash"]):
                    clear_failure(ip)
                    session.clear()
                    session["csrf"] = new_csrf()
                    session["user_id"] = row["id"]
                    session.permanent = True
                    return redirect(safe_next(request.args.get("next")))
                note_failure(ip)
                notice = "Email or password is wrong."
        users = db().execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]
        return render_template("login.html", notice=notice, configured=users > 0)

    @app.post("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"))

    @app.get("/")
    def dashboard():
        actor = current()
        if actor["role"] == "finance":
            return redirect(url_for("finance_home"))
        limits = load_stall_limits(db())
        rows = [present_contact(row, limits) for row in contact_rows(actor)]
        tasks = []
        for row in rows:
            found = db().execute(
                "SELECT * FROM tasks WHERE contact_id = ? AND status = 'open' ORDER BY due_on",
                (row["id"],),
            ).fetchall()
            for task in found:
                tasks.append({**dict(task), "contact_name": row["name"], "contact_id": row["id"]})
        due = [task for task in tasks if task["due_on"] <= today().isoformat()]
        upcoming = [task for task in tasks if today().isoformat() < task["due_on"] <= (today() + timedelta(days=7)).isoformat()]
        new_people = [row for row in rows if (parse_iso(row["created_at"]) or today()) >= today() - timedelta(days=7)]
        tours = []
        for row in rows:
            found = db().execute(
                """
                SELECT * FROM tours
                WHERE contact_id = ? AND status = 'scheduled' AND tour_date <= ?
                ORDER BY tour_date
                """,
                (row["id"], (today() + timedelta(days=7)).isoformat()),
            ).fetchall()
            tours.extend({**dict(tour), "contact_name": row["name"]} for tour in found)
        move_ins = [row for row in rows if row["stage_index"] >= MOVE_IN_INDEX]
        stalled = sorted((row for row in rows if row["stalled"]), key=lambda row: -(row["days"] or 0))[:7]
        invoice_follow = [
            task for task in tasks if task["kind"] == "placement" and task["stage_index"] in (7, 8)
        ]
        return render_template(
            "dashboard.html",
            total=len(rows),
            due=due,
            upcoming=upcoming,
            new_people=new_people,
            tours=tours,
            move_ins=move_ins,
            stalled=stalled,
            invoice_follow=invoice_follow,
        )

    @app.get("/finance")
    @roles("finance", "super_admin")
    def finance_home():
        invoices = db().execute(
            "SELECT * FROM invoices WHERE organization_id = ? ORDER BY invoice_date DESC",
            (current()["organization_id"],),
        ).fetchall()
        buckets = {"Unpaid": 0, "Sent": 0, "Paid": 0}
        aging = []
        for invoice in invoices:
            buckets[invoice["status"]] += invoice["amount_cents"]
            if invoice["status"] == "Sent" and invoice["sent_at"]:
                sent = parse_iso(invoice["sent_at"])
                aging.append({**dict(invoice), "age": (today() - sent).days if sent else None})
        return render_template("finance.html", invoices=invoices, buckets=buckets, aging=aging)

    @app.get("/contacts")
    def contacts():
        actor = current()
        limits = load_stall_limits(db())
        rows = [present_contact(row, limits) for row in contact_rows(actor)]
        engagement = request.args.get("engagement") or "all"
        stage = request.args.get("stage") or "any"
        query = (request.args.get("q") or "").strip().lower()
        filtered = []
        for row in rows:
            if engagement == "inactive" and row["status"] != "Inactive":
                continue
            if engagement == "reachable" and not (
                row["status"] == "Inactive" and (row["email_consent"] or row["sms_consent"]) and (row["email"] or row["phone"])
            ):
                continue
            if stage == "in_progress" and row["stage_index"] >= MOVE_IN_INDEX:
                continue
            if stage == "moved_in" and row["stage_index"] < MOVE_IN_INDEX:
                continue
            if stage == "stalled" and not row["stalled"]:
                continue
            if stage.isdigit() and row["stage_index"] != int(stage):
                continue
            haystack = " ".join(part or "" for part in (row["name"], row["email"], row["phone"])).lower()
            if query and query not in haystack:
                continue
            filtered.append(row)
        return render_template(
            "contacts.html",
            rows=filtered,
            total=len(rows),
            showing=len(filtered),
            engagement=engagement,
            stage=stage,
            query=request.args.get("q") or "",
            limited=actor["role"] == "finance",
        )

    @app.route("/contacts/new", methods=["GET", "POST"])
    @roles("super_admin", "advisor")
    def contact_new():
        actor = current()
        values = {
            "name": "",
            "email": "",
            "phone": "",
            "tags": "",
            "email_consent": False,
            "sms_consent": False,
            "owner_id": actor["id"],
        }
        duplicate = None
        if request.method == "POST":
            values = _contact_form(actor)
            try:
                contact_id = create_contact(db(), crypto(), actor, values, today())
            except DuplicateContact as exc:
                duplicate = {"id": exc.contact_id, "name": exc.name, "field": exc.field}
                return render_template("contact_form.html", values=values, duplicate=duplicate, owners=_owners(), editing=False), 409
            return redirect(url_for("contact_detail", contact_id=contact_id))
        return render_template("contact_form.html", values=values, duplicate=duplicate, owners=_owners(), editing=False)

    def _owners():
        return db().execute(
            "SELECT id, display_name, role FROM users WHERE active = 1 ORDER BY display_name"
        ).fetchall()

    def _contact_form(actor):
        owner = request.form.get("owner_id") or actor["id"]
        if actor["role"] != "super_admin":
            owner = actor["id"]
        return {
            "name": request.form.get("name") or "",
            "email": request.form.get("email") or "",
            "phone": request.form.get("phone") or "",
            "tags": request.form.get("tags") or "",
            "email_consent": request.form.get("email_consent") == "1",
            "sms_consent": request.form.get("sms_consent") == "1",
            "owner_id": owner,
            "last_engagement": request.form.get("last_engagement") or today().isoformat(),
        }

    @app.route("/contacts/<contact_id>/edit", methods=["GET", "POST"])
    @roles("super_admin", "advisor")
    def contact_edit(contact_id):
        actor = current()
        contact = get_contact(contact_id)
        values = {
            "name": contact["name"],
            "email": contact["email"] or "",
            "phone": contact["phone"] or "",
            "tags": contact["tags"],
            "email_consent": bool(contact["email_consent"]),
            "sms_consent": bool(contact["sms_consent"]),
            "owner_id": contact["owner_id"],
            "last_engagement": contact["last_engagement"] or "",
        }
        if request.method == "POST":
            values = _contact_form(actor)
            if actor["role"] != "super_admin":
                values["owner_id"] = contact["owner_id"]
            try:
                update_contact(db(), actor, contact_id, values, today())
            except DuplicateContact as exc:
                duplicate = {"id": exc.contact_id, "name": exc.name, "field": exc.field}
                return render_template("contact_form.html", values=values, duplicate=duplicate, owners=_owners(), editing=True), 409
            return redirect(url_for("contact_detail", contact_id=contact_id))
        return render_template("contact_form.html", values=values, duplicate=None, owners=_owners(), editing=True)

    @app.get("/contacts/<contact_id>")
    def contact_detail(contact_id):
        actor = current()
        contact = get_contact(contact_id)
        limits = load_stall_limits(db())
        card = present_contact(contact, limits)
        tab = request.args.get("tab") or "overview"
        if actor["role"] == "finance":
            tab = "overview"
        if tab == "advisor" and actor["role"] not in {"super_admin", "advisor"}:
            abort(403)
        profile = {} if actor["role"] == "finance" else load_profile(db(), crypto(), contact_id)
        advisor = {}
        if tab == "advisor":
            advisor = load_advisor(db(), crypto(), contact_id)
            audit(db(), actor["id"], "advisor.view", contact_id, None, {"surface": "profile"}, today())
        milestones = milestone_map(db(), contact_id)
        tasks = db().execute(
            "SELECT * FROM tasks WHERE contact_id = ? ORDER BY due_on",
            (contact_id,),
        ).fetchall()
        tours = db().execute("SELECT * FROM tours WHERE contact_id = ? ORDER BY tour_date", (contact_id,)).fetchall()
        communities = db().execute(
            "SELECT * FROM communities WHERE organization_id = ? ORDER BY name",
            (actor["organization_id"],),
        ).fetchall()
        invoices = db().execute("SELECT * FROM invoices WHERE contact_id = ? ORDER BY created_at DESC", (contact_id,)).fetchall()
        celebrate = request.args.get("celebrate") if request.args.get("celebrate") in {"forward", "movein"} else ""
        progress = round(100 * card["stage_index"] / (len(STAGES) - 1), 1)
        return render_template(
            "contact_detail.html",
            contact=contact,
            card=card,
            tab=tab,
            profile=profile,
            advisor=advisor,
            milestones=milestones,
            tasks=tasks,
            tours=tours,
            communities=communities,
            invoices=invoices,
            celebrate=celebrate,
            progress=progress,
            can_edit=can_edit_care(actor, contact),
            out_of_order=dates_out_of_order(milestones),
            profile_keys=PROFILE_KEYS,
            advisor_keys=ADVISOR_KEYS,
        )

    @app.post("/contacts/<contact_id>/profile")
    @roles("super_admin", "advisor")
    def contact_profile(contact_id):
        contact = get_contact(contact_id)
        if not can_edit_care(current(), contact):
            abort(403)
        data = {
            key: (request.form.get(key) or "").strip()
            for key in PROFILE_KEYS
            if key in request.form
        }
        save_profile(db(), crypto(), current()["id"], contact_id, data, today())
        flash("Care and family details saved.")
        return redirect(url_for("contact_detail", contact_id=contact_id, tab=request.form.get("tab") or "care"))

    @app.post("/contacts/<contact_id>/advisor")
    @roles("super_admin", "advisor")
    def contact_advisor(contact_id):
        get_contact(contact_id)
        data = {key: (request.form.get(key) or "").strip() for key in ADVISOR_KEYS}
        save_advisor(db(), crypto(), current()["id"], contact_id, data, today())
        flash("Advisor notes saved.")
        return redirect(url_for("contact_detail", contact_id=contact_id, tab="advisor"))

    @app.post("/contacts/<contact_id>/stage")
    @roles("super_admin", "advisor")
    def contact_stage(contact_id):
        get_contact(contact_id)
        try:
            new_index = int(request.form.get("stage") or "-1")
        except ValueError:
            abort(400)
        try:
            result = change_stage(
                db(),
                current()["id"],
                contact_id,
                new_index,
                today(),
                request.form.get("confirm_backward") == "1",
            )
        except NeedsBackwardConfirm:
            flash("Moving backward needs the confirmation checkbox. Milestone dates were kept.")
            return redirect(url_for("contact_detail", contact_id=contact_id))
        flag = ""
        if result["move_in"]:
            flag = "movein"
        elif result["forward"]:
            flag = "forward"
        return redirect(url_for("contact_detail", contact_id=contact_id, celebrate=flag))

    @app.post("/contacts/<contact_id>/milestones")
    @roles("super_admin", "advisor")
    def contact_milestones(contact_id):
        get_contact(contact_id)
        for index in range(len(STAGES)):
            raw = (request.form.get(f"date_{index}") or "").strip()
            if raw == "":
                continue
            parsed = parse_iso(raw)
            if parsed is None:
                flash(f"{STAGES[index]} needs a YYYY-MM-DD date.")
                return redirect(url_for("contact_detail", contact_id=contact_id))
            set_milestone(db(), current()["id"], contact_id, index, parsed, today())
        flash("Milestone dates saved. Earlier stages were not filled in for you.")
        return redirect(url_for("contact_detail", contact_id=contact_id))

    @app.post("/contacts/<contact_id>/tasks/<task_id>/done")
    @roles("super_admin", "advisor")
    def task_done(contact_id, task_id):
        get_contact(contact_id)
        task = db().execute("SELECT * FROM tasks WHERE id = ? AND contact_id = ?", (task_id, contact_id)).fetchone()
        if task is None:
            abort(404)
        complete_task(db(), current()["id"], task_id, today())
        return redirect(url_for("contact_detail", contact_id=contact_id, tab="activity"))

    @app.post("/contacts/<contact_id>/intake/send")
    @roles("super_admin", "advisor")
    def intake_send(contact_id):
        actor = current()
        contact = get_contact(contact_id)
        test = request.form.get("test") == "1"
        settings = settings_row(db())
        if not documents_ready(settings):
            flash("Add phone, email, website, and the referral fee in settings before sending an intake form.")
            return redirect(url_for("contact_detail", contact_id=contact_id))
        if not test and not contact["email_consent"]:
            flash("Email was not sent. This person has not consented to email.")
            return redirect(url_for("contact_detail", contact_id=contact_id))
        recipient = actor["email"] if test else (contact["email"] or "")
        if not recipient:
            flash("Add an email address first.")
            return redirect(url_for("contact_detail", contact_id=contact_id))
        pdf = family_intake_pdf(business_dict(settings), {"name": contact["name"], "email": contact["email"], "phone": contact["phone"]})
        subject = ("TEST — " if test else "") + f"Intake form for {contact['name']}"
        body = (
            "The attached PDF can be filled in and returned. "
            "It does not include advisor notes."
        )
        try:
            send_message(
                app,
                to=recipient,
                subject=subject,
                body=body,
                attachment_name="Best-Fit-Prospect-Intake-Form.pdf",
                attachment=pdf,
            )
        except EmailNotConfigured as exc:
            flash(str(exc))
            return redirect(url_for("contact_detail", contact_id=contact_id))
        log_email(db(), actor["id"], contact_id, recipient, "", subject, "sent", "test" if test else "intake", today())
        mark_intake_sent(db(), actor["id"], contact_id, today(), test=test)
        flash("Test email sent. No follow-up was created." if test else "Intake email sent. A follow-up is due in 24 hours.")
        return redirect(url_for("contact_detail", contact_id=contact_id, tab="activity"))

    @app.post("/contacts/<contact_id>/intake/sent")
    @roles("super_admin", "advisor")
    def intake_mark_sent(contact_id):
        get_contact(contact_id)
        mark_intake_sent(db(), current()["id"], contact_id, today(), test=False)
        audit(db(), current()["id"], "intake.manual", contact_id, None, {"method": "outside the app"}, today())
        flash("Marked sent without a server email. A follow-up is due in 24 hours.")
        return redirect(url_for("contact_detail", contact_id=contact_id, tab="activity"))

    @app.get("/contacts/<contact_id>/family.pdf")
    @roles("super_admin", "advisor")
    def family_pdf(contact_id):
        contact = get_contact(contact_id)
        settings = settings_row(db())
        if not documents_ready(settings):
            flash("Add phone, email, website, and the referral fee before generating the family PDF.")
            return redirect(url_for("settings"))
        pdf = family_intake_pdf(
            business_dict(settings),
            {"name": contact["name"], "email": contact["email"], "phone": contact["phone"]},
        )
        audit(db(), current()["id"], "export.family_pdf", contact_id, None, {"pages": 4}, today())
        return send_file(
            io.BytesIO(pdf),
            mimetype="application/pdf",
            as_attachment=True,
            download_name="Best-Fit-Prospect-Intake-Form.pdf",
        )

    @app.get("/contacts/<contact_id>/advisor.pdf")
    @roles("super_admin")
    def advisor_pdf(contact_id):
        contact = get_contact(contact_id)
        profile = load_profile(db(), crypto(), contact_id)
        advisor = load_advisor(db(), crypto(), contact_id)
        stage = db().execute("SELECT stage_index FROM stages WHERE contact_id = ?", (contact_id,)).fetchone()
        pdf = advisor_page_pdf(
            business_dict(settings_row(db())),
            contact,
            profile,
            advisor,
            STAGES[int(stage["stage_index"])],
        )
        audit(db(), current()["id"], "advisor.view", contact_id, None, {"surface": "pdf"}, today())
        return send_file(io.BytesIO(pdf), mimetype="application/pdf", as_attachment=True, download_name="advisor-intake.pdf")

    @app.post("/contacts/<contact_id>/intake/import")
    @roles("super_admin", "advisor")
    def intake_import(contact_id):
        get_contact(contact_id)
        upload = request.files.get("pdf")
        if upload is None or not upload.filename:
            flash("Choose the filled-in PDF.")
            return redirect(url_for("contact_detail", contact_id=contact_id, tab="family"))
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(upload.read()))
        fields = reader.get_fields() or {}
        answers = {}
        for name, field in fields.items():
            value = field.get("/V") if isinstance(field, dict) else None
            if value not in (None, "", "/Off"):
                answers[str(name)] = str(value)
        profile = load_profile(db(), crypto(), contact_id)
        family = dict(profile.get("family_intake") or {})
        family.update(answers)
        profile_update = {"family_intake": family}
        for source, target in INTAKE_TO_PROFILE.items():
            if answers.get(source):
                profile_update[target] = answers[source]
        save_profile(db(), crypto(), current()["id"], contact_id, profile_update, today())
        flash("Returned answers were saved on the family tab. Advisor notes were left as they were.")
        return redirect(url_for("contact_detail", contact_id=contact_id, tab="family"))

    @app.post("/contacts/<contact_id>/tours")
    @roles("super_admin", "advisor")
    def add_tour(contact_id):
        get_contact(contact_id)
        title = (request.form.get("title") or "").strip()
        tour_date = parse_iso(request.form.get("tour_date") or "")
        if not title or tour_date is None:
            flash("A tour needs a title and a date.")
            return redirect(url_for("contact_detail", contact_id=contact_id, tab="communities"))
        from bestfit.service import new_id

        db().execute(
            """
            INSERT INTO tours (id, contact_id, community_id, title, tour_date, tour_time, status, feedback)
            VALUES (?, ?, ?, ?, ?, ?, 'scheduled', '')
            """,
            (
                new_id(),
                contact_id,
                request.form.get("community_id") or None,
                title,
                tour_date.isoformat(),
                (request.form.get("tour_time") or "").strip(),
            ),
        )
        flash("Tour saved on this prospect. It is not synced to an outside calendar.")
        return redirect(url_for("contact_detail", contact_id=contact_id, tab="communities"))

    @app.get("/pipeline")
    @roles("super_admin", "advisor")
    def pipeline():
        limits = load_stall_limits(db())
        columns = {index: [] for index in range(len(STAGES))}
        for row in contact_rows(current()):
            card = present_contact(row, limits)
            columns[card["stage_index"]].append(card)
        for cards in columns.values():
            cards.sort(key=lambda card: -(card["days"] if card["days"] is not None else -1))
        return render_template("pipeline.html", columns=columns, limits=limits)

    @app.post("/pipeline/move")
    @roles("super_admin", "advisor")
    def pipeline_move():
        payload = request.get_json(silent=True) or request.form
        contact_id = payload.get("contact_id")
        get_contact(contact_id)
        try:
            new_index = int(payload.get("stage"))
        except (TypeError, ValueError):
            abort(400)
        confirm = payload.get("confirm_backward") in (True, "1", "true", "on")
        try:
            result = change_stage(db(), current()["id"], contact_id, new_index, today(), confirm)
        except NeedsBackwardConfirm:
            if request.is_json:
                return jsonify({"ok": False, "error": "confirm"}), 409
            flash("Moving backward needs confirmation. Milestone dates were kept.")
            return redirect(url_for("pipeline"))
        if request.is_json:
            return jsonify({"ok": True, "forward": result["forward"], "move_in": result["move_in"]})
        flag = "movein" if result["move_in"] else ("forward" if result["forward"] else "")
        return redirect(url_for("contact_detail", contact_id=contact_id, celebrate=flag))

    @app.get("/reports")
    @roles("super_admin", "advisor")
    def reports():
        built = _report_model()
        audit(db(), current()["id"], "export.report_view", None, None, {"rows": len(built["rows"])}, today())
        return render_template("reports.html", report=built)

    @app.post("/reports.csv")
    @roles("super_admin", "advisor")
    def reports_csv():
        built = _report_model()
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(["Prospect", "Stage", "Days in stage", "Stalled", "Move-in", "Prospect to signed", "Signed to move-in", "Move-in to paid", "Prospect to move-in", "Review dates"])
        for row in built["rows"]:
            writer.writerow([
                row["name"],
                row["stage_name"],
                "" if row["days"] is None else row["days"],
                "yes" if row["stalled"] else "",
                row["move_in"] or "",
                _blank(row["durations"]["prospect_to_signed"]),
                _blank(row["durations"]["signed_to_move_in"]),
                _blank(row["durations"]["move_in_to_paid"]),
                _blank(row["durations"]["prospect_to_move_in"]),
                "Review dates" if row["out_of_order"] else "",
            ])
        writer.writerow([])
        writer.writerow(["Summary", "Count", "Average", "Median", "Fastest", "Slowest"])
        for label, summary in built["summaries"].items():
            writer.writerow([label, summary["count"], _blank(summary["average"]), _blank(summary["median"]), _blank(summary["fastest"]), _blank(summary["slowest"])])
        writer.writerow([])
        writer.writerow(["Funnel", "Reached", "Percent of prior"])
        for item in built["funnel"]:
            writer.writerow([item["stage"], item["count"], _blank(item["percent_of_prior"])])
        audit(db(), current()["id"], "export.report_csv", None, None, {"rows": len(built["rows"])}, today())
        return app.response_class(buffer.getvalue(), mimetype="text/csv", headers={"Content-Disposition": "attachment; filename=bestfit_placement_timeline.csv"})

    def _blank(value):
        return "" if value is None else value

    def _report_model():
        limits = load_stall_limits(db())
        rows = []
        reached = [0] * len(STAGES)
        buckets = {key: [] for key in ("prospect_to_signed", "signed_to_move_in", "move_in_to_paid", "prospect_to_move_in")}
        for contact in contact_rows(current()):
            card = present_contact(contact, limits)
            milestones = milestone_map(db(), contact["id"])
            for index in milestones:
                reached[index] += 1
            created = parse_iso(contact["created_at"])
            durations = duration_rows(milestones, created)
            for key, value in durations.items():
                if value is not None:
                    buckets[key].append(value)
            rows.append({
                **card,
                "durations": durations,
                "move_in": milestones.get(MOVE_IN_INDEX).isoformat() if milestones.get(MOVE_IN_INDEX) else None,
                "out_of_order": dates_out_of_order(milestones),
                "start": prospect_start(milestones, created),
            })
        labels = {
            "prospect_to_signed": "Prospect to Contract Signed",
            "signed_to_move_in": "Contract Signed to Actual Move-in",
            "move_in_to_paid": "Actual Move-in to Invoice Paid",
            "prospect_to_move_in": "Prospect to Actual Move-in",
        }
        return {
            "rows": rows,
            "summaries": {labels[key]: summarize(values) for key, values in buckets.items()},
            "funnel": funnel(reached),
        }

    def _community_plot(center, communities, radius, care, name, label):
        ranked = rank_communities(center, communities, radius, care, name)
        north = (center[0] + radius / 69.0, center[1])
        coords = project(
            center,
            [north] + [(row["lat"], row["lng"]) for row in ranked],
            radius,
        )
        lead_xy = coords[0]
        edge = coords[1]
        distance = max(abs(edge[0] - lead_xy[0]), abs(edge[1] - lead_xy[1]), 1)
        pins = [
            {
                "x": xy[0],
                "y": xy[1],
                "n": index + 1,
                "name": row["name"],
                "miles": row["miles"],
                "license": row["license"],
            }
            for index, (row, xy) in enumerate(zip(ranked, coords[2:]))
        ]
        short = label if len(label) <= 22 else label[:21] + "…"
        return {"x": lead_xy[0], "y": lead_xy[1], "r": distance, "label": short, "pins": pins}

    @app.route("/map", methods=["GET", "POST"])
    @roles("super_admin", "advisor")
    def community_map():
        actor = current()
        message = None
        plot = None
        try:
            radius = float(request.values.get("radius") or 25)
        except ValueError:
            radius = 25
        radius = min(120.0, max(1.0, radius))
        care = request.values.get("care") or ""
        name = request.values.get("name") or ""
        query = (request.values.get("query") or "").strip()
        searching = bool(query) and (
            request.method == "POST" or request.args.get("action") == "search" or "query" in request.args
        )
        saved = [
            dict(row)
            for row in db().execute(
                "SELECT * FROM communities WHERE organization_id = ?",
                (actor["organization_id"],),
            ).fetchall()
        ]
        if searching:
            cache_rows = db().execute("SELECT query, lat, lng FROM geocode_cache").fetchall()
            cache = {row["query"]: (row["lat"], row["lng"]) for row in cache_rows}
            geocoder = Geocoder(enabled=bool(app.config.get("NOMINATIM")), cache=cache)
            result = geocoder.locate(query)
            if result and result.get("store_cache"):
                db().execute(
                    """
                    INSERT INTO geocode_cache (query, lat, lng, cached_at) VALUES (?, ?, ?, ?)
                    ON CONFLICT(query) DO UPDATE SET lat = excluded.lat, lng = excluded.lng, cached_at = excluded.cached_at
                    """,
                    (query.lower(), result["lat"], result["lng"], datetime.now(ZoneInfo("America/Chicago")).isoformat()),
                )
            if result and result.get("out_of_area"):
                message = result["detail"]
            elif result and result.get("lat") is not None:
                message = result["detail"] + " Distances are straight-line, not drive time."
                plot = _community_plot(
                    (result["lat"], result["lng"]),
                    saved,
                    radius,
                    care,
                    name,
                    result.get("label") or "Search",
                )
                if not plot["pins"]:
                    message += " No saved communities are inside this radius."
            elif result:
                message = result["detail"]
        if plot is None:
            plot = _community_plot(AUSTIN, saved, max(radius, 25), "", "", "Austin")
            if not searching:
                message = "Showing the directory around Austin. Search a town, ZIP, or street to measure from a family’s address."
        communities = db().execute(
            "SELECT * FROM communities WHERE organization_id = ? ORDER BY name",
            (actor["organization_id"],),
        ).fetchall()
        return render_template(
            "map.html",
            message=message,
            plot=plot,
            communities=communities,
            query=query,
            radius=int(radius) if radius == int(radius) else radius,
            care=care,
            name=name,
        )

    @app.post("/communities")
    @roles("super_admin", "advisor")
    def community_add():
        actor = current()
        license_value = (request.form.get("license") or "unknown").strip().lower()
        if license_value not in {"yes", "no", "unknown"}:
            flash("License must be yes, no, or unknown.")
            return redirect(url_for("community_map"))
        fields = {
            "name": (request.form.get("name") or "").strip(),
            "care_types": (request.form.get("care_types") or "").strip(),
            "address": (request.form.get("address") or "").strip(),
            "city": (request.form.get("city") or "").strip(),
            "state": (request.form.get("state") or "TX").strip(),
            "zip": (request.form.get("zip") or "").strip(),
            "license": license_value,
            "note": (request.form.get("note") or "").strip(),
        }
        if not fields["name"]:
            flash("A community needs a name.")
            return redirect(url_for("community_map"))
        located = locate_offline(" ".join(part for part in (fields["zip"], fields["city"]) if part))
        lat = lng = None
        source = None
        if located and located.get("lat") is not None and not located.get("out_of_area"):
            lat, lng, source = located["lat"], located["lng"], located["source"]
        insert_community(db(), actor["organization_id"], fields, lat, lng, source)
        flash("Community saved. License stays unknown until you set it.")
        return redirect(url_for("community_map"))

    @app.post("/communities/import")
    @roles("super_admin")
    def community_import():
        upload = request.files.get("csv")
        if upload is None:
            flash("Choose a CSV file.")
            return redirect(url_for("community_map"))
        try:
            rows = parse_community_csv(upload.read().decode("utf-8-sig"))
        except (RuleError, UnicodeDecodeError) as exc:
            flash(str(exc))
            return redirect(url_for("community_map"))
        for fields in rows:
            located = locate_offline(" ".join(part for part in (fields["zip"], fields["city"]) if part))
            lat = lng = source = None
            if located and located.get("lat") is not None and not located.get("out_of_area"):
                lat, lng, source = located["lat"], located["lng"], located["source"]
            insert_community(db(), current()["organization_id"], fields, lat, lng, source)
        flash(f"Imported {len(rows)} communities. Unknown licenses stay visible.")
        return redirect(url_for("community_map"))

    @app.post("/contacts/<contact_id>/invoices")
    @roles("super_admin", "advisor", "finance")
    def invoice_create(contact_id):
        actor = current()
        contact = get_contact(contact_id)
        if actor["role"] == "advisor" and contact["owner_id"] != actor["id"]:
            abort(404)
        community = db().execute("SELECT * FROM communities WHERE id = ?", (request.form.get("community_id"),)).fetchone()
        if community is None:
            flash("Choose the community that will be billed.")
            return redirect(url_for("contact_detail", contact_id=contact_id))
        billing = db().execute("SELECT * FROM billing_contacts WHERE community_id = ?", (community["id"],)).fetchone()
        fields = {
            "attention": request.form.get("attention") or (billing["attention"] if billing else ""),
            "street": request.form.get("street") or (billing["street"] if billing else community["address"]),
            "city": request.form.get("city") or (billing["city"] if billing else community["city"]),
            "state": request.form.get("state") or (billing["state"] if billing else community["state"]),
            "zip": request.form.get("zip") or (billing["zip"] if billing else community["zip"]),
            "billing_email": request.form.get("billing_email") or (billing["billing_email"] if billing else ""),
            "cc_email": request.form.get("cc_email") or (billing["cc_email"] if billing else ""),
            "description": request.form.get("description") or "",
            "notes": request.form.get("notes") or "",
        }
        try:
            invoice_id = create_invoice(db(), actor, contact, community, fields, today())
        except RuleError as exc:
            flash(str(exc))
            return redirect(url_for("contact_detail", contact_id=contact_id))
        return redirect(url_for("invoice_detail", invoice_id=invoice_id))

    @app.get("/invoices/<invoice_id>")
    def invoice_detail(invoice_id):
        invoice, community = _invoice_for_actor(invoice_id)
        settings = settings_row(db())
        return render_template("invoice.html", invoice=invoice, community=community, settings=settings)

    @app.get("/invoices")
    def invoice_list():
        actor = current()
        if actor["role"] == "advisor":
            invoices = db().execute(
                """
                SELECT invoices.*, contacts.name AS contact_name FROM invoices
                JOIN contacts ON contacts.id = invoices.contact_id
                WHERE invoices.organization_id = ? AND contacts.owner_id = ?
                ORDER BY invoices.created_at DESC
                """,
                (actor["organization_id"], actor["id"]),
            ).fetchall()
        else:
            invoices = db().execute(
                """
                SELECT invoices.*, contacts.name AS contact_name FROM invoices
                JOIN contacts ON contacts.id = invoices.contact_id
                WHERE invoices.organization_id = ?
                ORDER BY invoices.created_at DESC
                """,
                (actor["organization_id"],),
            ).fetchall()
        return render_template("invoices.html", invoices=invoices)

    def _invoice_for_actor(invoice_id: str):
        actor = current()
        invoice = db().execute("SELECT * FROM invoices WHERE id = ?", (invoice_id,)).fetchone()
        if invoice is None:
            abort(404)
        contact = get_contact(invoice["contact_id"])
        community = db().execute("SELECT * FROM communities WHERE id = ?", (invoice["community_id"],)).fetchone()
        return invoice, community

    @app.post("/invoices/<invoice_id>/send")
    @roles("super_admin", "advisor", "finance")
    def invoice_send(invoice_id):
        invoice, community = _invoice_for_actor(invoice_id)
        manual = request.form.get("manual") == "1"
        if not invoice["billing_email"]:
            flash("Add a billing email first.")
            return redirect(url_for("invoice_detail", invoice_id=invoice_id))
        if manual:
            mark_invoice_sent(db(), current()["id"], invoice, "manual", today())
            flash("Marked sent. Best Fit did not deliver this email.")
            return redirect(url_for("invoice_detail", invoice_id=invoice_id))
        settings = settings_row(db())
        pdf = invoice_pdf(business_dict(settings), _invoice_view(invoice, community))
        subject = f"{invoice['number']} referral fee for {invoice['resident']}"
        try:
            send_message(
                app,
                to=invoice["billing_email"],
                cc=invoice["cc_email"],
                subject=subject,
                body="The referral invoice is attached.",
                attachment_name=f"{invoice['number']}.pdf",
                attachment=pdf,
            )
        except EmailNotConfigured as exc:
            flash(str(exc))
            return redirect(url_for("invoice_detail", invoice_id=invoice_id))
        log_email(db(), current()["id"], invoice["contact_id"], invoice["billing_email"], invoice["cc_email"], subject, "sent", "invoice", today())
        mark_invoice_sent(db(), current()["id"], invoice, "server", today())
        flash("Invoice emailed with the PDF attached.")
        return redirect(url_for("invoice_detail", invoice_id=invoice_id))

    @app.post("/invoices/<invoice_id>/paid")
    @roles("super_admin", "advisor", "finance")
    def invoice_paid(invoice_id):
        invoice, _community = _invoice_for_actor(invoice_id)
        try:
            mark_invoice_paid(db(), current()["id"], invoice, request.form.get("reference") or "", today())
        except RuleError as exc:
            flash(str(exc))
            return redirect(url_for("invoice_detail", invoice_id=invoice_id))
        flash("Payment recorded. This is not a bank verification.")
        return redirect(url_for("invoice_detail", invoice_id=invoice_id))

    @app.get("/invoices/<invoice_id>/pdf")
    def invoice_download(invoice_id):
        invoice, community = _invoice_for_actor(invoice_id)
        pdf = invoice_pdf(business_dict(settings_row(db())), _invoice_view(invoice, community))
        audit(db(), current()["id"], "export.invoice_pdf", invoice["contact_id"], None, {"number": invoice["number"]}, today())
        return send_file(io.BytesIO(pdf), mimetype="application/pdf", as_attachment=True, download_name=f"{invoice['number']}.pdf")

    def _invoice_view(invoice, community) -> dict:
        return {
            "number": invoice["number"],
            "invoice_date": invoice["invoice_date"],
            "due_on": invoice["due_on"],
            "community_name": community["name"] if community else "",
            "attention": invoice["attention"],
            "street": invoice["street"],
            "city": invoice["city"],
            "state": invoice["state"],
            "zip": invoice["zip"],
            "resident": invoice["resident"],
            "move_in": invoice["move_in"],
            "description": invoice["description"],
            "amount": money(invoice["amount_cents"]),
            "fee_rule": invoice["fee_rule"],
            "notes": invoice["notes"],
        }

    @app.route("/admin/users", methods=["GET", "POST"])
    @roles("super_admin")
    def users():
        actor = current()
        if request.method == "POST":
            try:
                create_user(
                    db(),
                    actor["organization_id"],
                    request.form.get("display_name") or "",
                    request.form.get("email") or "",
                    request.form.get("password") or "",
                    request.form.get("role") or "",
                    today(),
                )
            except (RuleError, ValueError) as exc:
                flash(str(exc))
                return redirect(url_for("users"))
            audit(db(), actor["id"], "user.create", None, None, {"email": (request.form.get("email") or "").strip().lower()}, today())
            flash("User created. The password is stored as a hash.")
            return redirect(url_for("users"))
        people = db().execute("SELECT id, display_name, email, role, active FROM users ORDER BY display_name").fetchall()
        return render_template("users.html", people=people)

    @app.post("/admin/users/<user_id>")
    @roles("super_admin")
    def user_update(user_id):
        actor = current()
        person = db().execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        if person is None:
            abort(404)
        active = request.form.get("active") == "1"
        if person["role"] == "super_admin" and person["active"] and not active:
            others = db().execute(
                "SELECT COUNT(*) AS n FROM users WHERE role = 'super_admin' AND active = 1 AND id != ?",
                (user_id,),
            ).fetchone()["n"]
            if others == 0:
                flash("Keep at least one active administrator.")
                return redirect(url_for("users"))
        password = request.form.get("password") or ""
        if password:
            try:
                digest = hash_password(password)
            except ValueError as exc:
                flash(str(exc))
                return redirect(url_for("users"))
            db().execute("UPDATE users SET password_hash = ?, active = ? WHERE id = ?", (digest, 1 if active else 0, user_id))
        else:
            db().execute("UPDATE users SET active = ? WHERE id = ?", (1 if active else 0, user_id))
        audit(db(), actor["id"], "user.update", None, None, {"user": user_id, "active": active}, today())
        flash("User updated.")
        return redirect(url_for("users"))

    @app.route("/admin/settings", methods=["GET", "POST"])
    @roles("super_admin")
    def settings():
        current_settings = settings_row(db())
        if request.method == "POST":
            try:
                fee = _parse_fee(request.form.get("referral_fee") or "")
                limits = {}
                for index in range(INVOICE_PAID_INDEX):
                    limits[index] = int(request.form.get(f"stall_{index}") or "0")
                limits = validate_stall_limits(limits)
                retention_raw = (request.form.get("retention_days") or "").strip()
                retention = int(retention_raw) if retention_raw else None
                if retention is not None and retention < 1:
                    raise ValueError("Retention must be a positive number of days, or blank.")
            except (ValueError, RuleError) as exc:
                flash(str(exc))
                return redirect(url_for("settings"))
            db().execute(
                """
                UPDATE settings SET
                    business_name = ?, street = ?, city_line = ?, phone = ?, email = ?, website = ?,
                    terms = ?, referral_fee_cents = ?, retention_days = ?, stall_json = ?
                WHERE id = 1
                """,
                (
                    (request.form.get("business_name") or "").strip(),
                    (request.form.get("street") or "").strip(),
                    (request.form.get("city_line") or "").strip(),
                    (request.form.get("phone") or "").strip(),
                    (request.form.get("email") or "").strip(),
                    (request.form.get("website") or "").strip(),
                    (request.form.get("terms") or "Net 30").strip(),
                    fee,
                    retention,
                    json.dumps(limits),
                ),
            )
            audit(db(), current()["id"], "settings.update", None, None, {"referral_fee_cents": fee}, today())
            flash("Settings saved.")
            return redirect(url_for("settings"))
        limits = load_stall_limits(db())
        eligible = _retention_candidates(current_settings)
        return render_template("settings.html", settings=current_settings, limits=limits, eligible=eligible, stage_names=STAGES)

    def _parse_fee(raw: str) -> int:
        text = raw.strip().replace("$", "").replace(",", "")
        if not text:
            raise ValueError("Enter the flat referral fee.")
        if not __import__("re").fullmatch(r"\d+(\.\d{1,2})?", text):
            raise ValueError("Enter the referral fee in dollars, like 2500 or 2500.00.")
        from decimal import Decimal

        cents = int(Decimal(text) * 100)
        if cents <= 0:
            raise ValueError("The referral fee has to be greater than zero.")
        return cents

    def _retention_candidates(current_settings):
        if not current_settings["retention_days"]:
            return []
        cutoff = today() - timedelta(days=int(current_settings["retention_days"]))
        found = []
        rows = db().execute(
            """
            SELECT contacts.id, contacts.name, invoices.paid_at
            FROM contacts
            JOIN stages ON stages.contact_id = contacts.id
            JOIN invoices ON invoices.contact_id = contacts.id AND invoices.status = 'Paid'
            WHERE stages.stage_index = ?
            """,
            (INVOICE_PAID_INDEX,),
        ).fetchall()
        for row in rows:
            paid = parse_iso(row["paid_at"])
            if paid and paid <= cutoff:
                found.append(row)
        return found

    @app.post("/admin/retention/<contact_id>")
    @roles("super_admin")
    def retention_delete(contact_id):
        contact = get_contact(contact_id)
        eligible = {row["id"] for row in _retention_candidates(settings_row(db()))}
        if contact_id not in eligible:
            flash("That record is not past the retention date.")
            return redirect(url_for("settings"))
        audit(db(), current()["id"], "contact.delete", contact_id, {"name": contact["name"]}, {"reason": "retention"}, today())
        db().execute("DELETE FROM contacts WHERE id = ?", (contact_id,))
        flash("The closed record was deleted and the deletion was audited.")
        return redirect(url_for("settings"))

    @app.get("/admin/audit")
    @roles("super_admin")
    def audit_log():
        events = db().execute(
            """
            SELECT audit_events.*, users.display_name
            FROM audit_events
            LEFT JOIN users ON users.id = audit_events.actor_id
            ORDER BY occurred_at DESC
            LIMIT 200
            """
        ).fetchall()
        return render_template("audit.html", events=events)

    @app.route("/backup", methods=["GET", "POST"])
    @roles("super_admin")
    def backup():
        if request.method == "POST" and request.form.get("action") == "restore":
            if request.form.get("confirm") != "RESTORE":
                flash("Type RESTORE to replace the current records.")
                return redirect(url_for("backup"))
            upload = request.files.get("file")
            if upload is None or not upload.filename:
                flash("Choose an encrypted backup file.")
                return redirect(url_for("backup"))
            try:
                summary = restore_backup(db(), crypto(), upload.read())
            except ValueError as exc:
                flash(str(exc))
                return redirect(url_for("backup"))
            audit(db(), current()["id"], "backup.restore", None, None, summary, today())
            flash(f"Restored {summary['contacts']} contacts.")
            return redirect(url_for("dashboard"))
        return render_template("backup.html")

    @app.post("/backup/export")
    @roles("super_admin")
    def backup_export():
        blob = export_backup(db(), crypto())
        audit(db(), current()["id"], "backup.export", None, None, {"bytes": len(blob)}, today())
        stamp = today().isoformat()
        return send_file(
            io.BytesIO(blob),
            mimetype="application/octet-stream",
            as_attachment=True,
            download_name=f"bestfit_backup_{stamp}.bfe",
        )

    @app.get("/privacy")
    def privacy():
        return render_template("privacy.html")

    @app.errorhandler(403)
    def forbidden(_exc):
        return render_template("error.html", title="Not allowed", message="Your role cannot open that."), 403

    @app.errorhandler(404)
    def missing(_exc):
        if current() is None:
            return redirect(url_for("login"))
        return render_template("error.html", title="Not found", message="That record is not available to you."), 404

    @app.errorhandler(400)
    def bad(_exc):
        return render_template("error.html", title="Form expired", message="Reload the page and try again."), 400

    return app
