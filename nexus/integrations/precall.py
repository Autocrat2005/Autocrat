"""
Pre-call pack: walk into every call (and every hot reply) already knowing the deal.

For each lead with a booked call or a hot reply, once:
  1. a passive look at their website (site_audit, zero tokens),
  2. ONE cheap LLM call (task "copy" → Haiku) for a one-screen brief: who they are, their pain,
     what to sell, the demo to build before the call (1-2 hours, on THEIR stuff), the price and
     deposit line, likely objections with answers, the first question to ask,
  3. a Telegram card with all of it, plus an optional "ahead of our call" email for them that
     lists 2-3 real things we noticed (you tap to send it; nothing goes out on its own).
"""

from __future__ import annotations

import html
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from nexus.core.logger import get_logger
from nexus.core.runtime import RuntimeControl
from nexus.core.secrets import get_secret
from nexus.integrations import site_audit
from nexus.integrations.deals import _send_in_thread, email_of, first_name

log = get_logger("precall")

AGGREGATORS = ("ycombinator.com", "reddit.com", "google.com", "goo.gl", "madewithlovable.com", "tinylaunch.com",
               "startupbase.io", "x.com", "twitter.com", "linkedin.com", "facebook.com", "instagram.com",
               "producthunt.com", "bayut.com", "propertyfinder.ae", "dubizzle.com")
OFFERS = ("Offers (USD): rapid workflow automation from 1,250 (about a week); custom portal from 2,500; "
          "real-estate WhatsApp AI + CRM from 2,500; vibe-coder rescue (Lovable/Bolt/Cursor app to production) "
          "from 1,250. Always 50% deposit to start, rest on delivery. Free working demo before they pay. "
          "Quick-start first yes (paid in full, credited if they upgrade within 30 days): WhatsApp auto-reply "
          "live in 48h for 399; app health check + top 3 fixes in 72h for 349.")


def site_for(lead: Dict[str, Any]) -> str:
    data = lead.get("data") or {}
    for value in (data.get("website"), data.get("site")):
        if value and str(value).strip():
            return str(value).strip()
    url = (lead.get("url") or "").strip()
    if url.startswith(("http://", "https://")) and not any(a in site_audit.domain_of(url) for a in AGGREGATORS):
        return url
    from nexus.integrations.email_channel import FREEMAIL
    mail = email_of(lead)
    domain = mail.split("@")[-1].lower() if "@" in mail else ""
    return f"https://{domain}" if domain and domain not in FREEMAIL else ""


def leads_needing_pack(store, now: Optional[datetime] = None, limit: int = 3) -> List[str]:
    """Booked calls first (soonest first), then hot replies from the last 3 days. Each lead gets one pack."""
    from nexus.integrations import booking
    now = now or datetime.now(timezone.utc)
    have = {r[0] for r in store._conn().execute("SELECT DISTINCT lead_id FROM lead_events WHERE type='precall_pack'")}
    out: List[str] = [m["lead_id"] for m in booking.booked_meetings(store, since=now) if m["lead_id"] not in have]
    rows = store._conn().execute(
        "SELECT DISTINCT e.lead_id FROM lead_events e JOIN leads l ON l.id=e.lead_id WHERE e.type='message_in'"
        " AND json_extract(e.payload,'$.hot') AND e.ts >= ? AND l.state NOT IN ('DNC','LOST','ARCHIVED','WON')"
        " ORDER BY e.ts DESC", (now.timestamp() - 3 * 86400,)).fetchall()
    out += [r[0] for r in rows if r[0] not in have and r[0] not in out]
    return out[:limit]


