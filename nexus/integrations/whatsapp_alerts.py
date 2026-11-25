"""
Autocrat — internal alerts over WhatsApp Web (founders only).

Sends Autocrat's own alerts (hot lead, demo forwarded, deposit opened, kill
switch…) to YOUR numbers and YOUR group, from a WhatsApp account logged into a
dedicated Playwright browser profile.

Hard limits (enforced in code, not just config):
  * Recipients are an allowlist: WHATSAPP_ALERT_NUMBERS / WHATSAPP_ALERT_GROUPS.
    send() refuses anything else. This module never messages prospects —
    prospect conversations go through the official Cloud API closer only.
  * Rate-limited (WHATSAPP_ALERT_MAX_PER_HOUR, default 12) and batched: alerts
    arriving close together become one message per recipient.
  * Quiet hours (TELEGRAM_QUIET_HOURS / WHATSAPP_QUIET_HOURS, IST): orange alerts
    are held and delivered as one digest when quiet hours end; red ones go now.

Use a separate sender account (a spare number). WhatsApp gives no notification
for messages you send to yourself, and automating the business number used by
the Cloud API closer would put that number at risk.

CLI:
    python -m nexus.integrations.whatsapp_alerts login            # scan QR in a window (laptop)
    python -m nexus.integrations.whatsapp_alerts login --headless # VM: QR saved to .autocrat/whatsapp-qr.png
    python -m nexus.integrations.whatsapp_alerts test             # send a test alert to the allowlist
"""

from __future__ import annotations

import argparse
import html
import queue
import re
import threading
import time
from collections import deque
from pathlib import Path
from typing import Callable, Deque, Dict, List, Optional
from urllib.parse import quote

from nexus.core.events import EventBus
from nexus.core.logger import get_logger
from nexus.core.secrets import PROJECT_ROOT, env_bool, env_list, get_secret
from nexus.integrations.telegram_alerts import build_alert, in_quiet_hours

log = get_logger("whatsapp_alerts")

WEB_URL = "https://web.whatsapp.com/"
DEFAULT_PROFILE = PROJECT_ROOT / ".autocrat" / "whatsapp-alerts-profile"

# WhatsApp Web changes its DOM often; each target has fallbacks, tried in order.
SELECTORS = {
    "logged_in": ["#pane-side", "div[aria-label='Chat list']", "div[data-testid='chat-list']"],
    "qr": ["canvas[aria-label*='QR' i]", "div[data-ref] canvas", "canvas"],
    "compose": ["footer div[contenteditable='true']", "div[contenteditable='true'][data-tab='10']",
                "div[aria-label='Type a message']"],
    "search": ["div[contenteditable='true'][data-tab='3']", "div[aria-label='Search input textbox']",
               "div[aria-label*='Search' i][contenteditable='true']", "input[aria-label*='Search' i]",
               "[role='textbox'][aria-label*='Search' i]", "input[placeholder*='Search' i]"],
    "stay_logged_in": ["input[type='checkbox']"],
    "invalid_number": ["div[data-animate-modal-popup='true']"],
}


def launch_context(pw, profile_dir: Path, headless: bool):
    """Persistent Chromium context WhatsApp Web accepts.

    Headless Chromium reports "HeadlessChrome" in its user agent and WhatsApp
    answers with an "update your browser" page, so send the regular Chrome UA
    for the same Chromium version and OS.
    """
    import platform
    import sys as _sys
    probe = pw.chromium.launch(headless=True)
    major = probe.version.split(".")[0]
    probe.close()
    if _sys.platform.startswith("win"):
        os_part = "Windows NT 10.0; Win64; x64"
    elif _sys.platform == "darwin":
        os_part = "Macintosh; Intel Mac OS X 10_15_7"
    else:
        os_part = f"X11; Linux {platform.machine() or 'x86_64'}"
    user_agent = (f"Mozilla/5.0 ({os_part}) AppleWebKit/537.36 (KHTML, like Gecko) "
                  f"Chrome/{major}.0.0.0 Safari/537.36")
    profile_dir.mkdir(parents=True, exist_ok=True)
    return pw.chromium.launch_persistent_context(
        str(profile_dir), headless=headless, user_agent=user_agent,
        viewport={"width": 1280, "height": 900}, locale="en-US")


class RecipientNotAllowed(ValueError):
    pass


def to_whatsapp_text(markup: str) -> str:
    """Convert the alert HTML used for Telegram into WhatsApp formatting."""
    text = re.sub(r"</?b>", "*", markup)
    text = re.sub(r"</?i>", "_", text)
    text = re.sub(r"</?code>", "", text)  # IDs stay inline and copyable
    text = re.sub(r"<a\s+href=\"([^\"]+)\"[^>]*>(.*?)</a>", r"\2: \1", text)
    text = re.sub(r"<[^>]+>", "", text)
    return html.unescape(text).strip()


