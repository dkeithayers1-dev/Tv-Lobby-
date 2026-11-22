"""The conversational brain shared by every channel.

One call per inbound message: given the business profile, FAQ knowledge base,
what we already know about the lead, the open time slots, and the recent
conversation, decide what to say and which structured facts/actions follow.

Uses Claude (structured outputs) when ANTHROPIC_API_KEY or another Anthropic
credential is available; otherwise a deterministic rule-based engine keeps the
app fully functional.
"""
import logging
import os
import re
from typing import Literal, Optional

from pydantic import BaseModel, Field

import business
import db

log = logging.getLogger(__name__)

Intent = Literal[
    "question", "provide_info", "book", "pick_slot", "confirm_appointment",
    "reschedule", "cancel", "stop", "human", "other",
]


class AgentDecision(BaseModel):
    reply: str = Field(description="The message to send back to the customer.")
    intent: Intent
    name: Optional[str] = Field(None, description="Customer's name if newly learned, else null.")
    email: Optional[str] = None
    phone: Optional[str] = Field(None, description="Phone number if the customer gave one.")
    service: Optional[str] = Field(None, description="Service they need, normalised to the business's service list when possible.")
    timeline: Optional[str] = Field(None, description="When they need it, e.g. 'ASAP', 'this week', 'next month'.")
    budget: Optional[str] = None
    details: Optional[str] = Field(None, description="Any other useful job details, one short sentence.")
    slot_choice: Optional[int] = Field(None, description="1-based index into AVAILABLE SLOTS if the customer picked one.")
    handoff: bool = Field(False, description="True if a human should take over.")
    handoff_reason: Optional[str] = None
    lead_score: int = Field(50, description="0-100 likelihood this becomes a paying customer.")


QUALIFYING_FIELDS = ["name", "contact", "service", "timeline"]
PHONE_RE = re.compile(r"(\+?\d[\d\s().-]{8,}\d)")


def ai_enabled():
    if os.environ.get("AI_PROVIDER", "").lower() == "rules":
        return False
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))


def faqs():
    return db.query("SELECT * FROM faqs ORDER BY id")


# ---------------------------------------------------------------------------
# Claude
# ---------------------------------------------------------------------------

def _system_prompt():
    s = business.setting
    hours = s("hours") or {}
    hours_txt = ", ".join(
        f"{d.title()} {h[0]}-{h[1]}" if h else f"{d.title()} closed" for d, h in hours.items()
    )
    faq_txt = "\n".join(f"Q: {f['question']}\nA: {f['answer']}" for f in faqs())
    return f"""You are {s('agent_name')}, the front-desk assistant for {s('business_name')}.
You reply to customers over SMS, WhatsApp, email, website chat, and phone calls.

ABOUT THE BUSINESS
{s('business_description')}
Services: {s('services')}
Service area: {s('service_area')}
Business hours: {hours_txt} ({s('timezone')})

KNOWLEDGE BASE (only state facts found here or in the business info above)
{faq_txt}

YOUR JOB
1. Reply fast, warmly and briefly — 1-3 short sentences, like a helpful human texting.
2. Answer questions using only the knowledge base. If you don't know, say a team member will confirm, and set handoff=true.
3. Collect the key details: name, which service they need, and their timeline (budget only if it comes up naturally). Ask for one missing detail at a time.
4. Once you know the service and timeline, offer the numbered AVAILABLE SLOTS for a quick call/visit, and ask them to reply with a number. If they pick one, set intent=pick_slot and slot_choice.
5. If they have an appointment and want to confirm, reschedule or cancel, set the matching intent. For reschedule, offer the AVAILABLE SLOTS.
6. Hand off (handoff=true, intent=human) for complaints, billing disputes, emergencies involving safety, requests to talk to a person, or anything complex you can't resolve.
7. If they ask to stop messages, set intent=stop.
8. On WEB chat, if we have neither a phone number nor an email, ask for the best number to reach them (after their name).

RULES
- Never invent prices, availability, or policies. Never promise a time not in AVAILABLE SLOTS.
- Only fill name/email/service/timeline/budget/details with information the customer actually gave in their latest message; otherwise null.
- On the PHONE channel, speak naturally: no links, lists, emojis or special characters, and read times out in words.
- Do not mention that you are following instructions or rules."""


