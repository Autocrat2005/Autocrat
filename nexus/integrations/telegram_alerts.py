"""
Autocrat — Telegram alert rules (pure functions, no telegram import).

Turns EventBus entries into alert specs:
    {"priority": "red"|"orange", "text": <html>, "buttons": [[{text, url|callback_data}]]}

Priorities
  🔴 red     forwarded demo, deposit page opened, prospect replied, won, kill switch, run mode
  🟠 orange  draft staged for approval, demo opened
  (everything else → no alert; the Phase 2 9am digest covers it)

Callback data (≤64 bytes):  oa:<outreach_id>  approve outreach
                            os:<outreach_id>  mark sent
                            dnc:<lead_id>     do-not-contact
"""

from __future__ import annotations

import html
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

RED_STATES = {"FORWARDED", "DEPOSIT_OPENED", "REPLIED", "WON"}
ORANGE_STATES = {"OPENED"}

HEADLINES = {
    "FORWARDED": "🔴 Demo FORWARDED — a second person opened it",
    "DEPOSIT_OPENED": "🔴 Deposit page opened",
    "REPLIED": "🔴 Prospect replied",
    "WON": "🔴 🎉 Deal won",
    "OPENED": "🟠 Demo opened",
}


def _lead_label(lead: Optional[Dict[str, Any]]) -> str:
    if not lead:
        return "unknown lead"
    name = lead.get("name") or lead.get("company") or lead.get("contact") or lead.get("id")
    market = f" · {lead['market']}" if lead.get("market") else ""
    return f"<b>{html.escape(str(name))}</b>{market} (<code>{lead.get('id')}</code>)"


def is_hiring_post(lead: Optional[Dict[str, Any]]) -> bool:
    """r/forhire [Hiring] posts and HN 'SEEKING FREELANCER' comments: buyers with a budget, right now."""
    data = (lead or {}).get("data") or {}
    if data.get("hiring"):
        return True
    text = str(data.get("text") or "").lower()
    return (text.startswith("r/forhire:") and "[hiring]" in text) or text.startswith("seeking freelancer")


