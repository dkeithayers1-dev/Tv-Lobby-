# Best Fit Senior Placement

Server-backed placement CRM for one firm. Prospects, care notes, stages, tours, invoices, and backups live in SQLite. Sign-in is checked on the server. Passwords are hashed. Care notes are encrypted. Backups are an encrypted file, not JSON.

The simulated marketing modules from the old HTML prototype are not in this app.

## Run

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Fill in:

- `BESTFIT_SECRET_KEY` — any long random string
- `BESTFIT_DATA_KEY` — a Fernet key from `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`
- `BESTFIT_ADMIN_EMAIL` and `BESTFIT_ADMIN_PASSWORD` (12 characters or more)

There is no default administrator password. Keep the data key somewhere other than the database file. Losing it means encrypted notes and backups cannot be opened.

```bash
python -m bestfit
```

The app listens on `127.0.0.1:5050`. For a real deployment, run it behind HTTPS with `BESTFIT_COOKIE_SECURE=1` and a production WSGI server:

```bash
gunicorn -w 1 -b 127.0.0.1:5050 "bestfit:create_app()"
```

Use one worker. SQLite is the database (`instance/bestfit.sqlite`).

Email uses the same SMTP variables as the expense report. If they are empty, Best Fit will not pretend a message was sent. Intake email also requires the family's email consent.

Set `BESTFIT_NOMINATIM=1` only if street rooftops should be resolved on the server. Otherwise a town or ZIP uses a built-in center and the page says so. Distances are straight-line.

Before the first family PDF or invoice, save the business phone, email, website, and flat referral fee in Settings.

## Tests

```bash
pytest tests/test_bestfit.py tests/test_receipt_parser.py
```
