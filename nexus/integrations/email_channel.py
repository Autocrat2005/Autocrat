"""
Autocrat — email channel: sends openers and answers replies with the same closer brain.

Outbound
  * Any email outreach you APPROVE in Telegram is sent from your mailbox.
  * With EMAIL_AUTO_SEND=1, staged email openers are sent automatically too (no tap),
    inside EMAIL_SEND_HOURS in the recipient's timezone, max EMAIL_DAILY_CAP a day.
  * Every opener carries a one-line opt-out ("reply 'no' and i won't email again");
    a "no" makes the lead DNC forever.

Inbound
  * Polls the inbox (IMAP), only looks at mail from people already in the pipeline,
    reads their reply, asks the closer (playbook + voice + what we know about them)
    for the next message, and replies in the same thread (RUN_MODE=live) or alerts you
    with the draft (dry_run). Hot intents ping you on Telegram/WhatsApp.

Growth (all automatic, all inside the same daily cap)
  * Follow-ups: no reply EMAIL_FOLLOWUP_DAYS after the opener (default 3, then 4 more) → a short,
    new-angle bump in the SAME thread; at most two; any reply, "no" or bounce stops them.
  * Bounces: mailer-daemon reports are matched to the lead → contact suppressed, lead LOST.
  * Warm-up: the cap starts at EMAIL_DAILY_CAP and rises EMAIL_CAP_STEP per clean sending day
    (sent mail, zero bounces) up to EMAIL_CAP_MAX; a 7-day bounce rate over 5% drops it back.

Nothing runs while the kill switch is engaged. Settings: EMAIL_* in .env.
"""

from __future__ import annotations

import email
import imaplib
import random
import re
import smtplib
import ssl
import threading
import time
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import make_msgid, parseaddr
from typing import Any, Callable, Dict, List, Optional

from nexus.core.lead_state import contact_key
from nexus.core.logger import get_logger
from nexus.core.runtime import RuntimeControl
from nexus.core.secrets import env_bool, get_secret

log = get_logger("email")

# UTC offsets (minutes) used to send inside the RECIPIENT's business hours.
# Standard time; good enough for a 9-18 window. Unknown markets fall back to EMAIL_UTC_OFFSET_MINUTES.
MARKET_UTC_OFFSETS = {
    "uae": 240, "dubai": 240, "abu dhabi": 240, "saudi": 180, "qatar": 180,
    "india": 330, "uk": 0, "united kingdom": 0, "ireland": 0, "europe": 60, "germany": 60,
    "us": -300, "usa": -300, "united states": -300, "us east": -300, "us west": -480,
    "us central": -360, "us mountain": -420, "us pacific": -480,
    "canada": -300, "toronto": -300, "vancouver": -480, "australia": 600, "sydney": 600,
    "singapore": 480, "global": None, "unknown": None,
}


def market_offset(market: str, default: int) -> int:
    key = (market or "").strip().lower()
    for name, offset in MARKET_UTC_OFFSETS.items():
        if offset is not None and (key == name or key.startswith(name + " ") or name in key.split(", ")):
            return offset
    return default


# Template leftovers like [first name] or {company}: never send these to a real person.
PLACEHOLDER_RE = re.compile(r"\[[^\]]{1,30}\]|\{[^}]{1,30}\}|\b(?:first name|your name|company name)\b", re.I)

POSTAL_MARKETS = ("us", "usa", "united states", "us east", "us west", "canada", "toronto", "vancouver")


def needs_postal(market: str) -> bool:
    """US (CAN-SPAM) and Canada (CASL) require the sender's postal address in commercial email."""
    key = (market or "").strip().lower()
    return any(key == m or key.startswith(m + " ") or m in key.split(", ") for m in POSTAL_MARKETS)


FREEMAIL = {"gmail.com", "googlemail.com", "yahoo.com", "outlook.com", "hotmail.com", "live.com", "icloud.com",
            "me.com", "aol.com", "proton.me", "protonmail.com", "zoho.com", "zohomail.com", "zohomail.in", "gmx.com",
            "yandex.com", "mail.com", "msn.com", "rediffmail.com", "yahoo.co.in", "ymail.com"}
AUTO_SUBJECT = re.compile(r"^\s*(auto(matic)?[ -]?(reply|response)|autoreply|out of (the )?office|ooo\b|away:|auto:)", re.I)


def html_to_text(raw: str) -> str:
    import html as _html
    raw = re.sub(r"(?is)<(script|style|head).*?</\1>", " ", raw or "")
    raw = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>|</li>|</h\d>", "\n", raw)
    text = _html.unescape(re.sub(r"<[^>]+>", " ", raw))
    return "\n".join(" ".join(line.split()) for line in text.splitlines() if line.strip())


def is_auto_reply(msg) -> bool:
    """Out-of-office and robot replies: recorded, never counted as a reply, never answered."""
    auto = (msg.get("Auto-Submitted") or "").strip().lower()
    if auto and auto != "no":
        return True
    if msg.get("X-Autoreply") or msg.get("X-Autorespond") or msg.get("X-Autoresponse"):
        return True
    if (msg.get("Precedence") or "").strip().lower() in ("auto_reply", "bulk", "junk"):
        return True
    return bool(AUTO_SUBJECT.search(msg.get("Subject") or ""))


def parse_mail(raw: bytes, uid: str = "", flags: str = "") -> Dict[str, Any]:
    from email.header import decode_header, make_header
    msg = email.message_from_bytes(raw or b"")
    plain, html_body = "", ""
    for part in (msg.walk() if msg.is_multipart() else [msg]):
        if part.get_filename():
            continue
        ctype = part.get_content_type()
        if ctype in ("text/plain", "text/html"):
            text = (part.get_payload(decode=True) or b"").decode(part.get_content_charset() or "utf-8", "replace")
            if ctype == "text/plain" and not plain:
                plain = text
            elif ctype == "text/html" and not html_body:
                html_body = text
    try:
        subject = str(make_header(decode_header(msg.get("Subject", ""))))
    except Exception:
        subject = msg.get("Subject", "")
    dsn = " ".join(str(part) for part in msg.walk()
                   if part.get_content_type() in ("message/delivery-status", "text/rfc822-headers"))
    return {"uid": uid, "seen": "\\Seen" in (flags or ""), "from": parseaddr(msg.get("From", ""))[1].lower(),
            "subject": subject, "message_id": msg.get("Message-ID", "").strip(),
            "in_reply_to": msg.get("In-Reply-To", "").strip(), "references": msg.get("References", ""),
            "body": plain.strip() or html_to_text(html_body), "dsn": dsn[:4000], "auto": is_auto_reply(msg)}


BOUNCE_SENDERS = ("mailer-daemon", "postmaster", "mail-daemon", "mailerdaemon")
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def is_bounce_sender(addr: str) -> bool:
    local = (addr or "").split("@")[0].lower()
    return any(local.startswith(b) for b in BOUNCE_SENDERS)


def is_delay_notice(mail: Dict[str, Any]) -> bool:
    """A 'still trying to deliver' warning, not a failure. The server retries for days and a real failure arrives
    as its own report, so counting a delay as a bounce wrongly holds the sending limit down."""
    dsn = str(mail.get("dsn") or "")
    if re.search(r"^\s*action:\s*failed", dsn, re.I | re.M):
        return False
    if re.search(r"^\s*action:\s*delayed", dsn, re.I | re.M):
        return True
    subject = str(mail.get("subject") or "")
    return bool(re.search(r"\bdelay", subject, re.I)) and not re.search(r"fail|undeliver|returned|rejected", subject, re.I)


