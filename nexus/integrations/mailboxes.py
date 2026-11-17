"""
Several inboxes on the sending domain, behind the same interface as one mailbox.

  EMAIL_EXTRA_ACCOUNTS=EMAIL2,EMAIL3        the prefixes of the extra inboxes
  EMAIL2_ADDRESS=...   EMAIL2_PASSWORD=...  (an app password)   EMAIL2_FROM_NAME=Parth

Rules
  * A thread always continues from the inbox that started it (replies, follow-ups, invoices, reminders).
  * A new first email goes out from the inbox with the most room left today.
  * Each extra inbox warms up on its own: EMAIL_EXTRA_START (5) a day, +EMAIL_CAP_STEP per day since its
    first send, up to EMAIL_CAP_MAX. If bounces trip the safety rule, every inbox drops back to its start.
  * Replies are read from every inbox.
  * The sender's first name in the text follows the inbox (Yash's mail says Yash, Parth's says Parth).
"""

from __future__ import annotations

import hashlib
import re
import time
from typing import Any, Callable, Dict, List, Optional

from nexus.core.logger import get_logger
from nexus.core.secrets import get_secret

log = get_logger("mailboxes")

# The provider stopped this account sending ("unusual activity", rate limits). Retrying makes it worse.
BLOCK_MARKERS = ("5.4.6", "unusual sending activity", "unblockme", "5.7.1 ", "rate limit", "too many",
                 "sending limit", "temporarily blocked", "account is blocked", "suspended")


class MailboxBlocked(RuntimeError):
    def __init__(self, reason: str, address: str):
        super().__init__(f"{address}: {reason}")
        self.address, self.reason = address, reason


def is_block_error(e: Exception) -> bool:
    text = str(e).lower()
    return isinstance(e, MailboxBlocked) or any(m in text for m in BLOCK_MARKERS)


def is_paused(store, address: str) -> bool:
    until = float(store.kv_get(f"mailbox:paused:{(address or '').lower()}") or 0)
    return until > time.time()


def pause(store, address: str, hours: float) -> bool:
    """Stop sending from this inbox for a while. Returns True the first time (so you're alerted once)."""
    key = f"mailbox:paused:{(address or '').lower()}"
    first = not is_paused(store, address)
    store.kv_set(key, str(time.time() + hours * 3600))
    return first


