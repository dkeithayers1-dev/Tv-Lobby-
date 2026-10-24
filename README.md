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

## Employee schedule

Open `/schedule` for a monthly shift grid:

- **Top row:** every day of the selected month (weekends are shaded).
- **First column (from row 2 down):** employee names, 35 to start
  ("Employee 1"–"Employee 35"). Click a name to rename it. Add or remove
  employees, and enter their mobile numbers, under **Employees & phone
  numbers** below the grid.
- **Cells:** pick a shift: **7a-3p**, **3p-11p**, **11p-7a** or **Off**. You can
  also click a cell and type `7`, `3`, `1` or `O`, then press Tab to move
  to the next day.
- **Bottom rows:** total scheduled hours for each day (8 hours per shift),
  plus how many people are on each shift that day. The right-hand
  **Hours** column shows each employee's total for the month.

### Administrator login

Anyone who opens `/schedule` can **view** the schedule and print it. Only
an administrator can change it: after signing in with **Admin login** (top
right), the grid becomes editable and the Save, texting and employee/phone
controls appear. Employees never see those controls or anyone's phone
numbers, and the server refuses changes from anyone who isn't logged in as
the administrator.

Set the admin login and a secret key in `.env`:

```
ADMIN_USERNAME=admin
ADMIN_PASSWORD=choose-a-strong-password
FLASK_SECRET_KEY=<long random value>
```

Generate the secret key with
`python -c "import secrets; print(secrets.token_hex(32))"`. It signs the
login cookie, so keep it private. If it isn't set, the app picks a random
one each time it starts, which still keeps logins safe, but logs the admin
out whenever the app restarts. Until `ADMIN_PASSWORD` is set, nobody can
log in, so the schedule stays view-only. If the app is reachable from the
internet, run it behind HTTPS so the password isn't sent in the clear.

As administrator, press **Save** to keep your changes. Schedules are saved per month under
`instance/schedule/`, which is never committed to git, since the files
include phone numbers. **Print** gives a landscape copy that fits one page.

### Downloading the schedule to a phone

**Download PDF** (next to Print) saves the month as a one-page PDF, laid
out like the grid: days across the top, one employee per row, and the
daily totals at the bottom. It's on the view-only page too, so employees
can download it themselves without logging in. The PDF never includes
phone numbers. Shifts are shortened to fit: **7-3** = 7a-3p,
**3-11** = 3p-11p, **11-7** = 11p-7a.

On an iPhone, open the PDF and tap Share, then **Save to Files** (or
**Books**). On Android, it lands in the Downloads folder. Employees can
open it any time without signal, but it won't change on its own: if the
schedule changes, they need to download it again.

If `PUBLIC_URL` is set in `.env` (the address employees use to reach the
app), each schedule text ends with a link to the PDF, so employees can
download it straight from the message.

### Texting schedules with Twilio

**Text everyone their schedule** (or the **Text** button next to one
employee) sends each employee with a phone number a message like:

```
Jane Smith, your November 2026 schedule (40 hrs):
11/1 7a-3p
11/2 7a-3p
...
```

To turn it on, add these to `.env` from your Twilio Console
(<https://console.twilio.com>):

```
TWILIO_ACCOUNT_SID=AC...
TWILIO_AUTH_TOKEN=...
TWILIO_FROM_NUMBER=+14805551234
```

US numbers can be typed any way, e.g. `(480) 555-1234`. Put a `+` and the
country code in front of numbers outside the US. Note that Twilio requires
US senders to complete **A2P 10DLC registration** (or use a verified
toll-free number) before texts are delivered reliably, and a trial account
can only text numbers you've verified in the console.

## Running the tests

```bash
pip install pytest
pytest tests/
```
