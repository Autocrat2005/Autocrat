"""
Autocrat — WhatsApp Web closer (inbound sales conversations, your voice).

Runs inside the same WhatsApp Web session as the alerts (one browser profile,
one thread). Every poll it:
  1. finds chats with unread messages (skipping your own numbers, the founders'
     group and anything in WHATSAPP_CLOSER_IGNORE),
  2. reads the recent conversation,
  3. asks the LLM router (task "closer") for the next reply in your voice plus
     an intent label,
  4. waits like a person would (reading time, then types at a human speed),
  5. sends it (RUN_MODE=live) or alerts you with the draft (dry_run),
  6. logs both sides on the lead and pings you on hot intents.

Fixed boundaries:
  * It only answers people who messaged the number. It never opens a cold
    conversation. Follow-ups happen only inside conversations the prospect started.
  * On a personal account it only touches unsaved numbers (chat title is a phone
    number) and chats already in the lead pipeline, never saved contacts, unless
    listed in WHATSAPP_CLOSER_ALLOW (WHATSAPP_CLOSER_ONLY_UNSAVED=1, the default).
  * If a message clearly isn't about business, it stays silent.
  * If someone sincerely asks whether they're talking to a bot, it says it's an
    assistant you set up and that you read everything. It doesn't volunteer it.
  * "stop" / not interested → lead goes DNC, one short polite close, then silence.
  * Kill switch → no sends.

Voice: put 15–30 real messages you've sent (and any rules) in the file named by
AUTOCRAT_VOICE_FILE. Em and en dashes are always removed from replies.
"""

from __future__ import annotations

import json
import random
import re
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from nexus.core.events import EventBus
from nexus.core.lead_state import ENGAGEMENT_RANK, LeadState
from nexus.core.logger import get_logger
from nexus.core.runtime import RuntimeControl
from nexus.core.secrets import PROJECT_ROOT, env_list, get_secret

log = get_logger("whatsapp_closer")

INTENTS = ("question", "interested", "pricing", "call", "ready_to_pay", "objection",
           "not_interested", "stop", "bot_check", "other")
HOT_INTENTS = {"pricing", "call", "ready_to_pay"}
CLOSE_INTENTS = {"not_interested", "stop"}

# Best-effort DOM hooks for WhatsApp Web; adjust here if WhatsApp changes its markup.
JS_UNREAD_CHATS = """
() => {
  const pane = document.querySelector('#pane-side') || document;
  const out = [];
  for (const badge of pane.querySelectorAll('span[aria-label*="unread message" i]')) {
    const row = badge.closest('[role="listitem"], [role="row"], [role="gridcell"]') || badge.parentElement;
    const titleEl = row && row.querySelector('span[title]');
    if (titleEl) out.push({title: titleEl.getAttribute('title'), unread: badge.innerText || '1'});
  }
  return out;
}
"""
JS_READ_MESSAGES = """
(limit) => {
  const nodes = Array.from(document.querySelectorAll('#main div.copyable-text[data-pre-plain-text]'));
  return nodes.slice(-limit).map(el => {
    const t = el.querySelector('span.selectable-text');
    return {
      meta: el.getAttribute('data-pre-plain-text') || '',
      text: (t ? t.innerText : el.innerText || '').trim(),
      incoming: !!el.closest('.message-in'),
    };
  }).filter(m => m.text);
}
"""

DASHES = re.compile(r"\s*[—–‒―]\s*")
AI_TELLS = [
    (re.compile(r"\bas an ai( language model| assistant)?\b,?\s*", re.I), ""),
    (re.compile(r"\bI hope this (message )?finds you well[.!]?\s*", re.I), ""),
    (re.compile(r"\b(certainly|absolutely)!\s*", re.I), ""),
]


