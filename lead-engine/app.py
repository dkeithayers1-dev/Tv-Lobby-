"""LeadPilot — AI lead response, booking, CRM and follow-up for local businesses."""
import functools
import hmac
import json
import logging
import os
import threading
import time
from datetime import timedelta
from pathlib import Path
from xml.sax.saxutils import escape

from dotenv import load_dotenv
from flask import (Flask, Response, abort, flash, jsonify, redirect, render_template,
                   request, session, url_for)

load_dotenv()

import ai  # noqa: E402
import automations as auto  # noqa: E402
import business  # noqa: E402
import db  # noqa: E402
import messaging  # noqa: E402
import security  # noqa: E402

BASE_DIR = Path(__file__).resolve().parent
log = logging.getLogger("leadpilot")


def create_app(config=None):
    app = Flask(__name__)
    app.secret_key = os.environ.get("FLASK_SECRET_KEY", "dev-only-secret-change-me")
    app.config.update(
        DATABASE=os.environ.get("DATABASE_PATH", str(BASE_DIR / "instance" / "leadpilot.db")),
        SYNC_TASKS=False,
        RUN_SCHEDULER=os.environ.get("RUN_SCHEDULER", "1") == "1",
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
    )
    if config:
        app.config.update(config)
    db.init(app.config["DATABASE"])
    business.seed()
    register(app)
    if app.config["RUN_SCHEDULER"] and not app.config.get("TESTING"):
        start_scheduler(app)
    return app


# ---------------------------------------------------------------------------
# Background work
# ---------------------------------------------------------------------------

_scheduler_started = False


def start_scheduler(app, interval=30):
    global _scheduler_started
    if _scheduler_started:
        return
    _scheduler_started = True

    def loop():
        while True:
            try:
                auto.run_due_jobs()
            except Exception:
                log.exception("Scheduler tick failed")
            time.sleep(interval)

    threading.Thread(target=loop, name="leadpilot-scheduler", daemon=True).start()


def background(app, fn, *args, **kwargs):
    """Run fn off the request thread (inline when SYNC_TASKS, e.g. in tests)."""
    if app.config.get("SYNC_TASKS"):
        return fn(*args, **kwargs)

    def run():
        try:
            fn(*args, **kwargs)
        except Exception:
            log.exception("Background task %s failed", getattr(fn, "__name__", fn))

    threading.Thread(target=run, daemon=True).start()


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

def admin_required(view):
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        if os.environ.get("ADMIN_PASSWORD") and not session.get("admin"):
            if request.path.startswith("/api/"):
                return jsonify({"error": "unauthorized"}), 401
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


def api_key_required(view):
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        expected = os.environ.get("LEAD_API_KEY")
        given = request.headers.get("X-API-Key") or request.args.get("api_key") or ""
        if not expected or not hmac.compare_digest(expected, given):
            return jsonify({"error": "invalid or missing API key"}), 401
        return view(*args, **kwargs)
    return wrapped


def twilio_required(view):
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        base = business.base_url()
        url = (base + request.full_path.rstrip("?")) if base else request.url
        if not security.twilio_signature_valid(url, request.form.to_dict(), request.headers.get("X-Twilio-Signature")):
            abort(403)
        return view(*args, **kwargs)
    return wrapped


def twiml(*parts):
    return Response('<?xml version="1.0" encoding="UTF-8"?><Response>' + "".join(parts) + "</Response>",
                    mimetype="text/xml")


def say(text):
    return f'<Say voice="Polly.Joanna">{escape(text)}</Say>'


def gather(action, prompt=None):
    inner = say(prompt) if prompt else ""
    return (f'<Gather input="speech" action="{escape(action)}" method="POST" speechTimeout="auto" '
            f'timeout="6" language="en-US">{inner}</Gather>')


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

