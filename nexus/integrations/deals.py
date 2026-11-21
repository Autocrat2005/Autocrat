"""
Cash: deposit requests, gentle chasers, and the follow-through after every call. Zero LLM tokens.

You collect the money yourself (the closer never sends payment details on its own). When a
prospect is ready, one Telegram tap ("📤 Send deposit request") emails them, in the same thread:
an invoice number, the amount, and how to pay (your Wise tag, an optional pay-online link, or
"reply for bank/card details"). Then:

  * 48h with no payment → one friendly "did the invoice land?" email, and you're told.
  * 96h → a red ping to you (call them); nothing more is sent automatically.
  * `paid <id> <amount>` records the money (lead → DEPOSIT_OPENED / WON).

After each booked call (start + MEETING_MINUTES + 15 min) Telegram asks how it went:
  🔥 Wants it   → the deposit request is one tap away
  🤔 Thinking   → a short "want a working demo first?" email goes out
  👻 No-show    → a "we missed each other" email with two fresh times
  ❌ Not a fit  → lead LOST

Settings (.env)
  WISE_TAG            your Wisetag without the @ (shown as @tag)
  PAYMENT_LINK        optional pay-online link (Stripe / Razorpay / Wise), listed first
  DEPOSIT_PCT         50
  DEFAULT_DEAL_USD    1250   when no price was discussed yet (real estate builds default to 2500)
  INVOICE_PREFIX      WR
  PAYMENT_CHASE_HOURS 48
"""

from __future__ import annotations

import html
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from nexus.core.logger import get_logger
from nexus.core.runtime import RuntimeControl
from nexus.core.secrets import env_bool, get_secret

log = get_logger("deals")

NICHE_DEFAULT_USD = {"real_estate": 2500.0}
MEETING_OUTCOMES = {"won": "🔥 Wants it", "think": "🤔 Thinking", "noshow": "👻 No-show", "no": "❌ Not a fit"}
CLOSED_STATES = ("DNC", "WON", "LOST", "ARCHIVED")


# ── small helpers ────────────────────────────────────────────────

def money(amount: float, currency: str = "USD") -> str:
    amount = float(amount or 0)
    return f"{currency.upper()} {amount:,.0f}" if amount == int(amount) else f"{currency.upper()} {amount:,.2f}"


def deposit_pct() -> float:
    try:
        return max(1.0, min(100.0, float(get_secret("DEPOSIT_PCT", "50") or 50)))
    except ValueError:
        return 50.0


def first_name(lead: Dict[str, Any]) -> str:
    name = (lead.get("name") or "").strip()
    if not name or name.lower() == (lead.get("company") or "").strip().lower():
        return ""   # a business list: the "name" is the company, so don't greet it like a person
    return name.split()[0].title()


def email_of(lead: Dict[str, Any]) -> str:
    """Who to email: whoever is actually talking to us (a colleague who replied), else the contact."""
    data = lead.get("data") or {}
    if "@" in str(data.get("reply_to") or ""):
        return str(data["reply_to"]).strip()
    contact = (lead.get("contact") or "").strip()
    if "@" in contact:
        return contact
    return str(data.get("email") or "").strip()


def _payload(e: Dict[str, Any]) -> Dict[str, Any]:
    p = e.get("payload")
    if isinstance(p, str):
        try:
            return json.loads(p or "{}")
        except ValueError:
            return {}
    return p or {}


def deal_value(store, lead: Dict[str, Any]) -> float:
    """The price on the table: the last one the closer quoted or agreed (USD), else a sane default."""
    for e in reversed(store.lead_events(lead["id"])):
        try:
            value = float(_payload(e).get("deal_usd") or 0)
        except (TypeError, ValueError):
            value = 0
        if value > 0:
            return value
    try:
        value = float((lead.get("data") or {}).get("deal_usd") or 0)
    except (TypeError, ValueError):
        value = 0
    if value > 0:
        return value
    if lead.get("niche") in NICHE_DEFAULT_USD:
        return NICHE_DEFAULT_USD[lead["niche"]]
    return float(get_secret("DEFAULT_DEAL_USD", "1250") or 1250)