def humanize(text: str) -> str:
    """Strip em/en dashes and common AI tells; keep it WhatsApp-short."""
    text = DASHES.sub(", ", text or "")
    text = text.replace(", ,", ",").replace(" ,", ",")
    for pattern, repl in AI_TELLS:
        text = pattern.sub(repl, text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip().strip('"').strip()


def phone_from_title(title: str) -> str:
    digits = re.sub(r"\D", "", title or "")
    return f"+{digits}" if 8 <= len(digits) <= 15 and re.fullmatch(r"[+\d\s()-]+", title.strip()) else ""


def reading_delay(incoming_text: str, rng: random.Random) -> float:
    """Seconds a person would take to notice and read before replying."""
    return min(90.0, rng.uniform(12, 35) + len(incoming_text) / 18.0)


def typing_delay_per_char(rng: random.Random) -> int:
    return rng.randint(45, 95)  # ms per character


def load_voice(path: Optional[str] = None) -> str:
    raw = path or get_secret("AUTOCRAT_VOICE_FILE", "../autocrat-revenue-plugins/config/voice.md")
    p = Path(raw)
    if not p.is_absolute():
        p = (PROJECT_ROOT / p).resolve()
    if p.exists():
        return p.read_text(encoding="utf-8")
    log.warning(f"No voice file at {p} — replies will use a neutral casual style")
    return ""


def load_playbook(path: Optional[str] = None) -> str:
    raw = path or get_secret("AUTOCRAT_PLAYBOOK_FILE", "../autocrat-revenue-plugins/config/playbook.md")
    p = Path(raw)
    if not p.is_absolute():
        p = (PROJECT_ROOT / p).resolve()
    return p.read_text(encoding="utf-8") if p.exists() else ""


def lead_context(lead: Optional[Dict[str, Any]], events: Optional[List[Dict[str, Any]]] = None) -> str:
    """What we already know about this person, so the closer never asks twice."""
    if not lead:
        return "(new contact, nothing known yet)"
    triage = (lead.get("data") or {}).get("triage") or {}
    parts = [f"name: {lead.get('name') or 'unknown'}"]
    for key, label in (("company", "company"), ("market", "market"), ("niche", "segment"), ("source", "found via")):
        if lead.get(key):
            parts.append(f"{label}: {lead[key]}")
    if triage.get("summary"):
        parts.append(f"what we know: {triage['summary']}")
    if triage.get("pain"):
        parts.append(f"their pain in their words: {triage['pain']}")
    parts.append(f"pipeline stage: {lead.get('state')}")
    if events:
        sent = sum(1 for e in events if e.get("type") == "message_out")
        received = sum(1 for e in events if e.get("type") == "message_in")
        parts.append(f"messages so far: {received} from them, {sent} from us")
    return "\n".join(parts)


def build_system_prompt(voice: str, offers: Dict[str, Any], name: str = "Yash", playbook: str = "",
                        context: str = "", channel: str = "WhatsApp") -> str:
    booking_link = offers.get("booking_link") or ""
    booking = (f"share this link: {booking_link}" if booking_link else
               "offer exactly TWO of the open times listed with the conversation, written in their timezone "
               "(e.g. 'does tue 10:30am or wed 4pm your time work?'). When they clearly agree to one, put that "
               "time's ISO value in \"meeting\" and confirm it in their time plus the meeting link given with the "
               "conversation. If none fit, ask what suits them and offer the next ones. Move to a call fast: "
               "as soon as they show real interest, propose the call")
    pay_usd = offers.get("deposit_link_usd") or ""
    pay_inr = offers.get("deposit_link_inr") or ""
    if pay_usd or pay_inr:
        payment = (f"- Deposit links (only when they say they want to start): USD/GBP/AED {pay_usd or '(Yash sends it)'}; "
                   f"INR {pay_inr or '(Yash sends it)'}")
    else:
        payment = ("- Payment: Yash collects it himself. When they say they want to start, reply in ONE message: what "
                   "they get, the price, 50% to start and the rest when it's live, and that Yash is sending the invoice "
                   "with payment details in a few minutes. Never write payment or bank details yourself, never "
                   "invent an invoice number or a link.")
    prompt = f"""You are replying on {channel} on behalf of Yash, co-founder of a two-person studio that ships
fast workflow automations and small custom portals. Write exactly the way Yash writes (see the voice
samples). Your job: understand what they need and move them to a call or a deposit, using CLOSER.

Sales framework: CLOSER. Work out which step the conversation is at and do only that step's next move.
- C, Clarify why they're here: ask what made them reach out / what they want fixed.
- L, Label the problem: restate it in one line in their words and check you've got it right.
- O, Overview the past: what have they tried already, and why didn't it work.
- S, Sell the outcome, not the build: what their day looks like once it's done (time saved, leads
  answered in seconds). Keep it concrete and believable, no hype.
- E, Explain away concerns: price ("half to start, half once it's live", "free demo first"),
  time ("about a week"), trust ("we're new, so we show a working demo before you pay"),
  "need to think / ask my partner" (offer a short call with both of them, ask what would make it a yes).
- R, Reinforce the decision: once they say yes, congratulate briefly, confirm next steps and the call time,
  and keep momentum. Don't re-sell after they've agreed.
Never skip ahead to price in the first message unless they asked. One question per message.

Offers (quote these freely when they ask or when you reach E):
- Rapid workflow automation: from $1,250, usually delivered in about 7 days
- Custom micro-SaaS portal: from $2,500
- 50% deposit to start, rest on delivery
- Quick-start offers (the easy first yes when the full build feels big, or they hesitate on price; paid
  in full up front; the price is credited if they upgrade within 30 days):
  WhatsApp auto-reply live in 48 hours, $399 (real estate / any WhatsApp-heavy business);
  app health check + top 3 fixes done in 72 hours, $349 (AI-built Lovable/Bolt/Cursor apps)
- Booking a call: {booking}
{payment}
- Lowest-risk next step when they hesitate: a free working demo on their own stuff before they pay anything.

Style rules:
- WhatsApp length: 1 to 3 short sentences, sometimes 2 short messages separated by a blank line.
- Never use em dashes or en dashes. Use commas or full stops.
- No corporate filler, no "I hope this finds you well", no bullet lists unless they asked for detail.
- Match their language and energy. One question at a time.

Honesty rules (never break these):
- Don't bring up that you're an assistant. But if they sincerely ask whether they're talking to a bot,
  AI or a real person, say it's an assistant Yash set up and that Yash reads every chat and can jump in.
- Never invent past clients, results or guarantees. Never promise dates or prices beyond the offers above.
- If they say stop or not interested, reply once briefly and politely, and stop.

Return ONLY a JSON object:
{{"reply": "<message to send, or empty string to stay silent>",
  "intent": one of {list(INTENTS)},
  "hot": true|false,
  "summary": "<one line for Yash about where this lead stands>",
  "meeting": "<ISO time from the open times, ONLY once they confirmed it, else empty string>",
  "deal_usd": <the price in USD you quoted or they agreed to in this conversation, as a number, else 0>}}
Use intent "ready_to_pay" only when they clearly said yes to going ahead (or asked how to pay).

Everyone here is a prospect we contacted first: ALWAYS reply (sales is the goal) unless they said stop
or not interested. If it's a canned support or ticket answer (a help desk, "check our docs/terms"), thank
them in one line, say it isn't a support question, and ask who looks after product or engineering (or the
founder's email) so you reach someone who can buy. Return an empty reply only for messages that are
clearly not from a prospect at all (a friend, a delivery, a wrong number).

Sales playbook (follow it like a professional closer would):
{playbook or '(no playbook file: use CLOSER above)'}

Voice samples from Yash (match tone, casing, slang, emoji use):
{voice or '(none provided: be casual, warm and direct)'}

What we already know about this person (don't re-ask things answered here):
{context or '(nothing yet)'}
"""
    return prompt.replace("Yash", name or "Yash")


def format_transcript(messages: List[Dict[str, Any]], contact: str) -> str:
    lines = []
    for m in messages:
        who = (contact or "Them") if m.get("incoming") else "Me"
        lines.append(f"{who}: {m.get('text', '')}")
    return "\n".join(lines)


def booking_offset(lead: Optional[Dict[str, Any]]) -> int:
    """The prospect's UTC offset in minutes, from their market (falls back to EMAIL_UTC_OFFSET_MINUTES)."""
    from nexus.integrations.email_channel import market_offset
    default = int(get_secret("EMAIL_UTC_OFFSET_MINUTES", "240") or 240)
    return market_offset((lead or {}).get("market") or "", default)


def _iso(t) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%SZ") if t else ""


class WhatsAppCloser:
    def __init__(self, notifier, store=None, router=None, events: Optional[EventBus] = None,
                 voice: Optional[str] = None, rng: Optional[random.Random] = None,
                 sleep: Callable[[float], None] = time.sleep):
        self.notifier = notifier
        self._store = store
        self._router = router
        self.events = events or EventBus()
        self.voice = load_voice() if voice is None else voice
        self.playbook = load_playbook()
        self.rng = rng or random.Random()
        self.sleep = sleep
        self.ignore = {x.strip().lower() for x in env_list("WHATSAPP_CLOSER_IGNORE")}
        self.allow = {x.strip().lower() for x in env_list("WHATSAPP_CLOSER_ALLOW")}
        self.only_unsaved = (get_secret("WHATSAPP_CLOSER_ONLY_UNSAVED", "1") or "1").lower() in ("1", "true", "yes")
        self.persona = get_secret("AUTOCRAT_CLOSER_NAME", "Yash") or "Yash"
        self.max_replies_per_hour = int(get_secret("WHATSAPP_CLOSER_MAX_PER_CHAT_HOUR", "12") or 12)
        self.poll_seconds = float(get_secret("WHATSAPP_CLOSER_POLL_SECONDS", "40") or 40)
        self.offers = {
            "booking_link": get_secret("BOOKING_LINK"),
            "deposit_link_usd": get_secret("DEPOSIT_LINK_USD"),
            "deposit_link_inr": get_secret("DEPOSIT_LINK_INR"),
        }
        self._replies: Dict[str, List[float]] = {}

    # ── wiring ───────────────────────────────────────────────────

    @property
    def store(self):
        if self._store is None:
            from nexus.core.store import get_store
            self._store = get_store()
        return self._store

    @property
    def router(self):
        if self._router is None:
            from nexus.core.llm_router import get_router
            self._router = get_router()
        return self._router

    def attach(self) -> None:
        self.notifier.add_poller(self.poll, lambda: self.poll_seconds * self.rng.uniform(0.7, 1.4))

    # ── decisions (pure, testable) ───────────────────────────────

    def should_skip(self, title: str) -> bool:
        t = (title or "").strip()
        if not t or t.lower() in self.ignore:
            return True
        if t in self.notifier.groups:
            return True
        phone = phone_from_title(t)
        if phone and re.sub(r"\D", "", phone) in self.notifier.numbers:
            return True
        if not self.only_unsaved or phone or t.lower() in self.allow:
            return False
        # A saved contact (named chat): only if it's already a lead in the pipeline.
        return self.store.get_lead_by_dedupe_key(f"whatsapp:name:{t.lower()}") is None

    def rate_ok(self, key: str, now: Optional[float] = None) -> bool:
        now = now or time.time()
        recent = [t for t in self._replies.get(key, []) if now - t < 3600]
        self._replies[key] = recent
        return len(recent) < self.max_replies_per_hour

    def lead_for(self, title: str) -> Dict[str, Any]:
        phone = phone_from_title(title)
        key = f"whatsapp:{re.sub(r'[^0-9]', '', phone)}" if phone else f"whatsapp:name:{title.lower()}"
        lead, _ = self.store.upsert_lead(source="whatsapp_inbound", dedupe_key=key, name=title,
                                         channel="whatsapp", contact=phone or title, actor="closer")
        return lead

    def decide(self, transcript: str, lead_id: Optional[str] = None, channel: str = "WhatsApp") -> Dict[str, Any]:
        lead = self.store.get_lead(lead_id) if lead_id else None
        events = self.store.lead_events(lead_id) if lead_id else None
        system = build_system_prompt(self.voice, self.offers, self.persona, playbook=self.playbook,
                                     context=lead_context(lead, events), channel=channel)
        # Live booking info goes in the message, not the system prompt, so the big system prompt stays cacheable.
        from nexus.integrations import booking
        their_offset = booking_offset(lead)
        busy = [m["start"] for m in booking.booked_meetings(self.store)]
        slots = booking.open_slots(their_offset, busy=busy) if not self.offers.get("booking_link") else []
        link = booking.meeting_link(lead_id or "")
        call_info = (f"\n\nOpen times for a call (their time = ISO): {booking.slots_text(slots, their_offset)}\n"
                     f"Meeting link: {link}") if slots else ""
        prompt = (f"Conversation so far (latest last):\n{transcript}{call_info}\n\n"
                  f"Write {self.persona}'s next {channel} reply.")
        result = self.router.complete(prompt, task="closer", system=system, json_mode=True,
                                      max_tokens=600, lead_id=lead_id)
        data = result.data if isinstance(result.data, dict) else {}
        intent = data.get("intent") if data.get("intent") in INTENTS else "other"
        try:
            deal = float(str(data.get("deal_usd") or 0).replace(",", "").replace("$", ""))
        except ValueError:
            deal = 0.0
        return {
            "deal_usd": deal if 50 <= deal <= 1_000_000 else 0.0,
            "reply": humanize(str(data.get("reply") or "")),
            "intent": intent,
            "hot": bool(data.get("hot")) or intent in HOT_INTENTS,
            "summary": str(data.get("summary") or "")[:300],
            "channel": channel,
            "meeting": _iso(booking.parse_meeting(str(data.get("meeting") or ""), slots)),
            "meeting_link": link,
            "their_offset": their_offset,
        }

    def apply_outcome(self, lead: Dict[str, Any], incoming: str, decision: Dict[str, Any], alert: bool = True) -> None:
        """Update lead state + alert founders. Called whether or not we send."""
        lead_id = lead["id"]
        self.store.log_event(lead_id, "message_in", {"text": incoming[:2000], "intent": decision.get("intent"),
                                                     "hot": bool(decision.get("hot")),
                                                     **({"deal_usd": decision["deal_usd"]} if decision.get("deal_usd") else {})},
                             actor="prospect")
        if decision.get("meeting"):
            self.store.log_event(lead_id, "meeting_booked", {"start": decision["meeting"],
                                                             "link": decision.get("meeting_link", ""),
                                                             "their_offset": decision.get("their_offset", 0),
                                                             "channel": decision.get("channel", "")}, actor="closer")
        state = LeadState(lead["state"])
        if state in ENGAGEMENT_RANK and ENGAGEMENT_RANK[state] < ENGAGEMENT_RANK[LeadState.REPLIED]:
            self.store.transition(lead_id, "REPLIED", actor="closer")
        if decision["intent"] in CLOSE_INTENTS:
            try:
                self.store.transition(lead_id, "DNC", actor="closer", reason=decision["intent"])
            except Exception as e:
                log.warning(f"DNC transition failed for {lead_id}: {e}")
        if decision["hot"] and alert:
            name = lead.get("name") or lead.get("contact")
            where = "email" if decision.get("channel") == "email" else "WhatsApp"
            self.notifier.notify(f"🔴 Hot on {where}: *{name}* ({decision['intent']})\n{decision['summary']}",
                                 priority="red")

    # ── the poll (runs on the WhatsApp Web thread) ───────────────

    def poll(self, page) -> int:
        handled = 0
        try:
            chats = page.evaluate(JS_UNREAD_CHATS) or []
        except Exception as e:
            log.warning(f"Could not read chat list: {e}")
            return 0
        for chat in chats[:5]:
            title = chat.get("title") or ""
            if self.should_skip(title):
                continue
            try:
                if self._handle_chat(page, title):
                    handled += 1
            except Exception as e:
                log.warning(f"Closer failed on chat {title!r}: {e}")
        return handled

    def _open_chat(self, page, title: str) -> None:
        css = '"' + title.replace("\\", "\\\\").replace('"', '\\"') + '"'
        page.locator(f"#pane-side span[title={css}]").first.click()
        page.locator("#main").wait_for(state="visible", timeout=15_000)
        self.sleep(self.rng.uniform(1.0, 2.5))

    def _handle_chat(self, page, title: str) -> bool:
        self._open_chat(page, title)
        messages = page.evaluate(JS_READ_MESSAGES, 30) or []
        if not messages or not messages[-1].get("incoming"):
            return False  # last word is already ours
        lead = self.lead_for(title)
        if lead["state"] == LeadState.DNC.value:
            self.notifier.notify(f"🟠 DNC contact *{title}* messaged again; not auto-replying.", priority="orange")
            return False
        incoming = "\n".join(m["text"] for m in messages[-3:] if m.get("incoming"))
        decision = self.decide(format_transcript(messages, title), lead_id=lead["id"])
        self.apply_outcome(lead, incoming, decision)
        reply = decision["reply"]
        if not reply:
            return False

        runtime = RuntimeControl()
        if runtime.killed:
            log.warning("Kill switch engaged; closer not sending")
            return False
        if runtime.is_dry_run:
            self.notifier.notify(f"🟠 [dry run] Closer would reply to *{title}* ({decision['intent']}):\n{reply}",
                                 priority="orange")
            self.store.log_event(lead["id"], "draft_out", {"text": reply, "intent": decision["intent"]},
                                 actor="closer")
            return True
        if not self.rate_ok(title):
            self.notifier.notify(f"🟠 Closer paused on *{title}*: reply limit reached this hour.", priority="orange")
            return False

        self.sleep(reading_delay(incoming, self.rng))
        self._type_and_send(page, reply)
        self._replies.setdefault(title, []).append(time.time())
        self.store.log_event(lead["id"], "message_out", {"text": reply, "intent": decision["intent"]},
                             actor="closer")
        return True

    def _type_and_send(self, page, reply: str) -> None:
        parts = [p.strip() for p in reply.split("\n\n") if p.strip()]
        for i, part in enumerate(parts):
            compose = self.notifier._wait_any(page, "compose", 15_000)
            if compose is None:
                raise RuntimeError("compose box not found")
            compose.click()
            for j, line in enumerate(part.split("\n")):
                if j:
                    page.keyboard.press("Shift+Enter")
                page.keyboard.type(line, delay=typing_delay_per_char(self.rng))
            self.sleep(self.rng.uniform(0.4, 1.2))
            page.keyboard.press("Enter")
            if i < len(parts) - 1:
                self.sleep(self.rng.uniform(2.0, 6.0))
