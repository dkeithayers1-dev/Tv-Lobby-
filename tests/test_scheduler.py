import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import scheduler
import sms


@pytest.fixture(autouse=True)
def tmp_schedule_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(scheduler, "SCHEDULE_DIR", tmp_path)
    monkeypatch.setattr(scheduler, "ROSTER_PATH", tmp_path / "employees.json")


def test_day_headers():
    days = scheduler.day_headers("2026-02")
    assert len(days) == 28
    assert days[0] == {"day": 1, "weekday": "Sun"}


def test_default_roster_has_35_employees():
    roster = scheduler.load_roster()
    assert len(roster) == 35
    assert roster[0]["name"] == "Employee 1"


def test_save_and_load_shifts_drops_blanks():
    scheduler.save_shifts("2026-11", {"e1": {"1": "D", "2": "", "3": "OFF"}, "e2": {"1": ""}})
    assert scheduler.load_shifts("2026-11") == {"e1": {"1": "D", "3": "OFF"}}


def test_save_shifts_rejects_bad_input():
    with pytest.raises(ValueError):
        scheduler.save_shifts("2026-11", {"e1": {"1": "X"}})
    with pytest.raises(ValueError):
        scheduler.save_shifts("2026-11", {"e1": {"31": "D"}})
    with pytest.raises(ValueError):
        scheduler.save_shifts("2026-13", {})


def test_daily_totals():
    roster = scheduler.default_roster()
    shifts = {"e1": {"1": "D"}, "e2": {"1": "E"}, "e3": {"1": "N", "2": "OFF"}, "e4": {"1": "D"}}
    totals = scheduler.daily_totals("2026-11", roster, shifts)
    assert totals[0]["hours"] == 32
    assert totals[0]["counts"]["D"] == 2
    assert totals[1]["hours"] == 0


def test_schedule_text():
    emp = {"id": "e1", "name": "Pat"}
    text = scheduler.schedule_text("2026-11", emp, {"3": "N", "1": "D", "2": "OFF"})
    assert text == "Pat, your November 2026 schedule (16 hrs):\n11/1 7a-3p\n11/3 11p-7a"
    assert "no shifts" in scheduler.schedule_text("2026-11", emp, {})


def test_normalize_phone():
    assert sms.normalize_phone("(480) 555-1234") == "+14805551234"
    assert sms.normalize_phone("1-480-555-1234") == "+14805551234"
    assert sms.normalize_phone("+44 20 7946 0958") == "+442079460958"
    with pytest.raises(ValueError):
        sms.normalize_phone("555-1234")


def test_text_endpoint_sends_each_employee_their_schedule(monkeypatch):
    import app as app_module

    sent = []
    monkeypatch.setattr(sms, "is_configured", lambda: True)
    monkeypatch.setattr(sms, "send_sms", lambda to, body: sent.append((to, body)) or "SM1")
    scheduler.save_roster([
        {"id": "e1", "name": "Pat", "phone": "480-555-1234"},
        {"id": "e2", "name": "Sam", "phone": ""},
    ])
    scheduler.save_shifts("2026-11", {"e1": {"1": "D"}})

    client = app_module.app.test_client()
    res = client.post("/api/schedule/text", json={"month": "2026-11"})
    data = res.get_json()
    assert data["sent"] == ["Pat"]
    assert data["skipped"] == ["Sam (no phone)"]
    assert sent == [("480-555-1234", "Pat, your November 2026 schedule (8 hrs):\n11/1 7a-3p")]
