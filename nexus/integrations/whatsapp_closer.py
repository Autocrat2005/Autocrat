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
from nexus.core.secrets import PROJECT_ROOT, env_bool, env_list, get_secret

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


SIGNOFF_WORDS = r"(?:cheers|thanks|thank you|best|regards|best regards|talk soon|speak soon|ttyl)"


def strip_signoff(text: str, names=("yash",)) -> str:
    """The email signature is added separately: drop a sign-off the AI wrote itself ("yash", "cheers, yash")."""
    lines = (text or "").rstrip().split("\n")
    who = "|".join(re.escape(n.strip().lower()) for n in names if n and n.strip())
    if not who:
        return (text or "").strip()
    name_line = re.compile(rf"^\W*(?:{SIGNOFF_WORDS}\W*)?(?:{who})(?:\s+\w+)?\W*$", re.I)
    bare = re.compile(rf"^\W*{SIGNOFF_WORDS}\W*$", re.I)
    while lines and not lines[-1].strip():
        lines.pop()
    if len(lines) > 1 and name_line.match(lines[-1].strip()):
        lines.pop()
        while lines and not lines[-1].strip():
            lines.pop()
        if len(lines) > 1 and bare.match(lines[-1].strip()):   # "thanks," on the line above the name
            lines.pop()
    return "\n".join(lines).strip()


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


def load_proof(path: Optional[str] = None) -> str:
    """True things we may cite (case studies, guarantees). Lives in the private config, never invented."""
    raw = path or get_secret("AUTOCRAT_PROOF_FILE", "../autocrat-revenue-plugins/config/proof.md")
    p = Path(raw)
    if not p.is_absolute():
        p = (PROJECT_ROOT / p).resolve()
    return p.read_text(encoding="utf-8").strip() if p.exists() else ""


def lead_context(lead: Optional[Dict[str, Any]], events: Optional[List[Dict[str, Any]]] = None) -> str:
    """What we already know about this person, so the closer never asks twice."""
    if not lead:
        return "(new contact, nothing known yet)"
    data = lead.get("data") or {}
    triage = data.get("triage") or {}
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
    from nexus.integrations.site_audit import relevant_line
    findings = [str(f) for f in ((data.get("site_check") or {}).get("findings") or [])
                if relevant_line(str(f), lead.get("niche") or "")][:3]
    if findings:
        parts.append("things we noticed on their website (send these, as a short list, when they ask to see "
                     "them or say yes to our offer to share them): " + " | ".join(findings))
    if data.get("demo_url"):
        parts.append(f"their personal demo page (share when they ask for details): {data['demo_url']}")
    return "\n".join(parts)


