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


def compute_cap(store, base: int, cap_max: int = 40, step: int = 3,
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
    phone = (lead.get("data") or {}).get("phone") or (lead.get("contact") if (lead.get("contact") or "").startswith("+") else "")
    number = normalize(phone or "")
    if not number:
        return ""
    first = (lead.get("name") or "").split()[0].title() if lead.get("name") else ""
    note = f"hi{' ' + first if first else ''}, yash here, just saw your email. easier to sort this on whatsapp?"
    return f"https://wa.me/{number}?text={quote(note)}"


QUOTE_MARKERS = [re.compile(p, re.I | re.M) for p in (
    r"^On .+wrote:\s*$", r"^-{2,}\s*Original Message", r"^From:\s.+$", r"^Sent from my ",
)]


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

    def notify(self, text: str, priority: str = "orange") -> None:
        for n in self.notifiers:
            try:
                n.notify(text, priority=priority)
            except Exception as e:
                log.warning(f"alert via {type(n).__name__} failed: {e}")


class EmailTransport:
    """SMTP + IMAP for one mailbox (e.g. Zoho on your cold domain)."""

    def __init__(self):
        self.address = get_secret("EMAIL_ADDRESS")
        self.password = get_secret("EMAIL_PASSWORD")
        self.smtp_host = get_secret("EMAIL_SMTP_HOST", "smtp.zoho.com")
        self.smtp_port = int(get_secret("EMAIL_SMTP_PORT", "465") or 465)
        self.imap_host = get_secret("EMAIL_IMAP_HOST", "imap.zoho.com")
        self.from_name = get_secret("EMAIL_FROM_NAME", "Yash")

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
        self.transport = transport or EmailTransport()
        self.sleep = sleep
        self.auto_send = env_bool("EMAIL_AUTO_SEND", False)
        self.daily_cap = int(get_secret("EMAIL_DAILY_CAP", "20") or 20)
        self.hours = get_secret("EMAIL_SEND_HOURS", "9-18")
        self.offset = int(get_secret("EMAIL_UTC_OFFSET_MINUTES", "240") or 240)
        self.poll_seconds = float(get_secret("EMAIL_POLL_SECONDS", "120") or 120)
        self.optout = get_secret("EMAIL_OPTOUT_LINE", "p.s. if this isn't relevant, just reply 'no' and i won't email again")
        self.signature = get_secret("EMAIL_SIGNATURE", "Yash")
        self.postal = (get_secret("EMAIL_POSTAL_ADDRESS", "") or "").strip()
        self.wa_number = (get_secret("WHATSAPP_BUSINESS_NUMBER", "") or "").strip()
        self.wa_markets = [m.strip().lower() for m in
                           (get_secret("EMAIL_WHATSAPP_LINK_MARKETS", "UAE") or "UAE").split(",") if m.strip()]
        self.cap_max = int(get_secret("EMAIL_CAP_MAX", "40") or 40)
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
        for name, fn in (("reminders", self.meeting_reminders), ("nudged", self.nudge_quiet_conversations),
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
        return booking.ics_invite(start, summary=f"{self.signature} x {who}", link=decision.get("meeting_link", ""),
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
            for window, label in ((65, "60"), (12, "10")):
                key = f"meeting:remind{label}:{m['lead_id']}:{m['start']:%Y%m%d%H%M}"
                if 0 < mins <= window and not self.store.kv_get(key):
                    self.store.kv_set(key, "1")
                    self.alerts.notify(f"⏰ Call with {who} in {int(mins)} min ({mine}) · {m['link']}", "red")
                    sent += 1
                    if label == "60" and "@" in (lead.get("contact") or "") and not RuntimeControl().is_dry_run \
                            and not RuntimeControl().killed:
                        thread = self._thread_of(lead["id"])
                        theirs = booking.describe(m["start"], int(m.get("their_offset") or 0))
                        try:
                            self.transport.send(lead["contact"], f"Re: {thread['subject'] or 'our call'}".replace("Re: Re:", "Re:"),
                                                f"quick reminder, we're on at {theirs} your time: {m['link']}\n\n{self.signature}",
                                                in_reply_to=thread["last_id"], references=thread["message_id"])
                        except Exception as e:
                            log.info(f"meeting reminder email failed: {e}")
        return sent

    # ── conversations that went quiet after they replied ─────────

    def nudge_quiet_conversations(self, now: Optional[datetime] = None) -> int:
        """They replied, we answered, then silence: one short nudge after EMAIL_NUDGE_HOURS (48),
        a second after another 72h, then stop. Business hours only, inside the daily cap."""
        if RuntimeControl().is_dry_run or RuntimeControl().killed:
            return 0
        now = now or datetime.now(timezone.utc)
        hours = float(get_secret("EMAIL_NUDGE_HOURS", "48") or 48)
        sent = 0
        rows = self.store._conn().execute("SELECT id FROM leads WHERE state='REPLIED' ORDER BY updated_at LIMIT 40").fetchall()
        for (lead_id,) in rows:
            lead = self.store.get_lead(lead_id)
            if "@" not in (lead.get("contact") or ""):
                continue
            events = self.store.lead_events(lead_id)
            talk = [e for e in events if e["type"] in ("message_in", "message_out")]
            nudges = sum(1 for e in events if e["type"] == "message_out" and (e.get("payload") or {}).get("kind") == "nudge")
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
                msg_id = self.transport.send(lead["contact"], subject, f"{draft}\n\n{self.signature}",
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
        return compute_cap(self.store, self.daily_cap, self.cap_max, self.cap_step, now_ts)

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

    def draft_followup(self, lead: Dict[str, Any], n: int, opener: str) -> str:
        from nexus.integrations.whatsapp_closer import humanize
        voice = getattr(self.closer, "voice", "") or ""
        system = (
            f"Write follow-up #{n} of 2 from Yash in an email thread. They got the message below and didn't reply.\n"
            "Rules: 1-2 short sentences, lowercase and casual like Yash's voice, no greeting line needed.\n"
            + ("#1: add ONE new, specific, useful angle tied to their product (a quick free look at one thing, "
               "or a concrete example of what usually breaks), then an easy yes/no question.\n" if n == 1 else
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

    def send_openers(self, now: Optional[datetime] = None) -> int:
        local = self._local_now(now)
        day_key = f"email:sent:{local.strftime('%Y-%m-%d')}"
        sent_today = int(self.store.kv_get(day_key) or 0)
        queue = self.store.list_outreach("approved", channel="email")
        if self.auto_send:
            queue += self.store.list_outreach("staged", channel="email")
        queue.sort(key=lambda o: 0 if (o.get("kind") or "").startswith("followup") else 1)  # warm threads first
        cap = self.cap_status()["cap"]
        sent = 0
        for out in queue:
            if sent_today + sent >= cap:
                break
            lead = self.store.get_lead(out["lead_id"])
            to = (lead or {}).get("contact") or ""
            if not lead or "@" not in to:
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
            if self.postal:  # CAN-SPAM (US) wants a physical postal address on commercial email
                body = f"{body}\n{self.postal}"
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
                log.warning(f"Email opener to lead {lead['id']} failed: {e}")
                continue
            self.store.log_event(lead["id"], "message_out", {"text": body, "subject": subject, "kind": out.get("kind"),
                                                             "message_id": msg_id, "channel": "email"}, actor="email")
            sent += 1
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
        text = f"{mail.get('dsn', '')}\n{mail.get('body', '')}"
        for addr in dict.fromkeys(a.lower().strip(".") for a in EMAIL_RE.findall(text)):
            if addr == own or is_bounce_sender(addr):
                continue
            key = contact_key("email", addr)
            lead = self.store.find_lead_by_contact(key)
            if not lead or not any(o["status"] == "sent" for o in self.store.outreach_for_lead(lead["id"])):
                continue
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
            if not text:
                self.store.kv_set(key, "empty")
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
            transcript = self.transcript_for(lead["id"], text)
            decision = self.closer.decide(transcript, lead_id=lead["id"], channel="email")
            if not decision.get("reply") and decision.get("intent") not in ("stop", "not_interested"):
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
            if reply and RuntimeControl().is_dry_run:
                outcome = f"[dry run] would answer:\n{reply}"
            elif reply and not RuntimeControl().killed:
                body = f"{reply}\n\n{self.signature}"
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
            elif decision["intent"] in ("stop", "not_interested"):
                outcome = "marked do-not-contact"
            else:
                outcome = "no auto-reply: your move"
            log.info(f"📩 reply from {mail['from']} ({decision.get('intent')}): {outcome.splitlines()[0][:120]}")
            self.alerts.notify(reply_alert(lead, mail["from"], text, decision, outcome),
                               "red" if decision.get("hot") else "orange")
            self.store.kv_set(key, "replied")
            if not mail.get("seen"):
                self.transport.mark_seen(mail["uid"])
            handled += 1
        self.store.kv_set("email:scan_v2", "1")
        return handled
