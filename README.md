# Receipt → Expense Report

A small mobile-friendly web app: snap a photo of a receipt (or upload a PDF)
from your phone, it reads the vendor/date/amount, lets you confirm or edit
the details, adds it as a new line to your Expense Reimbursement Form
(`.xlsx`), and emails you the updated report automatically.

The included template (`data/expense_report_template.xlsx`) is your own
expense form — the app fills in new rows in the same layout (Date,
Description, From/To/Mileage, Amount) without touching its formatting or
the `Total Reimbursement` formula.

## How it works

1. Open the app on your phone's browser and take a photo of a receipt, or
   upload a PDF (e.g. an emailed Uber/Lyft receipt).
2. The server extracts text from the PDF, or runs OCR on the photo, and
   guesses the vendor, date, description, and amount (with special handling
   for Uber-style receipts: it also captures mileage and the pickup/dropoff
   addresses).
3. You review/correct the extracted fields in the browser before anything
   is saved.
4. Tapping **Add to Expense Report & Email It** appends a row to your
   report and emails you the updated `.xlsx` as an attachment.
5. Visit `/report` any time to see all expenses logged so far, download the
   spreadsheet, or resend it by email.

## Setup

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

Photo receipts are read with Tesseract OCR, which needs the system binary
installed separately (PDF receipts don't need this):

```bash
# Debian/Ubuntu
sudo apt-get install tesseract-ocr

# macOS
brew install tesseract
```

### Email

Copy `.env.example` to `.env` and fill in your SMTP details:

```bash
cp .env.example .env
```

For Gmail: turn on 2-Step Verification, then create an **App Password** at
<https://myaccount.google.com/apppasswords> and use it as `SMTP_PASSWORD`
(not your normal Gmail password). Set `MAIL_TO` to the address(es) that
should receive the updated report (comma-separated for more than one).

If `.env` isn't set up, the app still works — it just tells you the email
wasn't sent instead of failing the whole request.

#### If a recipient never seems to receive the report

A successful send from this app only means the report left your SMTP
server — it doesn't guarantee the recipient's mail server accepted it into
their inbox. If someone consistently doesn't receive the report:

- Have them check their **spam/junk folder** first.
- If the address is at a company (e.g. a corporate domain rather than
  Gmail/Outlook/etc.), their employer likely runs an email security
  gateway (Proofpoint, Mimecast, Microsoft Defender for Office 365, and
  similar are common). These frequently **quarantine mail from unfamiliar
  external senders silently — with no bounce sent back to you** — especially
  when it carries an attachment. There's often a quarantine digest email or
  a web portal their IT team can point them to, and they can ask IT to
  allowlist the sending address.
- Check the sending mailbox for a bounce-back (an email from
  `mailer-daemon` or `postmaster`, subject like "Undeliverable" or "Mail
  Delivery Failure"). If there isn't one, the message was almost certainly
  accepted by the recipient's server, which points at spam/quarantine
  rather than a bad address or a bug in this app.

## Running it

```bash
python app.py
```

By default this serves on `http://0.0.0.0:5000`. To use it from your phone,
run it on a computer on the same Wi-Fi network and visit
`http://<that computer's LAN IP>:5000` from your phone's browser, or deploy
it to any small always-on host (a Raspberry Pi, an old laptop, or a free
tier on Render/Railway/Fly.io) so it's reachable from anywhere. For a real
deployment, run it behind a production WSGI server (e.g. `gunicorn app:app`)
and put it behind HTTPS, since receipts and email credentials pass through it.

## Starting a new report period

The working copy of your report lives at `instance/expense_report.xlsx`
(created automatically from the template on first use, and never committed
to git). To start a fresh report, delete that file — the app will
recreate it from the blank template on the next expense you add. There's
room for 18 expense rows (rows 13–30) per report before you need to start
a new one.

## Running the tests

```bash
pip install pytest
pytest tests/
```