def teaser_email(lead: Dict[str, Any], audit: Dict[str, Any], when: str = "", signature: str = "Yash") -> str:
    lines = site_audit.top_findings(audit, lead.get("niche") or "", n=3)
    if len(lines) < 2:
        return ""
    hi = f"hey {first_name(lead)}," if first_name(lead) else "hey,"
    dom = site_audit.domain_of(audit.get("url") or "")
    bullets = "\n".join(f"- {line}" for line in lines)
    tail = f"happy to walk you through the fixes on our call{(' ' + when) if when else ''}." if when else \
        "none of it is hard to fix. want me to show you how we'd sort it?"
    return f"{hi} i had a quick look at {dom} so we don't waste your time. a few things stood out:\n\n{bullets}\n\n{tail}\n\n{signature}"


def build_brief(router, lead: Dict[str, Any], events: List[Dict[str, Any]], audit: Dict[str, Any],
                meeting: str = "") -> Dict[str, Any]:
    from nexus.integrations.whatsapp_closer import lead_context
    talk = [e for e in events if e["type"] in ("message_in", "message_out")]
    convo = "\n".join(f"{'Them' if e['type'] == 'message_in' else 'Us'}: {str((e.get('payload') or {}).get('text', ''))[:600]}"
                      for e in talk[-6:]) or "(no conversation yet)"
    seen = "\n".join(f"- {f['line']}" for f in (audit.get("findings") or [])[:6]) or "- (site not checked)"
    facts = audit.get("facts") or {}
    system = ("You prep Yash (co-founder of a two-person product studio) for a sales call. Be concrete and brief. "
              "Use only the facts given; when you guess, say 'probably'. Never invent clients or results. "
              "No em or en dashes. Return ONLY JSON: {\"who\": \"one line\", \"pain\": \"one line\", "
              "\"sell\": \"the one outcome to sell, in their terms\", \"demo\": \"a demo Yash or Parth can build in 1-2 "
              "hours on THEIR own stuff that would make them say wow on the call\", \"price\": \"which offer, the price "
              "anchor and the exact deposit sentence to say\", \"objections\": [\"objection: one-line answer\", "
              "\"objection: one-line answer\"], \"first_question\": \"the opening question for the call\"}")
    prompt = (f"{OFFERS}\n\nProspect:\n{lead_context(lead, events)}\n"
              f"Website: {audit.get('url') or 'unknown'} (built with: {facts.get('built_with') or 'unknown'})\n"
              f"What we noticed on their site:\n{seen}\n\nConversation so far:\n{convo}\n\n"
              f"{'Call booked: ' + meeting if meeting else 'No call booked yet: the goal is to book one.'}")
    result = router.complete(prompt, task="copy", system=system, json_mode=True, max_tokens=700, lead_id=lead["id"])
    return result.data if isinstance(result.data, dict) else {}


def pack_text(lead: Dict[str, Any], brief: Dict[str, Any], audit: Dict[str, Any], meeting: str = "") -> str:
    e = lambda v: html.escape(str(v or "")).strip()  # noqa: E731
    who = e(lead.get("company") or lead.get("name") or lead.get("contact"))
    lines = [f"🎯 <b>Pre-call pack: {who}</b>" + (f" · call {e(meeting)}" if meeting else " · hot, no call yet")]
    for key, label in (("who", "Who"), ("pain", "Pain"), ("sell", "Sell"), ("demo", "Demo to build first"),
                       ("price", "Price"), ("first_question", "Open with")):
        if brief.get(key):
            lines.append(f"<b>{label}:</b> {e(brief[key])}")
    objections = [o for o in (brief.get("objections") or []) if o][:3]
    if objections:
        lines.append("<b>Objections:</b>\n" + "\n".join(f"  • {e(o)}" for o in objections))
    found = site_audit.top_findings(audit, lead.get("niche") or "", n=4)
    if found:
        lines.append(f"<b>Site check ({e(site_audit.domain_of(audit.get('url') or ''))}):</b>\n"
                     + "\n".join(f"  • {e(x)}" for x in found))
    phone = (lead.get("data") or {}).get("call_phone") or (lead.get("data") or {}).get("phone") or ""
    if phone:
        lines.append(f"📞 {e(phone)}")
    lines.append(f"<code>{lead['id']}</code>")
    return "\n".join(lines)


