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

    def send(self, to: str, subject: str, body: str, in_reply_to: str = "", references: str = "") -> str:
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
                msg = email.message_from_bytes(raw)
                body = ""
                if msg.is_multipart():
                    for part in msg.walk():
                        if part.get_content_type() == "text/plain" and not part.get_filename():
                            body = part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", "replace")
                            break
                else:
                    body = msg.get_payload(decode=True).decode(msg.get_content_charset() or "utf-8", "replace")
                out.append({"uid": uid.decode(), "from": parseaddr(msg.get("From", ""))[1].lower(),
                            "subject": msg.get("Subject", ""), "message_id": msg.get("Message-ID", ""),
                            "references": msg.get("References", ""), "body": body})
        return out

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
        return {"sent": self.send_openers(now), "replied": self.handle_replies()}

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
        sent = 0
        for out in queue:
            if sent_today + sent >= self.daily_cap:
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
            subject, body = split_subject(out["draft"])
            body = f"{body}\n\n{self.signature}\n\n{self.optout}"
            if RuntimeControl().is_dry_run:
                continue  # dry run: the Telegram alert already shows the draft; nothing is sent
            try:
                if out["status"] == "staged":
                    self.store.approve_outreach(out["id"], actor="email-auto")
                    if lead["state"] == "STAGED":
                        self.store.transition(lead["id"], "APPROVED", actor="email-auto")
                msg_id = self.transport.send(to, subject, body)
                self.store.mark_outreach_sent(out["id"], actor="email")
            except Exception as e:
                log.warning(f"Email opener to lead {lead['id']} failed: {e}")
                continue
            self.store.log_event(lead["id"], "message_out", {"text": body, "subject": subject,
                                                             "message_id": msg_id, "channel": "email"}, actor="email")
            sent += 1
        if sent:
            self.store.kv_set(day_key, str(sent_today + sent))
            self.alerts.notify(f"📧 Sent {sent} email opener(s) ({sent_today + sent}/{self.daily_cap} today)", "orange")
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

    def handle_replies(self) -> int:
        handled = 0
        known = lambda addr: self.store.find_lead_by_contact(contact_key("email", addr)) is not None  # noqa: E731
        for mail in self.transport.fetch_unseen(wanted=known):
            lead = self.store.find_lead_by_contact(contact_key("email", mail["from"]))
            if not lead:
                continue  # not someone we're talking to: leave it unread for you
            text = strip_quoted(mail["body"])[:4000]
            if not text:
                self.transport.mark_seen(mail["uid"])
                continue
            if lead["state"] == "DNC":
                self.alerts.notify(f"🟠 DNC contact {lead.get('name') or mail['from']} emailed again; not auto-replying.")
                self.transport.mark_seen(mail["uid"])
                continue
            decision = self.closer.decide(self.transcript_for(lead["id"], text), lead_id=lead["id"], channel="email")
            self.closer.apply_outcome(lead, text, decision)   # logs message_in, REPLIED/DNC, hot alerts
            reply = decision["reply"]
            subject = mail["subject"] if mail["subject"].lower().startswith("re:") else f"Re: {mail['subject']}"
            if reply and RuntimeControl().is_dry_run:
                self.alerts.notify(f"🟠 [dry run] Email reply to {lead.get('name') or mail['from']} "
                                   f"({decision['intent']}):\n{reply}", "orange")
            elif reply and not RuntimeControl().killed:
                body = f"{reply}\n\n{self.signature}"
                self.sleep(min(120.0, 20 + len(text) / 10))   # don't reply in 2 seconds
                msg_id = self.transport.send(mail["from"], subject, body, in_reply_to=mail["message_id"],
                                             references=mail["references"])
                self.store.log_event(lead["id"], "message_out", {"text": reply, "subject": subject,
                                                                 "message_id": msg_id, "channel": "email",
                                                                 "intent": decision["intent"]}, actor="closer")
            self.transport.mark_seen(mail["uid"])
            handled += 1
        return handled