def build_system_prompt(voice: str, offers: Dict[str, Any], name: str = "Yash", playbook: str = "",
                        context: str = "", channel: str = "WhatsApp", proof: str = "") -> str:
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
    company = offers.get("company") or "Windrose Labs"
    site = offers.get("site") or ""
    mvp_range = offers.get("mvp_range") or ""
    mvp_price = (f"quote this range when they ask: {mvp_range}" if mvp_range else
                 "do NOT quote a price in writing: say it depends on scope and takes a 20 minute call to pin down")
    terms = []
    if offers.get("guarantee", True):
        terms.append("- Deposit-back guarantee on the smaller builds: if they don't like the first working version, "
                     "the deposit is refunded. Use it for trust or price worries.")
    if offers.get("care_plan_usd"):
        terms.append(f"- Care plan after delivery: ${offers['care_plan_usd']:g} a month for upkeep and small changes. "
                     "Mention it only once they've agreed to a build or ask about support.")
    if offers.get("referral_usd"):
        terms.append(f"- Referral thank-you: ${offers['referral_usd']:g} for an introduction that becomes a paying "
                     "client. Mention it only if they say it's not for them but know someone, or after a sale.")
    terms_text = "\n".join(terms)
    site_line = f"Our site, share it only when they ask who we are or want to see work: {site}" if site else ""
    known = ("What we already know about this person (do not re-ask things answered here):\n" + context) if context \
        else "What we already know about this person comes with the conversation: do not re-ask things answered there."
    prompt = f"""You are replying on {channel} on behalf of Yash, co-founder of {company}, a two-person product
studio (Yash + Parth) that ships MVPs, AI agents, workflow automations and small custom portals. Write exactly
the way Yash writes (see the voice samples). Your job: understand what they need and move them to a call
or a deposit, using CLOSER.

Sales framework: CLOSER. Work out which step the conversation is at and do only that step's next move.
- C, Clarify why they're here: ask what made them reach out / what they want fixed.
- L, Label the problem: restate it in one line in their words and check you've got it right.
- O, Overview the past: what have they tried already, and why didn't it work.
- S, Sell the outcome, not the build: what their day looks like once it's done (time saved, leads
  answered in seconds). Keep it concrete and believable, no hype.
- E, Explain away concerns: price ("half to start, half once it's live", "free demo first"),
  time ("about a week"), trust (name the ONE most relevant piece of real work from the proof list, plus
  "we show you a working demo before you pay"),
  "need to think / ask my partner" (offer a short call with both of them, ask what would make it a yes).
- R, Reinforce the decision: once they say yes, congratulate briefly, confirm next steps and the call time,
  and keep momentum. Don't re-sell after they've agreed.
Never skip ahead to price in the first message unless they asked. One question per message.

Sell what fits THIS person (their segment is in "What we know about them"):
- Software founders, SaaS, apps, games, AI-built products (segment vibe_coder, mvp, or a product launch): talk
  about the product itself: shipping the next feature they don't have time for, payments and payouts, auth,
  scaling, reliability, the MVP. NEVER pitch WhatsApp, live chat, "enquiries at 2am" or lead response to them.
- Agencies, brokers, insurance, clinics, setup consultants (real_estate, insurance, business_setup): enquiries
  answered instantly, follow-ups, booking, client portals.
- Small teams with manual work (automation): the one recurring task worth automating first.

Momentum (a reply that only asks another question is a wasted reply):
- If they answered your question and showed no problem, do NOT dig for a new problem from an unrelated angle.
  Say in a few words what was good about their answer (specific, peer to peer, no flattery), then give ONE
  concrete, relevant idea for their product or the likely next hard thing at their stage, say in one line what
  we do, and offer an easy next step: two call times, or "want me to send 3 quick ideas for X?".
- By our second message in a thread there must be a concrete next step on the table (a call, a demo, a small
  scoped offer). Discovery questions: two at most in the whole thread.
- If they clearly need nothing, close warmly in one or two lines and leave the door open. No pushing.
- Never sign off with a name: the signature is added automatically.

Offers (quote these freely when they ask or when you reach E):
- Rapid workflow automation: from $1,250, usually delivered in about 7 days
- Custom micro-SaaS portal: from $2,500
- 50% deposit to start, rest on delivery
- Quick-start offers (the easy first yes when the full build feels big, or they hesitate on price; paid
  in full up front; the price is credited if they upgrade within 30 days):
  WhatsApp auto-reply live in 48 hours, $399 (real estate / any WhatsApp-heavy business);
  app health check + top 3 fixes done in 72 hours, $349 (AI-built Lovable/Bolt/Cursor apps)
- MVP in 30 days (a new product, an AI agent, a knowledge/RAG tool, a marketplace, anything bigger than a
  small automation): "live in 30 days or you don't pay", for the scope agreed in writing before kickoff.
  Price: {mvp_price}. Name the guarantee and the one most relevant case study, then offer two call times.
- Booking a call: {booking}. When you confirm a call, ask in the same message for the best number to reach
  them on in case the link fails.
{payment}
- Lowest-risk next step when they hesitate: a free working demo on their own stuff before they pay anything.
{terms_text}

Real proof you may cite (true; at most ONE per message, only when it helps, in your own short words, never
embellished, never with numbers that aren't written here):
{proof or '(none on file: do not mention past work at all)'}
{site_line}

Style rules:
- WhatsApp length: 1 to 3 short sentences, sometimes 2 short messages separated by a blank line.
- Never use em dashes or en dashes. Use commas or full stops.
- No corporate filler, no "I hope this finds you well", no bullet lists unless they asked for detail.
- Match their language and energy. One question at a time.

Honesty rules (never break these):
- Don't bring up that you're an assistant. But if they sincerely ask whether they're talking to a bot,
  AI or a real person, say it's an assistant Yash set up and that Yash reads every chat and can jump in.
- Never invent clients, results, numbers, reviews or guarantees. The only past work you may mention is the
  proof list above. Never promise dates or prices beyond the offers above.
- If they say stop or not interested, reply once briefly and politely, and stop.

Return ONLY a JSON object:
{{"reply": "<message to send, or empty string to stay silent>",
  "intent": one of {list(INTENTS)},
  "hot": true|false,
  "summary": "<one line for Yash about where this lead stands>",
  "meeting": "<ISO time from the open times, ONLY once they confirmed it, else empty string>",
  "deal_usd": <the price in USD you quoted or they agreed to in this conversation, as a number, else 0>,
  "follow_up_days": <if they asked to be contacted later ("try me in january", "after eid", "next month"),
                     the number of days from today until then, else 0>,
  "phone": "<their phone or WhatsApp number if they wrote one in this conversation, else empty string>"}}
Use intent "ready_to_pay" only when they clearly said yes to going ahead (or asked how to pay).
If they say "not now" with a time, agree in one line, set follow_up_days, and don't push.

Everyone here is a prospect we contacted first: ALWAYS reply (sales is the goal) unless they said stop
or not interested. If it's a canned support or ticket answer (a help desk, "check our docs/terms"), thank
them in one line, say it isn't a support question, and ask who looks after product or engineering (or the
founder's email) so you reach someone who can buy. Return an empty reply only for messages that are
clearly not from a prospect at all (a friend, a delivery, a wrong number).

Sales playbook (follow it like a professional closer would):
{playbook or '(no playbook file: use CLOSER above)'}

Voice samples from Yash (match tone, casing, slang, emoji use):
{voice or '(none provided: be casual, warm and direct)'}

{known}
"""
    return prompt.replace("Yash", name or "Yash")