def prepare_packs(worker, now: Optional[datetime] = None, limit: int = 2,
                  fetch=None) -> int:
    """Build and send up to `limit` packs this tick. Costs one Haiku call per lead, once."""
    if RuntimeControl().killed:
        return 0
    from nexus.integrations import booking
    store = worker.store
    now = now or datetime.now(timezone.utc)
    owner = int(get_secret("OWNER_UTC_OFFSET_MINUTES", "330") or 330)
    meetings = {m["lead_id"]: m for m in booking.booked_meetings(store, since=now)}
    made = 0
    for lead_id in leads_needing_pack(store, now, limit=limit):
        lead = store.get_lead(lead_id)
        if not lead:
            continue
        # Claim it first: a failure later must never retry (and re-bill) every tick.
        store.log_event(lead_id, "precall_pack", {"status": "started"}, actor="precall")
        site = site_for(lead)
        audit = site_audit.audit(site, fetch=fetch) if site else {"url": "", "findings": [], "facts": {}}
        m = meetings.get(lead_id)
        meeting_mine = booking.describe(m["start"], owner) if m else ""
        meeting_theirs = booking.describe(m["start"], int(m.get("their_offset") or 0)) if m else ""
        try:
            brief = build_brief(worker.closer.router, lead, store.lead_events(lead_id), audit,
                                meeting=f"{meeting_mine} your time ({meeting_theirs} theirs)" if m else "")
        except Exception as e:
            log.info(f"pre-call brief for {lead_id} failed: {e}")
            brief = {}
        teaser = teaser_email(lead, audit, when=meeting_theirs and f"on {meeting_theirs}",
                              signature=getattr(worker, "signature", "Yash")) if email_of(lead) else ""
        store.log_event(lead_id, "precall_pack", {"status": "done", "brief": brief, "site": audit.get("url"),
                                                  "findings": [f["line"] for f in audit.get("findings") or []][:8],
                                                  "teaser": teaser}, actor="precall")
        buttons = [[{"text": "📤 Send them the 'ahead of our call' note", "callback_data": f"pt:{lead_id}"}]] if teaser else []
        worker.alerts.notify(pack_text(lead, brief, audit, f"{meeting_mine} your time" if m else ""),
                             "red" if m and m["start"] - now < timedelta(hours=6) else "orange",
                             buttons=buttons, html_text=True)
        made += 1
    return made


def send_teaser(store, worker, lead_id: str, actor: str = "telegram") -> str:
    lead = store.get_lead(lead_id)
    if not lead:
        return f"Unknown lead {lead_id}"
    if any(e["type"] == "message_out" and (e.get("payload") or {}).get("kind") == "precall_teaser"
           for e in store.lead_events(lead_id)):
        return "Already sent."
    packs = [e for e in store.lead_events(lead_id) if e["type"] == "precall_pack" and (e.get("payload") or {}).get("teaser")]
    if not packs or not email_of(lead):
        return "No note to send for this lead."
    body = packs[-1]["payload"]["teaser"]
    runtime = RuntimeControl()
    if runtime.killed or runtime.is_dry_run:
        return f"[{'kill switch' if runtime.killed else 'dry run'}] would send:\n{body}"
    if worker is None:
        return "Email channel isn't running."
    try:
        subject, msg_id = _send_in_thread(worker, lead, email_of(lead), body, "before our call")
    except Exception as e:
        return f"Couldn't send ({e}). Here it is to send yourself:\n{body}"
    store.log_event(lead_id, "message_out", {"text": body, "subject": subject, "message_id": msg_id,
                                             "channel": "email", "kind": "precall_teaser"}, actor=actor)
    return f"📤 Sent to {lead.get('company') or lead.get('name') or email_of(lead)}."


def latest_pack(store, lead_id: str) -> Dict[str, Any]:
    for e in reversed(store.lead_events(lead_id)):
        p = e.get("payload") or {}
        if e["type"] == "precall_pack" and p.get("status") == "done":
            return p if isinstance(p, dict) else json.loads(p)
    return {}