def deposit_for(store, lead: Dict[str, Any]) -> Tuple[float, float]:
    """(amount to ask now, total). Small quick-start offers are paid in full up front."""
    total = deal_value(store, lead)
    if total <= float(get_secret("FULL_UPFRONT_UNDER_USD", "500") or 500):
        return round(total, 2), total
    return round(total * deposit_pct() / 100.0, 2), total


def next_invoice_no(store, now: Optional[datetime] = None) -> str:
    now = now or datetime.now(timezone.utc)
    seq = int(store.kv_get("invoice:seq") or 0) + 1
    store.kv_set("invoice:seq", str(seq))
    return f"{(get_secret('INVOICE_PREFIX', 'WR') or 'WR').strip()}-{now:%Y}-{seq:04d}"


def payment_lines() -> List[str]:
    lines = []
    link = (get_secret("PAYMENT_LINK", "") or "").strip()
    tag = (get_secret("WISE_TAG", "") or "").strip().lstrip("@")
    if link:
        lines.append(f"- pay online: {link}")
    if tag:
        lines.append(f"- wise: send to @{tag} (search that wisetag in the wise app)")
    lines.append("- card or bank transfer: just reply \"bank\" and i'll send the details")
    return lines


def payment_email(lead: Dict[str, Any], amount: float, currency: str, invoice_no: str, total: float = 0,
                  scope: str = "", signature: str = "Yash") -> str:
    hi = f"hey {first_name(lead)}," if first_name(lead) else "hey,"
    pct = deposit_pct()
    lines = [hi, "", "great, excited to get going on this. here's the deposit to lock in your build slot:", "",
             f"invoice {invoice_no}"]
    if scope:
        lines.append(f"for: {scope}")
    if total and abs(total * pct / 100.0 - amount) < 0.01 and pct < 100:
        lines += [f"total: {money(total, currency)}", f"deposit now ({pct:g}%): {money(amount, currency)}",
                  "the rest once it's live and you're happy with it."]
    else:
        lines.append(f"amount: {money(amount, currency)}")
    lines += ["", "how to pay:", *payment_lines(), ""]
    if env_bool("DEPOSIT_GUARANTEE", True):
        lines += ["our promise: if you don't like the first working version, you get this back in full.", ""]
    lines += ["as soon as it lands i'll send the kickoff plan the same day.", "", signature]
    return "\n".join(lines)


def sig(worker) -> str:
    """Sign-off for people already talking to us (name, company, site)."""
    if hasattr(worker, "sign"):
        return worker.sign(True)
    return getattr(worker, "signature", "Yash")


def _send_in_thread(worker, lead: Dict[str, Any], to: str, body: str, fallback_subject: str) -> Tuple[str, str]:
    thread = worker._thread_of(lead["id"])
    subject = thread["subject"] or fallback_subject
    if thread["subject"] and not subject.lower().startswith("re:"):
        subject = f"Re: {subject}"
    msg_id = worker.transport.send(to, subject, body, in_reply_to=thread["last_id"], references=thread["message_id"])
    return subject, msg_id


# ── deposit requests ─────────────────────────────────────────────

