"""
Autocrat HUD — one snapshot of the revenue engine, plus the few actions the deck can take.

Everything here reads the local store; nothing calls an LLM, so the HUD costs nothing to run.
Numbers are real counts only (no invented pipeline dollars): leads per stage, sends against
the daily cap, replies, LLM spend against each provider's 30-day budget.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any, Dict, List, Optional

from nexus.core.secrets import get_secret

FUNNEL = [("FOUND", None), ("QUALIFIED", "TRIAGED"), ("STAGED", "STAGED"), ("SENT", "SENT"),
          ("REPLIED", "REPLIED"), ("WON", "WON")]
REPORT_LINE = re.compile(r"^(\d+)\.\s+(.+?)\s+\(wtp\s+(\d+)/10,\s+(\d+)\s+mentions\)\s*$")


def _midnight() -> float:
    t = time.localtime()
    return time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 0, 0, 0, 0, 0, -1))


def _ago(ts: Optional[float]) -> str:
    if not ts:
        return "never"
    s = max(0, int(time.time() - float(ts)))
    if s < 90:
        return f"{s}s ago"
    if s < 5400:
        return f"{s // 60}m ago"
    if s < 172800:
        return f"{s // 3600}h ago"
    return f"{s // 86400}d ago"


def _plugin(engine, name: str):
    return (getattr(engine, "plugins", {}) or {}).get(name)


def parse_opportunities(report: str) -> List[Dict[str, Any]]:
    """Fallback for reports saved before the structured copy existed."""
    ideas: List[Dict[str, Any]] = []
    for line in (report or "").splitlines():
        m = REPORT_LINE.match(line.strip())
        if m:
            ideas.append({"pain": m.group(2), "wtp": int(m.group(3)), "mentions": int(m.group(4)), "service": ""})
        elif ideas and line.strip().startswith("sell now:"):
            ideas[-1]["service"] = line.strip()[len("sell now:"):].strip()
    return ideas


def build_snapshot(engine) -> Dict[str, Any]:
    store = engine.store
    c = store._conn()
    now = time.time()
    today = _midnight()

    # ── funnel: cumulative, from the event log (a lead that moved on still counts as having been staged)
    total = c.execute("SELECT COUNT(*) FROM leads").fetchone()[0]
    reached = {r[0]: r[1] for r in c.execute(
        "SELECT to_state, COUNT(DISTINCT lead_id) FROM lead_events WHERE to_state IS NOT NULL GROUP BY to_state")}
    sent_total = c.execute("SELECT COUNT(*) FROM outreach WHERE status='sent'").fetchone()[0]
    funnel = []
    for label, state in FUNNEL:
        n = total if state is None else (sent_total if state == "SENT" else reached.get(state, 0))
        funnel.append({"label": label, "n": n})
    replied = reached.get("REPLIED", 0)

    states = {r[0]: r[1] for r in c.execute("SELECT state, COUNT(*) FROM leads GROUP BY state")}
    sent_today = c.execute("SELECT COUNT(*) FROM outreach WHERE status='sent' AND sent_at >= ?",
                           (today,)).fetchone()[0]
    found_today = c.execute("SELECT COUNT(*) FROM leads WHERE created_at >= ?", (today,)).fetchone()[0]

    # ── approval queue
    queue = []
    auto_email = str(get_secret("EMAIL_AUTO_SEND", "0")).strip() in ("1", "true", "yes")
    for r in c.execute(
            "SELECT o.id, o.lead_id, o.channel, o.kind, o.draft, o.status, o.created_at, l.name, l.company, l.source,"
            " l.market, l.contact, l.url FROM outreach o JOIN leads l ON l.id = o.lead_id"
            " WHERE o.status IN ('staged','approved') ORDER BY o.created_at DESC LIMIT 60"):
        row = dict(r)
        row["auto"] = bool(auto_email and row["channel"] == "email")
        row["age"] = _ago(row["created_at"])
        queue.append(row)

    # ── radar sources
    sources = []
    radar = _plugin(engine, "scout_radar")
    if radar is not None:
        try:
            cfgs = radar._sources()
        except Exception:
            cfgs = {}
        counts = {r[0]: (r[1], r[2]) for r in c.execute(
            "SELECT source, COUNT(*), SUM(CASE WHEN created_at >= ? THEN 1 ELSE 0 END) FROM leads GROUP BY source",
            (now - 86400,))}
        runnable = set(getattr(radar, "_fetchers", {}) or {})
        for name, cfg in cfgs.items():
            cfg = cfg or {}
            if name not in runnable and cfg.get("type") != "lead_list":
                continue
            last = store.kv_get(f"radar:last:{name}")
            every = float(cfg.get("every_hours", 12))
            tot, day = counts.get(name, (0, 0))
            next_in = max(0.0, float(last or 0) + every * 3600 - now) if last else 0.0
            sources.append({
                "name": name, "enabled": bool(cfg.get("enabled")), "every_hours": every,
                "last": _ago(float(last)) if last else "never",
                "next": ("due" if next_in <= 0 else f"in {int(next_in // 3600)}h {int(next_in % 3600 // 60)}m"),
                "leads_total": tot or 0, "leads_24h": day or 0,
                "kind": "list" if cfg.get("type") == "lead_list" else "radar",
            })
        sources.sort(key=lambda s: (not s["enabled"], -s["leads_24h"], s["name"]))

    # ── LLM budget
    budget: Dict[str, Any] = {}
    try:
        from nexus.core.llm_router import get_router
        budget = get_router().budget_status()
    except Exception as e:
        budget = {"error": str(e)}
    spend_today = store.spend_since(today)

    # ── live feed: lead events + audit trail, newest first
    feed = []
    for r in c.execute(
            "SELECT e.ts, e.type, e.from_state, e.to_state, e.actor, l.company, l.name, l.source"
            " FROM lead_events e JOIN leads l ON l.id = e.lead_id ORDER BY e.id DESC LIMIT 40"):
        who = (r["company"] or r["name"] or r["source"] or "")[:60]
        moved = r["to_state"] and r["to_state"] != r["from_state"]
        text = (f"{who}: {(r['from_state'] or 'new').lower()} → {r['to_state'].lower()}" if moved
                else f"{who}: {(r['type'] or '').replace('_', ' ')}")
        feed.append({"ts": r["ts"], "text": text, "actor": r["actor"] or "", "kind": (r["to_state"] or r["type"] or "").lower()})
    for a in store.audit_entries(limit=20):
        action = (a.get("action") or "").replace("_", " ")
        feed.append({"ts": a.get("ts"), "text": f"{action} by {a.get('actor') or 'system'}",
                     "actor": a.get("actor", ""), "kind": "audit"})
    feed.sort(key=lambda f: f["ts"] or 0, reverse=True)
    feed = feed[:40]

    # ── opportunity scout
    ideas: List[Dict[str, Any]] = []
    raw = store.kv_get("opportunity:last_json")
    if raw:
        try:
            ideas = (json.loads(raw) or {}).get("opportunities") or []
        except (TypeError, json.JSONDecodeError):
            ideas = []
    if not ideas:
        ideas = parse_opportunities(store.kv_get("opportunity:last_report") or "")
    last_scout = store.kv_get("opportunity:last_run")

    # ── money: attribution by source (which radar actually pays)
    by_source: Dict[str, Dict[str, Any]] = {}
    for r in c.execute("SELECT source, COUNT(*) FROM leads GROUP BY source"):
        by_source[r[0]] = {"source": r[0], "found": r[1], "contacted": 0, "replied": 0, "hot": 0,
                           "paid_usd": 0.0, "won": 0}
    for r in c.execute("SELECT l.source, COUNT(DISTINCT o.lead_id) FROM outreach o JOIN leads l ON l.id=o.lead_id"
                       " WHERE o.status='sent' GROUP BY l.source"):
        by_source.setdefault(r[0], {"source": r[0], "found": 0})["contacted"] = r[1]
    for r in c.execute("SELECT l.source, COUNT(DISTINCT e.lead_id),"
                       " COUNT(DISTINCT CASE WHEN json_extract(e.payload,'$.hot') THEN e.lead_id END)"
                       " FROM lead_events e JOIN leads l ON l.id=e.lead_id WHERE e.type='message_in' GROUP BY l.source"):
        by_source[r[0]]["replied"], by_source[r[0]]["hot"] = r[1], r[2]
    paid_total: Dict[str, float] = {}
    for r in c.execute("SELECT l.source, p.currency, SUM(p.amount) FROM payments p JOIN leads l ON l.id=p.lead_id"
                       " GROUP BY l.source, p.currency"):
        paid_total[r[1]] = paid_total.get(r[1], 0.0) + float(r[2] or 0)
        if r[1] == "USD":
            by_source[r[0]]["paid_usd"] = round(float(r[2] or 0), 2)
    for r in c.execute("SELECT source, COUNT(*) FROM leads WHERE state='WON' GROUP BY source"):
        by_source[r[0]]["won"] = r[1]
    attribution = sorted((dict({"contacted": 0, "replied": 0, "hot": 0, "paid_usd": 0.0, "won": 0}, **v)
                          for v in by_source.values()),
                         key=lambda x: (-x["paid_usd"], -x["hot"], -x["replied"], -x["contacted"], -x["found"]))

    # ── replies that need a human (or a deposit link)
    replies = []
    for r in c.execute(
            "SELECT l.id, l.company, l.name, l.source, l.state, l.contact, e.ts, e.payload FROM leads l"
            " JOIN lead_events e ON e.id = (SELECT MAX(id) FROM lead_events WHERE lead_id=l.id AND type='message_in')"
            " WHERE l.state NOT IN ('DNC','ARCHIVED','LOST') ORDER BY e.ts DESC LIMIT 25"):
        try:
            p = json.loads(r["payload"] or "{}")
        except (TypeError, json.JSONDecodeError):
            p = {}
        paid = c.execute("SELECT COALESCE(SUM(amount),0) FROM payments WHERE lead_id=?", (r["id"],)).fetchone()[0]
        replies.append({"lead_id": r["id"], "company": r["company"], "name": r["name"], "source": r["source"],
                        "state": r["state"], "contact": r["contact"], "age": _ago(r["ts"]), "paid": paid,
                        "text": (p.get("text") or "")[:400], "intent": p.get("intent") or "", "hot": bool(p.get("hot"))})

    followups = {k: v for k, v in c.execute(
        "SELECT status, COUNT(*) FROM outreach WHERE kind LIKE 'followup%' GROUP BY status")}
    try:
        from nexus.integrations.email_channel import compute_cap
        cap = compute_cap(store, int(get_secret("EMAIL_DAILY_CAP", "20") or 20),
                          int(get_secret("EMAIL_CAP_MAX", "40") or 40), int(get_secret("EMAIL_CAP_STEP", "3") or 3))
    except Exception as e:
        cap = {"cap": int(get_secret("EMAIL_DAILY_CAP", "20") or 20), "note": str(e)}

    rt = engine.runtime.status()
    return {
        "attribution": attribution,
        "revenue": {"paid": {k: round(v, 2) for k, v in paid_total.items()}, "won": reached.get("WON", 0),
                    "deposits": reached.get("DEPOSIT_OPENED", 0)},
        "replies": replies,
        "followups": {"staged": followups.get("staged", 0) + followups.get("approved", 0),
                      "sent": followups.get("sent", 0)},
        "cap": cap,
        "ts": now,
        "control": {**rt, "profile": getattr(engine, "profile", "full")},
        "today": {"sent": sent_today, "cap": int(cap.get("cap") or 20),
                  "found": found_today, "llm_usd": round(spend_today.get("cost_usd", 0.0), 4),
                  "llm_calls": spend_today.get("calls", 0)},
        "funnel": funnel,
        "states": states,
        "rates": {"reply_pct": round(100 * replied / sent_total, 1) if sent_total else 0.0,
                  "cost_per_lead": round(budget.get("cost_usd", 0.0) / total, 5) if total and "cost_usd" in budget else 0.0,
                  "sent_total": sent_total, "replied": replied, "won": reached.get("WON", 0)},
        "queue": queue,
        "sources": sources,
        "budget": budget,
        "feed": feed,
        "opportunities": {"ideas": ideas[:5], "last": _ago(float(last_scout)) if last_scout else "never"},
    }


def do_action(engine, action: str, ident: str = "", text: str = "", actor: str = "hud") -> Dict[str, Any]:
    """The deck's buttons. Approvals and DNC go through the same engine commands Telegram uses."""
    store = engine.store
    if action == "approve":
        return engine.execute(f"approve outreach {ident}", actor=actor)
    if action == "dnc":
        out = store.get_outreach(ident)
        lead_id = out["lead_id"] if out else ident
        return engine.execute(f"dnc {lead_id}", actor=actor)
    if action == "skip":
        store.reject_outreach(ident, actor=actor, reason=text or "skipped from HUD")
        return {"success": True, "result": "Skipped. The lead is archived and nothing was sent."}
    if action == "edit":
        store.update_outreach_draft(ident, text, actor=actor)
        return {"success": True, "result": "Draft updated."}
    if action == "run_source":
        radar = _plugin(engine, "scout_radar")
        if radar is None:
            return {"success": False, "error": "scout_radar plugin not loaded"}
        store.audit(actor, "radar_run", {"source": ident})
        return {"success": True, "result": radar.run_source(ident)}
    if action == "payment":
        # text: "<amount> [currency] [deposit|final]", e.g. "625 USD deposit"
        parts = (text or "").replace("$", "").replace(",", "").split()
        if not parts:
            return {"success": False, "error": "amount needed, e.g. 625 USD deposit"}
        amount = float(parts[0])
        currency = next((p.upper() for p in parts[1:] if len(p) == 3 and p.isalpha()), "USD")
        kind = "final" if any(p.lower() in ("final", "won", "full") for p in parts[1:]) else "deposit"
        pay = store.record_payment(ident, amount, currency=currency, kind=kind, actor=actor)
        return {"success": True, "result": f"{pay['currency']} {pay['amount']:,.2f} {pay['kind']} recorded."}
    if action == "announce":
        # A note to the founders (Telegram + WhatsApp group) through the normal alert path.
        from nexus.core.events import EventBus
        if not (text or "").strip():
            return {"success": False, "error": "nothing to announce"}
        store.audit(actor, "announce", {"chars": len(text), "to": ident or "all"})
        if ident == "group":   # re-send to the WhatsApp group(s) only
            wa = getattr(engine, "wa_alerts", None)
            if wa is None:
                return {"success": False, "error": "WhatsApp alerts are not running"}
            wa.notify(text.strip()[:3500], "red", only="groups")
            return {"success": True, "result": "Queued for the WhatsApp group."}
        EventBus().emit("report.announcement", {"text": text.strip()[:3500]})
        return {"success": True, "result": "Sent to Telegram and WhatsApp."}
    if action == "opportunities":
        return engine.execute("opportunities", actor=actor)
    return {"success": False, "error": f"unknown action '{action}'"}
