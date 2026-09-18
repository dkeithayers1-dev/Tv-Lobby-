"""Extracts vendor/date/amount/description info from a receipt image or PDF.

Digital PDFs (Uber, Lyft, most emailed receipts) are parsed from their text
layer via pdfplumber. Photos are parsed via Tesseract OCR. Extraction is
best-effort: the web UI always shows the result for the user to correct
before it is written to the expense report.
"""
import re
from datetime import datetime
from pathlib import Path

import pdfplumber
from PIL import Image
import pytesseract

try:
    import pillow_heif
    pillow_heif.register_heif_opener()
except ImportError:
    pass

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".bmp", ".tiff", ".webp"}
PDF_EXTS = {".pdf"}

DATE_PATTERNS = [
    r"[A-Z][a-z]{2,8}\s+\d{1,2},\s+\d{4}",   # Sep 16, 2026
    r"\d{1,2}/\d{1,2}/\d{2,4}",              # 9/16/26 or 09/16/2026
    r"\d{4}-\d{2}-\d{2}",                    # 2026-09-16
]
DATE_FORMATS = [
    "%b %d, %Y", "%B %d, %Y",
    "%m/%d/%y", "%m/%d/%Y",
    "%Y-%m-%d",
]

TOTAL_RE = re.compile(r"(?<![A-Za-z])Total\s*\$?\s*([\d,]+\.\d{2})", re.IGNORECASE)
MILES_RE = re.compile(r"([\d.]+)\s*miles?", re.IGNORECASE)


def extract_text(file_path: str) -> str:
    ext = Path(file_path).suffix.lower()
    if ext in PDF_EXTS:
        text_parts = []
        with pdfplumber.open(file_path) as pdf:
            for page in pdf.pages:
                text_parts.append(page.extract_text() or "")
        return "\n".join(text_parts)
    if ext in IMAGE_EXTS:
        image = Image.open(file_path)
        return pytesseract.image_to_string(image)
    raise ValueError(f"Unsupported file type: {ext}")


def _parse_date(text: str):
    for pattern in DATE_PATTERNS:
        match = re.search(pattern, text)
        if not match:
            continue
        raw = match.group(0)
        for fmt in DATE_FORMATS:
            try:
                return datetime.strptime(raw, fmt).date()
            except ValueError:
                continue
    return None


def _parse_total(text: str):
    match = TOTAL_RE.search(text)
    if match:
        return float(match.group(1).replace(",", ""))
    return None


def _parse_uber(text: str, amount, date):
    from_addr = to_addr = None
    mileage = None
    ride_type = None

    trip_idx = text.find("Trip details")
    trip_section = text[trip_idx:] if trip_idx != -1 else text

    lines = [l.strip() for l in trip_section.splitlines() if l.strip()]
    for i, line in enumerate(lines):
        m = MILES_RE.search(line)
        if m:
            mileage = float(m.group(1))
            if i > 0:
                ride_type = lines[i - 1]
            break

    addr_lines = [
        l for l in lines
        if re.search(r"\d{1,6}\s+\w", l) and ("," in l) and "mile" not in l.lower()
    ]
    if len(addr_lines) >= 1:
        from_addr = addr_lines[0]
    if len(addr_lines) >= 2:
        to_addr = addr_lines[1]

    desc_bits = ["Uber ride"]
    if ride_type:
        desc_bits.append(f"({ride_type})")
    if mileage:
        desc_bits.append(f"- {mileage} mi")
    description = " ".join(desc_bits)

    return {
        "vendor": "Uber",
        "description": description,
        "from_addr": from_addr,
        "to_addr": to_addr,
        "mileage": mileage,
    }


def _parse_lyft(text: str, amount, date):
    return {
        "vendor": "Lyft",
        "description": "Lyft ride",
        "from_addr": None,
        "to_addr": None,
        "mileage": None,
    }


def _parse_generic(text: str, amount, date):
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    vendor = lines[0] if lines else "Unknown vendor"
    return {
        "vendor": vendor,
        "description": vendor,
        "from_addr": None,
        "to_addr": None,
        "mileage": None,
    }


def parse_receipt(file_path: str) -> dict:
    text = extract_text(file_path)
    lowered = text.lower()
    amount = _parse_total(text)
    date = _parse_date(text)

    if "uber" in lowered:
        details = _parse_uber(text, amount, date)
    elif "lyft" in lowered:
        details = _parse_lyft(text, amount, date)
    else:
        details = _parse_generic(text, amount, date)

    return {
        "vendor": details["vendor"],
        "description": details["description"],
        "date": date.isoformat() if date else None,
        "amount": amount,
        "from_addr": details.get("from_addr"),
        "to_addr": details.get("to_addr"),
        "mileage": details.get("mileage"),
        "raw_text": text.strip(),
    }