def request_payment(store, worker, lead_id: str, amount: Optional[float] = None, currency: str = "USD",
                    scope: str = "", actor: str = "telegram", now: Optional[datetime] = None) -> Dict[str, Any]:
    """Email the deposit request in the lead's thread and log it. Returns {"ok", "note", ...}."""
    lead = store.get_lead(lead_id)
    if not lead:
        return {"ok": False, "note": f"Unknown lead {lead_id}"}
    if lead["state"] == "DNC":
        return {"ok": False, "note": "That lead is do-not-contact."}
    to = email_of(lead)
    if not to:
        return {"ok": False, "note": "No email for this lead: send the payment details on WhatsApp or the call, "
                                     f"then log it with: paid {lead_id} <amount>"}
    if worker is None:
        return {"ok": False, "note": "Email channel isn't running, so nothing was sent."}
    runtime = RuntimeControl()
    if runtime.killed:
        return {"ok": False, "note": "Kill switch is on: nothing sent."}
    total = 0.0
    if amount is None:
        amount, total = deposit_for(store, lead)
    currency = (currency or "USD").upper()
    invoice = next_invoice_no(store, now)
    body = payment_email(lead, float(amount), currency, invoice, total=total, scope=scope,
                         signature=sig(worker))
    who = lead.get("company") or lead.get("name") or to
    if runtime.is_dry_run:
        return {"ok": True, "dry_run": True, "note": f"[dry run] would email {who}:\n{body}", "body": body}
    subject, msg_id = _send_in_thread(worker, lead, to, body, f"invoice {invoice}")
    store.log_event(lead_id, "message_out", {"text": body, "subject": subject, "message_id": msg_id,
                                             "channel": "email", "kind": "payment_request"}, actor=actor)
    store.log_event(lead_id, "payment_requested", {"amount": float(amount), "currency": currency, "invoice": invoice,
                                                   "total": total, "scope": scope}, actor=actor)
    store.audit(actor, "payment_requested", {"lead_id": lead_id, "amount": amount, "currency": currency,
                                             "invoice": invoice})
    return {"ok": True, "invoice": invoice, "amount": float(amount), "currency": currency,
            "note": f"📤 Deposit request {invoice} ({money(amount, currency)}) sent to {who}. "
                    f"When it lands: paid {lead_id} {amount:g} {currency}"}


def pending_deposits(store) -> List[Dict[str, Any]]:
    """Deposit requests with no payment logged since. Newest request per lead."""
    c = store._conn()
    latest: Dict[str, Dict[str, Any]] = {}
    for lead_id, ts, payload in c.execute(
            "SELECT lead_id, ts, payload FROM lead_events WHERE type='payment_requested' ORDER BY id"):
        latest[lead_id] = {"lead_id": lead_id, "ts": float(ts), **json.loads(payload or "{}")}
    out = []
    for lead_id, req in latest.items():
        paid = c.execute("SELECT COUNT(*) FROM payments WHERE lead_id=? AND ts >= ?", (lead_id, req["ts"])).fetchone()[0]
        lead = store.get_lead(lead_id) or {}
        if paid or lead.get("state") in CLOSED_STATES:
            continue
        out.append({**req, "lead": lead})
    return sorted(out, key=lambda r: r["ts"])


def chase_payments(worker, now: Optional[datetime] = None) -> int:
    """One friendly nudge after PAYMENT_CHASE_HOURS, then a red ping to you at twice that."""
    runtime = RuntimeControl()
    if runtime.killed or runtime.is_dry_run:
        return 0
    store = worker.store
    now = now or datetime.now(timezone.utc)
    hours = float(get_secret("PAYMENT_CHASE_HOURS", "48") or 48)
    done = 0
    for req in pending_deposits(store):
        lead, age_h = req["lead"], (now.timestamp() - req["ts"]) / 3600
        events = [e for e in store.lead_events(lead["id"]) if float(e["ts"]) >= req["ts"]]
        chased = any(e["type"] == "payment_chased" for e in events)
        escalated = any(e["type"] == "payment_escalated" for e in events)
        who = lead.get("company") or lead.get("name") or lead.get("contact")
        due = money(req.get("amount", 0), req.get("currency", "USD"))
        if not chased and age_h >= hours and email_of(lead):
            from nexus.integrations.email_channel import market_offset
            their_now = now + timedelta(minutes=market_offset(lead.get("market") or "", worker.offset))
            if not worker._in_hours(their_now):
                continue
            hi = f"hey {first_name(lead)}," if first_name(lead) else "hey,"
            body = (f"{hi} just checking the invoice came through ok? if wise is a hassle i can send bank "
                    f"details instead, just say the word.\n\n{sig(worker)}")
            try:
                subject, msg_id = _send_in_thread(worker, lead, email_of(lead), body, f"invoice {req.get('invoice', '')}")
            except Exception as e:
                log.info(f"payment chaser for {lead['id']} failed: {e}")
                continue
            store.log_event(lead["id"], "message_out", {"text": body, "subject": subject, "message_id": msg_id,
                                                        "channel": "email", "kind": "payment_chaser"}, actor="deals")
            store.log_event(lead["id"], "payment_chased", {"invoice": req.get("invoice")}, actor="deals")
            worker.alerts.notify(f"💰 Nudged {who} about the {due} deposit ({req.get('invoice')}, {age_h:.0f}h).",
                                 "orange")
            done += 1
        elif chased and not escalated and age_h >= 2 * hours:
            store.log_event(lead["id"], "payment_escalated", {"invoice": req.get("invoice")}, actor="deals")
            phone = (lead.get("data") or {}).get("call_phone") or (lead.get("data") or {}).get("phone") or ""
            worker.alerts.notify(f"💰 {who} still hasn't paid the {due} deposit ({req.get('invoice')}, "
                                 f"{age_h / 24:.0f} days). Worth a personal call or WhatsApp"
                                 f"{': ' + phone if phone else ''}.", "red")
            done += 1
    return done


