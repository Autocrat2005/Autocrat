"""
Call desk: who to ring right now, what to open with, and one tap to log how it went.

You dial by hand (your phone or a VoIP app with a local number). Autocrat never places calls,
never auto-dials and never leaves voicemails. The sheet lists:
  * 🔥 hot replies with a phone number, any market: ring them while it's warm;
  * cold leads with a phone, only in CALL_MARKETS (default US: business lines, manual dialing),
    only Mon-Fri 9-17 their time, max 3 tries, never DNC.

Outcome buttons
  ✅ Interested → lead REPLIED + a "great talking just now" email with two call times (if we have
                  their email; otherwise you're asked for it: `email <id> name@company.com`)
  🔁 Call back  → back on the sheet tomorrow
  📵 No answer  → next working day, max 3 tries
  ❌ Not interested → do-not-contact, forever (same rule as a written "no")

Settings (.env): CALL_MARKETS (US), CALL_HOURS (9-17), CALL_MAX_TRIES (3)
"""

from __future__ import annotations

import html
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from nexus.core.logger import get_logger
from nexus.core.runtime import RuntimeControl
from nexus.core.secrets import get_secret
from nexus.integrations.deals import _send_in_thread, email_of, first_name, sig

log = get_logger("calls")

CALL_OUTCOMES = {"yes": "✅ Interested", "later": "🔁 Call back", "na": "📵 No answer", "no": "❌ Not interested"}
COLD_STATES = ("DISCOVERED", "TRIAGED", "STAGED", "APPROVED", "SENT", "OPENED", "FORWARDED")
OPENERS = {
    "insurance": "hi, is this the owner? it's yash, i build small automations for independent agencies. quick one: "
                 "when a quote request comes in after hours or while you're on the phone, what happens to it?",
    "real_estate": "hi, it's yash, quick one: when a portal lead comes in while you're out on a viewing, "
                   "how fast does it get a reply right now?",
    "vibe_coder": "hi, it's yash, i saw {company} launch. quick one: how are you handling payments and auth "
                  "as real users come in?",
    "default": "hi, it's yash from windrose labs, we build small automations for businesses like yours. "
               "quick one: what's the most manual part of your week right now?",
}


def e164(raw: str, default_cc: str = "") -> str:
    """'(713) 555-0100' + cc '1' → '+17135550100'. Returns '' for junk."""
    raw = (raw or "").strip()
    digits = re.sub(r"\D", "", raw)
    if not digits:
        return ""
    if raw.startswith("+"):
        pass
    elif digits.startswith("00"):
        digits = digits[2:]
    elif default_cc == "1" and len(digits) == 10:
        digits = "1" + digits
    elif default_cc == "1" and len(digits) == 11 and digits.startswith("1"):
        pass
    elif digits.startswith("0") and default_cc:
        digits = default_cc + digits[1:]
    elif default_cc and len(digits) <= 10:
        digits = default_cc + digits
    elif not default_cc:
        return ""             # a local number and no idea which country: don't guess
    return f"+{digits}" if 8 <= len(digits) <= 15 else ""


MARKET_CC = {"uae": "971", "dubai": "971", "abu dhabi": "971", "us": "1", "usa": "1", "united states": "1",
             "canada": "1", "uk": "44", "united kingdom": "44", "india": "91", "australia": "61", "saudi": "966",
             "qatar": "974", "singapore": "65", "ireland": "353"}


def cc_for_market(market: str) -> str:
    key = (market or "").strip().lower()
    return next((cc for name, cc in MARKET_CC.items() if key == name or key.startswith(name + " ")), "")


def phone_of(lead: Dict[str, Any]) -> str:
    data = lead.get("data") or {}
    for value in (data.get("call_phone"), lead.get("contact") if lead.get("channel") == "call" else "", data.get("phone")):
        number = e164(str(value or ""))
        if number:
            return number
    return ""


def _markets() -> List[str]:
    return [m.strip().lower() for m in (get_secret("CALL_MARKETS", "US") or "US").split(",") if m.strip()]


def _in_markets(market: str) -> bool:
    key = (market or "").strip().lower()
    return any(key == m or key.startswith(m + " ") or m in key.split(", ") for m in _markets())


def _their_now(lead: Dict[str, Any], now: datetime) -> datetime:
    from nexus.integrations.email_channel import market_offset
    default = int(get_secret("EMAIL_UTC_OFFSET_MINUTES", "240") or 240)
    return now + timedelta(minutes=market_offset(lead.get("market") or "", default))


def _open_now(lead: Dict[str, Any], now: datetime) -> bool:
    start, _, end = (get_secret("CALL_HOURS", "9-17") or "9-17").partition("-")
    local = _their_now(lead, now)
    return local.weekday() < 5 and int(start) <= local.hour < int(end or 17)


def _call_events(store, lead_id: str) -> List[Dict[str, Any]]:
    return [e for e in store.lead_events(lead_id) if e["type"] == "call"]


