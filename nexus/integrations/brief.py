"""The 9am cash brief: what happened, what's booked, what needs you. Pure SQL + templates, zero tokens."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Optional

from nexus.core.secrets import get_secret


def daily_brief(store, now: Optional[datetime] = None) -> str:
    from nexus.integrations import booking
    now = now or datetime.now(timezone.utc)
    owner = int(get_secret("OWNER_UTC_OFFSET_MINUTES", "330") or 330)
    day_ago = now.timestamp() - 86400
    c = store._conn()
    one = lambda sql, *a: c.execute(sql, a).fetchone()[0] or 0  # noqa: E731
    found = one("SELECT COUNT(*) FROM leads WHERE created_at >= ?", day_ago)
    sent = one("SELECT COUNT(*) FROM outreach WHERE status='sent' AND sent_at >= ?", day_ago)
    followups = one("SELECT COUNT(*) FROM outreach WHERE status='sent' AND kind LIKE 'followup%' AND sent_at >= ?", day_ago)
    replies = c.execute("SELECT payload FROM lead_events WHERE type='message_in' AND ts >= ?", (day_ago,)).fetchall()
    hot = sum(1 for (p,) in replies if (json.loads(p or "{}") or {}).get("hot"))
    money = c.execute("SELECT currency, SUM(amount) FROM payments WHERE ts >= ? GROUP BY currency", (day_ago,)).fetchall()
    waiting = one("SELECT COUNT(*) FROM outreach WHERE status IN ('staged','approved') AND channel='whatsapp'")
    queued = one("SELECT COUNT(*) FROM outreach WHERE status IN ('staged','approved') AND channel='email'")
    open_replied = one("SELECT COUNT(*) FROM leads WHERE state IN ('REPLIED','DEPOSIT_OPENED')")
    total_paid = c.execute("SELECT currency, SUM(amount) FROM payments GROUP BY currency").fetchall()

    lines = [f"☀️ Autocrat brief · {(now + timedelta(minutes=owner)):%a %d %b}",
             f"Last 24h: {found} leads found · {sent} emails sent ({followups} follow-ups) · "
             f"{len(replies)} replies ({hot} hot)"]
    if money:
        lines.append("💰 Money in: " + ", ".join(f"{cur} {amt:,.0f}" for cur, amt in money))
    today_end = now + timedelta(hours=24)
    calls = [m for m in booking.booked_meetings(store, since=now) if m["start"] <= today_end]
    if calls:
        lines.append("📅 Calls in the next 24h:")
        for m in calls:
            lead = store.get_lead(m["lead_id"]) or {}
            lines.append(f"  • {booking.describe(m['start'], owner)} · {lead.get('company') or lead.get('name') or '?'} · {m['link']}")
    from nexus.integrations import calls, deals
    pending = deals.pending_deposits(store)
    if pending:
        lines.append("💰 Deposits waiting: " + " · ".join(
            f"{r['lead'].get('company') or r['lead'].get('name') or r['lead_id']} "
            f"{deals.money(r.get('amount', 0), r.get('currency', 'USD'))}" for r in pending[:6]))
    todo = []
    to_call = len(calls.call_sheet(store, now, ignore_hours=True, limit=500))
    if to_call:
        todo.append(f"{to_call} number(s) on the call sheet: /calls in their business hours")
    if waiting:
        todo.append(f"{waiting} WhatsApp message(s) ready: type /wa")
    if open_replied:
        todo.append(f"{open_replied} live conversation(s): check the deck's Replies panel")
    if todo:
        lines.append("👉 Your move: " + " · ".join(todo))
    lines.append(f"Queue: {queued} email(s) going out in their business hours.")
    if total_paid:
        lines.append("Total paid so far: " + ", ".join(f"{cur} {amt:,.0f}" for cur, amt in total_paid))
    return "\n".join(lines)
