# LeadPilot — AI lead response, booking & follow-up

A working web app that implements the AI automation systems from the
"Speed-to-Lead" carousel for a local service business:

| # | System | What it does in this app |
|---|--------|--------------------------|
| 1 | **Speed-to-lead AI** | Web form, website chat, lead-ads API, SMS, WhatsApp and calls all create a lead and get a reply **within seconds**, 24/7. The AI collects name, service and timeline, then offers 3 open times. |
| 2 | **Appointment reminders** | Booking sends a confirmation, then reminders (24h and 2h before, configurable). Every message has a self-service link to **confirm, cancel or reschedule**. Customers can also just text `C` or `R`. |
| 4 | **CRM lead intelligence** | Dashboard with new inquiries, deal stages with value, **overdue follow-ups**, speed-to-lead and show-rate metrics, alerts, and a drag-and-drop pipeline. |
| 5 | **AI sales agent** | The same agent answers phone calls (Twilio voice) after hours or when your team doesn't pick up: it qualifies, answers common questions and **books straight onto your calendar**, then texts a confirmation. |
| 6 | **Follow-up + nurture sequences** | Multi-step SMS / WhatsApp / email sequences. Moving a deal to *Quote sent* starts one automatically; **any reply stops it and alerts your team.** |
| 7 | **WhatsApp Q&A + text-back** | Answers FAQs from your knowledge base on WhatsApp/SMS, **texts back missed callers**, and hands complex requests (complaints, refunds, emergencies, "talk to a person") to a human. |

(The carousel's slide #3 wasn't in the screenshots, so it isn't modeled separately.)

## Quick start

```bash
cd lead-engine
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env           # optional: fill in keys (see below)
python demo_data.py            # optional: sample leads so the dashboard isn't empty
python app.py                  # http://localhost:5050
```

Everything works **without any accounts**: messages to customers are logged
as *simulated*, and the AI falls back to a built-in rules engine. Open
**Test console** in the sidebar to chat as a customer over SMS, WhatsApp,
phone or email, simulate a missed call, and fast-forward scheduled reminders
and sequence steps.

Run the tests with `python -m pytest tests`.

## Going live

| Feature | Set in `.env` | Then |
|---|---|---|
| Claude AI brain | `ANTHROPIC_API_KEY` | Nothing else. Default model `claude-opus-5-5` at low effort for fast replies; override with `ANTHROPIC_MODEL`. Falls back to the rules engine if the API is unreachable. |
| SMS | `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_SMS_FROM` | In Twilio, set the number's *A message comes in* webhook to `https://YOUR-HOST/webhooks/twilio/message` |
| WhatsApp | `TWILIO_WHATSAPP_FROM` | Point the WhatsApp sender's inbound webhook at the same URL |
| AI phone agent | Twilio keys (+ optional `TEAM_FORWARD_NUMBER`) | Set the number's *A call comes in* webhook to `https://YOUR-HOST/webhooks/twilio/voice`. During business hours the call rings `TEAM_FORWARD_NUMBER` first; if no one answers, the caller gets a text-back and the AI picks up. After hours the AI answers directly. |
| Email | `SMTP_*` | Inbound email replies: POST `{from, text}` to `/webhooks/email` (e.g. from your email provider's inbound parse) with `X-API-Key` |
| Lead ads / Zapier | `LEAD_API_KEY` | `POST /api/v1/leads` JSON `{name, phone, email, service, timeline, message, value, source}` with header `X-API-Key` |
| Other phone systems | `LEAD_API_KEY` | `POST /webhooks/missed-call` `{from}` to trigger text-back |
| Team alerts | `TEAM_ALERT_PHONE`, `TEAM_ALERT_EMAIL` | Get pinged for new leads, prospect replies, and AI handoffs |

Also set `ADMIN_PASSWORD` (the dashboard is open without it),
`FLASK_SECRET_KEY`, and `PUBLIC_BASE_URL` so links in texts point at your
public domain. Twilio webhook signatures are verified whenever
`TWILIO_AUTH_TOKEN` is set.

Production: `gunicorn -w 1 --threads 8 -b 0.0.0.0:5050 app:app`. Use a single
worker process — the background scheduler (reminders, sequences) runs inside
it; jobs are claimed atomically so a second process won't double-send.

Embed on your site (URLs are shown on the Settings page):

```html
<iframe src="https://YOUR-HOST/f/contact" style="border:0;width:100%;height:640px"></iframe>
<iframe src="https://YOUR-HOST/chat?embed=1" style="border:0;width:380px;height:560px"></iframe>
```

## Customizing

**AI agent & settings** holds the business profile, services, hours,
appointment length, reminder timing, the missed-call text, and the FAQ
knowledge base. The AI only states facts from there and hands off when it
doesn't know. **Follow-up sequences** lets you edit steps, delays, channels
and messages, with placeholders like `{first_name}`, `{service}` and
`{booking_link}`.

## How it's built

- `app.py`: Flask routes, Twilio/webhook endpoints, background scheduler
- `automations.py`: the workflows (speed-to-lead, booking, reminders, sequences, text-back, handoff)
- `ai.py`: Claude structured-output agent plus the offline rules engine
- `messaging.py`: Twilio SMS/WhatsApp and SMTP delivery, logged per lead
- `business.py`: settings, business hours, slot finder, templates
- `db.py`: SQLite schema, stored in `instance/leadpilot.db`

Opt-outs: `STOP` unsubscribes a contact and blocks all automated messages
until they text `START`. Check your local SMS/WhatsApp consent rules (e.g.
A2P 10DLC registration in the US, WhatsApp template rules for messages sent
outside the 24-hour window) before running sequences at scale.
