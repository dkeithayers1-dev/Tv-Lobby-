"""Family intake PDF and the separate advisor-only page.

The family file is prefilled with name, email, and phone only. Care notes and
advisor fields are never written into it.
"""

from __future__ import annotations

from io import BytesIO

from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas


FAMILY_TEXT_FIELDS = (
    "p1_Your_full_name",
    "p1_Your_email",
    "p1_Mobile_phone",
    "p1_Best_time",
    "p1_Who_else_is_involved",
    "p2_Senior_s_full_name",
    "p2_Age",
    "p2_City",
    "p2_ZIP",
    "p2_Areas_other",
    "p2_Near_whom_where",
    "p2_Prompting_other",
    "p3_Must_haves_other",
    "p3_What_do_they_enjoy",
    "p3_Rule_out",
    "p3_Tour_availability_specifics",
    "p4_Who_should_be_on_the_tours",
    "p4_Anything_else_we_should_know",
    "p4_Signature",
    "p4_Printed_name",
    "p4_Date",
)

CHECKBOX_GROUPS = (
    ("Care setting of interest", ("IL", "AL", "MC", "SNF", "Respite", "Not sure")),
    ("Getting around", ("Independent", "Cane", "Walker", "Wheelchair", "Needs help to transfer")),
    ("Memory and thinking", ("No concern", "Mild forgetfulness", "Often confused", "Needs memory care")),
    ("Help during the day", ("Meals", "Medications", "Bathing", "Dressing", "Housekeeping")),
    ("Timing", ("As soon as possible", "Within 30 days", "1 to 3 months", "Just planning")),
    ("Budget the family can discuss", ("Under 4000", "4000 to 6000", "6000 to 8000", "Over 8000", "Not sure yet")),
    ("Must be nearby", ("Family", "Doctor", "Place of worship", "No preference")),
)


def _header(pdf: canvas.Canvas, business: dict, page: int) -> None:
    pdf.setFont("Times-Bold", 14)
    pdf.drawString(54, 750, business["business_name"])
    pdf.setFont("Times-Roman", 9)
    pdf.drawString(54, 736, business["street"])
    pdf.drawString(54, 724, business["city_line"])
    pdf.drawString(54, 712, f"{business['phone']}  ·  {business['email']}  ·  {business['website']}")
    pdf.setFont("Times-Bold", 12)
    pdf.drawString(54, 688, "Prospect intake")
    pdf.setFont("Times-Roman", 9)
    pdf.drawString(54, 674, "Fill this in and send it back. It is for your family, not an internal advisor worksheet.")
    pdf.line(54, 666, 558, 666)
    pdf.setFont("Times-Roman", 9)
    pdf.drawString(54, 36, f"Page {page} of 4")
    pdf.drawRightString(558, 36, business["business_name"])


def _text_field(pdf: canvas.Canvas, name: str, label: str, x: float, y: float, width: float = 240, value: str = "") -> None:
    pdf.setFont("Times-Roman", 9)
    pdf.drawString(x, y + 16, label)
    pdf.acroForm.textfield(
        name=name,
        x=x,
        y=y,
        width=width,
        height=16,
        borderWidth=0.6,
        forceBorder=True,
        value=value,
        fontSize=9,
    )


