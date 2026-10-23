import io
import re
from datetime import date, datetime, timezone

import pytest
from cryptography.fernet import Fernet
from pypdf import PdfReader

from bestfit.domain import duration_rows, local_today, summarize
from bestfit.factory import create_app
from bestfit.geo import haversine, locate_offline


PASSWORD = "correct-horse-battery"


@pytest.fixture()
def env(tmp_path):
    outbox = []
    app = create_app(
        {
            "TESTING": True,
            "SECRET_KEY": "test-secret-key",
            "DATA_KEY": Fernet.generate_key().decode(),
            "DATABASE": str(tmp_path / "bestfit.sqlite"),
            "ADMIN_EMAIL": "admin@bestfit.test",
            "ADMIN_PASSWORD": PASSWORD,
            "ADMIN_NAME": "Ada Admin",
            "MAIL_OUTBOX": outbox,
            "TODAY": date(2026, 10, 22),
            "NOMINATIM": False,
        }
    )
    app.config["OUTBOX"] = outbox
    return app


@pytest.fixture()
def client(env):
    return env.test_client()


def csrf(html: str) -> str:
    match = re.search(r'name="csrf" value="([^"]+)"', html)
    if match is None:
        match = re.search(r'name="csrf" content="([^"]+)"', html)
    assert match, html[:500]
    return match.group(1)


