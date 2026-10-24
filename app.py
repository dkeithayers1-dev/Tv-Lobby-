import os
import secrets
import uuid
from datetime import date, datetime
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, jsonify, request, render_template, send_file, abort, redirect, url_for

load_dotenv()

import auth
import emailer
import expense_report
import receipt_parser
import scheduler
import sms

BASE_DIR = Path(__file__).resolve().parent
UPLOAD_DIR = BASE_DIR / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)

ALLOWED_EXTS = receipt_parser.IMAGE_EXTS | receipt_parser.PDF_EXTS
MAX_UPLOAD_MB = 20

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_MB * 1024 * 1024
# The secret key signs the admin login cookie. Without one set in .env, use a
# random key so nobody can forge a login (logins then reset on restart).
app.secret_key = os.environ.get("FLASK_SECRET_KEY") or secrets.token_hex(32)
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_HTTPONLY"] = True

# temp_id -> saved file path, cleared once committed or replaced
PENDING_UPLOADS = {}


@app.route("/")
def index():
    return render_template("index.html", email_configured=emailer.is_configured())


@app.route("/api/parse", methods=["POST"])
def api_parse():
    if "file" not in request.files:
        return jsonify({"error": "No file uploaded"}), 400
    upload = request.files["file"]
    if not upload.filename:
        return jsonify({"error": "No file selected"}), 400

    ext = Path(upload.filename).suffix.lower()
    if ext not in ALLOWED_EXTS:
        return jsonify({"error": f"Unsupported file type: {ext or 'unknown'}"}), 400

    temp_id = uuid.uuid4().hex
    saved_path = UPLOAD_DIR / f"{temp_id}{ext}"
    upload.save(saved_path)
    PENDING_UPLOADS[temp_id] = saved_path

    try:
        parsed = receipt_parser.parse_receipt(str(saved_path))
    except Exception as exc:
        saved_path.unlink(missing_ok=True)
        PENDING_UPLOADS.pop(temp_id, None)
        return jsonify({"error": f"Could not read receipt: {exc}"}), 422

    parsed["id"] = temp_id
    parsed["original_filename"] = upload.filename
    return jsonify(parsed)


@app.route("/api/commit", methods=["POST"])
def api_commit():
    payload = request.get_json(force=True, silent=True) or {}
    entries = payload.get("entries", [])
    if not entries:
        return jsonify({"error": "No entries to add"}), 400

    added = []
    errors = []
    for entry in entries:
        try:
            entry_date = datetime.strptime(entry["date"], "%Y-%m-%d").date() if entry.get("date") else date.today()
            amount = float(entry["amount"])
            row = expense_report.add_expense(
                date=entry_date,
                description=entry.get("description") or "Expense",
                amount=amount,
                from_addr=entry.get("from_addr") or None,
                to_addr=entry.get("to_addr") or None,
                mileage=float(entry["mileage"]) if entry.get("mileage") else None,
            )
            added.append(row)
        except expense_report.ReportFullError as exc:
            errors.append(str(exc))
            break
        except (KeyError, ValueError) as exc:
            errors.append(f"Skipped an entry: {exc}")
        finally:
            temp_id = entry.get("id")
            saved_path = PENDING_UPLOADS.pop(temp_id, None)
            if saved_path:
                Path(saved_path).unlink(missing_ok=True)

    emailed = False
    email_error = None
    if added:
        try:
            summary = expense_report.list_expenses()
            emailer.send_report_email(
                attachment_path=expense_report.REPORT_PATH,
                subject="Updated Expense Report",
                body=(
                    f"Attached is the updated expense report for {summary['name'] or 'you'}.\n"
                    f"{len(added)} new expense(s) added just now.\n"
                    f"Running total: ${summary['total']:.2f}"
                ),
            )
            emailed = True
        except emailer.EmailNotConfigured as exc:
            email_error = str(exc)
        except Exception as exc:
            email_error = f"Failed to send email: {exc}"

    return jsonify({
        "added_rows": added,
        "errors": errors,
        "emailed": emailed,
        "email_error": email_error,
    })