def family_intake_pdf(business: dict, prefill: dict) -> bytes:
    """prefill may contain name, email, and phone. Anything else is ignored."""
    allowed = {
        "p1_Your_full_name": prefill.get("name") or "",
        "p1_Your_email": prefill.get("email") or "",
        "p1_Mobile_phone": prefill.get("phone") or "",
    }
    buffer = BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=letter)
    pdf.setTitle("Best Fit prospect intake")

    _header(pdf, business, 1)
    y = 630
    _text_field(pdf, "p1_Your_full_name", "Your full name", 54, y, 240, allowed["p1_Your_full_name"])
    _text_field(pdf, "p1_Your_email", "Your email", 320, y, 220, allowed["p1_Your_email"])
    y -= 48
    _text_field(pdf, "p1_Mobile_phone", "Mobile phone", 54, y, 240, allowed["p1_Mobile_phone"])
    _text_field(pdf, "p1_Best_time", "Best time to talk", 320, y, 220)
    y -= 48
    _text_field(pdf, "p1_Who_else_is_involved", "Who else is involved in this decision?", 54, y, 486)
    y -= 36
    pdf.setFont("Times-Bold", 11)
    pdf.drawString(54, y, "What kind of help are you looking for?")
    y -= 8
    for label, options in CHECKBOX_GROUPS[:2]:
        y -= 18
        pdf.setFont("Times-Roman", 9)
        pdf.drawString(54, y, label)
        x = 54
        y -= 18
        for option in options:
            field = "cb_" + label.split()[0].lower() + "_" + option.lower().replace(" ", "_")
            pdf.acroForm.checkbox(name=field, x=x, y=y - 2, size=10, buttonStyle="check", checked=False)
            pdf.setFont("Times-Roman", 8)
            pdf.drawString(x + 14, y, option)
            x += 100
            if x > 500:
                x = 54
                y -= 16
        y -= 8
    pdf.showPage()

    _header(pdf, business, 2)
    y = 630
    pdf.setFont("Times-Bold", 11)
    pdf.drawString(54, y, "About the person who would move")
    y -= 36
    _text_field(pdf, "p2_Senior_s_full_name", "Their full name", 54, y, 300)
    _text_field(pdf, "p2_Age", "Age", 380, y, 80)
    y -= 48
    _text_field(pdf, "p2_City", "City", 54, y, 240)
    _text_field(pdf, "p2_ZIP", "ZIP", 320, y, 100)
    y -= 48
    _text_field(pdf, "p2_Areas_other", "Towns or areas to include", 54, y, 486)
    y -= 48
    _text_field(pdf, "p2_Near_whom_where", "Who or what should they live near?", 54, y, 486)
    y -= 48
    _text_field(pdf, "p2_Prompting_other", "What prompted this search?", 54, y, 486, )
    y -= 28
    for label, options in CHECKBOX_GROUPS[2:4]:
        pdf.setFont("Times-Roman", 9)
        pdf.drawString(54, y, label)
        y -= 18
        x = 54
        for option in options:
            field = "cb_" + label.split()[0].lower() + "_" + option.lower().replace(" ", "_")
            pdf.acroForm.checkbox(name=field, x=x, y=y - 2, size=10, buttonStyle="check", checked=False)
            pdf.setFont("Times-Roman", 8)
            pdf.drawString(x + 14, y, option)
            x += 110
        y -= 28
    pdf.showPage()

    _header(pdf, business, 3)
    y = 630
    _text_field(pdf, "p3_Must_haves_other", "Must-haves", 54, y, 486)
    y -= 56
    _text_field(pdf, "p3_What_do_they_enjoy", "What do they enjoy?", 54, y, 486)
    y -= 56
    _text_field(pdf, "p3_Rule_out", "Anything we should rule out?", 54, y, 486)
    y -= 56
    _text_field(pdf, "p3_Tour_availability_specifics", "When can the family tour?", 54, y, 486)
    y -= 36
    for label, options in CHECKBOX_GROUPS[4:]:
        pdf.setFont("Times-Roman", 9)
        pdf.drawString(54, y, label)
        y -= 18
        x = 54
        for option in options:
            field = "cb_" + label.split()[0].lower() + "_" + option.lower().replace(" ", "_")
            pdf.acroForm.checkbox(name=field, x=x, y=y - 2, size=10, buttonStyle="check", checked=False)
            pdf.setFont("Times-Roman", 8)
            pdf.drawString(x + 14, y, option)
            x += 120
            if x > 520:
                x = 54
                y -= 16
        y -= 24
    pdf.showPage()

    _header(pdf, business, 4)
    y = 630
    _text_field(pdf, "p4_Who_should_be_on_the_tours", "Who should be on the tours?", 54, y, 486)
    y -= 70
    pdf.setFont("Times-Roman", 9)
    pdf.drawString(54, y + 16, "Anything else we should know?")
    pdf.acroForm.textfield(
        name="p4_Anything_else_we_should_know",
        x=54,
        y=y - 48,
        width=486,
        height=64,
        borderWidth=0.6,
        forceBorder=True,
        fontSize=9,
        fieldFlags="multiline",
    )
    y -= 90
    _text_field(pdf, "p4_Signature", "Signature", 54, y, 220)
    _text_field(pdf, "p4_Printed_name", "Printed name", 300, y, 220)
    y -= 48
    _text_field(pdf, "p4_Date", "Date", 54, y, 140)
    pdf.showPage()
    pdf.save()
    return buffer.getvalue()