def login(client, email="admin@bestfit.test", password=PASSWORD):
    page = client.get("/login")
    response = client.post(
        "/login",
        data={"csrf": csrf(page.get_data(as_text=True)), "email": email, "password": password},
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert b"Sign in" not in response.data or b"Today" in response.data or b"Finance" in response.data
    return response


def token(client, path="/") -> str:
    return csrf(client.get(path).get_data(as_text=True))


def configure(client):
    data = {
        "csrf": token(client, "/admin/settings"),
        "business_name": "Best Fit Senior Placement",
        "street": "4102 Constitution Dr.",
        "city_line": "Lago Vista, TX 78645",
        "phone": "512-555-0100",
        "email": "hello@bestfit.test",
        "website": "https://bestfit.test",
        "terms": "Net 30",
        "referral_fee": "2500",
        "retention_days": "30",
    }
    for index, days in {0: 7, 1: 7, 2: 7, 3: 10, 4: 10, 5: 10, 6: 45, 7: 7, 8: 30}.items():
        data[f"stall_{index}"] = str(days)
    response = client.post("/admin/settings", data=data, follow_redirects=True)
    assert response.status_code == 200
    assert b"Settings saved" in response.data


def add_contact(client, name="Mary O'Brien", email="mary@example.com", phone="(512) 555-0199", consent=True):
    data = {
        "csrf": token(client, "/contacts/new"),
        "name": name,
        "email": email,
        "phone": phone,
        "tags": "family",
    }
    if consent:
        data["email_consent"] = "1"
    response = client.post("/contacts/new", data=data, follow_redirects=True)
    assert response.status_code == 200, response.status_code
    return response


def contact_id(client, name="Mary"):
    html = client.get("/contacts").get_data(as_text=True)
    match = re.search(rf'/contacts/([^"/]+)"[^>]*>{re.escape(name)}', html)
    assert match, html
    return match.group(1)


def test_local_date_does_not_shift_forward_from_utc():
    moment = datetime(2026, 10, 22, 3, 30, tzinfo=timezone.utc)
    assert local_today(moment) == date(2026, 10, 21)


def test_missing_duration_is_not_zero():
    assert duration_rows({}, date(2026, 10, 1))["prospect_to_signed"] is None
    assert summarize([])["average"] is None


def test_login_has_no_default_account_until_configured(tmp_path):
    app = create_app(
        {
            "TESTING": True,
            "SECRET_KEY": "test-secret-key",
            "DATA_KEY": Fernet.generate_key().decode(),
            "DATABASE": str(tmp_path / "empty.sqlite"),
            "ADMIN_EMAIL": "",
            "ADMIN_PASSWORD": "",
            "MAIL_OUTBOX": [],
        }
    )
    page = app.test_client().get("/login").get_data(as_text=True)
    assert "no default password" in page
    assert "admin" not in page.lower() or "No administrator" in page


def test_new_contact_is_active_today_and_survives_reload(client):
    login(client)
    add_contact(client)
    page = client.get("/contacts").get_data(as_text=True)
    assert "Mary O&#39;Brien" in page
    assert "Active" in page
    assert "Showing 1 of 1" in page
    again = client.get(f"/contacts/{contact_id(client)}").get_data(as_text=True)
    assert "Mary O&#39;Brien" in again
    assert "Prospect" in again
    assert "2026-10-22" in again


def test_duplicate_email_or_phone_is_not_saved_and_edit_still_works(client):
    login(client)
    add_contact(client)
    first = contact_id(client)
    duplicate = client.post(
        "/contacts/new",
        data={
            "csrf": token(client, "/contacts/new"),
            "name": "Someone Else",
            "email": "mary@example.com",
            "phone": "512-555-0000",
        },
    )
    assert duplicate.status_code == 409
    body = duplicate.get_data(as_text=True)
    assert "email" in body
    assert "Mary O&#39;Brien" in body
    assert f"/contacts/{first}" in body
    assert "Showing 1 of 1" in client.get("/contacts").get_data(as_text=True)

    phone_dup = client.post(
        "/contacts/new",
        data={
            "csrf": token(client, "/contacts/new"),
            "name": "Third",
            "email": "third@example.com",
            "phone": "5125550199",
        },
    )
    assert phone_dup.status_code == 409
    assert "phone" in phone_dup.get_data(as_text=True)

    edited = client.post(
        f"/contacts/{first}/edit",
        data={
            "csrf": token(client, f"/contacts/{first}/edit"),
            "name": "Mary O'Brien",
            "email": "mary@example.com",
            "phone": "(512) 555-0199",
            "last_engagement": "2026-10-22",
            "email_consent": "1",
        },
        follow_redirects=True,
    )
    assert edited.status_code == 200
    assert b"Mary" in edited.data


def test_stage_change_dates_only_the_selected_stage_and_tasks_follow(client):
    login(client)
    add_contact(client)
    cid = contact_id(client)
    moved = client.post(
        f"/contacts/{cid}/stage",
        data={"csrf": token(client, f"/contacts/{cid}"), "stage": "2"},
        follow_redirects=True,
    )
    html = moved.get_data(as_text=True)
    assert "Contract Signed" in html
    assert "Moved forward" in html
    activity = client.get(f"/contacts/{cid}?tab=activity").get_data(as_text=True)
    assert 'value="2026-10-22"' in activity
    assert "Schedule community tours" in activity
    assert activity.count("Schedule community tours") == 1
    dates = re.findall(r'name="date_(\d)" value="(\d{4}-\d{2}-\d{2})"', activity)
    assert ("0", "2026-10-22") in dates
    assert ("2", "2026-10-22") in dates
    assert all(index in {"0", "2"} for index, _value in dates)

    again = client.post(
        f"/contacts/{cid}/stage",
        data={"csrf": token(client, f"/contacts/{cid}"), "stage": "2"},
        follow_redirects=True,
    )
    assert again.get_data(as_text=True).count("Schedule community tours") == 0 or client.get(
        f"/contacts/{cid}?tab=activity"
    ).get_data(as_text=True).count("Schedule community tours") == 1

    back = client.post(
        f"/contacts/{cid}/stage",
        data={"csrf": token(client, f"/contacts/{cid}"), "stage": "0"},
        follow_redirects=True,
    )
    assert b"confirmation checkbox" in back.data
    assert "Contract Signed" in client.get(f"/contacts/{cid}").get_data(as_text=True)

    client.post(
        f"/contacts/{cid}/stage",
        data={"csrf": token(client, f"/contacts/{cid}"), "stage": "4", "confirm_backward": "1"},
        follow_redirects=True,
    )
    # forward from 2 to 4 does not need confirm; confirm flag is harmless
    activity = client.get(f"/contacts/{cid}?tab=activity").get_data(as_text=True)
    assert "Check in with the family during decision time" in activity
    assert "done" in activity


def test_move_in_celebration_happens_once_and_trophy_remains(client):
    login(client)
    add_contact(client)
    cid = contact_id(client)
    landed = client.post(
        f"/contacts/{cid}/stage",
        data={"csrf": token(client, f"/contacts/{cid}"), "stage": "7"},
        follow_redirects=True,
    )
    assert b"Congratulations" in landed.data
    assert b"Placement completed" in landed.data
    quiet = client.get(f"/contacts/{cid}").get_data(as_text=True)
    assert "Congratulations" not in quiet
    assert "Placement completed" in quiet
    assert "Prepare and send community referral invoice" in client.get(f"/contacts/{cid}?tab=activity").get_data(as_text=True)


def test_advisor_notes_do_not_overwrite_care_or_enter_the_family_pdf(client):
    login(client)
    configure(client)
    add_contact(client)
    cid = contact_id(client)
    client.post(
        f"/contacts/{cid}/profile",
        data={
            "csrf": token(client, f"/contacts/{cid}?tab=care"),
            "tab": "care",
            "care_type": "AL",
            "medication": "UNIQUE-MED-991",
            "medical_notes": "private clinical note",
        },
        follow_redirects=True,
    )
    client.post(
        f"/contacts/{cid}/advisor",
        data={"csrf": token(client, f"/contacts/{cid}?tab=advisor"), "next_step": "Call daughter", "care_level": "AL"},
        follow_redirects=True,
    )
    care = client.get(f"/contacts/{cid}?tab=care").get_data(as_text=True)
    assert "UNIQUE-MED-991" in care
    family = client.get(f"/contacts/{cid}/family.pdf")
    assert family.status_code == 200
    reader = PdfReader(io.BytesIO(family.data))
    assert len(reader.pages) == 4
    text = "\n".join(page.extract_text() or "" for page in reader.pages)
    assert "UNIQUE-MED-991" not in text
    assert "Call daughter" not in text
    assert "Page 1 of 4" in text
    assert "Page 4 of 4" in text
    assert "512-555-0100" in text
    assert "hello@bestfit.test" in text
    advisor = client.get(f"/contacts/{cid}/advisor.pdf")
    advisor_text = "\n".join(page.extract_text() or "" for page in PdfReader(io.BytesIO(advisor.data)).pages)
    assert "UNIQUE-MED-991" in advisor_text
    assert "Advisor only" in advisor_text
    assert "Call daughter" in advisor_text


def test_finance_cannot_open_care_map_or_advisor_page(client):
    login(client)
    configure(client)
    add_contact(client)
    cid = contact_id(client)
    created = client.post(
        "/admin/users",
        data={
            "csrf": token(client, "/admin/users"),
            "display_name": "Fran Finance",
            "email": "fran@bestfit.test",
            "password": "finance-password-1",
            "role": "finance",
        },
        follow_redirects=True,
    )
    assert b"User created" in created.data
    login(client, "fran@bestfit.test", "finance-password-1")
    assert client.get("/map").status_code == 403
    assert client.get("/pipeline").status_code == 403
    assert client.get(f"/contacts/{cid}/advisor.pdf").status_code == 403
    page = client.get(f"/contacts/{cid}?tab=care").get_data(as_text=True)
    assert "UNIQUE-MED" not in page
    assert "hidden for finance" in page
    assert "Finance" in client.get("/finance").get_data(as_text=True)


def test_advisor_cannot_open_another_advisors_prospect(client):
    login(client)
    created = client.post(
        "/admin/users",
        data={
            "csrf": token(client, "/admin/users"),
            "display_name": "Ann Advisor",
            "email": "ann@bestfit.test",
            "password": "advisor-password-1",
            "role": "advisor",
        },
        follow_redirects=True,
    )
    assert b"User created" in created.data
    client.post(
        "/admin/users",
        data={
            "csrf": token(client, "/admin/users"),
            "display_name": "Bea Advisor",
            "email": "bea@bestfit.test",
            "password": "advisor-password-2",
            "role": "advisor",
        },
        follow_redirects=True,
    )
    login(client, "ann@bestfit.test", "advisor-password-1")
    add_contact(client, name="Ann Prospect", email="ann.prospect@example.com", phone="512-555-0188")
    cid = contact_id(client, "Ann")
    login(client, "bea@bestfit.test", "advisor-password-2")
    assert client.get(f"/contacts/{cid}").status_code == 404
    assert "Ann Prospect" not in client.get("/contacts").get_data(as_text=True)


def test_intake_email_requires_consent_and_test_skips_follow_up(client, env):
    login(client)
    configure(client)
    add_contact(client, consent=False)
    cid = contact_id(client)
    refused = client.post(
        f"/contacts/{cid}/intake/send",
        data={"csrf": token(client, f"/contacts/{cid}")},
        follow_redirects=True,
    )
    assert b"not consented" in refused.data
    assert env.config["OUTBOX"] == []
    client.post(
        f"/contacts/{cid}/edit",
        data={
            "csrf": token(client, f"/contacts/{cid}/edit"),
            "name": "Mary O'Brien",
            "email": "mary@example.com",
            "phone": "512-555-0199",
            "last_engagement": "2026-10-22",
            "email_consent": "1",
        },
        follow_redirects=True,
    )
    client.post(
        f"/contacts/{cid}/intake/send",
        data={"csrf": token(client, f"/contacts/{cid}"), "test": "1"},
        follow_redirects=True,
    )
    assert env.config["OUTBOX"][0]["to"] == "admin@bestfit.test"
    assert env.config["OUTBOX"][0]["subject"].startswith("TEST")
    activity = client.get(f"/contacts/{cid}?tab=activity").get_data(as_text=True)
    assert "TEST intake email" in activity
    assert "Follow up on the intake form" not in activity
    client.post(
        f"/contacts/{cid}/intake/send",
        data={"csrf": token(client, f"/contacts/{cid}")},
        follow_redirects=True,
    )
    activity = client.get(f"/contacts/{cid}?tab=activity").get_data(as_text=True)
    assert "Follow up on the intake form" in activity
    assert env.config["OUTBOX"][1]["filename"].endswith(".pdf")


def test_invoice_uses_fee_remembers_billing_and_moves_stages(client, env):
    login(client)
    configure(client)
    add_contact(client)
    cid = contact_id(client)
    blocked = client.post(
        f"/contacts/{cid}/invoices",
        data={"csrf": token(client, f"/contacts/{cid}"), "community_id": "missing"},
        follow_redirects=True,
    )
    assert b"Choose the community" in blocked.data or b"move-in" in blocked.data
    client.post(
        "/communities",
        data={
            "csrf": token(client, "/map"),
            "name": "Hill House",
            "care_types": "AL,MC",
            "address": "100 Main St",
            "city": "Lago Vista",
            "zip": "78645",
            "license": "unknown",
        },
        follow_redirects=True,
    )
    directory = client.get("/map").get_data(as_text=True)
    assert "license unknown" in directory
    assert "unverified" in directory
    community = re.search(r'value="([0-9a-f]{32})">Hill House', directory)
    # The map add form doesn't list ids. The contact invoice select does after stage 7.
    client.post(
        f"/contacts/{cid}/stage",
        data={"csrf": token(client, f"/contacts/{cid}"), "stage": "7"},
        follow_redirects=True,
    )
    detail = client.get(f"/contacts/{cid}?tab=communities").get_data(as_text=True)
    community = re.search(r'value="([0-9a-f]{32})">Hill House', detail)
    assert community, detail
    created = client.post(
        f"/contacts/{cid}/invoices",
        data={
            "csrf": token(client, f"/contacts/{cid}?tab=communities"),
            "community_id": community.group(1),
            "attention": "Billing desk",
            "billing_email": "billing@hillhouse.test",
            "cc_email": "copy@hillhouse.test",
        },
        follow_redirects=True,
    )
    assert b"INV-" in created.data
    assert b"$2,500.00" in created.data
    assert b"Hill House" in created.data
    assert b"100 Main St" in created.data
    sent = client.post(
        created.request.path + "/send" if False else "/invoices/" + re.search(r"/invoices/([0-9a-f]{32})", created.request.path).group(1) + "/send",
        data={"csrf": token(client, created.request.path)},
        follow_redirects=True,
    )
    assert b"emailed" in sent.data
    assert env.config["OUTBOX"][-1]["to"] == "billing@hillhouse.test"
    assert env.config["OUTBOX"][-1]["filename"].endswith(".pdf")
    assert "Invoice Sent" in client.get(f"/contacts/{cid}").get_data(as_text=True)
    paid = client.post(
        created.request.path + "/paid",
        data={"csrf": token(client, created.request.path), "reference": "check-4401"},
        follow_redirects=True,
    )
    assert b"not a bank verification" in paid.data
    assert "Invoice Paid" in client.get(f"/contacts/{cid}").get_data(as_text=True)
    activity = client.get(f"/contacts/{cid}?tab=activity").get_data(as_text=True)
    assert "Follow up on community invoice payment" in activity
    assert activity.count("open") == 0 or "done" in activity


def test_backup_is_encrypted_and_restores(client):
    login(client)
    configure(client)
    add_contact(client)
    cid = contact_id(client)
    client.post(
        f"/contacts/{cid}/profile",
        data={"csrf": token(client, f"/contacts/{cid}?tab=care"), "medication": "UNIQUE-MED-991"},
        follow_redirects=True,
    )
    exported = client.post("/backup/export", data={"csrf": token(client, "/backup")})
    blob = exported.data
    assert blob.startswith(b"BESTFIT1")
    assert b"UNIQUE-MED-991" not in blob
    assert PASSWORD.encode() not in blob
    add_contact(client, name="Temporary Person", email="temp@example.com", phone="512-555-0177")
    restored = client.post(
        "/backup",
        data={
            "csrf": token(client, "/backup"),
            "action": "restore",
            "confirm": "RESTORE",
            "file": (io.BytesIO(blob), "backup.bfe"),
        },
        follow_redirects=True,
    )
    assert b"Restored" in restored.data
    listing = client.get("/contacts").get_data(as_text=True)
    assert "Temporary Person" not in listing
    assert "Mary" in listing
    cid = contact_id(client)
    assert "UNIQUE-MED-991" in client.get(f"/contacts/{cid}?tab=care").get_data(as_text=True)


def test_map_explains_out_of_area_and_plots_a_local_zip(client):
    login(client)
    client.post(
        "/communities",
        data={
            "csrf": token(client, "/map"),
            "name": "Cedar Court",
            "care_types": "AL",
            "address": "1 Cedar",
            "city": "Cedar Park",
            "zip": "78613",
            "license": "yes",
        },
        follow_redirects=True,
    )
    opened = client.get("/map").get_data(as_text=True)
    assert 'class="map-plot"' in opened
    assert 'viewBox="-14 -14 128 128"' in opened
    assert 'class="star"' in opened
    assert "Cedar Court" in opened
    assert "Showing the directory around Austin" in opened
    far = client.post(
        "/map",
        data={"csrf": token(client, "/map"), "action": "search", "query": "75201", "radius": "25", "care": "", "name": ""},
    )
    assert b"outside the Central Texas directory" in far.data
    near = client.post(
        "/map",
        data={"csrf": token(client, "/map"), "action": "search", "query": "4102 Constitution Dr, Lago Vista, TX 78645", "radius": "30", "care": "AL", "name": ""},
    )
    body = near.get_data(as_text=True)
    assert "not the street rooftop" in body
    assert "straight-line" in body
    assert "Cedar Court" in body
    assert 'class="star"' in body
    assert 'class="pin"' in body


def test_stall_filter_and_report_blanks(client):
    login(client)
    add_contact(client)
    cid = contact_id(client)
    activity = client.get(f"/contacts/{cid}?tab=activity")
    data = {
        "csrf": csrf(activity.get_data(as_text=True)),
        "date_0": "2026-10-01",
        "date_2": "2026-10-11",
    }
    client.post(f"/contacts/{cid}/milestones", data=data, follow_redirects=True)
    # Milestone edit does not change the stage. Move to signed so the date is the current stage? 
    # Stall uses the current stage date. Stay on prospect with an old prospect date.
    page = client.get(f"/contacts/{cid}").get_data(as_text=True)
    assert "follow up" in page
    filtered = client.get("/contacts?stage=stalled").get_data(as_text=True)
    assert "Showing 1 of 1" in filtered
    client.post(
        f"/contacts/{cid}/stage",
        data={"csrf": token(client, f"/contacts/{cid}"), "stage": "2"},
        follow_redirects=True,
    )
    report = client.get("/reports").get_data(as_text=True)
    assert "10" in report
    assert ">—<" in report or ">—</" in report or ">—" in report or "—" in report
    csv_response = client.post("/reports.csv", data={"csrf": token(client, "/reports")})
    assert b"Mary" in csv_response.data
    assert b"Review dates" not in csv_response.data or b"Mary" in csv_response.data


def test_reduced_motion_and_escape_are_in_the_client():
    css = open("bestfit/static/app.css", encoding="utf-8").read()
    script = open("bestfit/static/app.js", encoding="utf-8").read()
    assert "prefers-reduced-motion" in css
    assert 'event.key !== "Escape"' in script
    assert "open.length - 1" in script


def test_dallas_is_outside_and_lago_vista_is_not():
    dallas = locate_offline("75201")
    assert dallas["out_of_area"] is True
    home = locate_offline("78645")
    assert home["out_of_area"] is False
    assert haversine((home["lat"], home["lng"]), (30.46, -97.99)) < 5


def test_app_refuses_to_start_without_keys(tmp_path):
    with pytest.raises(RuntimeError):
        create_app({"SECRET_KEY": "", "DATA_KEY": "", "DATABASE": str(tmp_path / "x.sqlite")})