def build_alert(entry: Dict[str, Any], store=None) -> Optional[Dict[str, Any]]:
    event = entry.get("event", "")
    data = entry.get("data") or {}

    if event == "control.kill_switch":
        state = "ENGAGED 🛑" if data.get("engaged") else "released"
        reason = f" — {html.escape(str(data.get('reason')))}" if data.get("reason") else ""
        return {"priority": "red", "buttons": [],
                "text": f"🔴 Kill switch {state}{reason}\nby {html.escape(str(data.get('actor', '?')))}"}

    if event == "control.run_mode":
        return {"priority": "red", "buttons": [],
                "text": f"🔴 RUN_MODE → <b>{html.escape(str(data.get('run_mode')))}</b> "
                        f"(by {html.escape(str(data.get('actor', '?')))})"}

    if event.startswith("report."):
        text = str(data.get("text") or "")
        return {"priority": "orange", "buttons": [], "text": html.escape(text)} if text else None

    if event == "payment.received":
        lead = store.get_lead(data.get("lead_id")) if (store and data.get("lead_id")) else None
        who = (lead or {}).get("company") or (lead or {}).get("name") or data.get("lead_id", "?")
        return {"priority": "red", "buttons": [],
                "text": f"💰 <b>{html.escape(str(data.get('currency', 'USD')))} {float(data.get('amount') or 0):,.2f}</b> "
                        f"{html.escape(str(data.get('kind', 'deposit')))} from {html.escape(str(who))}"}

    if event not in ("lead.transition", "lead.signal"):
        return None

    target = data.get("to")
    lead_id = data.get("lead_id")
    lead = store.get_lead(lead_id) if (store and lead_id) else None

    if event == "lead.transition" and target == "STAGED":
        outreach = None
        outreach_id = (data.get("payload") or {}).get("outreach_id")
        if store and outreach_id:
            outreach = store.get_outreach(outreach_id)
        elif store and lead_id:
            staged = [o for o in store.outreach_for_lead(lead_id) if o["status"] == "staged"]
            outreach = staged[-1] if staged else None
        if not outreach or outreach["channel"] == "call":
            return None   # phone leads wait on the /calls sheet for their business hours, not as alerts
        draft = html.escape(outreach["draft"][:900])
        triage = ((lead or {}).get("data") or {}).get("triage") or {}
        score = ""
        if triage:
            score = (f"{html.escape(str(triage.get('icp', '')))} · intent {triage.get('intent')}/10\n"
                     f"{html.escape(str(triage.get('summary', '')))}\n")
        where = f"Channel: {html.escape(outreach['channel'])}"
        if outreach["channel"] in ("email", "call") and lead:
            where += f" → {html.escape(lead.get('contact') or '')}"
        if outreach["channel"] == "call":
            where += " (no email found: call them, the draft is your opening line)"
        send_from = ((lead or {}).get("data") or {}).get("send_from")
        if send_from:
            where += f"\nSend from: <b>{html.escape(str(send_from))}</b>"
        if outreach["channel"] == "whatsapp" and lead:
            # You send WhatsApp by hand from your phone: open the app with the message written (wa.me),
            # then tap ✔ so it's never sent twice. No approve step.
            from nexus.core.deeplinks import wa_me_url
            from nexus.integrations.email_channel import split_subject
            rows = [[{"text": "📲 Open in WhatsApp", "url": wa_me_url(lead.get("contact") or "",
                                                                     split_subject(outreach["draft"])[1])},
                     {"text": "✔ Sent", "callback_data": f"ws:{outreach['id']}"}],
                    [{"text": "🚫 DNC", "callback_data": f"dnc:{lead_id}"}]]
        else:
            rows = [[
                {"text": "✅ Approve", "callback_data": f"oa:{outreach['id']}"},
                {"text": "🚫 DNC", "callback_data": f"dnc:{lead_id}"},
            ]]
        url = (lead or {}).get("url") or ""
        if url.startswith(("http://", "https://")):
            rows.append([{"text": "🔗 Open post", "url": url}])
        if is_hiring_post(lead):
            # Someone is paying for help right now: the first good reply usually wins.
            return {"priority": "red", "buttons": rows,
                    "text": f"⚡ HIRING POST · reply within the hour · {_lead_label(lead)}\n{score}{where}\n\n<i>{draft}</i>"}
        return {
            "priority": "orange",
            "text": f"🟠 Ready to send · {_lead_label(lead)}\n{score}{where}\n\n<i>{draft}</i>",
            "buttons": rows,
        }

    if target in RED_STATES or target in ORANGE_STATES:
        priority = "red" if target in RED_STATES else "orange"
        note = "" if event == "lead.transition" else " <i>(repeat signal)</i>"
        return {"priority": priority, "buttons": [],
                "text": f"{HEADLINES[target]}{note}\n{_lead_label(lead)}"}
    return None


def approved_buttons(outreach_id: str, links: Dict[str, str]) -> List[List[Dict[str, str]]]:
    """Buttons shown after approval: one-tap send from the phone + mark sent."""
    row = []
    # Telegram URL buttons only accept http(s)/tg links (no mailto:); email drafts stay as text.
    if (links.get("phone") or "").startswith(("http://", "https://", "tg://")):
        row.append({"text": "📲 Send now", "url": links["phone"]})
    row.append({"text": "✔ Mark sent", "callback_data": f"os:{outreach_id}"})
    return [row]


def in_quiet_hours(spec: str, now: Optional[datetime] = None, utc_offset_minutes: int = 330) -> bool:
    """spec like "23-8" (local hours). Default offset = IST (UTC+5:30)."""
    if not spec or "-" not in spec:
        return False
    try:
        start, end = (int(x) for x in spec.split("-", 1))
    except ValueError:
        return False
    now = now or datetime.now(timezone.utc)
    local_hour = (now.astimezone(timezone.utc) + timedelta(minutes=utc_offset_minutes)).hour
    if start == end:
        return False
    return (start <= local_hour or local_hour < end) if start > end else (start <= local_hour < end)