def advisor_page_pdf(business: dict, contact: dict, profile: dict, advisor: dict, stage_name: str) -> bytes:
    contact = dict(contact)
    profile = dict(profile)
    advisor = dict(advisor)
    buffer = BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=letter)
    pdf.setTitle("Advisor intake — internal")
    pdf.setFont("Times-Bold", 14)
    pdf.drawString(54, 750, "Advisor only — do not send to the family")
    pdf.setFont("Times-Roman", 10)
    pdf.drawString(54, 730, business["business_name"])
    y = 700
    lines = [
        f"Prospect: {contact['name']}",
        f"Stage: {stage_name}",
        f"Email: {contact.get('email') or '—'}",
        f"Phone: {contact.get('phone') or '—'}",
        f"Lead source: {advisor.get('lead_source') or '—'}",
        f"Care level: {advisor.get('care_level') or '—'}",
        f"Urgency: {advisor.get('urgency') or '—'}",
        f"Next step: {advisor.get('next_step') or '—'}",
        f"Outcome: {advisor.get('outcome') or '—'}",
        f"Care notes: {profile.get('medical_notes') or '—'}",
        f"Medication: {profile.get('medication') or '—'}",
        f"Shortlist: {advisor.get('shortlisted') or '—'}",
    ]
    for line in lines:
        pdf.drawString(54, y, line[:110])
        y -= 18
    pdf.save()
    return buffer.getvalue()


def invoice_pdf(business: dict, invoice: dict) -> bytes:
    buffer = BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=letter)
    pdf.setTitle(invoice["number"])
    pdf.setFont("Times-Bold", 16)
    pdf.drawString(54, 740, business["business_name"])
    pdf.setFont("Times-Roman", 10)
    y = 722
    for line in (business["street"], business["city_line"], business["phone"], business["email"], business["website"]):
        pdf.drawString(54, y, line)
        y -= 14
    pdf.setFont("Times-Bold", 13)
    pdf.drawString(380, 740, invoice["number"])
    pdf.setFont("Times-Roman", 10)
    pdf.drawString(380, 722, f"Date {invoice['invoice_date']}")
    pdf.drawString(380, 708, f"Due {invoice['due_on']}")
    pdf.drawString(380, 694, f"Terms {business['terms']}")
    pdf.setFont("Times-Bold", 11)
    pdf.drawString(54, 620, "Bill to")
    pdf.setFont("Times-Roman", 10)
    pdf.drawString(54, 604, invoice["community_name"])
    pdf.drawString(54, 590, invoice.get("attention") or "")
    pdf.drawString(54, 576, invoice.get("street") or "")
    pdf.drawString(54, 562, f"{invoice.get('city') or ''} {invoice.get('state') or ''} {invoice.get('zip') or ''}")
    pdf.drawString(54, 530, f"Resident placed: {invoice['resident']}")
    pdf.drawString(54, 516, f"Move-in date: {invoice['move_in']}")
    pdf.line(54, 496, 558, 496)
    pdf.drawString(54, 478, invoice["description"])
    pdf.drawRightString(558, 478, invoice["amount"])
    pdf.line(54, 464, 558, 464)
    pdf.setFont("Times-Bold", 11)
    pdf.drawRightString(558, 444, f"Total {invoice['amount']}")
    pdf.setFont("Times-Roman", 9)
    pdf.drawString(54, 420, invoice["fee_rule"])
    pdf.drawString(54, 400, "Recording a payment in Best Fit stores the reference an advisor entered. It is not a bank verification.")
    if invoice.get("notes"):
        pdf.drawString(54, 370, f"Notes: {invoice['notes'][:180]}")
    pdf.showPage()
    pdf.save()
    return buffer.getvalue()


INTAKE_TO_PROFILE = {
    "p2_Senior_s_full_name": "senior_name",
    "p2_Age": "age",
    "p2_City": "city",
    "p2_ZIP": "zip",
    "p2_Areas_other": "preferred_areas",
    "p2_Near_whom_where": "near",
    "p3_Must_haves_other": "must_haves",
    "p3_What_do_they_enjoy": "enjoys",
    "p3_Rule_out": "rule_out",
    "p3_Tour_availability_specifics": "tour_availability",
    "p4_Anything_else_we_should_know": "other_notes",
    "p1_Who_else_is_involved": "who_else",
}
