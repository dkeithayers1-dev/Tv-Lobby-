import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from receipt_parser import _parse_date, _parse_total, _parse_uber

UBER_RECEIPT_TEXT = """Sep 16, 2026
1:56 PM
Tip
Thanks for tipping, David
We hope you enjoyed your ride this afternoon.
Total $21.26
Trip fare $13.87
Booking Fee $2.39
Tip $5.00
Payments
Visa ****7536 $21.26
9/16/26 2:48 PM
Trip details
Wait & Save
4.62 miles, 10 minutes
2:17 PM
8300 N Hayden Rd, Scottsdale, AZ 85258-2449, USA
2:27 PM
4925 N Scottsdale Rd, Scottsdale, AZ 85251, USA
You rode with IZATULLAH 4.95
"""


def test_parse_total():
    assert _parse_total(UBER_RECEIPT_TEXT) == 21.26


def test_parse_date():
    assert _parse_date(UBER_RECEIPT_TEXT).isoformat() == "2026-09-16"


def test_parse_uber_details():
    details = _parse_uber(UBER_RECEIPT_TEXT, 21.26, None)
    assert details["vendor"] == "Uber"
    assert details["mileage"] == 4.62
    assert "Wait & Save" in details["description"]
    assert details["from_addr"].startswith("8300 N Hayden Rd")
    assert details["to_addr"].startswith("4925 N Scottsdale Rd")


def test_parse_total_ignores_subtotal_like_words():
    text = "Subtotal $5.00\nTotal $21.26\n"
    assert _parse_total(text) == 21.26