def money_buttons(store, lead: Dict[str, Any]) -> List[List[Dict[str, str]]]:
    """The one-tap 'send the deposit request' button for a lead that's ready to pay."""
    if not email_of(lead):
        return []
    amount, _ = deposit_for(store, lead)
    return [[{"text": f"📤 Send deposit request ({money(amount)})", "callback_data": f"pay:{lead['id']}"}]]


# ── after the call ───────────────────────────────────────────────

def ask_meeting_outcomes(worker, now: Optional[datetime] = None) -> int:
    """Once per booked call, after it should have ended: 'how did it go?' with four buttons."""
    from nexus.integrations import booking
    store = worker.store
    now = now or datetime.now(timezone.utc)
    minutes = int(get_secret("MEETING_MINUTES", "20") or 20)
    asked = 0
    for m in booking.booked_meetings(store, since=now - timedelta(days=3)):
        if m["start"] + timedelta(minutes=minutes + 15) > now:
            continue
        key = f"meeting:debrief:{m['lead_id']}:{m['start']:%Y%m%d%H%M}"
        if store.kv_get(key):
            continue
        store.kv_set(key, "asked")
        lead = store.get_lead(m["lead_id"]) or {}
        if lead.get("state") in CLOSED_STATES:
            continue
        who = html.escape(str(lead.get("company") or lead.get("name") or lead.get("contact") or "them"))
        mine = booking.describe(m["start"], int(get_secret("OWNER_UTC_OFFSET_MINUTES", "330") or 330))
        buttons = [[{"text": label, "callback_data": f"mo:{m['lead_id']}:{code}"}
                    for code, label in list(MEETING_OUTCOMES.items())[:2]],
                   [{"text": label, "callback_data": f"mo:{m['lead_id']}:{code}"}
                    for code, label in list(MEETING_OUTCOMES.items())[2:]]]
        worker.alerts.notify(f"📞 How did the call with <b>{who}</b> go? ({html.escape(mine)})\n"
                             f"Tap one and Autocrat does the follow-through.", "red", buttons=buttons, html_text=True)
        asked += 1
    return asked