# Things a reply must never contain: proof we can't back up, payment details, made-up paperwork.
LINT_RULES = [
    (re.compile(r"\bour (other |past |previous |existing |happy )?(clients|customers)\b", re.I),
     "talks about 'our clients' in general (name one real piece of work from the proof list, or none)"),
    (re.compile(r"\b(we've|we have|i've|i have) (helped|worked with|built for|served) (\d+|dozens|hundreds|many|several|lots)", re.I),
     "claims a number of past clients"),
    (re.compile(r"\b\d+\+?\s+(clients|customers|businesses|agencies|brokers|brokerages|companies|founders|startups|teams)\b", re.I),
     "claims a number of clients"),
    (re.compile(r"\b\d+\+?\s+years?\b[^.?!\n]{0,30}\b(experience|in business|building|shipping)", re.I),
     "claims years of experience"),
    (re.compile(r"\b(testimonials?|award[- ]winning|5[- ]star|five[- ]star|top[- ]rated|trusted by|as seen (in|on))\b", re.I),
     "claims reviews or awards"),
    (re.compile(r"\b(increase|boost|grow|improve|reduce|cut|save|lift)\w*\b[^.?!\n]{0,40}\d+\s?%|"
                r"\d+\s?%\s+(more|fewer|faster|higher|lower|increase|reduction|growth|boost|uplift)", re.I),
     "promises a result percentage"),
    (re.compile(r"\b(iban|swift code|routing number|account number|sort code|ifsc|wise\.com|paypal\.me)\b", re.I),
     "writes payment details (Yash sends those)"),
    (re.compile(r"\b[A-Z]{2,4}-20\d{2}-\d{3,}\b"), "invents an invoice number"),
]
LINK_RE = re.compile(r"(?:https?://|www\.)[^\s)>\]\"']+", re.I)