def _digits(number: str) -> str:
    digits = re.sub(r"\D", "", number or "")
    return digits[2:] if digits.startswith("00") else digits


class WhatsAppAlertNotifier:
    def __init__(self, numbers: Optional[List[str]] = None, groups: Optional[List[str]] = None,
                 profile_dir: Optional[Path] = None, headless: Optional[bool] = None,
                 max_per_hour: Optional[int] = None, batch_seconds: float = 45.0,
                 quiet_hours: Optional[str] = None, events: Optional[EventBus] = None,
                 store=None, sender: Optional[Callable[[str, str, str], None]] = None):
        self.numbers = [_digits(n) for n in (numbers if numbers is not None
                                             else env_list("WHATSAPP_ALERT_NUMBERS")) if _digits(n)]
        self.groups = [g.strip() for g in (groups if groups is not None
                                           else env_list("WHATSAPP_ALERT_GROUPS")) if g.strip()]
        self.profile_dir = Path(profile_dir or get_secret("WHATSAPP_ALERT_PROFILE") or DEFAULT_PROFILE)
        self.headless = env_bool("WHATSAPP_ALERT_HEADLESS", True) if headless is None else headless
        self.max_per_hour = int(max_per_hour or get_secret("WHATSAPP_ALERT_MAX_PER_HOUR", "12") or 12)
        self.level = (get_secret("WHATSAPP_ALERT_LEVEL", "essential") or "essential").strip().lower()
        self.batch_seconds = batch_seconds
        self.quiet_hours = quiet_hours if quiet_hours is not None else (
            get_secret("WHATSAPP_QUIET_HOURS") or get_secret("TELEGRAM_QUIET_HOURS", "23-8"))
        self.events = events or EventBus()
        self._store = store
        self._sender = sender  # injectable for tests: (kind, target, text) -> None
        self._queue: "queue.Queue[Dict]" = queue.Queue()
        self._held: List[str] = []
        self._sent_times: Deque[float] = deque()
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._page = None
        self._context = None
        self._pw = None
        # Other features (the closer) share this one WhatsApp Web session: they
        # register a poller that runs on this thread, so only one browser profile
        # is ever open. Each entry: [callable(page), interval_fn() -> seconds, next_due].
        self._pollers: List[list] = []

    def add_poller(self, fn: Callable, interval_fn: Callable[[], float]) -> None:
        self._pollers.append([fn, interval_fn, 0.0])

    def _run_due_pollers(self) -> None:
        now = time.time()
        for entry in self._pollers:
            fn, interval_fn, due = entry
            if now < due:
                continue
            entry[2] = now + max(5.0, float(interval_fn()))
            try:
                fn(self._ensure_page())
            except Exception as e:
                log.warning(f"WhatsApp poller {getattr(fn, '__qualname__', fn)} failed: {e}")

    def _next_wait(self) -> float:
        if not self._pollers:
            return 30.0
        soonest = min(entry[2] for entry in self._pollers)
        return max(1.0, min(30.0, soonest - time.time()))

    # ── allowlist ────────────────────────────────────────────────

    @property
    def configured(self) -> bool:
        return bool(self.numbers or self.groups)

    def recipients(self) -> List[tuple]:
        return [("number", n) for n in self.numbers] + [("group", g) for g in self.groups]

    def _check_allowed(self, kind: str, target: str) -> None:
        allowed = (kind == "number" and _digits(target) in self.numbers) or \
                  (kind == "group" and target in self.groups)
        if not allowed:
            raise RecipientNotAllowed(f"{kind} {target!r} is not on the WhatsApp alert allowlist")

    # ── event wiring ─────────────────────────────────────────────

    def attach(self) -> None:
        for pattern in ("lead.*", "control.*", "payment.*", "report.*"):
            self.events.on(pattern, self._on_event)

    def _on_event(self, entry: Dict) -> None:
        try:
            alert = build_alert(entry, store=self._store)
        except Exception as e:
            log.warning(f"WhatsApp alert build failed for {entry.get('event')}: {e}")
            return
        # "essential" (default): WhatsApp only carries what needs you now. Staged drafts and other
        # routine lead events stay on Telegram, which keeps this number's volume low.
        if alert and self.level == "essential" and (entry.get("event") or "").startswith("lead.") \
                and alert.get("priority") != "red":
            return
        if alert:
            self.notify(to_whatsapp_text(alert["text"]), priority=alert["priority"])

    def notify(self, text: str, priority: str = "orange", only: Optional[str] = None) -> None:
        """Thread-safe: queue an alert for the sender thread. only="groups" skips the numbers."""
        self._queue.put({"text": text, "priority": priority, "ts": time.time(), "only": only})

    # ── batching / rate limiting (pure, testable) ────────────────

    def _rate_ok(self, now: float) -> bool:
        while self._sent_times and now - self._sent_times[0] > 3600:
            self._sent_times.popleft()
        return len(self._sent_times) < self.max_per_hour

    def compose_batch(self, items: List[Dict], now: Optional[float] = None) -> Optional[str]:
        """Merge queued alerts (+ held ones if quiet hours ended) into one message."""
        quiet = in_quiet_hours(self.quiet_hours) if self.quiet_hours else False
        lines: List[str] = []
        for item in items:
            if quiet and item["priority"] != "red":
                self._held.append(item["text"])
            else:
                lines.append(item["text"])
        if not quiet and self._held:
            lines = [f"🌅 Held overnight ({len(self._held)}):"] + self._held + ([""] + lines if lines else [])
            self._held = []
        if not lines:
            return None
        header = "🤖 Autocrat" if len(lines) == 1 else f"🤖 Autocrat · {len(lines)} alerts"
        return header + "\n" + "\n\n".join(lines)

    # ── sender thread ────────────────────────────────────────────

    def start(self) -> bool:
        if not self.configured and not self._pollers:
            log.info("WhatsApp: no alert recipients and no closer — not starting")
            return False
        if self._running:
            return True
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True, name="whatsapp-alerts")
        self._thread.start()
        log.info(f"📲 WhatsApp alerts → {len(self.numbers)} number(s), {len(self.groups)} group(s)")
        return True

    def stop(self) -> None:
        self._running = False
        self._queue.put({"stop": True})

    def _run(self) -> None:
        while self._running:
            self._run_due_pollers()
            try:
                first = self._queue.get(timeout=self._next_wait())
            except queue.Empty:
                if self._held and not in_quiet_hours(self.quiet_hours):
                    self._flush([])
                continue
            if first.get("stop"):
                break
            items = [first]
            deadline = time.time() + (5 if first["priority"] == "red" else self.batch_seconds)
            while time.time() < deadline:
                try:
                    nxt = self._queue.get(timeout=max(0.1, deadline - time.time()))
                except queue.Empty:
                    break
                if nxt.get("stop"):
                    self._running = False
                    break
                items.append(nxt)
            self._flush(items)
        self._close_browser()

    def _flush(self, items: List[Dict]) -> None:
        message = self.compose_batch(items)
        if not message:
            return
        now = time.time()
        if not self._rate_ok(now):
            log.warning("WhatsApp alert rate limit reached — holding message")
            self._held.append(message)
            return
        targets = self.recipients()
        if items and all(i.get("only") == "groups" for i in items):
            targets = [t for t in targets if t[0] == "group"]
        # groups first: a bad number can leave a popup that blocks the next send
        targets.sort(key=lambda t: 0 if t[0] == "group" else 1)
        for kind, target in targets:
            try:
                self.send(kind, target, message)
            except Exception as e:
                log.warning(f"WhatsApp alert to {kind} {target} failed: {e}")
        self._sent_times.append(now)

    def send(self, kind: str, target: str, text: str) -> None:
        self._check_allowed(kind, target)
        if self._sender:
            self._sender(kind, target, text)
            return
        page = self._ensure_page()
        if kind == "number":
            self._send_to_number(page, target, text)
        else:
            self._send_to_group(page, target, text)

    # ── Playwright (only on the sender thread) ───────────────────

    def _ensure_page(self):
        if self._page is not None:
            return self._page
        from playwright.sync_api import sync_playwright
        self._pw = sync_playwright().start()
        self._context = launch_context(self._pw, self.profile_dir, self.headless)
        self._page = self._context.pages[0] if self._context.pages else self._context.new_page()
        self._page.goto(WEB_URL, wait_until="domcontentloaded")
        if not self._wait_any(self._page, "logged_in", 60_000):
            raise RuntimeError("WhatsApp Web is not logged in — run: python -m nexus.integrations.whatsapp_alerts login")
        return self._page

    @staticmethod
    def _wait_any(page, key: str, timeout_ms: int):
        deadline = time.time() + timeout_ms / 1000
        while time.time() < deadline:
            for selector in SELECTORS[key]:
                loc = page.locator(selector).first
                try:
                    if loc.count() and loc.is_visible():
                        return loc
                except Exception:
                    pass
            time.sleep(0.5)
        return None

    def _send_to_number(self, page, number: str, text: str) -> None:
        page.goto(f"{WEB_URL}send?phone={_digits(number)}&text={quote(text)}", wait_until="domcontentloaded")
        compose = self._wait_any(page, "compose", 45_000)
        if compose is None:
            try:
                page.keyboard.press("Escape")
                page.goto(WEB_URL, wait_until="domcontentloaded")
            except Exception:
                pass
            raise RuntimeError(f"Chat for {number} did not open (number not on WhatsApp, or missing country code?)")
        time.sleep(1.0)
        compose.click()
        page.keyboard.press("Enter")
        time.sleep(2.0)

    def _send_to_group(self, page, group: str, text: str) -> None:
        page.keyboard.press("Escape")
        if self._wait_any(page, "logged_in", 10_000) is None or self._wait_any(page, "search", 3_000) is None:
            page.goto(WEB_URL, wait_until="domcontentloaded")
            if self._wait_any(page, "logged_in", 60_000) is None:
                if self._wait_any(page, "qr", 2_000) is not None:
                    raise RuntimeError("WhatsApp Web is logged out: run `python -m nexus.integrations.whatsapp_alerts login`")
        search = self._wait_any(page, "search", 15_000)
        if search is None:
            raise RuntimeError("WhatsApp Web search box not found")
        search.click()
        page.keyboard.press("Control+A")
        page.keyboard.insert_text(group)
        chat = page.locator(f"span[title={_css_string(group)}]").first
        chat.wait_for(state="visible", timeout=15_000)
        chat.click()
        compose = self._wait_any(page, "compose", 15_000)
        if compose is None:
            raise RuntimeError(f"Group {group!r} did not open")
        compose.click()
        for i, line in enumerate(text.split("\n")):
            if i:
                page.keyboard.press("Shift+Enter")
            if line:
                page.keyboard.insert_text(line)
        page.keyboard.press("Enter")
        time.sleep(2.0)

    def _close_browser(self) -> None:
        for closer in (getattr(self._context, "close", None), getattr(self._pw, "stop", None)):
            try:
                if closer:
                    closer()
            except Exception:
                pass
        self._page = self._context = self._pw = None