def apply_meeting_outcome(store, worker, lead_id: str, outcome: str, actor: str = "telegram",
                          now: Optional[datetime] = None) -> Dict[str, Any]:
    """Returns {"note": str, "buttons": [...]} for the Telegram reply."""
    lead = store.get_lead(lead_id)
    if not lead:
        return {"note": f"Unknown lead {lead_id}", "buttons": []}
    if outcome not in MEETING_OUTCOMES:
        return {"note": f"Unknown outcome {outcome}", "buttons": []}
    store.log_event(lead_id, "meeting_outcome", {"outcome": outcome}, actor=actor)
    who = lead.get("company") or lead.get("name") or lead.get("contact")
    runtime = RuntimeControl()
    live = not runtime.killed and not runtime.is_dry_run
    to = email_of(lead)

    if outcome == "won":
        amount, total = deposit_for(store, lead)
        buttons = money_buttons(store, lead)
        note = (f"🔥 {who}: deposit request ready ({money(amount)} = {deposit_pct():g}% of {money(total)}). "
                f"Tap to send it, or type: invoice {lead_id} <amount> [currency]")
        if not buttons:
            note = (f"🔥 {who}: no email on file. Send your Wise tag / bank details on WhatsApp, then log it: "
                    f"paid {lead_id} <amount>")
        return {"note": note, "buttons": buttons}

    if outcome == "no":
        try:
            store.transition(lead_id, "LOST", actor=actor, reason="call: not a fit")
        except Exception as e:
            log.info(f"meeting outcome: {lead_id} stays {lead['state']} ({e})")
        return {"note": f"❌ {who} marked lost. Nothing more goes out.", "buttons": []}

    if not to:
        return {"note": f"Noted. {who} has no email on file, so follow up on WhatsApp or by phone.", "buttons": []}
    hi = f"hey {first_name(lead)}," if first_name(lead) else "hey,"
    if outcome == "think":
        body = (f"{hi} thanks for the chat. no pressure at all. if it helps you decide, i can put together a quick "
                f"working demo on your own setup this week so you can see it before committing. want me to?"
                f"\n\n{sig(worker)}")
        kind = "after_call"
    else:   # noshow
        from nexus.integrations import booking
        from nexus.integrations.whatsapp_closer import booking_offset
        their = booking_offset(lead)
        busy = [m["start"] for m in booking.booked_meetings(store)]
        slots = booking.open_slots(their, now=now, busy=busy, count=2)
        times = " or ".join(booking.describe(s, their) for s in slots) or "sometime this week"
        body = (f"{hi} looks like we missed each other, no worries at all. does {times} your time work instead? "
                f"same link either way.\n\n{sig(worker)}")
        kind = "rebook"
    if not live:
        return {"note": f"[{'kill switch' if runtime.killed else 'dry run'}] would email {who}:\n{body}", "buttons": []}
    try:
        subject, msg_id = _send_in_thread(worker, lead, to, body, "our call")
    except Exception as e:
        return {"note": f"Couldn't email {who} ({e}). Send this yourself:\n{body}", "buttons": []}
    store.log_event(lead_id, "message_out", {"text": body, "subject": subject, "message_id": msg_id,
                                             "channel": "email", "kind": kind}, actor=actor)
    return {"note": f"{'🤔' if outcome == 'think' else '👻'} Sent to {who}:\n{body}", "buttons": []}


def post_sale_followups(worker, now: Optional[datetime] = None) -> int:
    """After the final payment: day 7, ask how it's running, for two lines of testimonial, and offer the care
    plan; day 21, ask for an introduction (with the referral thank-you). Templates, once each, no AI cost.
    Settings: CARE_PLAN_USD (149), REFERRAL_REWARD_USD (150); 0 switches that part off."""
    runtime = RuntimeControl()
    if runtime.killed or runtime.is_dry_run:
        return 0
    from nexus.integrations.email_channel import market_offset
    store = worker.store
    now = now or datetime.now(timezone.utc)
    care = float(get_secret("CARE_PLAN_USD", "149") or 0)
    reward = float(get_secret("REFERRAL_REWARD_USD", "150") or 0)
    done = 0
    rows = store._conn().execute("SELECT lead_id, MIN(ts) FROM payments WHERE kind='final' GROUP BY lead_id").fetchall()
    for lead_id, paid_ts in rows:
        lead = store.get_lead(lead_id) or {}
        if lead.get("state") != "WON" or not email_of(lead):
            continue
        steps = {(_payload(e).get("step")) for e in store.lead_events(lead_id) if e["type"] == "post_sale"}
        days = (now.timestamp() - float(paid_ts)) / 86400
        hi = f"hey {first_name(lead)}," if first_name(lead) else "hey,"
        if days >= 7 and "care" not in steps:
            step = "care"
            body = (f"{hi} it's been a week since we went live. how's it running?\n\n"
                    "if you're happy with it, would you write two lines i can share as a testimonial? it helps a lot.")
            if care:
                body += (f"\n\nand if you'd like us to keep an eye on it and handle small changes, we do a care plan "
                         f"at ${care:g} a month, cancel anytime. want me to set that up?")
        elif days >= 21 and "referral" not in steps:
            step = "referral"
            body = (f"{hi} quick one: if you know someone else who'd want something like what we built for you, "
                    "an intro would mean a lot.")
            if reward:
                body += f" there's a ${reward:g} thank-you for any intro that becomes a client."
        else:
            continue
        their_now = now + timedelta(minutes=market_offset(lead.get("market") or "", worker.offset))
        if not worker._in_hours(their_now):
            continue
        body = f"{body}\n\n{sig(worker)}"
        try:
            subject, msg_id = _send_in_thread(worker, lead, email_of(lead), body, "how's it running?")
        except Exception as e:
            log.info(f"post-sale {step} email for {lead_id} failed: {e}")
            continue
        store.log_event(lead_id, "message_out", {"text": body, "subject": subject, "message_id": msg_id,
                                                 "channel": "email", "kind": f"post_sale_{step}"}, actor="deals")
        store.log_event(lead_id, "post_sale", {"step": step}, actor="deals")
        who = lead.get("company") or lead.get("name") or email_of(lead)
        worker.alerts.notify(f"🤝 Sent {who} the {'check-in + care plan' if step == 'care' else 'referral ask'}.", "orange")
        done += 1
    return done