def opener_for(store, lead: Dict[str, Any]) -> str:
    for o in reversed(store.outreach_for_lead(lead["id"])):
        if o["channel"] == "call" and o.get("draft"):
            from nexus.integrations.email_channel import split_subject
            return split_subject(o["draft"])[1][:400]
    data = lead.get("data") or {}
    line = OPENERS.get(lead.get("niche") or "", OPENERS["default"])
    line = line.replace("{company}", lead.get("company") or "your product")
    reviews = str(data.get("reviews") or "").replace(",", "")
    if reviews.isdigit() and int(reviews) >= 25:
        line += f" (hook: {reviews} google reviews = lots of enquiries to handle)"
    return line


def call_sheet(store, now: Optional[datetime] = None, limit: int = 12, ignore_hours: bool = False) -> List[Dict[str, Any]]:
    now = now or datetime.now(timezone.utc)
    max_tries = int(get_secret("CALL_MAX_TRIES", "3") or 3)
    c = store._conn()
    hot_ids = {r[0] for r in c.execute(
        "SELECT DISTINCT lead_id FROM lead_events WHERE type='message_in' AND json_extract(payload,'$.hot') AND ts >= ?",
        (now.timestamp() - 3 * 86400,))}
    rows = c.execute("SELECT id FROM leads WHERE state IN ({}) ORDER BY score_intent DESC, created_at LIMIT 400".format(
        ",".join("?" * (len(COLD_STATES) + 1))), (*COLD_STATES, "REPLIED")).fetchall()
    sheet = []
    for (lead_id,) in rows:
        lead = store.get_lead(lead_id)
        phone = phone_of(lead or {})
        if not lead or not phone or store.is_suppressed(f"phone:{phone}"):
            continue
        hot = lead_id in hot_ids and lead["state"] == "REPLIED"
        if lead["state"] == "REPLIED" and not hot:
            continue          # a live email conversation: let the closer and you handle it there
        if not hot and not _in_markets(lead.get("market") or ""):
            continue
        if not ignore_hours and not _open_now(lead, now):
            continue
        calls = _call_events(store, lead_id)
        if any((e.get("payload") or {}).get("outcome") in ("yes", "no") for e in calls):
            continue
        tries = sum(1 for e in calls if (e.get("payload") or {}).get("outcome") == "na")
        if tries >= max_tries:
            continue
        if calls and now.timestamp() - float(calls[-1]["ts"]) < 20 * 3600:
            continue          # tried (or asked to call back) in the last 20h
        local = _their_now(lead, now)
        sheet.append({"lead": lead, "phone": phone, "hot": hot, "tries": tries,
                      "local_time": local.strftime("%I:%M %p").lstrip("0").lower(), "opener": opener_for(store, lead)})
    sheet.sort(key=lambda s: (not s["hot"], s["tries"]))
    return sheet[:limit]


def sheet_card(item: Dict[str, Any], n: int) -> str:
    lead, data = item["lead"], item["lead"].get("data") or {}
    e = lambda v: html.escape(str(v or ""))  # noqa: E731
    bits = [e(data.get("city") or lead.get("market") or "")]
    if data.get("rating"):
        bits.append(f"⭐{e(data['rating'])}" + (f" ({e(data.get('reviews'))})" if data.get("reviews") else ""))
    bits.append(f"{e(item['local_time'])} there")
    head = f"{'🔥 ' if item['hot'] else ''}{n}. <b>{e(lead.get('company') or lead.get('name') or item['phone'])}</b>"
    tries = f" · try {item['tries'] + 1}" if item["tries"] else ""
    return (f"{head} · {' · '.join(b for b in bits if b)}{tries}\n{e(item['phone'])}\n"
            f"<i>“{e(item['opener'])}”</i>")


def card_buttons(lead_id: str) -> List[List[Dict[str, str]]]:
    return [[{"text": label, "callback_data": f"cl:{lead_id}:{code}"} for code, label in CALL_OUTCOMES.items()]]


def _mark_contacted(store, lead: Dict[str, Any], actor: str) -> None:
    """A real call is a first touch: close out a staged 'call' draft so the lead reads SENT."""
    for o in store.outreach_for_lead(lead["id"]):
        if o["channel"] == "call" and o["status"] in ("staged", "approved"):
            try:
                if o["status"] == "staged":
                    store.approve_outreach(o["id"], actor=actor)
                    if (store.get_lead(lead["id"]) or {}).get("state") == "STAGED":
                        store.transition(lead["id"], "APPROVED", actor=actor)
                store.mark_outreach_sent(o["id"], actor=actor)
            except Exception as e:
                log.info(f"call bookkeeping for {lead['id']}: {e}")


def _advance_to_replied(store, lead_id: str, actor: str) -> None:
    path = {"DISCOVERED": "TRIAGED", "TRIAGED": "STAGED", "STAGED": "APPROVED", "APPROVED": "SENT"}
    for _ in range(6):
        state = (store.get_lead(lead_id) or {}).get("state")
        if state in ("REPLIED", "DEPOSIT_OPENED", "WON", "DNC", "LOST", "ARCHIVED") or not state:
            return
        target = "REPLIED" if state in ("SENT", "OPENED", "FORWARDED") else path.get(state)
        if not target:
            return
        try:
            store.transition(lead_id, target, actor=actor, reason="call: interested")
        except Exception as e:
            log.info(f"call: {lead_id} {state}→{target} refused ({e})")
            return