@app.route("/report")
def report_view():
    summary = expense_report.list_expenses()
    return render_template("report.html", summary=summary, email_configured=emailer.is_configured())


@app.route("/report/download")
def report_download():
    expense_report.ensure_report_exists()
    return send_file(
        expense_report.REPORT_PATH,
        as_attachment=True,
        download_name="expense_report.xlsx",
    )


@app.route("/report/send", methods=["POST"])
def report_send():
    try:
        summary = expense_report.list_expenses()
        emailer.send_report_email(
            attachment_path=expense_report.REPORT_PATH,
            subject="Expense Report",
            body=f"Attached is your current expense report. Running total: ${summary['total']:.2f}",
        )
        return jsonify({"emailed": True})
    except emailer.EmailNotConfigured as exc:
        return jsonify({"emailed": False, "error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"emailed": False, "error": str(exc)}), 500


def _safe_next(target):
    # Only redirect back to a page on this site after logging in.
    if target and target.startswith("/") and not target.startswith("//"):
        return target
    return url_for("schedule_view")


@app.route("/login", methods=["GET", "POST"])
def login():
    next_url = _safe_next(request.values.get("next"))
    error = None
    if request.method == "POST":
        if auth.check_credentials(request.form.get("username"), request.form.get("password")):
            auth.log_in()
            return redirect(next_url)
        error = "Wrong username or password."
    return render_template(
        "login.html",
        error=error,
        next_url=next_url,
        auth_configured=auth.is_configured(),
    )


@app.route("/logout", methods=["POST"])
def logout():
    auth.log_out()
    return redirect(_safe_next(request.form.get("next")))


@app.route("/schedule")
def schedule_view():
    return render_template(
        "schedule.html",
        month=request.args.get("month") or date.today().strftime("%Y-%m"),
        shifts=scheduler.SHIFTS,
        sms_configured=sms.is_configured(),
        is_admin=auth.is_admin(),
    )


@app.route("/api/schedule")
def api_schedule_get():
    month = request.args.get("month") or date.today().strftime("%Y-%m")
    try:
        days = scheduler.day_headers(month)
        shifts = scheduler.load_shifts(month)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    employees = scheduler.load_roster()
    if not auth.is_admin():
        # Employees can see the schedule, but not each other's phone numbers.
        employees = [{"id": e["id"], "name": e["name"]} for e in employees]
    return jsonify({
        "month": month,
        "days": days,
        "employees": employees,
        "shifts": shifts,
        "is_admin": auth.is_admin(),
    })


@app.route("/api/schedule", methods=["POST"])
@auth.admin_required
def api_schedule_save():
    payload = request.get_json(force=True, silent=True) or {}
    month = payload.get("month") or ""
    try:
        employees = scheduler.save_roster(payload.get("employees") or [])
        shifts = scheduler.save_shifts(month, payload.get("shifts") or {})
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({
        "saved": True,
        "totals": scheduler.daily_totals(month, employees, shifts),
    })


@app.route("/api/schedule/text", methods=["POST"])
@auth.admin_required
def api_schedule_text():
    """Text each selected employee their saved shifts for the month."""
    payload = request.get_json(force=True, silent=True) or {}
    month = payload.get("month") or ""
    wanted = set(payload.get("employee_ids") or [])
    try:
        shifts = scheduler.load_shifts(month)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    if not sms.is_configured():
        return jsonify({"error": "Twilio is not configured. See README."}), 400

    sent, skipped, errors = [], [], []
    for emp in scheduler.load_roster():
        if wanted and emp["id"] not in wanted:
            continue
        if not emp["phone"]:
            skipped.append(f"{emp['name']} (no phone)")
            continue
        body = scheduler.schedule_text(month, emp, shifts.get(emp["id"]))
        try:
            sms.send_sms(emp["phone"], body)
            sent.append(emp["name"])
        except Exception as exc:
            errors.append(f"{emp['name']}: {exc}")

    return jsonify({"sent": sent, "skipped": skipped, "errors": errors})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=True)