def _css_string(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


# ── CLI ──────────────────────────────────────────────────────────────────────

def _login(headless: bool) -> int:
    from playwright.sync_api import sync_playwright
    from nexus.core.secrets import load_env
    load_env()
    notifier = WhatsAppAlertNotifier(headless=headless)
    notifier.profile_dir.mkdir(parents=True, exist_ok=True)
    qr_path = PROJECT_ROOT / ".autocrat" / "whatsapp-qr.png"
    with sync_playwright() as pw:
        ctx = launch_context(pw, notifier.profile_dir, headless)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(WEB_URL, wait_until="domcontentloaded")
        for _ in range(20):   # WhatsApp only keeps the session after the browser closes if this is ticked
            box = page.locator("input[type='checkbox']").first
            try:
                if box.count():
                    if not box.is_checked():
                        box.check(force=True)
                    print("☑ 'Stay logged in on this browser' is ticked")
                    break
            except Exception:
                pass
            time.sleep(1)
        print("Scan the QR with the SENDER phone: WhatsApp → Linked devices → Link a device.")
        deadline = time.time() + 300
        while time.time() < deadline:
            if notifier._wait_any(page, "logged_in", 2_000):
                print("✅ Logged in — session saved to", notifier.profile_dir)
                time.sleep(25)  # let WhatsApp finish syncing chats before closing
                ctx.close()
                return 0
            if headless:
                qr = notifier._wait_any(page, "qr", 1_000)
                if qr is not None:
                    qr.screenshot(path=str(qr_path))
                    print(f"QR refreshed → {qr_path} (open it and scan; it rotates every ~20s)")
            time.sleep(3)
        ctx.close()
        print("Timed out waiting for login.")
        return 1


def _test() -> int:
    from nexus.core.secrets import load_env
    load_env()
    notifier = WhatsAppAlertNotifier(headless=env_bool("WHATSAPP_ALERT_HEADLESS", False))
    if not notifier.configured:
        print("Set WHATSAPP_ALERT_NUMBERS and/or WHATSAPP_ALERT_GROUPS in .env first.")
        return 1
    message = "🤖 Autocrat alert test ✅\nIf you can read this, WhatsApp alerts work."
    failures = 0
    for kind, target in notifier.recipients():
        try:
            notifier.send(kind, target, message)
            print(f"sent → {kind} {target}")
        except Exception as e:
            failures += 1
            print(f"FAILED → {kind} {target}: {e}")
    notifier._close_browser()
    return 1 if failures else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Autocrat WhatsApp Web alerts")
    sub = parser.add_subparsers(dest="cmd", required=True)
    login = sub.add_parser("login")
    login.add_argument("--headless", action="store_true", help="for servers: save the QR as a PNG")
    sub.add_parser("test")
    args = parser.parse_args()
    raise SystemExit(_login(args.headless) if args.cmd == "login" else _test())