class MailboxPool:
    def __init__(self, store, boxes: List[Any]):
        self.store = store
        self.boxes = [b for b in boxes if getattr(b, "configured", False)]
        self.primary_cap = 0          # set by the worker each cycle (the main inbox's warm-up limit)
        self.reset = False            # True while the bounce safety rule holds everything at its start
        self.day = time.strftime("%Y-%m-%d")

    # ── identity ─────────────────────────────────────────────────

    @property
    def primary(self):
        return self.boxes[0]

    @property
    def address(self) -> str:
        return self.primary.address

    @property
    def addresses(self) -> set:
        return {(b.address or "").lower() for b in self.boxes}

    @property
    def configured(self) -> bool:
        return bool(self.boxes)

    def _box(self, address: str):
        return next((b for b in self.boxes if (b.address or "").lower() == (address or "").lower()), None)

    # ── per-inbox limits ─────────────────────────────────────────

    def sent_today(self, box) -> int:
        return int(self.store.kv_get(f"email:sent:{box.address.lower()}:{self.day}") or 0)

    def cap_for(self, box, now_ts: Optional[float] = None) -> int:
        if box is self.primary:
            return int(self.primary_cap)
        start = int(get_secret("EMAIL_EXTRA_START", "5") or 5)
        first = float(self.store.kv_get(f"mailbox:first:{box.address.lower()}") or 0)
        if self.reset or not first:
            return start
        days = int(((now_ts or time.time()) - first) // 86400)
        step = int(get_secret("EMAIL_CAP_STEP", "3") or 3)
        return min(int(get_secret("EMAIL_CAP_MAX", "60") or 60), start + step * days)

    def extra_capacity(self) -> int:
        return sum(self.cap_for(b) for b in self.boxes[1:] if getattr(b, "readable", False) and not self.paused(b))

    def status(self) -> List[Dict[str, Any]]:
        return [{"address": b.address, "name": b.from_name, "cap": self.cap_for(b), "sent": self.sent_today(b)}
                for b in self.boxes]

    def paused(self, box) -> bool:
        return is_paused(self.store, box.address)

    def all_paused(self) -> bool:
        return all(self.paused(b) for b in self.boxes)

    def pick(self):
        """The inbox with the most room left today, for a brand-new thread."""
        # An extra inbox only starts threads once we've managed to read it: replies we can't see are lost sales.
        usable = [(i, b) for i, b in enumerate(self.boxes) if not self.paused(b)
                  and (b is self.primary or getattr(b, "readable", False))]
        if not usable:
            raise MailboxBlocked("every inbox is paused by the provider", self.primary.address)
        room = [(self.cap_for(b) - self.sent_today(b), -i, b) for i, b in usable]
        best = max(room, key=lambda r: (r[0], r[1]))
        return best[2] if best[0] > 0 else self.primary

    # ── which inbox owns a thread ────────────────────────────────

    @staticmethod
    def _key(message_id: str) -> str:
        return "msgbox:" + hashlib.sha1(message_id.strip("<> ").lower().encode("utf-8", "replace")).hexdigest()[:20]

    def _remember(self, message_id: str, box) -> None:
        if message_id:
            self.store.kv_set(self._key(message_id), box.address.lower())

    def box_for_thread(self, in_reply_to: str = "", references: str = ""):
        for mid in re.findall(r"<([^>]+)>", f"{in_reply_to} {references}"):
            box = self._box(self.store.kv_get(self._key(mid)) or "")
            if box:
                return box
        return None

    def personalise(self, body: str, box) -> str:
        """The text is drafted in the main sender's name: swap the first name when another inbox sends it."""
        mine, theirs = (self.primary.from_name or "").split(" ")[0], (box.from_name or "").split(" ")[0]
        if box is self.primary or not mine or not theirs or mine.lower() == theirs.lower():
            return body
        swap = lambda m: theirs.lower() if m.group(0).islower() else theirs  # noqa: E731
        return re.sub(rf"\b{re.escape(mine)}\b", swap, body, flags=re.I)

    # ── the EmailTransport interface ─────────────────────────────

    def send(self, to: str, subject: str, body: str, in_reply_to: str = "", references: str = "", ics: str = "") -> str:
        box = self.box_for_thread(in_reply_to, references)
        if box is None:
            box = self.pick() if not in_reply_to else self.primary
        kwargs = {"ics": ics} if ics else {}
        if self.paused(box):
            raise MailboxBlocked("this inbox is paused by the provider", box.address)
        try:
            message_id = box.send(to, subject, self.personalise(body, box), in_reply_to=in_reply_to,
                                  references=references, **kwargs)
        except Exception as e:
            if is_block_error(e):
                raise MailboxBlocked(str(e), box.address) from e
            raise
        self._remember(message_id, box)
        key = f"email:sent:{box.address.lower()}:{self.day}"
        self.store.kv_set(key, str(int(self.store.kv_get(key) or 0) + 1))
        if not self.store.kv_get(f"mailbox:first:{box.address.lower()}"):
            self.store.kv_set(f"mailbox:first:{box.address.lower()}", str(time.time()))
        return message_id

    def _tag(self, mails: List[Dict[str, Any]], box) -> List[Dict[str, Any]]:
        for m in mails:
            m["uid"] = f"{box.address}|{m.get('uid', '')}"
            m["mailbox"] = box.address
            self._remember(m.get("message_id", ""), box)   # so our answer leaves from the inbox they wrote to
        return mails

    def fetch_recent(self, days: int = 3, limit: int = 40, wanted: Optional[Callable] = None) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for box in self.boxes:
            try:
                out += self._tag(box.fetch_recent(days=days, limit=limit, wanted=wanted), box)
                if not getattr(box, "readable", False):
                    box.readable = True
                    if box is not self.primary:
                        log.info(f"📬 {box.address} is readable: it can now start new threads")
            except Exception as e:
                if getattr(box, "readable", None) is not False:
                    log.warning(f"Reading {box.address} failed: {e}" if box is self.primary else
                                f"Reading {box.address} failed, so it won't start new threads yet: {e}")
                box.readable = False
        return out

    def fetch_unseen(self, limit: int = 20, wanted: Optional[Callable] = None) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for box in self.boxes:
            try:
                out += self._tag(box.fetch_unseen(limit=limit, wanted=wanted), box)
            except Exception as e:
                log.warning(f"Reading {box.address} failed: {e}")
        return out

    def mark_seen(self, uid: str) -> None:
        address, _, real = str(uid).partition("|")
        box = self._box(address)
        if box and real:
            box.mark_seen(real)

    def answered_by_me(self, message_id: str) -> bool:
        owner = self.box_for_thread(message_id)
        for box in ([owner] if owner else self.boxes):
            try:
                if box.answered_by_me(message_id):
                    return True
            except Exception as e:
                log.info(f"Sent-folder check on {box.address} failed: {e}")
        return False


def build_transport(store):
    """One mailbox, or a pool when EMAIL_EXTRA_ACCOUNTS names more (those without a password are skipped)."""
    from nexus.integrations.email_channel import EmailTransport
    main = EmailTransport()
    extras = []
    for prefix in [p.strip().upper() for p in (get_secret("EMAIL_EXTRA_ACCOUNTS", "") or "").split(",") if p.strip()]:
        box = EmailTransport(prefix)
        if box.configured:
            extras.append(box)
        else:
            log.info(f"Mailbox {prefix}: no address or password yet, skipped")
    if not extras or not main.configured:
        return main
    log.info(f"📬 {1 + len(extras)} inboxes: {main.address} + {', '.join(b.address for b in extras)}")
    return MailboxPool(store, [main, *extras])