def follow_up_email(store, worker, lead: Dict[str, Any], actor: str = "calls", now: Optional[datetime] = None) -> str:
    """'great talking just now' + two concrete times. Returns a note for Telegram."""
    to = email_of(lead)
    who = lead.get("company") or lead.get("name") or lead.get("contact")
    if not to:
        return (f"Get their email on the call, then send: email {lead['id']} name@company.com "
                f"and the follow-up with two call times goes out.")
    runtime = RuntimeControl()
    from nexus.integrations import booking
    from nexus.integrations.whatsapp_closer import booking_offset
    their = booking_offset(lead)
    busy = [m["start"] for m in booking.booked_meetings(store)]
    slots = booking.open_slots(their, now=now, busy=busy, count=2)
    times = " or ".join(booking.describe(s, their) for s in slots) or "a time that suits you this week"
    hi = f"hey {first_name(lead)}," if first_name(lead) else "hey,"
    body = (f"{hi} great talking just now. as promised, here's the next step: a quick 20 min look at what we'd set "
            f"up for {lead.get('company') or 'you'}, with a working demo on your own setup. does {times} your time "
            f"work?\n\n{sig(worker)}")
    if runtime.killed or runtime.is_dry_run or worker is None:
        return f"[not sent: {'kill switch' if runtime.killed else 'dry run' if runtime.is_dry_run else 'no email channel'}]\n{body}"
    try:
        subject, msg_id = _send_in_thread(worker, lead, to, body, "great talking just now")
    except Exception as e:
        return f"Couldn't email {who} ({e}). Send this yourself:\n{body}"
    store.log_event(lead["id"], "message_out", {"text": body, "subject": subject, "message_id": msg_id,
                                                "channel": "email", "kind": "after_call"}, actor=actor)
    return f"📧 Follow-up with two times sent to {to}."


def log_call(store, worker, lead_id: str, outcome: str, actor: str = "telegram",
             now: Optional[datetime] = None) -> str:
    lead = store.get_lead(lead_id)
    if not lead:
        return f"Unknown lead {lead_id}"
    if outcome not in CALL_OUTCOMES:
        return f"Unknown outcome {outcome}"
    store.log_event(lead_id, "call", {"outcome": outcome, "phone": phone_of(lead)}, actor=actor)
    _mark_contacted(store, lead, actor)
    who = lead.get("company") or lead.get("name") or phone_of(lead)
    if outcome == "no":
        try:
            store.transition(lead_id, "DNC", actor=actor, reason="call: not interested")
        except Exception as e:
            log.info(f"call DNC for {lead_id}: {e}")
        if phone_of(lead):
            store.suppress(f"phone:{phone_of(lead)}", "call: not interested")
        return f"❌ {who}: do-not-contact. Never called or emailed again."
    if outcome == "yes":
        _advance_to_replied(store, lead_id, actor)
        store.log_event(lead_id, "message_in", {"text": "(said yes on a call: interested)", "hot": True,
                                                "intent": "interested", "channel": "call"}, actor="prospect")
        return f"✅ {who}: interested. " + follow_up_email(store, worker, store.get_lead(lead_id), actor, now)
    if outcome == "later":
        return f"🔁 {who}: back on the sheet tomorrow."
    tries = sum(1 for e in _call_events(store, lead_id) if (e.get("payload") or {}).get("outcome") == "na")
    max_tries = int(get_secret("CALL_MAX_TRIES", "3") or 3)
    return f"📵 {who}: no answer ({tries}/{max_tries})." + (" Done calling this one." if tries >= max_tries else "")


def set_email(store, worker, lead_id: str, address: str, actor: str = "telegram") -> str:
    from nexus.integrations.email_channel import EMAIL_RE
    lead = store.get_lead(lead_id)
    if not lead:
        return f"Unknown lead {lead_id}"
    match = EMAIL_RE.search(address or "")
    if not match:
        return "That doesn't look like an email address."
    email = match.group(0).lower().strip(".")
    if store.is_suppressed(f"email:{email}"):
        return "That address is on the do-not-contact list."
    store.update_lead_fields(lead_id, data={**(lead.get("data") or {}), "email": email})
    said_yes = any((e.get("payload") or {}).get("outcome") == "yes" for e in _call_events(store, lead_id))
    already = any((e.get("payload") or {}).get("kind") == "after_call" for e in store.lead_events(lead_id)
                  if e["type"] == "message_out")
    if said_yes and not already:
        return "📧 Saved. " + follow_up_email(store, worker, store.get_lead(lead_id), actor)
    return f"📧 Saved {email} on {lead.get('company') or lead.get('name') or lead_id}."