def money_board(store, now: Optional[datetime] = None) -> str:
    """/deals: everything with money attached, in one message (HTML)."""
    from nexus.integrations import booking
    now = now or datetime.now(timezone.utc)
    owner = int(get_secret("OWNER_UTC_OFFSET_MINUTES", "330") or 330)
    lines = ["💰 <b>Money board</b>"]
    pend = pending_deposits(store)
    if pend:
        on_table: Dict[str, float] = {}
        lines.append("Deposits requested, not paid yet:")
        for r in pend:
            on_table[r.get("currency", "USD")] = on_table.get(r.get("currency", "USD"), 0) + float(r.get("amount") or 0)
            age = (now.timestamp() - r["ts"]) / 3600
            who = html.escape(str(r["lead"].get("company") or r["lead"].get("name") or r["lead_id"]))
            lines.append(f"  • {who}: {money(r.get('amount', 0), r.get('currency', 'USD'))} · {r.get('invoice')} · "
                         f"{age:.0f}h ago · <code>paid {r['lead_id']} {float(r.get('amount') or 0):g}</code>")
        lines.append("On the table: " + ", ".join(money(v, k) for k, v in on_table.items()))
    calls = booking.booked_meetings(store, since=now)
    if calls:
        lines.append("Calls booked:")
        for m in calls:
            lead = store.get_lead(m["lead_id"]) or {}
            lines.append(f"  • {html.escape(booking.describe(m['start'], owner))} · "
                         f"{html.escape(str(lead.get('company') or lead.get('name') or '?'))}")
    c = store._conn()
    hot = c.execute("SELECT DISTINCT e.lead_id FROM lead_events e JOIN leads l ON l.id=e.lead_id WHERE e.type='message_in'"
                    " AND json_extract(e.payload,'$.hot') AND e.ts >= ? AND l.state IN ('REPLIED')",
                    (now.timestamp() - 7 * 86400,)).fetchall()
    booked = {m["lead_id"] for m in calls}
    hot = [h for (h,) in hot if h not in booked and h not in {r["lead_id"] for r in pend}]
    if hot:
        lines.append("Hot, no call booked yet (strike now):")
        for lead_id in hot[:10]:
            lead = store.get_lead(lead_id) or {}
            phone = (lead.get("data") or {}).get("call_phone") or (lead.get("data") or {}).get("phone") or ""
            lines.append(f"  • {html.escape(str(lead.get('company') or lead.get('name') or lead_id))}"
                         f"{' · ' + html.escape(phone) if phone else ''} · <code>{lead_id}</code>")
    paid = c.execute("SELECT currency, SUM(amount) FROM payments GROUP BY currency").fetchall()
    lines.append("Paid in so far: " + (", ".join(money(a, cur) for cur, a in paid) if paid else "nothing yet. Let's change that."))
    if len(lines) == 2 and not paid:
        lines.insert(1, "No deposits requested and no calls booked yet.")
    return "\n".join(lines)