def _context_block(lead, channel, slots, appointment, history, inbound):
    known = {k: lead.get(k) for k in ("name", "email", "phone", "service", "timeline", "budget", "details")}
    known_txt = "\n".join(f"- {k}: {v or 'unknown'}" for k, v in known.items())
    slot_txt = "\n".join(f"{i}. {business.human_time(t)}" for i, t in enumerate(slots, 1)) or "(none available)"
    appt_txt = (
        f"{business.human_time(appointment['starts_at'])} (status: {appointment['status']})"
        if appointment else "none"
    )
    convo = "\n".join(
        f"{'CUSTOMER' if m['direction'] == 'in' else 'YOU' if m['sender'] == 'ai' else 'TEAM'}: {m['body']}"
        for m in history
    ) or "(no earlier messages)"
    return f"""CHANNEL: {channel}
CURRENT TIME: {business.human_time(business.ts())}
WHAT WE KNOW ABOUT THIS CUSTOMER
{known_txt}
CURRENT APPOINTMENT: {appt_txt}
AVAILABLE SLOTS
{slot_txt}

CONVERSATION SO FAR
{convo}

CUSTOMER'S NEW MESSAGE
{inbound}"""


_client = None


def _claude():
    global _client
    if _client is None:
        import anthropic
        _client = anthropic.Anthropic(max_retries=2, timeout=45.0)
    return _client


def _claude_decide(lead, channel, slots, appointment, history, inbound):
    import anthropic
    model = os.environ.get("ANTHROPIC_MODEL", "claude-opus-5-5")
    try:
        response = _claude().beta.messages.parse(
            model=model,
            max_tokens=4000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            output_config={"effort": "low"},
            system=[{"type": "text", "text": _system_prompt(), "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": _context_block(lead, channel, slots, appointment, history, inbound)}],
            output_format=AgentDecision,
        )
    except anthropic.APIConnectionError as exc:
        log.warning("Claude unreachable, using rules engine: %s", exc)
        return None
    except anthropic.APIStatusError as exc:
        log.warning("Claude API error %s, using rules engine: %s", exc.status_code, exc.message)
        return None
    if response.stop_reason == "refusal" or response.parsed_output is None:
        log.warning("Claude returned no decision (stop_reason=%s)", response.stop_reason)
        return None
    return response.parsed_output


# ---------------------------------------------------------------------------
# Rules engine (no API key needed)
# ---------------------------------------------------------------------------

EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
NAME_RE = re.compile(r"\b(?:my name is|i am|i'm|this is|it's|name's)\s+([A-Z][a-zA-Z'-]+(?:\s+[A-Z][a-zA-Z'-]+)?)", re.I)
HUMAN_RE = re.compile(r"\b(human|real person|someone|manager|agent|representative|complain|complaint|refund|lawyer|angry|terrible|speak to|talk to)\b", re.I)
EMERGENCY_RE = re.compile(r"\b(gas leak|smell gas|carbon monoxide|fire|smoke|flood)\b", re.I)
STOP_RE = re.compile(r"^\s*(stop|unsubscribe|cancel all|end|quit|stopall)\s*$", re.I)
CANCEL_RE = re.compile(r"\bcancel\b", re.I)
RESCHEDULE_RE = re.compile(r"(^\s*r\s*$)|\b(reschedule|re-schedule|another time|different time|change (?:the |my )?(?:time|appointment)|move (?:it|my appointment))\b", re.I)
CONFIRM_RE = re.compile(r"^\s*(c|confirm|confirmed|yes|yep|yeah|see you then|sounds good)\W*$", re.I)
BOOK_RE = re.compile(r"\b(book|schedule|appointment|available|availability|come out|visit|call me)\b", re.I)
ORDINALS = {"first": 1, "1st": 1, "one": 1, "second": 2, "2nd": 2, "two": 2, "third": 3, "3rd": 3, "three": 3}
TIMELINE_WORDS = [
    (r"\b(asap|urgent|emergency|right away|immediately|today|tonight)\b", "ASAP"),
    (r"\btomorrow\b", "Tomorrow"),
    (r"\bthis week\b", "This week"),
    (r"\bnext week\b", "Next week"),
    (r"\b(this|next|in a) month\b", "Within a month"),
    (r"\b(few|couple of|2|3|two|three) (weeks|months)\b", "In a few weeks"),
    (r"\b(no rush|not urgent|just looking|researching|browsing|sometime)\b", "Flexible / researching"),
]