def _host(value: str) -> str:
    from urllib.parse import urlparse
    value = (value or "").strip()
    if not value:
        return ""
    if "@" in value and "://" not in value:
        value = value.split("@")[-1]
    host = (urlparse(value if "://" in value else f"https://{value}").hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def closer_lint(reply: str, allowed_hosts=()) -> List[str]:
    """Problems that must stop a reply from going out. Empty list = fine. No tokens, no network."""
    from nexus.integrations.email_channel import PLACEHOLDER_RE
    text = reply or ""
    problems = [why for pattern, why in LINT_RULES if pattern.search(text)]
    allowed = {h for h in (_host(a) for a in allowed_hosts) if h}
    for link in LINK_RE.findall(text):
        host = _host(link.rstrip(".,;!?"))
        if host and not any(host == a or host.endswith("." + a) for a in allowed):
            problems.append(f"includes a link that isn't ours or theirs ({host})")
    if PLACEHOLDER_RE.search(text):
        problems.append("has a template placeholder")
    if text.count("?") > 2:
        problems.append("asks more than one question")
    return problems


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
            "company": get_secret("EMAIL_COMPANY", "Windrose Labs") or "Windrose Labs",
            "site": (get_secret("EMAIL_SITE", "") or "").strip(),
            "mvp_range": (get_secret("MVP_PRICE_RANGE", "") or "").strip(),
            "guarantee": env_bool("DEPOSIT_GUARANTEE", True),
            "care_plan_usd": float(get_secret("CARE_PLAN_USD", "149") or 0),
            "referral_usd": float(get_secret("REFERRAL_REWARD_USD", "150") or 0),
        }
        self.proof = load_proof()
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

    def _ask(self, prompt: str, system: str, task: str, lead_id: Optional[str], max_tokens: int) -> Dict[str, Any]:
        result = self.router.complete(prompt, task=task, system=system, json_mode=True,
                                      max_tokens=max_tokens, lead_id=lead_id)
        return result.data if isinstance(result.data, dict) else {}

    def _hot_ok(self) -> bool:
        """The stronger model is used only while its own small monthly cap (llm.task_budgets) has room."""
        refusal = getattr(self.router, "task_refusal", None)
        return bool(refusal) and env_bool("CLOSER_HOT_MODEL", True) and not refusal("closer_hot")

    def _allowed_hosts(self, lead: Optional[Dict[str, Any]], link: str) -> List[str]:
        data = (lead or {}).get("data") or {}
        return [link, "meet.jit.si", "wa.me", "causalith.com", self.offers.get("site") or "",
                self.offers.get("booking_link") or "", self.offers.get("deposit_link_usd") or "",
                self.offers.get("deposit_link_inr") or "", get_secret("PAYMENT_LINK", "") or "",
                get_secret("MEETING_LINK", "") or "", (lead or {}).get("url") or "",
                (lead or {}).get("contact") or "", str(data.get("website") or ""), str(data.get("demo_url") or ""),
                str(data.get("reply_to") or "")]

    def decide(self, transcript: str, lead_id: Optional[str] = None, channel: str = "WhatsApp") -> Dict[str, Any]:
        lead = self.store.get_lead(lead_id) if lead_id else None
        events = self.store.lead_events(lead_id) if lead_id else None
        # Everything that changes per lead goes in the message, so the big system prompt stays cacheable.
        system = build_system_prompt(self.voice, self.offers, self.persona, playbook=self.playbook,
                                     channel=channel, proof=getattr(self, "proof", ""))
        from nexus.integrations import booking
        their_offset = booking_offset(lead)
        busy = [m["start"] for m in booking.booked_meetings(self.store)]
        slots = booking.open_slots(their_offset, busy=busy) if not self.offers.get("booking_link") else []
        link = booking.meeting_link(lead_id or "")
        call_info = (f"\n\nOpen times for a call (their time = ISO): {booking.slots_text(slots, their_offset)}\n"
                     f"Meeting link: {link}") if slots else ""
        prompt = (f"What we know about them:\n{lead_context(lead, events)}\n\n"
                  f"Conversation so far (latest last):\n{transcript}{call_info}\n\n"
                  f"Write {self.persona}'s next {channel} reply.")
        data = self._ask(prompt, system, "closer", lead_id, 1100)
        intent = data.get("intent") if data.get("intent") in INTENTS else "other"
        model = "standard"
        if (bool(data.get("hot")) or intent in HOT_INTENTS or intent == "objection") and data.get("reply") \
                and self._hot_ok():
            # A money moment: let the stronger model write this one (the first answer stays as the fallback).
            try:
                better = self._ask(prompt, system, "closer_hot", lead_id, 2000)
                if str(better.get("reply") or "").strip():
                    data = {**data, **{k: v for k, v in better.items() if v not in (None, "")}}
                    intent = data.get("intent") if data.get("intent") in INTENTS else intent
                    model = "strong"
            except Exception as e:
                log.info(f"hot-reply model unavailable, keeping the standard answer: {e}")

        reply = strip_signoff(humanize(str(data.get("reply") or "")), (self.persona, "yash", "parth"))
        held: List[str] = []
        hosts = self._allowed_hosts(lead, link)
        problems = closer_lint(reply, hosts) if reply else []
        if problems:
            draft = reply
            try:
                fix = self._ask(f"{prompt}\n\nYour draft was:\n{reply}\n\nIt breaks these rules: "
                                f"{'; '.join(problems)}. Rewrite it so it breaks none of them.",
                                system, "closer", lead_id, 1100)
                reply = strip_signoff(humanize(str(fix.get("reply") or "")), (self.persona, "yash", "parth"))
            except Exception as e:
                log.info(f"closer rewrite failed: {e}")
                reply = ""
            if not reply or closer_lint(reply, hosts):
                held, reply = problems, ""          # never send it: the founder gets the draft instead
                log.warning(f"Closer reply for {lead_id} held: {'; '.join(problems)}")
            else:
                draft = ""
        else:
            draft = ""
        try:
            deal = float(str(data.get("deal_usd") or 0).replace(",", "").replace("$", ""))
        except ValueError:
            deal = 0.0
        try:
            later = max(0, min(365, int(float(str(data.get("follow_up_days") or 0)))))
        except ValueError:
            later = 0
        from nexus.integrations.calls import cc_for_market, e164
        phone = e164(str(data.get("phone") or ""), cc_for_market((lead or {}).get("market") or ""))
        return {
            "deal_usd": deal if 50 <= deal <= 1_000_000 else 0.0,
            "reply": reply,
            "intent": intent,
            "hot": bool(data.get("hot")) or intent in HOT_INTENTS,
            "summary": str(data.get("summary") or "")[:300],
            "channel": channel,
            "meeting": _iso(booking.parse_meeting(str(data.get("meeting") or ""), slots)),
            "meeting_link": link,
            "their_offset": their_offset,
            "follow_up_days": later,
            "phone": phone,
            "model": model,
            "held": held,
            "draft": draft,
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
        if decision.get("follow_up_days"):
            # "not now, try me in January": remember the date; the email worker checks back then.
            self.store.log_event(lead_id, "snooze", {"until": time.time() + decision["follow_up_days"] * 86400,
                                                     "days": decision["follow_up_days"]}, actor="closer")
        if decision.get("phone"):
            fresh = self.store.get_lead(lead_id) or lead
            known = fresh.get("data") or {}
            if not known.get("call_phone") and not known.get("phone"):
                self.store.update_lead_fields(lead_id, data={**known, "call_phone": decision["phone"]})
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
