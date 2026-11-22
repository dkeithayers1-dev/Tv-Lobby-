"""Load realistic sample data so the dashboard isn't empty on first run.

    python demo_data.py          # adds sample leads to the configured database
"""
import os
from datetime import timedelta

os.environ.setdefault("LEADPILOT_NO_AUTOAPP", "1")
os.environ.setdefault("AI_PROVIDER", "rules")  # deterministic, no API spend

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

import app as app_module  # noqa: E402
import automations as auto  # noqa: E402
import business  # noqa: E402
import db  # noqa: E402


def back(lead_id, hours):
    """Shift a lead's timestamps into the past so the data looks lived-in."""
    t = business.ts(business.now() - timedelta(hours=hours))
    db.execute("UPDATE leads SET created_at = ?, first_response_at = CASE WHEN first_response_at IS NULL THEN NULL ELSE ? END WHERE id = ?",
               (t, t, lead_id))


def main():
    app_module.create_app({"RUN_SCHEDULER": False})

    # Fresh web-form inquiry, answered instantly with time slots
    lead = auto.new_lead({"name": "Jordan Miles", "phone": "+15550100001", "service": "AC repair",
                          "timeline": "ASAP", "message": "AC blowing warm air since this morning", "value": 450},
                         source="web_form")
    back(lead["id"], 1)

    # SMS conversation that qualified and booked
    sam, _ = auto.find_or_create_lead(phone="+15550100002", source="sms_inbound", channel="sms")
    for text in ["Hi, my name is Sam Patel", "my furnace is making a loud noise", "this week if possible", "2"]:
        auto.handle_inbound(sam, "sms", text)
    db.update("leads", sam["id"], {"value": 650})
    back(sam["id"], 20)

    # Quote sent -> nurture sequence running
    kim = auto.create_lead({"name": "Kim Alvarez", "phone": "+15550100003", "email": "kim@example.com",
                            "service": "New system install", "timeline": "Within a month", "value": 8900},
                           "google_ads")
    auto.set_stage(kim, "contacted")
    auto.set_stage(auto.get_lead(kim["id"]), "quoted")
    back(kim["id"], 72)

    # WhatsApp FAQ + handoff
    maria, _ = auto.find_or_create_lead(phone="+15550100004", source="whatsapp_inbound", channel="whatsapp")
    db.update("leads", maria["id"], {"name": "Maria Gomez"})
    auto.handle_inbound(auto.get_lead(maria["id"]), "whatsapp", "Do you offer financing?")
    auto.handle_inbound(auto.get_lead(maria["id"]), "whatsapp", "I want to talk to a real person about a warranty claim")

    # Missed call text-back
    auto.missed_call("+15550100005")

    # Overdue lead that went quiet
    quiet = auto.create_lead({"name": "Chris Doyle", "phone": "+15550100006", "service": "Maintenance plan",
                              "value": 240}, "facebook_lead_ad")
    auto.set_stage(quiet, "contacted")
    db.update("leads", quiet["id"], {"next_follow_up_at": business.ts(business.now() - timedelta(hours=26)),
                                     "last_contact_at": business.ts(business.now() - timedelta(hours=74))})
    back(quiet["id"], 80)

    # Closed deals
    for name, stage, value in [("Taylor Brooks", "won", 7200), ("Pat Nguyen", "lost", 3100)]:
        l = auto.create_lead({"name": name, "phone": f"+1555010{value:04d}", "service": "New system install",
                              "value": value}, "referral")
        auto.set_stage(l, stage)

    print(f"Loaded demo data into {db._DB_PATH}")


if __name__ == "__main__":
    main()
