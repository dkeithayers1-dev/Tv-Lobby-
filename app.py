import os
import uuid
from datetime import date, datetime
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, jsonify, request, render_template, send_file, abort

load_dotenv()

import emailer
import expense_report
import receipt_parser

BASE_DIR = Path(__file__).resolve().parent
UPLOAD_DIR = BASE_DIR / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)

ALLOWED_EXTS = receipt_parser.IMAGE_EXTS | receipt_parser.PDF_EXTS
MAX_UPLOAD_MB = 20

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_MB * 1024 * 1024
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "dev-only-secret")

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


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=True)