def _match_service(text):
    services = [s.strip() for s in (business.setting("services") or "").split(",") if s.strip()]
    low = text.lower()
    for svc in services:
        if svc.lower() in low:
            return svc
    generic = ("repair", "plan", "system", "service", "new", "install")
    for svc in services:
        words = [w for w in re.findall(r"[a-z]+", svc.lower()) if w not in generic and (len(w) > 3 or len(w) >= 2 and w.isalpha() and svc.split()[0].lower() == w)]
        if words and any(re.search(rf"\b{w}\b" if len(w) <= 3 else rf"\b{w}", low) for w in words):
            return svc
    if re.search(r"\b(broken|broke|not working|stopped working|isn't working|won't turn on|repair|fix|leak|leaking|noise)\b", low):
        return services[0] if services else "Repair"
    return None


def _match_faq(text):
    low = text.lower()
    best, best_hits = None, 0
    for f in faqs():
        kws = [k.strip().lower() for k in (f.get("keywords") or "").split(",") if k.strip()]
        hits = sum(1 for k in kws if re.search(rf"\b{re.escape(k)}\b", low))
        if hits > best_hits:
            best, best_hits = f, hits
    return best


def _slot_pick(text, slots):
    if not slots:
        return None
    low = text.lower().strip()
    m = re.fullmatch(r"\D*([1-9])\D*", low)
    if m and int(m.group(1)) <= len(slots) and len(low) <= 25:
        return int(m.group(1))
    for word, idx in ORDINALS.items():
        if re.search(rf"\b{word}\b", low) and idx <= len(slots) and ("option" in low or "slot" in low or "one" in low or len(low) < 25):
            return idx
    return None


