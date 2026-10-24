"""Renders a month's schedule as a one-page landscape PDF, laid out like the
on-screen grid: days across the top, one employee per row, and daily
hour/headcount totals at the bottom. This is what employees download to
keep on their phones. It never includes phone numbers.
"""
import calendar
from datetime import datetime

from fpdf import FPDF

import scheduler

# Matches the .shift-* colors in static/style.css
FILL = {
    "D": (219, 234, 254),
    "E": (254, 243, 199),
    "N": (237, 233, 254),
    "OFF": (243, 244, 246),
}
# Short labels so a 31-day month fits across one page
SHORT = {"D": "7-3", "E": "3-11", "N": "11-7", "OFF": "Off"}
WEEKEND_FILL = (238, 242, 255)
TOTALS_FILL = (249, 250, 251)

MARGIN = 6
NAME_W = 34
HOURS_W = 11
TITLE_H = 9
HEADER_H = 7


def _latin1(text):
    # The built-in PDF fonts only cover Latin-1; swap anything else for "?".
    return str(text).encode("latin-1", "replace").decode("latin-1")


def build_pdf(month: str) -> bytes:
    days = scheduler.day_headers(month)
    employees = scheduler.load_roster()
    shifts = scheduler.load_shifts(month)
    totals = scheduler.daily_totals(month, employees, shifts)
    work_codes = [c for c, s in scheduler.SHIFTS.items() if s["hours"] > 0]

    pdf = FPDF(orientation="L", unit="mm", format="Letter")
    pdf.set_auto_page_break(False)
    pdf.set_margins(MARGIN, MARGIN, MARGIN)
    pdf.add_page()
    pdf.set_draw_color(200, 204, 212)
    pdf.set_line_width(0.15)

    usable_w = pdf.w - 2 * MARGIN
    day_w = (usable_w - NAME_W - HOURS_W) / len(days)
    footer_rows = 1 + len(work_codes)
    body_h = pdf.h - 2 * MARGIN - TITLE_H - HEADER_H - 5
    row_h = max(3.2, min(5.5, body_h / (len(employees) + footer_rows)))
    font_size = min(7, row_h * 1.5)

    year, mon = month.split("-")
    pdf.set_font("Helvetica", "B", 13)
    pdf.cell(0, TITLE_H, f"Employee Schedule - {calendar.month_name[int(mon)]} {year}")
    pdf.set_font("Helvetica", "", 7)
    legend = "    ".join(f"{SHORT[c]} = {s['label']}" for c, s in scheduler.SHIFTS.items() if s["hours"])
    pdf.set_x(MARGIN)
    pdf.cell(0, TITLE_H, f"{legend}    (8 hours each)", align="R")
    pdf.ln(TITLE_H)

    def cell(w, h, text="", fill=None, bold=False, align="C"):
        pdf.set_font("Helvetica", "B" if bold else "", font_size)
        if fill:
            pdf.set_fill_color(*fill)
        pdf.cell(w, h, _latin1(text), border=1, align=align, fill=bool(fill))

    # Row 1: days of the month
    pdf.set_font("Helvetica", "B", font_size)
    cell(NAME_W, HEADER_H, "Employee", bold=True, align="L")
    for d in days:
        x, y = pdf.get_x(), pdf.get_y()
        fill = WEEKEND_FILL if d["weekday"] in ("Sat", "Sun") else None
        cell(day_w, HEADER_H, "", fill=fill)
        pdf.set_xy(x, y + 0.6)
        pdf.set_font("Helvetica", "B", font_size)
        pdf.cell(day_w, HEADER_H / 2, str(d["day"]), align="C")
        pdf.set_xy(x, y + HEADER_H / 2)
        pdf.set_font("Helvetica", "", font_size - 1.5)
        pdf.cell(day_w, HEADER_H / 2 - 0.6, d["weekday"], align="C")
        pdf.set_xy(x + day_w, y)
    cell(HOURS_W, HEADER_H, "Hours", bold=True)
    pdf.ln(HEADER_H)

    # Rows 2+: one per employee
    for emp in employees:
        row = shifts.get(emp["id"], {})
        cell(NAME_W, row_h, emp["name"], bold=True, align="L")
        for d in days:
            code = row.get(str(d["day"]))
            label = SHORT[code] if code else ""
            cell(day_w, row_h, label, fill=FILL.get(code))
        cell(HOURS_W, row_h, str(scheduler.employee_hours(row)), bold=True)
        pdf.ln(row_h)

    # Bottom: daily totals
    footer = [("Total hours", "hours")] + [
        (f"{scheduler.SHIFTS[c]['label']} staff", c) for c in work_codes
    ]
    for label, key in footer:
        cell(NAME_W, row_h, label, fill=TOTALS_FILL, bold=True, align="L")
        if key == "hours":
            values = [t["hours"] for t in totals]
        else:
            values = [t["counts"][key] for t in totals]
        for value in values:
            cell(day_w, row_h, str(value or ""), fill=TOTALS_FILL, bold=key == "hours")
        cell(HOURS_W, row_h, str(sum(values)), fill=TOTALS_FILL, bold=True)
        pdf.ln(row_h)

    pdf.set_font("Helvetica", "", 6)
    pdf.set_text_color(107, 114, 128)
    pdf.cell(0, 5, f"Printed {datetime.now():%b %d, %Y %I:%M %p}")

    return bytes(pdf.output())
