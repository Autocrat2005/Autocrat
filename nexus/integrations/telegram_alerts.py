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
        if not outreach:
            return None
        draft = html.escape(outreach["draft"][:900])
        return {
            "priority": "orange",
            "text": (f"🟠 Ready to send · {_lead_label(lead)}\n"
                     f"Channel: {html.escape(outreach['channel'])}\n\n<i>{draft}</i>"),
            "buttons": [[
                {"text": "✅ Approve", "callback_data": f"oa:{outreach['id']}"},
                {"text": "🚫 DNC", "callback_data": f"dnc:{lead_id}"},
            ]],
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
    if links.get("phone"):
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