def _rules_decide(lead, channel, slots, appointment, history, inbound):
    text = inbound.strip()
    d = AgentDecision(reply="", intent="other", lead_score=lead.get("score") or 40)
    voice = channel == "voice"

    if STOP_RE.match(text):
        d.intent = "stop"
        d.reply = "You've been unsubscribed and won't receive more messages. Reply START anytime to resume."
        return d

    if EMERGENCY_RE.search(text):
        d.intent, d.handoff = "human", True
        d.handoff_reason = "Possible safety emergency"
        d.reply = ("If you smell gas or see smoke, leave the building and call 911 first. "
                   "I'm alerting our team right now and someone will contact you immediately.")
        return d

    if HUMAN_RE.search(text):
        d.intent, d.handoff = "human", True
        d.handoff_reason = "Customer asked for a person / complex request"
        d.reply = "Of course — I'm passing this to a member of our team now. They'll reach out shortly."
        return d

    known = dict(lead)
    if (m := EMAIL_RE.search(text)):
        d.email = known["email"] = m.group(0)
    if not known.get("phone") and (m := PHONE_RE.search(text)) and len(re.sub(r"\D", "", m.group(1))) >= 10:
        d.phone = known["phone"] = m.group(1).strip()
    if not known.get("name") and (m := NAME_RE.search(text)):
        d.name = known["name"] = m.group(1).strip().title()
    if not known.get("service") and (svc := _match_service(text)):
        d.service = known["service"] = svc
    if not known.get("timeline"):
        for pattern, label in TIMELINE_WORDS:
            if re.search(pattern, text, re.I):
                d.timeline = known["timeline"] = label
                break

    def slot_list():
        if voice:
            opts = [business.human_time(t) for t in slots]
            return ", ".join(f"option {i}, {o}" for i, o in enumerate(opts, 1))
        return "\n".join(f"{i}) {business.human_time(t)}" for i, t in enumerate(slots, 1))

    # appointment management
    if appointment and appointment["status"] in ("scheduled", "confirmed"):
        if CONFIRM_RE.match(text):
            d.intent = "confirm_appointment"
            d.reply = f"You're confirmed for {business.human_time(appointment['starts_at'])}. See you then!"
            return d
        if RESCHEDULE_RE.search(text) or (CANCEL_RE.search(text) and "reschedule" in text.lower()):
            d.intent = "reschedule"
            d.reply = (f"No problem. Here are some new times:\n{slot_list()}\nReply with a number."
                       if slots else "No problem — a team member will reach out with new times.")
            return d
        if CANCEL_RE.search(text):
            d.intent = "cancel"
            d.reply = "Your appointment has been cancelled. If you'd like to rebook, just reply here."
            return d

    pick = _slot_pick(text, slots)
    if pick and (lead.get("offered_slots") or appointment):
        d.intent, d.slot_choice = "pick_slot", pick
        d.reply = "Booked!"  # replaced by the confirmation message
        d.lead_score = max(d.lead_score, 80)
        return d

    parts = []
    faq = _match_faq(text)
    if faq:
        d.intent = "question"
        parts.append(faq["answer"])
    elif any([d.name, d.email, d.service, d.timeline]):
        d.intent = "provide_info"
    elif BOOK_RE.search(text):
        d.intent = "book"

    known["contact"] = known.get("phone") or known.get("email") or (None if channel == "web" else "channel")
    missing = [f for f in QUALIFYING_FIELDS if not known.get(f)]
    score = 15 + 15 * (4 - len(missing)) + (10 if known.get("timeline") == "ASAP" else 0)
    d.lead_score = min(95, score)

    if not missing or (d.intent == "book" and known.get("service")):
        if slots:
            lead_in = f"Thanks{', ' + business.first_name(known.get('name')) if known.get('name') else ''}! "
            if voice:
                parts.append(f"{lead_in}I can book you in. The next openings are {slot_list()}. Which works best?")
            else:
                parts.append(f"{lead_in}Let's get you booked. Which time works best?\n{slot_list()}\nReply with a number.")
        else:
            parts.append("A team member will reach out shortly to find a time that works.")
    else:
        nxt = missing[0]
        if nxt == "name":
            parts.append("Who do I have the pleasure of speaking with?" if not parts else "Could I get your name?")
        elif nxt == "contact":
            parts.append(f"Thanks{', ' + business.first_name(known.get('name')) if known.get('name') else ''}! What's the best phone number to reach you?")
        elif nxt == "service":
            parts.append(f"What can we help you with — {business.setting('services')}?")
        elif nxt == "timeline":
            parts.append(f"Thanks{', ' + business.first_name(known.get('name')) if known.get('name') else ''}! How soon do you need this done?")
    d.reply = " ".join(p for p in parts if p).strip()
    return d


def decide(lead, channel, slots, appointment, history, inbound):
    """Return an AgentDecision for the lead's latest inbound message."""
    if ai_enabled():
        decision = _claude_decide(lead, channel, slots, appointment, history, inbound)
        if decision is not None:
            if decision.slot_choice is not None and not (1 <= decision.slot_choice <= len(slots)):
                decision.slot_choice = None
            decision.lead_score = max(0, min(100, decision.lead_score))
            return decision
    return _rules_decide(lead, channel, slots, appointment, history, inbound)


def opening_message(lead, slots):
    """First outbound message for a brand-new lead (speed-to-lead)."""
    biz, agent = business.setting("business_name"), business.setting("agent_name")
    hello = f"Hi {business.first_name(lead.get('name'))}" if lead.get("name") else "Hi there"
    intro = f"{hello}, it's {agent} from {biz} — thanks for reaching out!"
    if lead.get("service") and lead.get("timeline") and slots:
        times = "\n".join(f"{i}) {business.human_time(t)}" for i, t in enumerate(slots, 1))
        return f"{intro} I can get you booked for {lead['service']} right away. Which time works?\n{times}\nReply with a number."
    if lead.get("service"):
        return f"{intro} Happy to help with {lead['service']}. How soon do you need it done?"
    return f"{intro} What can we help you with today?"