def register(app):

    @app.before_request
    def track_host():
        if not request.path.startswith("/static"):
            business.remember_host(request.host_url)

    @app.context_processor
    def inject():
        return {
            "biz": business.setting("business_name"),
            "agent": business.setting("agent_name"),
            "stages": business.STAGES,
            "stage_labels": dict(business.STAGES),
            "human_time": business.human_time,
            "ai_live": ai.ai_enabled(),
            "auth_on": bool(os.environ.get("ADMIN_PASSWORD")),
            "unread": db.scalar("SELECT COUNT(*) FROM alerts WHERE read_at IS NULL"),
        }

    @app.template_filter("ago")
    def ago(value):
        dt = business.parse_ts(value)
        if not dt:
            return ""
        delta = business.now() - dt
        s = int(delta.total_seconds())
        future = s < 0
        s = abs(s)
        if s < 60:
            txt = f"{s}s"
        elif s < 3600:
            txt = f"{s // 60}m"
        elif s < 86400:
            txt = f"{s // 3600}h"
        else:
            txt = f"{s // 86400}d"
        return f"in {txt}" if future else f"{txt} ago"

    @app.template_filter("duration")
    def duration(secs):
        if secs is None:
            return "—"
        secs = int(secs)
        if secs < 60:
            return f"{secs}s"
        if secs < 3600:
            return f"{secs // 60}m {secs % 60}s"
        return f"{secs // 3600}h {(secs % 3600) // 60}m"

    @app.template_filter("money")
    def money(v):
        return f"${(v or 0):,.0f}"

    @app.template_filter("fromjson")
    def fromjson(v):
        return json.loads(v or "[]")

    @app.get("/healthz")
    def healthz():
        return {"ok": True, "ai": ai.ai_enabled(), "channels": messaging.channel_status()}

    # ---- auth ------------------------------------------------------------

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if request.method == "POST":
            pw = os.environ.get("ADMIN_PASSWORD", "")
            if pw and hmac.compare_digest(pw, request.form.get("password", "")):
                session["admin"] = True
                nxt = request.args.get("next") or "/"
                return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") else "/")
            flash("Incorrect password.", "error")
        return render_template("login.html")

    @app.post("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"))

    # ---- dashboard (CRM lead intelligence) --------------------------------

    @app.get("/")
    @admin_required
    def dashboard():
        stats = auto.dashboard_stats()
        new_inquiries = db.query(
            "SELECT * FROM leads WHERE stage IN ('new','contacted') ORDER BY created_at DESC LIMIT 8")
        overdue = auto.overdue_leads(limit=10)
        alerts = db.query(
            "SELECT a.*, l.name AS lead_name FROM alerts a LEFT JOIN leads l ON l.id = a.lead_id "
            "WHERE a.read_at IS NULL ORDER BY a.id DESC LIMIT 8")
        upcoming = db.query(
            "SELECT a.*, l.name AS lead_name, l.phone FROM appointments a JOIN leads l ON l.id = a.lead_id "
            "WHERE a.status IN ('scheduled','confirmed') AND a.starts_at >= ? ORDER BY a.starts_at LIMIT 6",
            (business.ts(),))
        counts = {r["stage"]: r for r in db.query(
            "SELECT stage, COUNT(*) AS n, COALESCE(SUM(value),0) AS v FROM leads GROUP BY stage")}
        return render_template("dashboard.html", stats=stats, new_inquiries=new_inquiries, overdue=overdue,
                               alerts=alerts, upcoming=upcoming, counts=counts,
                               channels=messaging.channel_status())

    @app.get("/pipeline")
    @admin_required
    def pipeline():
        leads = db.query("SELECT * FROM leads ORDER BY updated_at DESC")
        cols = {k: [] for k, _ in business.STAGES}
        for lead in leads:
            cols.setdefault(lead["stage"], []).append(lead)
        now = business.ts()
        return render_template("pipeline.html", cols=cols, now=now)

    # ---- leads -------------------------------------------------------------

    @app.get("/leads")
    @admin_required
    def leads():
        q = request.args.get("q", "").strip()
        stage = request.args.get("stage", "")
        sql, params = "SELECT * FROM leads WHERE 1=1", []
        if q:
            sql += " AND (name LIKE ? OR phone LIKE ? OR email LIKE ? OR service LIKE ?)"
            params += [f"%{q}%"] * 4
        if stage:
            sql += " AND stage = ?"
            params.append(stage)
        sql += " ORDER BY updated_at DESC LIMIT 500"
        return render_template("leads.html", leads=db.query(sql, params), q=q, stage=stage)

    @app.post("/leads")
    @admin_required
    def lead_create():
        data = request.form.to_dict()
        if not (data.get("phone") or data.get("email")):
            flash("A phone number or email is required.", "error")
            return redirect(url_for("leads"))
        if data.get("instant_reply"):
            lead = auto.new_lead(data, source=data.get("source") or "manual")
        else:
            lead = auto.create_lead(data, data.get("source") or "manual")
        flash("Lead added.", "ok")
        return redirect(url_for("lead_detail", lead_id=lead["id"]))

    @app.get("/leads/<int:lead_id>")
    @admin_required
    def lead_detail(lead_id):
        lead = auto.get_lead(lead_id) or abort(404)
        msgs = db.query("SELECT * FROM messages WHERE lead_id = ? ORDER BY id", (lead_id,))
        appts = db.query("SELECT * FROM appointments WHERE lead_id = ? ORDER BY starts_at DESC", (lead_id,))
        enrollments = db.query(
            "SELECT e.*, s.name AS sequence_name, s.steps FROM enrollments e JOIN sequences s ON s.id = e.sequence_id "
            "WHERE e.lead_id = ? ORDER BY e.id DESC", (lead_id,))
        notes = db.query("SELECT * FROM notes WHERE lead_id = ? ORDER BY id DESC", (lead_id,))
        sequences = db.query("SELECT * FROM sequences WHERE active = 1 ORDER BY name")
        slots = business.available_slots(limit=12, spread=False)
        db.execute("UPDATE alerts SET read_at = ? WHERE lead_id = ? AND read_at IS NULL", (business.ts(), lead_id))
        frs = None
        if lead["first_response_at"]:
            frs = max(0, (business.parse_ts(lead["first_response_at"]) - business.parse_ts(lead["created_at"])).total_seconds())
        return render_template("lead.html", lead=lead, first_reply_secs=frs, msgs=msgs, appts=appts, enrollments=enrollments,
                               notes=notes, sequences=sequences, slots=slots,
                               booking_link=business.booking_link(lead))

    @app.post("/leads/<int:lead_id>/update")
    @admin_required
    def lead_update(lead_id):
        lead = auto.get_lead(lead_id) or abort(404)
        f = request.form
        fields = {k: (f.get(k) or "").strip() or None
                  for k in ("name", "email", "service", "timeline", "budget", "details", "preferred_channel")}
        fields["phone"] = auto.normalize_phone(f.get("phone"))
        try:
            fields["value"] = float(f.get("value") or 0)
        except ValueError:
            fields["value"] = lead["value"]
        nfu = f.get("next_follow_up_at")
        fields["next_follow_up_at"] = business.ts(business.parse_ts(nfu)) if nfu else None
        fields["updated_at"] = business.ts()
        db.update("leads", lead_id, fields)
        if f.get("stage") and f.get("stage") != lead["stage"]:
            auto.set_stage(auto.get_lead(lead_id), f["stage"])
        flash("Lead updated.", "ok")
        return redirect(url_for("lead_detail", lead_id=lead_id))

    @app.post("/api/leads/<int:lead_id>/stage")
    @admin_required
    def api_lead_stage(lead_id):
        lead = auto.get_lead(lead_id) or abort(404)
        stage = (request.get_json(silent=True) or {}).get("stage") or request.form.get("stage")
        if stage not in business.STAGE_KEYS:
            return jsonify({"error": "invalid stage"}), 400
        auto.set_stage(lead, stage)
        return jsonify({"ok": True, "stage": stage})

    @app.post("/leads/<int:lead_id>/stage")
    @admin_required
    def lead_stage(lead_id):
        lead = auto.get_lead(lead_id) or abort(404)
        auto.set_stage(lead, request.form.get("stage"))
        return redirect(request.referrer or url_for("lead_detail", lead_id=lead_id))

    @app.post("/leads/<int:lead_id>/message")
    @admin_required
    def lead_message(lead_id):
        lead = auto.get_lead(lead_id) or abort(404)
        body = (request.form.get("body") or "").strip()
        if body:
            messaging.send(lead, body, channel=request.form.get("channel") or None, sender="team",
                           subject=request.form.get("subject") or None)
            db.update("leads", lead_id, {"next_follow_up_at": None})
            flash("Message sent.", "ok")
        return redirect(url_for("lead_detail", lead_id=lead_id) + "#composer")

    @app.post("/leads/<int:lead_id>/note")
    @admin_required
    def lead_note(lead_id):
        auto.get_lead(lead_id) or abort(404)
        body = (request.form.get("body") or "").strip()
        if body:
            auto.add_note(lead_id, body)
        return redirect(url_for("lead_detail", lead_id=lead_id))

    @app.post("/leads/<int:lead_id>/ai")
    @admin_required
    def lead_toggle_ai(lead_id):
        lead = auto.get_lead(lead_id) or abort(404)
        db.update("leads", lead_id, {"ai_paused": 0 if lead["ai_paused"] else 1})
        flash("AI assistant resumed for this lead." if lead["ai_paused"] else "AI paused — your team is handling this lead.", "ok")
        return redirect(url_for("lead_detail", lead_id=lead_id))

    @app.post("/leads/<int:lead_id>/enroll")
    @admin_required
    def lead_enroll(lead_id):
        lead = auto.get_lead(lead_id) or abort(404)
        if auto.enroll(lead, int(request.form.get("sequence_id") or 0)):
            flash("Enrolled in sequence.", "ok")
        else:
            flash("Could not enroll (already active, opted out, or empty sequence).", "error")
        return redirect(url_for("lead_detail", lead_id=lead_id))

    @app.post("/leads/<int:lead_id>/stop-sequences")
    @admin_required
    def lead_stop_sequences(lead_id):
        auto.stop_enrollments(lead_id, "stopped by team")
        flash("Sequences stopped.", "ok")
        return redirect(url_for("lead_detail", lead_id=lead_id))

    @app.post("/leads/<int:lead_id>/book")
    @admin_required
    def lead_book(lead_id):
        lead = auto.get_lead(lead_id) or abort(404)
        start = business.parse_ts(request.form.get("starts_at"))
        if not start:
            flash("Pick a valid date and time.", "error")
            return redirect(url_for("lead_detail", lead_id=lead_id))
        existing = auto.upcoming_appointment(lead_id)
        if existing:
            auto.reschedule_appointment(existing, start, notify_customer=bool(request.form.get("notify")))
        else:
            auto.book_appointment(lead, start, notes=request.form.get("notes"),
                                  notify_customer=bool(request.form.get("notify")))
        flash("Appointment saved.", "ok")
        return redirect(url_for("lead_detail", lead_id=lead_id))

    @app.post("/leads/<int:lead_id>/delete")
    @admin_required
    def lead_delete(lead_id):
        db.execute("DELETE FROM leads WHERE id = ?", (lead_id,))
        flash("Lead deleted.", "ok")
        return redirect(url_for("leads"))

    @app.get("/api/leads/<int:lead_id>/messages")
    @admin_required
    def api_lead_messages(lead_id):
        after = int(request.args.get("after", 0))
        return jsonify(db.query("SELECT * FROM messages WHERE lead_id = ? AND id > ? ORDER BY id", (lead_id, after)))

    # ---- appointments ------------------------------------------------------

    @app.get("/appointments")
    @admin_required
    def appointments():
        show = request.args.get("show", "upcoming")
        if show == "past":
            where, order = "a.starts_at < ?", "DESC"
        else:
            where, order = "a.starts_at >= ?", "ASC"
        rows = db.query(
            f"SELECT a.*, l.name AS lead_name, l.phone, l.email, l.service FROM appointments a "
            f"JOIN leads l ON l.id = a.lead_id WHERE {where} ORDER BY a.starts_at {order} LIMIT 300",
            (business.ts(business.now() - timedelta(hours=1)) if show != "past" else business.ts(),))
        reminders = {r["appointment_id"]: r for r in db.query(
            "SELECT appointment_id, MIN(run_at) AS run_at FROM jobs WHERE kind='reminder' AND status='pending' GROUP BY appointment_id")}
        return render_template("appointments.html", rows=rows, show=show, reminders=reminders)

    @app.post("/appointments/<int:appt_id>/status")
    @admin_required
    def appointment_status(appt_id):
        appt = db.one("SELECT * FROM appointments WHERE id = ?", (appt_id,)) or abort(404)
        status = request.form.get("status")
        if status == "cancelled":
            auto.cancel_appointment(appt, by="team", notify_customer=bool(request.form.get("notify", "1")))
        elif status in ("completed", "no_show", "confirmed", "scheduled"):
            db.update("appointments", appt_id, {"status": status, "updated_at": business.ts()})
            if status == "no_show":
                lead = auto.get_lead(appt["lead_id"])
                auto.add_alert(lead["id"], "no_show", f"{lead['name'] or lead['phone']} didn't show up — follow up to rebook")
                db.update("leads", lead["id"], {"next_follow_up_at": business.ts()})
        return redirect(request.referrer or url_for("appointments"))

    # ---- sequences ---------------------------------------------------------

    @app.get("/sequences")
    @admin_required
    def sequences():
        rows = db.query(
            "SELECT s.*, (SELECT COUNT(*) FROM enrollments e WHERE e.sequence_id = s.id AND e.status='active') AS active_n, "
            "(SELECT COUNT(*) FROM enrollments e WHERE e.sequence_id = s.id AND e.stop_reason='replied') AS replied_n, "
            "(SELECT COUNT(*) FROM enrollments e WHERE e.sequence_id = s.id) AS total_n FROM sequences s ORDER BY s.id")
        edit_id = request.args.get("edit", type=int)
        editing = db.one("SELECT * FROM sequences WHERE id = ?", (edit_id,)) if edit_id else None
        return render_template("sequences.html", rows=rows, editing=editing, channels=business.CHANNELS)

    @app.post("/sequences")
    @admin_required
    def sequence_save():
        f = request.form
        steps = []
        for delay, channel, template in zip(f.getlist("delay_hours"), f.getlist("channel"), f.getlist("template")):
            if template.strip():
                try:
                    d = max(0.0, float(delay or 0))
                except ValueError:
                    d = 24.0
                steps.append({"delay_hours": d, "channel": channel if channel in business.CHANNELS else "sms",
                              "template": template.strip()})
        data = {"name": f.get("name", "").strip() or "Untitled sequence", "description": f.get("description"),
                "trigger": f.get("trigger") or None, "active": 1 if f.get("active") else 0,
                "steps": json.dumps(steps)}
        if f.get("id"):
            db.update("sequences", int(f["id"]), data)
        else:
            db.insert("sequences", data)
        flash("Sequence saved.", "ok")
        return redirect(url_for("sequences"))

    @app.post("/sequences/<int:seq_id>/delete")
    @admin_required
    def sequence_delete(seq_id):
        db.execute("DELETE FROM sequences WHERE id = ?", (seq_id,))
        flash("Sequence deleted.", "ok")
        return redirect(url_for("sequences"))

    # ---- inbox / alerts ----------------------------------------------------

    @app.get("/inbox")
    @admin_required
    def inbox():
        alerts = db.query(
            "SELECT a.*, l.name AS lead_name, l.phone FROM alerts a LEFT JOIN leads l ON l.id = a.lead_id "
            "ORDER BY a.id DESC LIMIT 200")
        handoffs = db.query("SELECT * FROM leads WHERE ai_paused = 1 AND stage NOT IN ('won','lost') ORDER BY updated_at DESC")
        convos = db.query(
            "SELECT l.*, m.body AS last_body, m.direction AS last_dir, m.channel AS last_channel, m.created_at AS last_at "
            "FROM leads l JOIN messages m ON m.id = (SELECT MAX(id) FROM messages WHERE lead_id = l.id) "
            "ORDER BY m.id DESC LIMIT 50")
        return render_template("inbox.html", alerts=alerts, handoffs=handoffs, convos=convos)

    @app.post("/alerts/read-all")
    @admin_required
    def alerts_read_all():
        db.execute("UPDATE alerts SET read_at = ? WHERE read_at IS NULL", (business.ts(),))
        return redirect(request.referrer or url_for("inbox"))

    # ---- settings / knowledge base ----------------------------------------

    @app.route("/settings", methods=["GET", "POST"])
    @admin_required
    def settings():
        if request.method == "POST":
            f = request.form
            for key in ("business_name", "business_description", "services", "service_area", "agent_name",
                        "timezone", "missed_call_text", "public_base_url"):
                if key in f:
                    db.set_setting(key, f.get(key, "").strip())
            for key in ("slot_minutes", "min_notice_hours", "booking_days_ahead", "follow_up_after_hours"):
                try:
                    db.set_setting(key, float(f.get(key)) if "." in f.get(key, "") else int(f.get(key)))
                except (TypeError, ValueError):
                    pass
            offsets = [x.strip() for x in f.get("reminder_offsets_hours", "").split(",") if x.strip()]
            try:
                db.set_setting("reminder_offsets_hours", [float(x) for x in offsets])
            except ValueError:
                flash("Reminder offsets must be numbers, e.g. 24, 2", "error")
            hours = {}
            for d in business.DAYS:
                if f.get(f"open_{d}"):
                    hours[d] = [f.get(f"start_{d}") or "09:00", f.get(f"end_{d}") or "17:00"]
                else:
                    hours[d] = None
            db.set_setting("hours", hours)
            flash("Settings saved.", "ok")
            return redirect(url_for("settings"))
        cfg = {k: business.setting(k) for k in business.DEFAULT_SETTINGS}
        base = business.base_url() or request.host_url.rstrip("/")
        return render_template("settings.html", cfg=cfg, faqs=ai.faqs(), channels=messaging.channel_status(),
                               base=base, days=business.DAYS,
                               api_key_set=bool(os.environ.get("LEAD_API_KEY")))

    @app.post("/faqs")
    @admin_required
    def faq_save():
        f = request.form
        data = {"question": f.get("question", "").strip(), "answer": f.get("answer", "").strip(),
                "keywords": f.get("keywords", "").strip().lower()}
        if not data["question"] or not data["answer"]:
            flash("Question and answer are required.", "error")
        elif f.get("id"):
            db.update("faqs", int(f["id"]), data)
        else:
            db.insert("faqs", data)
        return redirect(url_for("settings") + "#faqs")

    @app.post("/faqs/<int:faq_id>/delete")
    @admin_required
    def faq_delete(faq_id):
        db.execute("DELETE FROM faqs WHERE id = ?", (faq_id,))
        return redirect(url_for("settings") + "#faqs")

    # ---- simulator (test every automation without a phone provider) -------

    @app.get("/simulator")
    @admin_required
    def simulator():
        pending = db.query(
            "SELECT j.*, l.name AS lead_name FROM jobs j LEFT JOIN leads l ON l.id = j.lead_id "
            "WHERE j.status = 'pending' ORDER BY j.run_at LIMIT 15")
        return render_template("simulator.html", pending=pending)

    @app.post("/api/simulate/message")
    @admin_required
    def sim_message():
        data = request.get_json(force=True)
        channel = data.get("channel", "sms")
        phone = data.get("phone") or "+15555550100"
        if channel == "email":
            lead, _ = auto.find_or_create_lead(email=data.get("email") or "customer@example.com", source="email", channel="email")
        else:
            lead, _ = auto.find_or_create_lead(phone=phone, source=f"{channel}_inbound", channel=channel)
        reply = auto.handle_inbound(lead, channel, data.get("text", ""))
        return jsonify({"lead_id": lead["id"], "reply": reply, "messages": db.query(
            "SELECT * FROM messages WHERE lead_id = ? ORDER BY id", (lead["id"],))})

    @app.post("/api/simulate/missed-call")
    @admin_required
    def sim_missed_call():
        data = request.get_json(force=True)
        lead = auto.missed_call(data.get("phone") or "+15555550100")
        return jsonify({"lead_id": lead["id"], "messages": db.query(
            "SELECT * FROM messages WHERE lead_id = ? ORDER BY id", (lead["id"],))})

    @app.post("/api/simulate/run-next-job")
    @admin_required
    def sim_run_next_job():
        job = db.one("SELECT * FROM jobs WHERE status = 'pending' ORDER BY run_at LIMIT 1")
        if not job:
            return jsonify({"ran": 0, "message": "No pending jobs."})
        db.update("jobs", job["id"], {"run_at": business.ts()})
        auto.run_due_jobs()
        done = db.one("SELECT * FROM jobs WHERE id = ?", (job["id"],))
        return jsonify({"ran": 1, "job": done, "lead_id": job["lead_id"]})

    @app.post("/api/simulate/reset-lead")
    @admin_required
    def sim_reset_lead():
        phone = auto.normalize_phone((request.get_json(force=True) or {}).get("phone"))
        if phone:
            db.execute("DELETE FROM leads WHERE phone = ?", (phone,))
        return jsonify({"ok": True})

    # ---- public: lead capture form (speed-to-lead) -------------------------

    @app.route("/f/contact", methods=["GET", "POST"])
    def contact_form():
        done = False
        if request.method == "POST":
            data = request.form.to_dict()
            if data.get("website"):  # honeypot
                return render_template("public/contact.html", done=True)
            if not (data.get("phone") or data.get("email")):
                flash("Please add a phone number or email so we can reply.", "error")
                return render_template("public/contact.html", done=False, form=data)
            background(app, auto.new_lead, data, request.form.get("source") or "web_form")
            done = True
        services = [s.strip() for s in (business.setting("services") or "").split(",") if s.strip()]
        return render_template("public/contact.html", done=done, form={}, services=services)

    @app.post("/api/v1/leads")
    @api_key_required
    def api_new_lead():
        data = request.get_json(silent=True) or request.form.to_dict()
        if not (data.get("phone") or data.get("email")):
            return jsonify({"error": "phone or email is required"}), 400
        lead = auto.new_lead(data, source=data.get("source") or "api")
        return jsonify({"id": lead["id"], "stage": lead["stage"]}), 201

    # ---- public: website chat (AI agent) -----------------------------------

    @app.get("/chat")
    def chat():
        return render_template("public/chat.html", embed=request.args.get("embed") == "1")

    @app.post("/chat/send")
    def chat_send():
        text = ((request.get_json(silent=True) or {}).get("text") or "").strip()[:1000]
        if not text:
            return jsonify({"error": "empty"}), 400
        lead = auto.get_lead(session.get("chat_lead_id") or 0)
        if not lead:
            lead = auto.create_lead({"preferred_channel": "web"}, "website_chat")
            session["chat_lead_id"] = lead["id"]
            auto.add_alert(lead["id"], "new_lead", "New website chat started")
        auto.handle_inbound(lead, "web", text)
        return jsonify(_chat_messages(lead["id"], 0))

    @app.get("/chat/poll")
    def chat_poll():
        lead_id = session.get("chat_lead_id")
        if not lead_id:
            return jsonify([])
        return jsonify(_chat_messages(lead_id, int(request.args.get("after", 0))))

    def _chat_messages(lead_id, after):
        rows = db.query("SELECT id, direction, sender, body, created_at FROM messages WHERE lead_id = ? AND id > ? "
                        "AND (channel = 'web' OR direction = 'out') ORDER BY id", (lead_id, after))
        return rows

    # ---- public: appointment management (reminder links) -------------------

    @app.route("/a/<token>", methods=["GET", "POST"])
    def manage_appointment(token):
        appt = db.one("SELECT * FROM appointments WHERE token = ?", (token,)) or abort(404)
        lead = auto.get_lead(appt["lead_id"])
        msg = None
        if request.method == "POST":
            action = request.form.get("action")
            active = appt["status"] in ("scheduled", "confirmed")
            if action == "confirm" and active:
                db.update("appointments", appt["id"], {"status": "confirmed", "updated_at": business.ts()})
                msg = "Thanks — your appointment is confirmed."
            elif action == "cancel" and active:
                auto.cancel_appointment(appt, by="customer", notify_customer=False)
                msg = "Your appointment has been cancelled."
            elif action == "reschedule":
                start = business.parse_ts(request.form.get("slot"))
                valid = {business.ts(s) for s in business.available_slots(limit=60, spread=False)}
                if start and business.ts(start) in valid:
                    if active:
                        auto.reschedule_appointment(appt, start)
                    else:
                        auto.book_appointment(lead, start)
                    msg = f"You're rescheduled for {business.human_time(start)}."
                else:
                    msg = "That time is no longer available — please pick another."
            appt = db.one("SELECT * FROM appointments WHERE id = ?", (appt["id"],))
            if appt["status"] == "cancelled" and action == "reschedule" and msg.startswith("You're"):
                newest = auto.upcoming_appointment(lead["id"])
                if newest:
                    return redirect(url_for("manage_appointment", token=newest["token"]))
        slots = business.available_slots(limit=12, spread=False)
        return render_template("public/appointment.html", appt=appt, lead=lead, slots=slots, msg=msg)

    @app.route("/book/<signed>", methods=["GET", "POST"])
    def public_book(signed):
        lead_id = security.unsign_lead(signed)
        lead = auto.get_lead(lead_id) if lead_id else None
        if not lead:
            abort(404)
        existing = auto.upcoming_appointment(lead["id"])
        if existing:
            return redirect(url_for("manage_appointment", token=existing["token"]))
        error = None
        if request.method == "POST":
            start = business.parse_ts(request.form.get("slot"))
            valid = {business.ts(s) for s in business.available_slots(limit=60, spread=False)}
            if start and business.ts(start) in valid:
                appt = auto.book_appointment(lead, start)
                return redirect(url_for("manage_appointment", token=appt["token"]))
            error = "That time was just taken — please pick another."
        slots = business.available_slots(limit=12, spread=False)
        return render_template("public/book.html", lead=lead, slots=slots, error=error)

    # ---- Twilio webhooks: SMS / WhatsApp -----------------------------------

    @app.post("/webhooks/twilio/message")
    @twilio_required
    def twilio_message():
        sender = request.form.get("From", "")
        body = request.form.get("Body", "")
        channel = "whatsapp" if sender.startswith("whatsapp:") else "sms"
        if (request.form.get("ProfileName") or "") and channel == "whatsapp":
            profile = request.form.get("ProfileName")
        else:
            profile = None

        def work():
            lead, _ = auto.find_or_create_lead(phone=sender, source=f"{channel}_inbound", channel=channel)
            if profile and not lead.get("name"):
                db.update("leads", lead["id"], {"name": profile})
                lead = auto.get_lead(lead["id"])
            auto.handle_inbound(lead, channel, body)

        background(app, work)
        return twiml()

    # ---- Twilio webhooks: voice (AI sales agent + missed-call text-back) ---

    @app.post("/webhooks/twilio/voice")
    @twilio_required
    def twilio_voice():
        caller = request.form.get("From", "")
        lead, _ = auto.find_or_create_lead(phone=caller, source="phone_call", channel="sms")
        forward = os.environ.get("TEAM_FORWARD_NUMBER")
        if forward and business.is_open():
            action = url_for("twilio_dial_status")
            return twiml(f'<Dial timeout="18" action="{action}" method="POST">{escape(forward)}</Dial>')
        greeting = (f"Hi, thanks for calling {business.setting('business_name')}. "
                    f"This is {business.setting('agent_name')}, the virtual assistant. How can I help you today?")
        if lead.get("name"):
            greeting = (f"Hi {business.first_name(lead['name'])}, thanks for calling {business.setting('business_name')}. "
                        f"This is {business.setting('agent_name')}. How can I help today?")
        db.insert("messages", {"lead_id": lead["id"], "direction": "out", "channel": "voice", "sender": "ai",
                               "body": greeting, "status": "sent", "created_at": business.ts()})
        return twiml(gather(url_for("twilio_voice_turn"), greeting), _voice_no_input())

    def _voice_no_input():
        return say("Sorry, I didn't catch that. I'll send you a text so we can help you there. Goodbye!") + \
            f'<Redirect method="POST">{escape(url_for("twilio_voice_hangup"))}</Redirect>'

    @app.post("/webhooks/twilio/voice/dial-status")
    @twilio_required
    def twilio_dial_status():
        status = request.form.get("DialCallStatus", "")
        if status in ("completed", "answered"):
            return twiml("<Hangup/>")
        caller = request.form.get("From", "")
        background(app, auto.missed_call, caller)
        prompt = (f"Sorry, our team is with other customers right now. I'm {business.setting('agent_name')}, "
                  f"the virtual assistant, and I can help you get booked in. What do you need help with?")
        return twiml(gather(url_for("twilio_voice_turn"), prompt), _voice_no_input())

    @app.post("/webhooks/twilio/voice/turn")
    @twilio_required
    def twilio_voice_turn():
        caller = request.form.get("From", "")
        speech = (request.form.get("SpeechResult") or "").strip()
        lead, _ = auto.find_or_create_lead(phone=caller, source="phone_call", channel="sms")
        if not speech:
            return twiml(_voice_no_input())
        reply = auto.handle_inbound(lead, "voice", speech, reply_channel="voice")
        lead = auto.get_lead(lead["id"])
        forward = os.environ.get("TEAM_FORWARD_NUMBER")
        if lead["ai_paused"]:
            parts = [say(reply or "Let me connect you with our team.")]
            if forward and business.is_open():
                parts.append(f"<Dial timeout=\"20\">{escape(forward)}</Dial>")
            else:
                parts.append(say("Someone from our team will call you back shortly. Goodbye!"))
                parts.append("<Hangup/>")
            return twiml(*parts)
        if lead["opted_out"]:
            return twiml(say(reply or "Okay, goodbye."), "<Hangup/>")
        return twiml(gather(url_for("twilio_voice_turn"), reply or "Could you say that again?"),
                     say("Thanks for calling. We'll follow up by text. Goodbye!"), "<Hangup/>")

    @app.post("/webhooks/twilio/voice/hangup")
    @twilio_required
    def twilio_voice_hangup():
        caller = request.form.get("From", "")
        lead = auto.find_lead(phone=caller)
        spoke = lead and db.scalar(
            "SELECT COUNT(*) FROM messages WHERE lead_id = ? AND channel = 'voice' AND direction = 'in'", (lead["id"],))
        if not spoke:
            background(app, auto.missed_call, caller)
        return twiml("<Hangup/>")

    # ---- generic webhooks (other phone systems, inbound email) ------------

    @app.post("/webhooks/missed-call")
    @api_key_required
    def generic_missed_call():
        data = request.get_json(silent=True) or request.form.to_dict()
        phone = data.get("from") or data.get("phone") or data.get("caller")
        if not phone:
            return jsonify({"error": "from is required"}), 400
        lead = auto.missed_call(phone)
        return jsonify({"lead_id": lead["id"]})

    @app.post("/webhooks/email")
    @api_key_required
    def inbound_email():
        data = request.get_json(silent=True) or request.form.to_dict()
        sender = data.get("from") or ""
        if "<" in sender:
            sender = sender.split("<")[-1].rstrip(">")
        text = data.get("text") or data.get("body") or ""
        if not sender or not text:
            return jsonify({"error": "from and text are required"}), 400
        lead, _ = auto.find_or_create_lead(email=sender, source="email", channel="email")
        background(app, auto.handle_inbound, lead, "email", text)
        return jsonify({"lead_id": lead["id"]})

    @app.errorhandler(404)
    def not_found(_):
        return render_template("error.html", code=404, message="We couldn't find that page."), 404


app = create_app() if os.environ.get("LEADPILOT_NO_AUTOAPP") != "1" else None

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5050"))
    app.run(host="0.0.0.0", port=port, debug=os.environ.get("FLASK_DEBUG") == "1", use_reloader=False)