def compute_cap(store, base: int, cap_max: int = 60, step: int = 3,
            now_ts: Optional[float] = None) -> Dict[str, Any]:
    """Today's cap: base + step per clean sending day (last 14), back to base if bounces spike."""
    now_ts = now_ts or time.time()
    c = store._conn()
    sends = {r[0]: r[1] for r in c.execute(
        "SELECT CAST((? - sent_at) / 86400 AS INTEGER), COUNT(*) FROM outreach WHERE channel='email'"
        " AND status='sent' AND sent_at >= ? GROUP BY 1", (now_ts, now_ts - 14 * 86400))}
    bounces = {r[0]: r[1] for r in c.execute(
        "SELECT CAST((? - ts) / 86400 AS INTEGER), COUNT(*) FROM lead_events WHERE type='bounce'"
        " AND ts >= ? GROUP BY 1", (now_ts, now_ts - 14 * 86400))}
    min_day = max(3, base // 4)
    clean = sum(1 for d, n in sends.items() if d >= 1 and n >= min_day and not bounces.get(d))
    s7 = sum(n for d, n in sends.items() if d < 7)
    b7 = sum(n for d, n in bounces.items() if d < 7)
    rate = (b7 / s7) if s7 else 0.0
    if s7 >= 10 and rate > 0.05:
        cap, why = base, f"held at base: {rate:.0%} bounces in 7 days"
    else:
        cap = min(cap_max, base + step * clean)
        why = f"{clean} clean day(s) → +{step * clean}" if clean else "warming up"
    return {"cap": max(1, cap), "base": base, "max": cap_max, "clean_days": clean,
            "sent_7d": s7, "bounces_7d": b7, "bounce_rate": round(rate, 4), "note": why}


def reply_alert(lead: Dict[str, Any], sender: str, text: str, decision: Dict[str, Any], outcome: str) -> str:
    """One readable alert per email reply: who, what they said, what happened."""
    who = lead.get("company") or lead.get("name") or sender
    if lead.get("name") and lead.get("company"):
        who = f"{lead['name']} ({lead['company']})"
    head = "🔴 HOT email reply" if decision.get("hot") else "📩 Email reply"
    said = " ".join((text or "").split())[:400]
    out = (f"{head} from {who} · {decision.get('intent') or 'other'}\n"
           f"“{said}”\n↳ {outcome[:600]}")
    if decision.get("meeting"):
        out = meeting_line(decision) + "\n" + out
    strike = strike_link(lead) if decision.get("hot") else ""
    if strike:
        out += f"\n📲 Strike while it's hot (you send): {strike}"
    if decision.get("hot"):
        from nexus.integrations.calls import phone_of
        phone = phone_of(lead)
        if phone and not strike:
            out += f"\n📞 Call while it's warm: {phone}"
    if decision.get("intent") == "ready_to_pay":
        out += "\n💰 They're ready to pay: tap below to email the deposit request (Wise / bank), or type: invoice " \
               f"{lead.get('id')} <amount>"
    return out


def meeting_line(decision: Dict[str, Any]) -> str:
    from nexus.integrations import booking
    t = datetime.fromisoformat(decision["meeting"].replace("Z", "+00:00"))
    mine = booking.describe(t, int(get_secret("OWNER_UTC_OFFSET_MINUTES", "330") or 330))
    theirs = booking.describe(t, int(decision.get("their_offset") or 0))
    return f"📅 MEETING BOOKED: {mine} your time ({theirs} theirs) · {decision.get('meeting_link', '')}"


def strike_link(lead: Dict[str, Any]) -> str:
    """wa.me link with a short note, when we know their mobile. You press send; no automation, no tokens."""
    from urllib.parse import quote
    from nexus.integrations.wa_links import normalize
    data = lead.get("data") or {}
    phone = data.get("phone") or data.get("call_phone") or \
        (lead.get("contact") if (lead.get("contact") or "").startswith("+") else "")
    number = normalize(phone or "")
    if not number:
        return ""
    first = (lead.get("name") or "").split()[0].title() if lead.get("name") else ""
    note = f"hi{' ' + first if first else ''}, yash here, just saw your email. easier to sort this on whatsapp?"
    return f"https://wa.me/{number}?text={quote(note)}"


QUOTE_MARKERS = [re.compile(p, re.I | re.M) for p in (
    r"^On .+wrote:\s*$", r"^-{2,}\s*Original Message", r"^From:\s.+$", r"^Sent from my ",
)]


# A bare "no" is NOT here on purpose: it can be the good answer to "would it be a bad idea to...?", so the
# reply-writer reads it in context instead.
OPT_OUT_BODY = re.compile(r"^\W*(no thanks?|no thank you|stop|unsubscribe|remove me|remove|opt[ -]?out|"
                          r"not interested|take me off( (your|the|this) list)?|do not (contact|email) me)\W*$", re.I)
OPT_OUT_SUBJECT = re.compile(r"^\s*(re:\s*)*(unsubscribe|remove me|opt[ -]?out)\b", re.I)


def is_opt_out(subject: str, text: str) -> bool:
    """A bare "no" / "unsubscribe" (in the body, or as the subject of an otherwise empty mail)."""
    body = (text or "").strip()
    if body and len(body) <= 60 and OPT_OUT_BODY.match(body):
        return True
    return bool(OPT_OUT_SUBJECT.match(subject or "")) and len(body) <= 60


def split_subject(draft: str, default: str = "quick question") -> tuple[str, str]:
    """Drafts may start with 'subject: ...'. Returns (subject, body)."""
    lines = (draft or "").strip().splitlines()
    if lines and lines[0].lower().startswith("subject:"):
        return lines[0].split(":", 1)[1].strip() or default, "\n".join(lines[1:]).strip()
    return default, (draft or "").strip()


def strip_quoted(body: str) -> str:
    """Keep only the new part of a reply (drop quoted history and signatures)."""
    cut = len(body)
    for marker in QUOTE_MARKERS:
        m = marker.search(body)
        if m:
            cut = min(cut, m.start())
    kept = [ln for ln in body[:cut].splitlines() if not ln.lstrip().startswith(">")]
    return "\n".join(kept).strip()


class AlertFanout:
    """Sends an alert to every configured channel (Telegram bot, WhatsApp notifier...)."""

    groups: List[str] = []
    numbers: List[str] = []

    def __init__(self, notifiers):
        self.notifiers = [n for n in notifiers if n is not None]

    def notify(self, text: str, priority: str = "orange", buttons=None, html_text: bool = False) -> None:
        """Telegram gets HTML (plain text is escaped, so a '<' from a prospect can't break the alert) and
        buttons; other channels get plain text."""
        import html as _html
        for n in self.notifiers:
            try:
                if hasattr(n, "alert_recipients"):
                    n.notify(text if html_text else _html.escape(text, quote=False), priority=priority,
                             buttons=buttons)
                else:
                    n.notify(_html.unescape(re.sub(r"<[^>]+>", "", text)) if html_text else text, priority=priority)
            except Exception as e:
                log.warning(f"alert via {type(n).__name__} failed: {e}")


_ACTIVE_WORKER: Optional["EmailWorker"] = None


def active_worker() -> Optional["EmailWorker"]:
    """The running email worker (for Telegram buttons and commands that send email)."""
    return _ACTIVE_WORKER


class EmailTransport:
    """SMTP + IMAP for one mailbox (e.g. Zoho on your cold domain)."""

    def __init__(self, prefix: str = "EMAIL"):
        # Extra inboxes use their own prefix (EMAIL2_ADDRESS, EMAIL2_PASSWORD, EMAIL2_FROM_NAME) and share
        # the main mailbox's servers unless they set their own.
        own = lambda k: get_secret(f"{prefix}_{k}") if prefix != "EMAIL" else None  # noqa: E731
        self.prefix = prefix
        self.address = get_secret(f"{prefix}_ADDRESS")
        self.password = get_secret(f"{prefix}_PASSWORD")
        self.smtp_host = own("SMTP_HOST") or get_secret("EMAIL_SMTP_HOST", "smtp.zoho.com")
        self.smtp_port = int(own("SMTP_PORT") or get_secret("EMAIL_SMTP_PORT", "465") or 465)
        self.imap_host = own("IMAP_HOST") or get_secret("EMAIL_IMAP_HOST", "imap.zoho.com")
        guess = (self.address or "").split("@")[0].split(".")[0].title()
        self.from_name = (own("FROM_NAME") or guess) if prefix != "EMAIL" else get_secret("EMAIL_FROM_NAME", "Yash")

    @property
    def configured(self) -> bool:
        return bool(self.address and self.password)

    def send(self, to: str, subject: str, body: str, in_reply_to: str = "", references: str = "", ics: str = "") -> str:
        # Headers copied from incoming mail can be folded over several lines: flatten them first.
        clean = lambda v: " ".join(str(v or "").split())  # noqa: E731
        subject, in_reply_to, references, to = clean(subject), clean(in_reply_to), clean(references), clean(to)
        msg = EmailMessage()
        msg["From"] = f"{self.from_name} <{self.address}>"
        msg["To"] = to
        msg["Subject"] = subject
        msg_id = make_msgid(domain=self.address.split("@")[-1])
        msg["Message-ID"] = msg_id
        # One-click opt-out in mail clients; a reply with that subject is honoured as do-not-contact.
        msg["List-Unsubscribe"] = f"<mailto:{self.address}?subject=unsubscribe>"
        if in_reply_to:
            msg["In-Reply-To"] = in_reply_to
            msg["References"] = (references + " " + in_reply_to).strip()
        msg.set_content(body)
        if ics:   # calendar invite: lands in their calendar with one click
            msg.add_attachment(ics.encode("utf-8"), maintype="text", subtype="calendar",
                               filename="invite.ics", params={"method": "REQUEST"})
        context = ssl.create_default_context()
        if self.smtp_port == 465:
            with smtplib.SMTP_SSL(self.smtp_host, self.smtp_port, context=context, timeout=30) as s:
                s.login(self.address, self.password)
                s.send_message(msg)
        else:
            with smtplib.SMTP(self.smtp_host, self.smtp_port, timeout=30) as s:
                s.starttls(context=context)
                s.login(self.address, self.password)
                s.send_message(msg)
        return msg_id

    def fetch_unseen(self, limit: int = 20, wanted: Optional[Callable[[str], bool]] = None) -> List[Dict[str, Any]]:
        """Unread mail, newest last. With `wanted`, only senders it accepts are downloaded:
        sender headers for ALL unread mail are checked first (cheap), so a lead's reply is
        never hidden behind newsletters."""
        out = []
        with imaplib.IMAP4_SSL(self.imap_host, 993) as imap:
            imap.login(self.address, self.password)
            imap.select("INBOX")
            _, data = imap.uid("search", None, "UNSEEN")
            uids = data[0].split() if data and data[0] else []
            if wanted and uids:
                _, headers = imap.uid("fetch", b",".join(uids), "(BODY.PEEK[HEADER.FIELDS (FROM)])")
                keep = []
                for item in headers or []:
                    if not isinstance(item, tuple):
                        continue
                    uid_match = re.search(rb"UID (\d+)", item[0])
                    sender = parseaddr(email.message_from_bytes(item[1]).get("From", ""))[1].lower()
                    if uid_match and wanted(sender):
                        keep.append(uid_match.group(1))
                uids = keep
            for uid in uids[-limit:]:
                _, msg_data = imap.uid("fetch", uid, "(BODY.PEEK[])")  # PEEK: don't mark read yet
                raw = msg_data[0][1] if msg_data and msg_data[0] else b""
                out.append(parse_mail(raw, uid.decode()))
        return out

    def fetch_recent(self, days: int = 3, limit: int = 40,
                     wanted: Optional[Callable[[Dict[str, str]], bool]] = None) -> List[Dict[str, Any]]:
        """Mail from the last `days`, read or not (you may open a reply in Zoho before Autocrat does).
        Headers of everything are checked first; only wanted mail is downloaded (PEEK: nothing is marked read)."""
        since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%d-%b-%Y")
        out = []
        with imaplib.IMAP4_SSL(self.imap_host, 993) as imap:
            imap.login(self.address, self.password)
            imap.select("INBOX", readonly=True)
            _, data = imap.uid("search", None, f"SINCE {since}")
            uids = data[0].split() if data and data[0] else []
            if wanted and uids:
                _, headers = imap.uid("fetch", b",".join(uids),
                                      "(BODY.PEEK[HEADER.FIELDS (FROM MESSAGE-ID IN-REPLY-TO REFERENCES)])")
                keep = []
                for item in headers or []:
                    if not isinstance(item, tuple):
                        continue
                    uid_match = re.search(rb"UID (\d+)", item[0])
                    h = email.message_from_bytes(item[1])
                    info = {"from": parseaddr(h.get("From", ""))[1].lower(), "in_reply_to": h.get("In-Reply-To", ""),
                            "references": h.get("References", ""), "message_id": h.get("Message-ID", "")}
                    if uid_match and wanted(info):
                        keep.append(uid_match.group(1))
                uids = keep
            for uid in uids[-limit:]:
                _, msg_data = imap.uid("fetch", uid, "(FLAGS BODY.PEEK[])")
                if not msg_data or not isinstance(msg_data[0], tuple):
                    continue
                out.append(parse_mail(msg_data[0][1], uid.decode(), msg_data[0][0].decode(errors="replace")))
        return out

    def answered_by_me(self, message_id: str) -> bool:
        """Did you already reply by hand (a message in Sent that answers this one)?"""
        if not message_id:
            return False
        try:
            with imaplib.IMAP4_SSL(self.imap_host, 993) as imap:
                imap.login(self.address, self.password)
                imap.select(get_secret("EMAIL_SENT_FOLDER", "Sent"), readonly=True)
                _, data = imap.uid("search", None, "HEADER", "In-Reply-To", message_id.strip("<>"))
                return bool(data and data[0])
        except Exception as e:
            log.info(f"Sent-folder check failed: {e}")
            return False

    def mark_seen(self, uid: str) -> None:
        with imaplib.IMAP4_SSL(self.imap_host, 993) as imap:
            imap.login(self.address, self.password)
            imap.select("INBOX")
            imap.uid("store", uid, "+FLAGS", "(\\Seen)")


class EmailWorker:
    def __init__(self, store, closer, alerts, transport=None, sleep: Callable[[float], None] = time.sleep):
        self.store = store
        self.closer = closer          # WhatsAppCloser instance used as the shared sales brain
        self.alerts = alerts          # AlertFanout
        if transport is None:
            from nexus.integrations.mailboxes import build_transport
            transport = build_transport(store)      # one mailbox, or a pool of them (EMAIL_EXTRA_ACCOUNTS)
        self.transport = transport
        self.sleep = sleep
        self.auto_send = env_bool("EMAIL_AUTO_SEND", False)
        # Answers to people who replied: sent automatically, or drafted for your one-tap approval (0).
        self.auto_reply = env_bool("EMAIL_AUTO_REPLY", True)
        self.daily_cap = int(get_secret("EMAIL_DAILY_CAP", "20") or 20)
        self.hours = get_secret("EMAIL_SEND_HOURS", "9-18")
        self.offset = int(get_secret("EMAIL_UTC_OFFSET_MINUTES", "240") or 240)
        self.poll_seconds = float(get_secret("EMAIL_POLL_SECONDS", "120") or 120)
        self.optout = get_secret("EMAIL_OPTOUT_LINE", "p.s. if this isn't relevant, just reply 'no' and i won't email again")
        self.sender_name = get_secret("EMAIL_SIGNATURE", "Yash") or "Yash"
        self.company = (get_secret("EMAIL_COMPANY", "Windrose Labs") or "").strip()
        self.site = (get_secret("EMAIL_SITE", "") or "").strip()
        # Cold mail signs with the name and company only; the site link appears once they've replied (sign(True)).
        self.signature = f"{self.sender_name}\n{self.company}" if self.company else self.sender_name
        self.rng = random.Random()
        self.postal = (get_secret("EMAIL_POSTAL_ADDRESS", "") or "").strip()
        self.wa_number = (get_secret("WHATSAPP_BUSINESS_NUMBER", "") or "").strip()
        self.wa_markets = [m.strip().lower() for m in
                           (get_secret("EMAIL_WHATSAPP_LINK_MARKETS", "UAE") or "UAE").split(",") if m.strip()]
        self.cap_max = int(get_secret("EMAIL_CAP_MAX", "60") or 60)
        self.no_followup_sources = {s.strip() for s in (get_secret("EMAIL_NO_FOLLOWUP_SOURCES", "") or "").split(",")
                                    if s.strip()}
        self.cap_step = int(get_secret("EMAIL_CAP_STEP", "3") or 3)
        self.followups_on = (get_secret("EMAIL_FOLLOWUPS", "1") or "1").lower() in ("1", "true", "yes")
        self.followup_days = [float(x) for x in (get_secret("EMAIL_FOLLOWUP_DAYS", "3,4") or "3,4").split(",") if x.strip()]
        self.max_followup_drafts = int(get_secret("EMAIL_FOLLOWUP_DRAFTS_PER_TICK", "6") or 6)
        self._postal_warned = False
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # ── lifecycle ────────────────────────────────────────────────

    def start(self) -> bool:
        if not self.transport.configured:
            log.info("Email: EMAIL_ADDRESS / EMAIL_PASSWORD not set, not starting")
            return False
        global _ACTIVE_WORKER
        _ACTIVE_WORKER = self
        self._thread = threading.Thread(target=self._loop, daemon=True, name="email-worker")
        self._thread.start()
        log.info(f"📧 Email channel on ({'auto-send openers' if self.auto_send else 'sends approved openers'}, "
                 f"cap {self.daily_cap}/day)")
        return True

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        self._stop.wait(30)
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as e:
                log.warning(f"Email tick failed: {e}")
            self._stop.wait(self.poll_seconds)

    def sign(self, reply: bool = False) -> str:
        """Sign-off. Once they've replied, the company site is added so they can look us up."""
        return f"{self.signature} · {self.site}" if (reply and self.site and self.company) else self.signature

    def tick(self, now: Optional[datetime] = None) -> Dict[str, int]:
        if RuntimeControl().killed:
            return {"sent": 0, "replied": 0}
        staged = 0
        if self.followups_on:
            try:
                staged = self.stage_followups()
            except Exception as e:
                log.warning(f"Follow-up staging failed: {e}")
        result = {"followups_staged": staged, "replied": self.handle_replies(), "sent": self.send_openers(now)}
        from nexus.integrations import deals, precall
        for name, fn in (("reminders", self.meeting_reminders), ("nudged", self.nudge_quiet_conversations),
                         ("wa_touch", self.stage_whatsapp_touch),
                         ("debriefs", lambda: deals.ask_meeting_outcomes(self)),
                         ("chased", lambda: deals.chase_payments(self)),
                         ("post_sale", lambda: deals.post_sale_followups(self)),
                         ("packs", lambda: precall.prepare_packs(self)),
                         ("brief", self.maybe_daily_brief)):
            try:
                result[name] = fn()
            except Exception as e:
                log.warning(f"Email {name} failed: {e}")
        return result

    # ── warm-up cap ──────────────────────────────────────────────

    # ── meetings: invite, reminders ──────────────────────────────

    def invite_for(self, lead: Dict[str, Any], decision: Dict[str, Any], to: str) -> str:
        if not decision.get("meeting"):
            return ""
        from nexus.integrations import booking
        start = datetime.fromisoformat(decision["meeting"].replace("Z", "+00:00"))
        who = lead.get("company") or lead.get("name") or to
        return booking.ics_invite(start, summary=f"{self.sender_name} x {who}", link=decision.get("meeting_link", ""),
                                  organizer=getattr(self.transport, "address", "") or "", attendee=to,
                                  minutes=int(get_secret("MEETING_MINUTES", "20") or 20), uid=f"{lead['id']}-{start:%Y%m%d%H%M}")

    def meeting_reminders(self, now: Optional[datetime] = None) -> int:
        """Telegram 60 and 10 minutes before; a one-line reminder email to them 60 minutes before."""
        from nexus.integrations import booking
        now = now or datetime.now(timezone.utc)
        sent = 0
        for m in booking.booked_meetings(self.store, since=now):
            mins = (m["start"] - now).total_seconds() / 60
            lead = self.store.get_lead(m["lead_id"]) or {}
            who = lead.get("company") or lead.get("name") or lead.get("contact") or "them"
            mine = booking.describe(m["start"], int(get_secret("OWNER_UTC_OFFSET_MINUTES", "330") or 330))
            from nexus.integrations.deals import email_of
            # The day before: one "still good for tomorrow?" so no-shows surface early.
            day_key = f"meeting:confirm24:{m['lead_id']}:{m['start']:%Y%m%d%H%M}"
            if 18 * 60 < mins <= 26 * 60 and not self.store.kv_get(day_key) and email_of(lead) \
                    and not RuntimeControl().is_dry_run and not RuntimeControl().killed:
                self.store.kv_set(day_key, "1")
                thread = self._thread_of(lead["id"])
                theirs = booking.describe(m["start"], int(m.get("their_offset") or 0))
                try:
                    self.transport.send(email_of(lead), f"Re: {thread['subject'] or 'our call'}".replace("Re: Re:", "Re:"),
                                        f"quick one, still good for {theirs} your time tomorrow? same link: {m['link']}"
                                        f"\n\n{self.sign(True)}",
                                        in_reply_to=thread["last_id"], references=thread["message_id"])
                    sent += 1
                except Exception as e:
                    log.info(f"day-before confirmation failed: {e}")
            for window, label in ((65, "60"), (12, "10")):
                key = f"meeting:remind{label}:{m['lead_id']}:{m['start']:%Y%m%d%H%M}"
                if 0 < mins <= window and not self.store.kv_get(key):
                    self.store.kv_set(key, "1")
                    self.alerts.notify(f"⏰ Call with {who} in {int(mins)} min ({mine}) · {m['link']}", "red")
                    sent += 1
                    if label == "60" and email_of(lead) and not RuntimeControl().is_dry_run \
                            and not RuntimeControl().killed:
                        thread = self._thread_of(lead["id"])
                        theirs = booking.describe(m["start"], int(m.get("their_offset") or 0))
                        try:
                            self.transport.send(email_of(lead), f"Re: {thread['subject'] or 'our call'}".replace("Re: Re:", "Re:"),
                                                f"quick reminder, we're on at {theirs} your time: {m['link']}\n\n{self.sign(True)}",
                                                in_reply_to=thread["last_id"], references=thread["message_id"])
                        except Exception as e:
                            log.info(f"meeting reminder email failed: {e}")
        return sent

    # ── conversations that went quiet after they replied ─────────

    def nudge_quiet_conversations(self, now: Optional[datetime] = None) -> int:
        """They replied, we answered, then silence: one short nudge after EMAIL_NUDGE_HOURS (48),
        a second after another 72h, then stop. Business hours only, inside the daily cap."""
        if RuntimeControl().is_dry_run or RuntimeControl().killed or not self.auto_reply:
            return 0           # with auto-replies paused, live conversations are yours: no automatic nudges either
        now = now or datetime.now(timezone.utc)
        hours = float(get_secret("EMAIL_NUDGE_HOURS", "48") or 48)
        sent = 0
        rows = self.store._conn().execute("SELECT id FROM leads WHERE state='REPLIED' ORDER BY updated_at LIMIT 40").fetchall()
        from nexus.integrations.deals import email_of
        for (lead_id,) in rows:
            lead = self.store.get_lead(lead_id)
            if not email_of(lead):
                continue
            events = self.store.lead_events(lead_id)
            talk = [e for e in events if e["type"] in ("message_in", "message_out")]
            nudges = sum(1 for e in events if e["type"] == "message_out" and (e.get("payload") or {}).get("kind") == "nudge")
            # "not now, try me in January": stay silent until that date, then one check-back (template, no AI cost).
            snoozes = [e for e in events if e["type"] == "snooze"]
            if snoozes:
                until = float((snoozes[-1].get("payload") or {}).get("until") or 0)
                checked = any(e["type"] == "message_out" and (e.get("payload") or {}).get("kind") == "checkback"
                              and e["id"] > snoozes[-1]["id"] for e in events)
                replied_since = any(e["type"] == "message_in" and e["id"] > snoozes[-1]["id"] for e in events)
                if not replied_since and (now.timestamp() < until or checked):
                    continue           # still waiting for their date, or already checked back once: leave it
                if not checked and not replied_since:
                    their_now = now + timedelta(minutes=market_offset(lead.get("market") or "", self.offset))
                    if not self._in_hours(their_now):
                        continue
                    from nexus.integrations.deals import first_name
                    hi = f"hey {first_name(lead)}," if first_name(lead) else "hey,"
                    body = f"{hi} you mentioned to check back around now. still worth a quick look, or should i leave it?"
                    thread = self._thread_of(lead_id)
                    subject = thread["subject"] if thread["subject"].lower().startswith("re:") else f"Re: {thread['subject'] or 'quick one'}"
                    try:
                        msg_id = self.transport.send(email_of(lead), subject, f"{body}\n\n{self.sign(True)}",
                                                     in_reply_to=thread["last_id"], references=thread["message_id"])
                    except Exception as e:
                        log.info(f"check-back send failed for {lead_id}: {e}")
                        continue
                    self.store.log_event(lead_id, "message_out", {"text": body, "subject": subject, "message_id": msg_id,
                                                                  "channel": "email", "kind": "checkback"}, actor="closer")
                    self.alerts.notify(f"🔁 Checked back with {lead.get('company') or email_of(lead)}, as they asked.", "orange")
                    sent += 1
                    continue
            if not talk or talk[-1]["type"] != "message_out" or nudges >= 2:
                continue
            wait = hours if nudges == 0 else hours + 72
            if (now.timestamp() - float(talk[-1]["ts"])) < wait * 3600:
                continue
            their_now = now + timedelta(minutes=market_offset(lead.get("market") or "", self.offset))
            if not self._in_hours(their_now):
                continue
            if any(m["lead_id"] == lead_id for m in __import__("nexus.integrations.booking", fromlist=["x"]).booked_meetings(self.store)):
                continue   # a call is already booked: don't chase
            transcript = "\n\n".join(f"{'Me' if e['type'] == 'message_out' else 'Them'}: {(e.get('payload') or {}).get('text', '')[:500]}"
                                     for e in talk[-6:])
            try:
                from nexus.integrations.whatsapp_closer import humanize
                result = self.closer.router.complete(
                    f"Conversation (latest last):\n{transcript}", task="copy", json_mode=True, max_tokens=400,
                    lead_id=lead_id, system=("Write a short follow-up from Yash in this email thread: they went quiet after "
                                             "our last message. 1-2 casual lowercase sentences in Yash's voice, pick up exactly "
                                             "where it stopped, make replying easy (a yes/no question or two concrete call times). "
                                             "If they ignored a question, don't repeat it: offer one concrete idea for THEIR "
                                             "product instead (software founders: never WhatsApp or live-chat angles). "
                                             + ("Last nudge: say you'll leave it here and the door is open. " if nudges else "")
                                             + "No dashes, no placeholders, never invent anything. Return ONLY JSON {\"draft\": \"...\"}\n\n"
                                             f"Yash's voice:\n{(getattr(self.closer, 'voice', '') or '')[:2000]}"))
                draft = humanize(str((result.data or {}).get("draft") or "")).strip() if isinstance(result.data, dict) else ""
            except Exception as e:
                log.info(f"nudge draft failed for {lead_id}: {e}")
                continue
            if not draft or PLACEHOLDER_RE.search(draft):
                continue
            thread = self._thread_of(lead_id)
            subject = thread["subject"] if thread["subject"].lower().startswith("re:") else f"Re: {thread['subject'] or 'quick one'}"
            try:
                msg_id = self.transport.send(email_of(lead), subject, f"{draft}\n\n{self.sign(True)}",
                                             in_reply_to=thread["last_id"], references=thread["message_id"])
            except Exception as e:
                log.info(f"nudge send failed for {lead_id}: {e}")
                continue
            self.store.log_event(lead_id, "message_out", {"text": draft, "subject": subject, "message_id": msg_id,
                                                          "channel": "email", "kind": "nudge"}, actor="closer")
            self.alerts.notify(f"🔁 Nudged {lead.get('company') or lead['contact']} (quiet {int(wait)}h+): {draft}", "orange")
            sent += 1
        return sent

    def maybe_daily_brief(self, now: Optional[datetime] = None) -> bool:
        """09:00 your time: yesterday's numbers + today's calls + what needs you. Template only, no tokens."""
        now = now or datetime.now(timezone.utc)
        mine = now + timedelta(minutes=int(get_secret("OWNER_UTC_OFFSET_MINUTES", "330") or 330))
        key = f"brief:{mine:%Y-%m-%d}"
        if mine.hour < int(get_secret("DAILY_BRIEF_HOUR", "9") or 9) or self.store.kv_get(key):
            return False
        self.store.kv_set(key, "1")
        from nexus.integrations.brief import daily_brief
        self.alerts.notify(daily_brief(self.store, now), "red")
        return True

    def whatsapp_line(self, lead: Dict[str, Any]) -> str:
        """'or just whatsapp me' + a wa.me link whose prefilled text carries the lead's ref.
        They message us first, so the Cloud API closer can answer for free."""
        market = (lead.get("market") or "").strip().lower()
        if not self.wa_number or not market or not any(market == m or market.startswith(m + " ") for m in self.wa_markets):
            return ""
        from nexus.integrations.whatsapp_cloud import click_to_chat, ref_for
        first = (lead.get("company") or lead.get("name") or "").strip()
        text = f"Hi Yash, {('re ' + first + ' ') if first else ''}(ref {ref_for(lead['id'])})"
        return f"or if whatsapp is easier: {click_to_chat(self.wa_number, text)}"

    def cap_status(self, now_ts: Optional[float] = None) -> Dict[str, Any]:
        cap = compute_cap(self.store, self.daily_cap, self.cap_max, self.cap_step, now_ts)
        pool = self.transport if hasattr(self.transport, "extra_capacity") else None
        if pool:       # more inboxes: each has its own limit, and the day's total is their sum
            pool.primary_cap, pool.reset = cap["cap"], cap.get("bounce_rate", 0) > 0.05
            pool.day = self._local_now(None).strftime("%Y-%m-%d")
            cap["mailboxes"] = pool.status()
            cap["cap"] += pool.extra_capacity()
            cap["note"] = f"{cap.get('note', '')} · {len(pool.boxes)} inboxes".strip(" ·")
        return cap

    # ── follow-ups ───────────────────────────────────────────────

    def _thread_of(self, lead_id: str) -> Dict[str, str]:
        """The opener's subject + Message-ID, so follow-ups land in the same thread."""
        first, last_id = {}, ""
        for e in self.store.lead_events(lead_id):
            payload = e.get("payload") or {}
            if e["type"] == "message_out" and payload.get("channel") == "email":
                if not first:
                    first = payload
                last_id = payload.get("message_id") or last_id
        return {"subject": first.get("subject", ""), "message_id": first.get("message_id", ""),
                "last_id": last_id, "text": first.get("text", "")}

    def _quiet(self, lead: Dict[str, Any]) -> bool:
        """Still waiting on them: contacted, no reply, not closed."""
        if not lead or lead["state"] not in ("SENT", "OPENED"):
            return False
        return not any(e["type"] in ("message_in", "bounce") for e in self.store.lead_events(lead["id"]))

    def followup_template(self, lead: Dict[str, Any], n: int) -> str:
        """Follow-up 1 when we looked at their site: offer what we noticed. Free (no AI call), and a 'yes'
        hands the reply-writer the list to send."""
        check = (lead.get("data") or {}).get("site_check") or {}
        from nexus.integrations.site_audit import relevant_line
        findings = [f for f in check.get("findings") or [] if relevant_line(str(f), lead.get("niche") or "")]
        if n != 1 or len(findings) < 2:
            return ""
        from nexus.integrations.site_audit import domain_of
        where = domain_of(check.get("url") or "") or "your site"
        return (f"i actually jotted down {min(3, len(findings))} quick things i noticed on {where} while i was "
                "looking at it. takes a minute to read. want me to send them over?")

    def draft_followup(self, lead: Dict[str, Any], n: int, opener: str) -> str:
        ai_check = (lead.get("data") or {}).get("track") == "ai_check"
        templated = "" if ai_check else self.followup_template(lead, n)
        if templated:
            return templated
        from nexus.integrations.whatsapp_closer import humanize
        voice = getattr(self.closer, "voice", "") or ""
        if ai_check and (get_secret("AI_CHECK_SAMPLE_URL", "") or "").strip():
            first = ("#1: they were offered a free quick security check of the AI in their product. Offer to send the "
                     "short sample report from when we ran the same check on our own AI assistant, so they see exactly "
                     "what they'd get. Then an easy yes/no question.\n")
        elif ai_check:
            first = ("#1: they were offered a free quick security check of the AI in their product. Say in different "
                     "words what we'd try (getting it to reveal its hidden instructions or another user's data) and "
                     "that they get the one or two real breaks we find. Then an easy yes/no question.\n")
        else:
            first = ("#1: offer to build them a small working demo of ONE concrete thing for their product, free, on "
                     "their own site or sample data, so they can click around before deciding anything (if the first "
                     "message already offered a demo, offer a 45 second video of it instead). Then an easy yes/no "
                     "question.\n")
        system = (
            f"Write follow-up #{n} of 2 from Yash in an email thread. They got the message below and didn't reply.\n"
            "Rules: 1-2 short sentences, lowercase and casual like Yash's voice, no greeting line needed.\n"
            + (first if n == 1 else
               "#2: a polite last note that you'll stop here, leave the door open, no guilt, no pressure.\n")
            + "Never repeat the first message, never use placeholders like [name], never use em or en dashes, "
              "never claim clients or results. Return ONLY JSON: {\"draft\": \"...\"}\n\n"
              f"Yash's voice:\n{voice[:2500]}")
        who = f"{lead.get('name') or ''} at {lead.get('company') or ''}".strip()
        result = self.closer.router.complete(f"To: {who}\nTheir first message from us:\n{opener[:1500]}",
                                             task="copy", system=system, json_mode=True, max_tokens=500,
                                             lead_id=lead["id"])
        data = result.data if isinstance(result.data, dict) else {}
        return humanize(str(data.get("draft") or "")).strip()

    def stage_followups(self, now_ts: Optional[float] = None) -> int:
        """Draft the due follow-ups (they then send through the normal queue, threaded)."""
        from nexus.core.store import AlreadyContacted, Suppressed
        now_ts = now_ts or time.time()
        c = self.store._conn()
        staged = 0
        steps = [("first_touch", "followup_1"), ("followup_1", "followup_2")][:len(self.followup_days)]
        for (prev, kind), days in zip(steps, self.followup_days):
            rows = c.execute(
                "SELECT o.lead_id FROM outreach o WHERE o.channel='email' AND o.kind=? AND o.status='sent'"
                " AND o.sent_at <= ? AND NOT EXISTS (SELECT 1 FROM outreach f WHERE f.lead_id=o.lead_id AND f.kind=?)"
                " ORDER BY o.sent_at LIMIT 50", (prev, now_ts - days * 86400, kind)).fetchall()
            for r in rows:
                if staged >= self.max_followup_drafts:
                    return staged
                lead = self.store.get_lead(r[0])
                if not self._quiet(lead):
                    continue
                if (lead.get("source") or "") in self.no_followup_sources:
                    continue   # a list we've dropped: its send slots go to sources that reply
                if any(e["type"] == "auto_reply" and now_ts - float(e["ts"]) < 5 * 86400
                       for e in self.store.lead_events(lead["id"])):
                    continue   # out of office: wait until they're likely back
                thread = self._thread_of(lead["id"])
                if not thread["message_id"]:
                    continue
                try:
                    draft = self.draft_followup(lead, 1 if kind == "followup_1" else 2, thread["text"])
                except Exception as e:
                    log.warning(f"Follow-up draft for {lead['id']} failed: {e}")
                    return staged
                if not draft or PLACEHOLDER_RE.search(draft):
                    continue
                try:
                    self.store.stage_outreach(lead["id"], "email", draft, kind=kind, actor="followup")
                    staged += 1
                except (AlreadyContacted, Suppressed, ValueError) as e:
                    log.info(f"Not staging {kind} for {lead['id']}: {e}")
        return staged

    # ── outbound ─────────────────────────────────────────────────

    def _local_now(self, now: Optional[datetime]) -> datetime:
        return (now or datetime.now(timezone.utc)) + timedelta(minutes=self.offset)

    def _in_hours(self, local: datetime) -> bool:
        if "-" not in (self.hours or ""):
            return True
        start, end = (int(x) for x in self.hours.split("-", 1))
        return start <= local.hour < end

    def source_scores(self) -> Dict[str, float]:
        """One draw per source from its reply record (Thompson sampling): sources that earn replies get
        the day's sends first, and untested sources still get tried."""
        c = self.store._conn()
        sent = dict(c.execute("SELECT l.source, COUNT(DISTINCT l.id) FROM leads l JOIN outreach o ON o.lead_id=l.id"
                              " WHERE o.status='sent' GROUP BY l.source").fetchall())
        replied = dict(c.execute("SELECT l.source, COUNT(DISTINCT l.id) FROM leads l JOIN lead_events e ON e.lead_id=l.id"
                                 " WHERE e.type='message_in' GROUP BY l.source").fetchall())
        return {s: self.rng.betavariate(replied.get(s, 0) + 1, max(0, n - replied.get(s, 0)) + 1) for s, n in sent.items()}

    def prioritise(self, queue: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """People hiring a freelancer right now first, then first emails, then cold follow-ups; inside each group
        by how often its source actually replies, so a source that works isn't starved by one that doesn't.
        First emails beat follow-ups because every reply so far came from a first email (0 from the first 90
        follow-ups), and a first email about a launch goes stale while it waits. EMAIL_FOLLOWUPS_LAST=0 mixes them."""
        from nexus.integrations.telegram_alerts import is_hiring_post
        scores = self.source_scores()
        followups_last = env_bool("EMAIL_FOLLOWUPS_LAST", True)

        def rank(out: Dict[str, Any]):
            lead = self.store.get_lead(out["lead_id"]) or {}
            data = lead.get("data") or {}
            followup = (out.get("kind") or "").startswith("followup")
            if not followup and is_hiring_post(lead) and not data.get("full_time"):
                return (0, 0.0)          # a buyer with a budget asking for help today: minutes matter
            source = lead.get("source") or ""
            if source not in scores:
                scores[source] = self.rng.betavariate(1, 1)
            pain = min(3, len((data.get("site_check") or {}).get("findings") or []))
            reviews = str(data.get("reviews") or "").replace(",", "")
            pain += 1 if reviews.isdigit() and int(reviews) >= 30 else 0
            group = 2 if followup and followups_last else 1
            return (group, -(scores[source] + 0.05 * pain + (0.01 if followup else 0.0)))
        return sorted(queue, key=rank)

    def stage_whatsapp_touch(self, now: Optional[datetime] = None) -> int:
        """A second channel, sent by you: emailed, silent for WA_TOUCH_AFTER_DAYS (5), and we know their
        mobile → a short WhatsApp draft shows up in /wa, at most WA_TOUCH_PER_DAY (10). Template, no AI cost."""
        per_day = int(get_secret("WA_TOUCH_PER_DAY", "10") or 0)
        if per_day <= 0 or RuntimeControl().killed:
            return 0
        from nexus.core.store import AlreadyContacted, Suppressed
        from nexus.integrations.deals import first_name
        from nexus.integrations.wa_links import normalize
        now = now or datetime.now(timezone.utc)
        after = float(get_secret("WA_TOUCH_AFTER_DAYS", "5") or 5)
        day_key = f"wa_touch:{self._local_now(now):%Y-%m-%d}"
        done = int(self.store.kv_get(day_key) or 0)
        if done >= per_day:
            return 0
        rows = self.store._conn().execute(
            "SELECT o.lead_id FROM outreach o JOIN leads l ON l.id=o.lead_id WHERE o.channel='email'"
            " AND o.kind='first_touch' AND o.status='sent' AND o.sent_at <= ? AND l.state IN ('SENT','OPENED')"
            " AND NOT EXISTS (SELECT 1 FROM outreach w WHERE w.lead_id=o.lead_id AND w.channel='whatsapp')"
            " ORDER BY o.sent_at LIMIT 200", (now.timestamp() - after * 86400,)).fetchall()
        staged = 0
        for (lead_id,) in rows:
            if done + staged >= per_day:
                break
            lead = self.store.get_lead(lead_id)
            market = (lead.get("market") or "").strip().lower()
            number = normalize(str((lead.get("data") or {}).get("phone") or ""))
            if not number or not any(market == m or market.startswith(m + " ") for m in self.wa_markets) \
                    or not self._quiet(lead):
                continue
            hi = f"hi {first_name(lead)}," if first_name(lead) else "hi,"
            draft = (f"{hi} {self.sender_name.lower()} here from {(self.company or 'our studio').lower()}. i sent you a "
                     "quick email a few days ago about how fast your whatsapp enquiries get answered. easier to chat here?")
            try:
                self.store.stage_outreach(lead_id, "whatsapp", draft, kind="wa_touch", contact=f"+{number}",
                                          actor="followup")
                staged += 1
            except (AlreadyContacted, Suppressed, ValueError) as e:
                log.info(f"No WhatsApp second touch for {lead_id}: {e}")
        if staged:
            self.store.kv_set(day_key, str(done + staged))
        return staged

    def send_reply_draft(self, lead_id: str, actor: str = "telegram") -> str:
        """You approved the drafted answer: send it in the thread (once)."""
        events = self.store.lead_events(lead_id)
        drafts = [e for e in events if e["type"] == "reply_draft"]
        if not drafts:
            return "No drafted reply for this lead."
        d, draft_id = drafts[-1].get("payload") or {}, drafts[-1]["id"]
        if any(e["type"] == "message_out" and e["id"] > draft_id for e in events):
            return "Something was already sent after this draft, so I didn't send it."
        if any(e["type"] == "reply_draft_done" and (e.get("payload") or {}).get("draft") == draft_id for e in events):
            return "This draft was already handled."
        if RuntimeControl().killed or RuntimeControl().is_dry_run:
            return "Kill switch or dry run is on: nothing sent."
        lead = self.store.get_lead(lead_id) or {}
        body = f"{d['text']}\n\n{self.sign(True)}"
        ics = self.invite_for(lead, {"meeting": d.get("meeting"), "meeting_link": d.get("meeting_link")}, d["to"]) \
            if d.get("meeting") else ""
        try:
            msg_id = self.transport.send(d["to"], d["subject"], body, in_reply_to=d.get("in_reply_to", ""),
                                         references=d.get("references", ""), **({"ics": ics} if ics else {}))
        except Exception as e:
            return f"Sending failed ({e}). Here it is to send yourself:\n{d['text']}"
        self.store.log_event(lead_id, "message_out", {"text": d["text"], "subject": d["subject"], "message_id": msg_id,
                                                      "channel": "email", "intent": d.get("intent")}, actor=actor)
        self.store.log_event(lead_id, "reply_draft_done", {"draft": draft_id, "sent": True}, actor=actor)
        return f"📤 Sent to {d['to']}."

    def skip_reply_draft(self, lead_id: str, actor: str = "telegram") -> str:
        drafts = [e for e in self.store.lead_events(lead_id) if e["type"] == "reply_draft"]
        if drafts:
            self.store.log_event(lead_id, "reply_draft_done", {"draft": drafts[-1]["id"], "sent": False}, actor=actor)
        return "✋ Okay, you're answering this one. The draft won't be sent."

    def sending_paused(self) -> bool:
        from nexus.integrations.mailboxes import is_paused
        pool = self.transport if hasattr(self.transport, "all_paused") else None
        return pool.all_paused() if pool else is_paused(self.store, getattr(self.transport, "address", "") or "")

    def send_openers(self, now: Optional[datetime] = None) -> int:
        # Pace like a person: one email per cycle, several minutes apart. A burst of 18 in a few minutes is
        # what made Zoho flag the account as "unusual sending activity".
        if self.sending_paused():
            return 0
        now_ts = time.time()
        if now_ts < float(self.store.kv_get("email:next_send_at") or 0):
            return 0
        per_tick = max(1, int(get_secret("EMAIL_MAX_PER_TICK", "1") or 1))
        gap = float(get_secret("EMAIL_MIN_GAP_SECONDS", "300") or 0)
        local = self._local_now(now)
        day_key = f"email:sent:{local.strftime('%Y-%m-%d')}"
        sent_today = int(self.store.kv_get(day_key) or 0)
        queue = self.store.list_outreach("approved", channel="email")
        if self.auto_send:
            queue += self.store.list_outreach("staged", channel="email")
        queue = self.prioritise(queue)
        cap = self.cap_status()["cap"]
        # Big cold lists must not eat the whole day: keep a share for leads with a live signal (hiring posts,
        # launches, replies' follow-ups), whose send hours often come later in the day.
        reserve = max(0, min(90, int(get_secret("EMAIL_RESERVE_PCT", "30") or 0)))
        list_cap = cap - (cap * reserve) // 100
        sent = 0
        for out in queue:
            if sent_today + sent >= cap:
                break
            lead = self.store.get_lead(out["lead_id"])
            to = (lead or {}).get("contact") or ""
            if not lead or "@" not in to:
                continue
            if env_bool("EMAIL_PAUSE_COLD", False) and not (out.get("kind") or "").startswith("followup"):
                continue   # new first emails are on hold (follow-ups to people already contacted still go)
            from_list = "row" in (lead.get("data") or {}) and not (out.get("kind") or "").startswith("followup")
            if from_list and sent_today + sent >= list_cap:
                continue
            # Send inside the recipient's own business hours (US leads at 9am New York, not 2am).
            their_now = (now or datetime.now(timezone.utc)) + timedelta(
                minutes=market_offset(lead.get("market") or "", self.offset))
            if not self._in_hours(their_now):
                continue
            if PLACEHOLDER_RE.search(out["draft"] or ""):
                log.warning(f"Email: draft {out['id']} has a template placeholder; never sending it")
                continue
            if needs_postal(lead.get("market") or "") and not self.postal:
                if not self._postal_warned:
                    log.warning("Email: US/Canada openers held until EMAIL_POSTAL_ADDRESS is set (CAN-SPAM/CASL)")
                    self._postal_warned = True
                continue
            followup = (out.get("kind") or "").startswith("followup")
            in_reply_to = references = ""
            if not followup:
                from nexus.integrations.mx_check import has_mail_server, is_unusable_address
                why = is_unusable_address(to)
                if not why and not has_mail_server(to, self.store):  # a dead domain would only bounce
                    why = "domain can't receive email"
                if why:
                    try:
                        self.store.reject_outreach(out["id"], actor="email-auto", reason=why)
                    except Exception:
                        pass
                    continue
            if followup:
                if not self._quiet(lead):   # they replied, said no or bounced since: never bump them
                    try:
                        self.store.reject_outreach(out["id"], actor="email-auto", reason="lead no longer quiet")
                    except Exception:
                        pass
                    continue
                thread = self._thread_of(lead["id"])
                subject = thread["subject"] if thread["subject"].lower().startswith("re:") else f"Re: {thread['subject']}"
                body, in_reply_to, references = out["draft"].strip(), thread["last_id"], thread["message_id"]
            else:
                subject, body = split_subject(out["draft"])
            wa_line = self.whatsapp_line(lead)
            body = f"{body}\n\n{wa_line}" if wa_line else body
            body = f"{body}\n\n{self.signature}\n\n{self.optout}"
            if self.postal and needs_postal(lead.get("market") or ""):
                body = f"{body}\n{self.postal}"     # US (CAN-SPAM) and Canada (CASL) only
            if RuntimeControl().is_dry_run:
                continue  # dry run: the Telegram alert already shows the draft; nothing is sent
            try:
                if out["status"] == "staged":
                    self.store.approve_outreach(out["id"], actor="email-auto")
                    if lead["state"] == "STAGED":
                        self.store.transition(lead["id"], "APPROVED", actor="email-auto")
                msg_id = self.transport.send(to, subject, body, in_reply_to=in_reply_to, references=references)
                self.store.mark_outreach_sent(out["id"], actor="email")
            except Exception as e:
                from nexus.integrations.mailboxes import is_block_error, pause
                if is_block_error(e):
                    address = getattr(e, "address", "") or getattr(self.transport, "address", "") or "the inbox"
                    hours = float(get_secret("EMAIL_BLOCK_PAUSE_HOURS", "24") or 24)
                    if pause(self.store, address, hours):
                        log.warning(f"Email: {address} blocked by the provider, paused {hours:g}h: {str(e)[:160]}")
                        self.alerts.notify(
                            f"🛑 Zoho stopped {address} from sending (\"unusual sending activity\"). Autocrat paused "
                            f"that inbox for {hours:g}h and is now sending more slowly. To unblock: sign in to Zoho Mail "
                            f"as {address} and follow the unblock banner (mail.zoho.in/UnblockMe). Replies keep being read.",
                            "red")
                    if self.sending_paused():
                        break
                    continue
                log.warning(f"Email opener to lead {lead['id']} failed: {e}")
                continue
            self.store.log_event(lead["id"], "message_out", {"text": body, "subject": subject, "kind": out.get("kind"),
                                                             "message_id": msg_id, "channel": "email"}, actor="email")
            sent += 1
            if gap:
                self.store.kv_set("email:next_send_at", str(time.time() + gap * self.rng.uniform(0.8, 1.6)))
            if sent >= per_tick:
                break
        if sent:
            self.store.kv_set(day_key, str(sent_today + sent))
            self.alerts.notify(f"📧 Sent {sent} email(s) ({sent_today + sent}/{cap} today)", "orange")
        return sent

    # ── inbound ──────────────────────────────────────────────────

    def transcript_for(self, lead_id: str, latest: str) -> str:
        lines = []
        for e in self.store.lead_events(lead_id):
            payload = e.get("payload") or {}
            if e["type"] == "message_out" and payload.get("text"):
                lines.append(f"Me: {payload['text']}")
            elif e["type"] == "message_in" and payload.get("text"):
                lines.append(f"Them: {payload['text']}")
        lines.append(f"Them: {latest}")
        return "\n\n".join(lines[-12:])

    def handle_bounce(self, mail: Dict[str, Any]) -> bool:
        """Match a delivery failure to the lead we emailed; never write to that address again."""
        own = (getattr(self.transport, "address", "") or "").lower()
        ours = getattr(self.transport, "addresses", None) or {own}
        text = f"{mail.get('dsn', '')}\n{mail.get('body', '')}"
        delay = is_delay_notice(mail)
        for addr in dict.fromkeys(a.lower().strip(".") for a in EMAIL_RE.findall(text)):
            if addr in ours or is_bounce_sender(addr):
                continue
            key = contact_key("email", addr)
            lead = self.store.find_lead_by_contact(key)
            if not lead or not any(o["status"] == "sent" for o in self.store.outreach_for_lead(lead["id"])):
                continue
            if delay:      # note it and wait: the server keeps trying, a real failure comes as its own report
                if not any(e["type"] == "delivery_delay" for e in self.store.lead_events(lead["id"])):
                    self.store.log_event(lead["id"], "delivery_delay",
                                         {"address": addr, "subject": mail.get("subject", "")[:200]}, actor="email")
                    log.info(f"⏳ Delivery delayed (not a bounce): {addr}")
                return True
            if any(e["type"] == "bounce" for e in self.store.lead_events(lead["id"])):
                return True   # already handled (e.g. seen again by the recent-mail scan): count each bounce once
            self.store.suppress(key, "bounced")
            self.store.log_event(lead["id"], "bounce", {"address": addr, "subject": mail.get("subject", "")[:200]},
                                 actor="email")
            if lead["state"] in ("SENT", "OPENED"):
                try:
                    self.store.transition(lead["id"], "LOST", actor="email", reason="bounced")
                except Exception as e:
                    log.info(f"bounce: no state change for {lead['id']}: {e}")
            for o in self.store.outreach_for_lead(lead["id"]):
                if o["status"] in ("staged", "approved"):
                    try:
                        self.store.reject_outreach(o["id"], actor="email", reason="bounced")
                    except Exception:
                        pass
            log.info(f"📭 Bounce: {addr} suppressed")
            return True
        return False

    def _our_message_ids(self) -> Dict[str, str]:
        rows = self.store._conn().execute(
            "SELECT lead_id, json_extract(payload, '$.message_id') FROM lead_events WHERE type='message_out'"
            " AND json_extract(payload, '$.channel')='email' AND json_extract(payload, '$.message_id') IS NOT NULL")
        return {str(mid).strip("<> ").lower(): lead_id for lead_id, mid in rows if mid}

    def lead_for_mail(self, mail: Dict[str, Any], ours: Optional[Dict[str, str]] = None) -> Optional[Dict[str, Any]]:
        """Who is this from? Same address; else a reply in one of our threads; else the same company domain."""
        lead = self.store.find_lead_by_contact(contact_key("email", mail.get("from", "")))
        if lead:
            return lead
        ours = self._our_message_ids() if ours is None else ours
        for mid in re.findall(r"<([^>]+)>", f"{mail.get('in_reply_to', '')} {mail.get('references', '')}"):
            if mid.lower() in ours:
                return self.store.get_lead(ours[mid.lower()])
        domain = (mail.get("from", "").split("@")[-1] or "").lower()
        if domain and domain not in FREEMAIL:
            rows = self.store._conn().execute(
                "SELECT DISTINCT l.id FROM leads l JOIN outreach o ON o.lead_id=l.id WHERE o.status='sent'"
                " AND o.channel='email' AND lower(l.contact) LIKE ?", (f"%@{domain}",)).fetchall()
            if len(rows) == 1:
                return self.store.get_lead(rows[0][0])
        return None

    def _handled_key(self, mail: Dict[str, Any]) -> str:
        import hashlib
        ident = mail.get("message_id") or f"uid:{mail.get('uid')}:{mail.get('from')}"
        return "email:handled:" + hashlib.sha1(ident.encode("utf-8", "replace")).hexdigest()[:20]

    def handle_replies(self) -> int:
        handled = 0
        ours = self._our_message_ids()
        recent = getattr(self.transport, "fetch_recent", None)
        if recent:
            wanted = lambda h: is_bounce_sender(h["from"]) or self.lead_for_mail(h, ours) is not None  # noqa: E731
            mails = recent(days=int(get_secret("EMAIL_SCAN_DAYS", "3") or 3), wanted=wanted)
        else:
            mails = self.transport.fetch_unseen(wanted=lambda a: is_bounce_sender(a) or
                                                self.lead_for_mail({"from": a}, ours) is not None)
        first_scan = not self.store.kv_get("email:scan_v2")
        for mail in mails:
            key = self._handled_key(mail)
            if self.store.kv_get(key):
                continue
            if is_bounce_sender(mail["from"]):
                if self.handle_bounce(mail):
                    self.store.kv_set(key, "bounce")
                    if not mail.get("seen"):
                        self.transport.mark_seen(mail["uid"])
                    handled += 1
                continue   # a bounce we can't match stays as it is for you
            lead = self.lead_for_mail(mail, ours)
            if not lead:
                continue  # not someone we're talking to: leave it for you
            if first_scan and any(e["type"] == "message_in" for e in self.store.lead_events(lead["id"])):
                self.store.kv_set(key, "before-v2")   # answered by the old unread-only reader
                continue
            if mail.get("auto"):
                self.store.log_event(lead["id"], "auto_reply", {"subject": mail.get("subject", "")[:200]}, actor="email")
                self.store.kv_set(key, "auto")
                continue
            text = strip_quoted(mail["body"])[:4000]
            who = lead.get("company") or lead.get("name") or mail["from"]
            if lead["state"] != "DNC" and is_opt_out(mail.get("subject", ""), text):
                # "no" / "unsubscribe" (also the one-click header's empty mail): never write again. No AI call.
                self.store.log_event(lead["id"], "message_in", {"text": (text or mail.get("subject", ""))[:500],
                                                                "intent": "stop", "channel": "email"}, actor="prospect")
                try:
                    self.store.transition(lead["id"], "DNC", actor="email", reason="opted out")
                except Exception as e:
                    log.info(f"opt-out: {lead['id']} stays {lead['state']} ({e})")
                self.store.kv_set(key, "opt-out")
                if not mail.get("seen"):
                    self.transport.mark_seen(mail["uid"])
                self.alerts.notify(f"🚫 {who} opted out: marked do-not-contact, nothing more will be sent.", "orange")
                handled += 1
                continue
            if not text:
                # Nothing readable (an image, a signature only): never drop it silently. Stop the follow-ups
                # and hand it to the founder.
                self.store.log_event(lead["id"], "message_in", {"text": "(replied with no text)", "channel": "email"},
                                     actor="prospect")
                if lead["state"] in ("SENT", "OPENED"):
                    self.store.transition(lead["id"], "REPLIED", actor="email")
                self.store.kv_set(key, "empty")
                self.alerts.notify(f"📩 {who} replied with no readable text (maybe an image or attachment). "
                                   f"Open it in your inbox: your move.", "red")
                handled += 1
                continue
            if mail.get("seen") and getattr(self.transport, "answered_by_me", None) and \
                    self.transport.answered_by_me(mail.get("message_id", "")):
                self.store.log_event(lead["id"], "message_in", {"text": text[:2000], "channel": "email"}, actor="prospect")
                if lead["state"] in ("SENT", "OPENED"):
                    self.store.transition(lead["id"], "REPLIED", actor="email")
                self.store.kv_set(key, "answered-by-hand")
                self.alerts.notify(f"📩 {lead.get('company') or mail['from']} replied and you already answered "
                                   f"by hand, so Autocrat stayed out of it.", "orange")
                continue
            if lead["state"] == "DNC":
                self.alerts.notify(f"🟠 DNC contact {lead.get('name') or mail['from']} emailed again; not auto-replying.")
                self.store.kv_set(key, "dnc")
                continue
            if (lead.get("data") or {}).get("hands_off"):
                # You're handling this one yourself: record it and tell you, never auto-answer.
                self.store.log_event(lead["id"], "message_in", {"text": text[:2000], "channel": "email"}, actor="prospect")
                if lead["state"] in ("SENT", "OPENED"):
                    self.store.transition(lead["id"], "REPLIED", actor="email")
                self.store.kv_set(key, "hands-off")
                if not mail.get("seen"):
                    self.transport.mark_seen(mail["uid"])
                self.alerts.notify(f"📩 {who} replied. You're handling this one by hand, so Autocrat didn't answer:\n"
                                   f"“{' '.join(text.split())[:600]}”\n(to hand it back: auto {lead['id']})", "red")
                handled += 1
                continue
            transcript = self.transcript_for(lead["id"], text)
            decision = self.closer.decide(transcript, lead_id=lead["id"], channel="email")
            if not decision.get("reply") and not decision.get("held") \
                    and decision.get("intent") not in ("stop", "not_interested"):
                # We contacted them; silence loses sales. Ask once more, insisting on an answer.
                retry = self.closer.decide(transcript + "\n\n(Note: this is a prospect we emailed. Write a reply.)",
                                           lead_id=lead["id"], channel="email")
                if retry.get("reply"):
                    decision = retry
            # logs message_in, REPLIED/DNC; the alert below replaces the closer's own (one message per reply)
            self.closer.apply_outcome(lead, text, decision, alert=False)
            reply = decision["reply"]
            subject = mail["subject"] if mail["subject"].lower().startswith("re:") else f"Re: {mail['subject']}"
            outcome = ""
            draft_buttons = None
            if reply and RuntimeControl().is_dry_run:
                outcome = f"[dry run] would answer:\n{reply}"
            elif reply and not self.auto_reply:
                # Auto-replies paused: you approve every answer. One tap sends the draft in the thread.
                self.store.log_event(lead["id"], "reply_draft", {
                    "text": reply, "to": mail["from"], "subject": subject, "in_reply_to": mail["message_id"],
                    "references": mail["references"], "meeting": decision.get("meeting", ""),
                    "meeting_link": decision.get("meeting_link", ""), "intent": decision.get("intent")}, actor="closer")
                outcome = f"DRAFT, not sent (auto-replies are paused):\n{reply}"
                if re.search(r"\bdemos?\b", f"{text} {reply}", re.I):
                    outcome += f"\n\n🛠️ To build their free demo now, send: demo {lead['id']}"
                if (lead.get("data") or {}).get("track") == "ai_check" \
                        and decision.get("intent") not in ("stop", "not_interested"):
                    outcome += ("\n\n🔐 They got the free AI security check offer. Once they send a link and a "
                                "written OK, run the check kit: staging 30-50 tries, production 25-30 gentle ones, "
                                "and verify every break by hand before sending 1-2 of them.")
                draft_buttons = [[{"text": "📤 Send this reply", "callback_data": f"rs:{lead['id']}"},
                                  {"text": "✋ I'll answer myself", "callback_data": f"rx:{lead['id']}"}]]
            elif reply and not RuntimeControl().killed:
                body = f"{reply}\n\n{self.sign(True)}"
                self.sleep(min(120.0, 20 + len(text) / 10))   # don't reply in 2 seconds
                try:
                    ics = self.invite_for(lead, decision, mail["from"])
                    msg_id = self.transport.send(mail["from"], subject, body, in_reply_to=mail["message_id"],
                                                 references=mail["references"], **({"ics": ics} if ics else {}))
                    self.store.log_event(lead["id"], "message_out", {"text": reply, "subject": subject,
                                                                     "message_id": msg_id, "channel": "email",
                                                                     "intent": decision["intent"]}, actor="closer")
                    outcome = f"auto-replied:\n{reply}"
                except Exception as e:
                    outcome = f"reply FAILED ({e}); answer them yourself:\n{reply}"
            elif decision.get("held"):
                outcome = ("NOT SENT, needs you: the draft broke a rule (" + "; ".join(decision["held"])[:300]
                           + f").\nDraft to fix and send yourself:\n{decision.get('draft', '')}")
            elif decision["intent"] in ("stop", "not_interested"):
                outcome = "marked do-not-contact"
            else:
                outcome = "no auto-reply: your move"
            if decision.get("model") == "strong" and outcome.startswith("auto-replied"):
                outcome = outcome.replace("auto-replied", "auto-replied (stronger model)", 1)
            log.info(f"📩 reply from {mail['from']} ({decision.get('intent')}): {outcome.splitlines()[0][:120]}")
            sender = parseaddr(mail["from"])[1].lower() or mail["from"].lower()
            fresh = self.store.get_lead(lead["id"]) or lead
            if "@" in sender and sender != (fresh.get("contact") or "").lower() and \
                    (fresh.get("data") or {}).get("reply_to") != sender:
                # A colleague answered: they're the one talking to us now (invoices and follow-ups go to them).
                fresh = self.store.update_lead_fields(lead["id"], data={**(fresh.get("data") or {}), "reply_to": sender})
            buttons = draft_buttons
            if decision.get("intent") == "ready_to_pay":
                from nexus.integrations.deals import money_buttons
                buttons = (draft_buttons or []) + (money_buttons(self.store, fresh) or []) or None
            self.alerts.notify(reply_alert(fresh, mail["from"], text, decision, outcome),
                               "red" if (decision.get("hot") or decision.get("held")) else "orange", buttons=buttons)
            self.store.kv_set(key, "replied")
            if not mail.get("seen"):
                self.transport.mark_seen(mail["uid"])
            handled += 1
        self.store.kv_set("email:scan_v2", "1")
        return handled
