"""
Autocrat — Telegram Bot Integration (alerts + approvals + commands).

Setup:
  1. @BotFather → /newbot → put the token in .env as TELEGRAM_BOT_TOKEN
  2. Each founder messages the bot /start; it replies with their numeric user ID
  3. Put those IDs in .env: TELEGRAM_ALLOWED_USER_IDS=111111111,222222222
  4. Optional: TELEGRAM_ALLOWED_CHAT_IDS=<private group id> to restrict where
     commands are accepted and where alerts go (otherwise alerts DM each user)

Security:
  * Authorization is by Telegram USER id, never by chat id alone, so anyone
    added to a group can't approve sends. There is no first-come auto-register.
  * Every approve / reject / kill / resume is written to the audit log with
    the approving user's id.
  * Long polling only — no public webhook endpoint.
"""

import asyncio
import html
import json
import threading
import time
from typing import Any, Dict, Iterable, List, Optional, Set

from nexus.core.events import EventBus
from nexus.core.logger import get_logger
from nexus.core.secrets import env_list, get_secret
from nexus.integrations.telegram_alerts import approved_buttons, build_alert, in_quiet_hours

log = get_logger("telegram")


class TelegramBot:
    """Telegram bridge running in its own thread with its own event loop."""

    def __init__(self, bot_token: str, allowed_user_ids: Optional[Iterable[str]] = None,
                 allowed_chat_ids: Optional[Iterable[str]] = None, message_bus=None,
                 events: Optional[EventBus] = None, store=None):
        self.bot_token = bot_token
        self.allowed_user_ids: Set[str] = {str(u) for u in (allowed_user_ids if allowed_user_ids is not None
                                                            else env_list("TELEGRAM_ALLOWED_USER_IDS"))}
        self.allowed_chat_ids: Set[str] = {str(c) for c in (allowed_chat_ids if allowed_chat_ids is not None
                                                            else env_list("TELEGRAM_ALLOWED_CHAT_IDS"))}
        self.quiet_hours = get_secret("TELEGRAM_QUIET_HOURS", "23-8")
        self.bus = message_bus
        self.events = events or EventBus()
        self._store = store
        self._bot = None
        self._app = None
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._running = False

        if not self.allowed_user_ids:
            log.warning("TELEGRAM_ALLOWED_USER_IDS is empty — the bot will refuse every user "
                        "until you add your numeric Telegram user IDs to .env")

        if self.bus:
            self.bus.register_channel("telegram", self._receive_bus_message)
        for pattern in ("lead.*", "control.*", "report.*", "payment.*"):
            self.events.on(pattern, self._on_event)

    # ── helpers ──────────────────────────────────────────────────

    @property
    def engine(self):
        return getattr(self.bus, "_engine", None) if self.bus else None

    @property
    def store(self):
        if self._store is None and self.engine is not None:
            self._store = self.engine.store
        return self._store

    def alert_recipients(self) -> List[int]:
        ids = self.allowed_chat_ids or self.allowed_user_ids
        return [int(i) for i in ids]

    @staticmethod
    def _actor(update) -> str:
        user = update.effective_user
        if not user:
            return "telegram:unknown"
        return f"telegram:{user.id}:{user.first_name or ''}".rstrip(":")

    def is_authorized_ids(self, user_id: Optional[int], chat_id: Optional[int]) -> bool:
        if user_id is None or str(user_id) not in self.allowed_user_ids:
            return False
        if self.allowed_chat_ids and str(chat_id) not in self.allowed_chat_ids:
            return False
        return True

    async def _is_authorized(self, update) -> bool:
        user = update.effective_user
        chat = update.effective_chat
        ok = self.is_authorized_ids(user.id if user else None, chat.id if chat else None)
        if not ok:
            log.warning(f"Rejected Telegram user {user.id if user else '?'} in chat {chat.id if chat else '?'}")
        return ok

    def _execute(self, text: str, actor: str) -> Dict[str, Any]:
        if self.engine is None:
            return {"success": False, "error": "Engine not connected"}
        return self.engine.execute(text, actor=actor)

    def _audit(self, actor: str, action: str, detail: Dict[str, Any]):
        try:
            if self.store is not None:
                self.store.audit(actor, action, detail)
        except Exception as e:
            log.warning(f"Audit write failed: {e}")

    @staticmethod
    def _keyboard(rows: List[List[Dict[str, str]]]):
        if not rows:
            return None
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        return InlineKeyboardMarkup([
            [InlineKeyboardButton(b["text"], url=b["url"]) if "url" in b
             else InlineKeyboardButton(b["text"], callback_data=b["callback_data"]) for b in row]
            for row in rows
        ])

    # ── alerts (called from ANY thread) ──────────────────────────

    def _on_event(self, entry: Dict[str, Any]):
        try:
            alert = build_alert(entry, store=self.store)
        except Exception as e:
            log.warning(f"Alert build failed for {entry.get('event')}: {e}")
            return
        if alert:
            self.notify(alert["text"], priority=alert["priority"], buttons=alert.get("buttons"))

    def notify(self, text: str, priority: str = "orange",
               buttons: Optional[List[List[Dict[str, str]]]] = None) -> bool:
        """Thread-safe: schedule an alert on the bot's own loop."""
        loop = self._loop
        if not (self._bot and loop and loop.is_running()):
            log.debug(f"Telegram not running — alert dropped: {text[:80]}")
            return False
        silent = priority != "red" and in_quiet_hours(self.quiet_hours)
        asyncio.run_coroutine_threadsafe(self._send_to_all(text, buttons, silent), loop)
        return True

    @staticmethod
    def _chunks(text: str, limit: int = 3900) -> List[str]:
        """Telegram caps a message at 4096 chars: split on line breaks."""
        parts, current = [], ""
        for line in text.split("\n"):
            while len(line) > limit:
                parts.append(line[:limit])
                line = line[limit:]
            if len(current) + len(line) + 1 > limit:
                parts.append(current)
                current = ""
            current = f"{current}\n{line}" if current else line
        if current:
            parts.append(current)
        return parts or [""]

    async def _send_to_all(self, text: str, buttons, silent: bool):
        markup = self._keyboard(buttons or [])
        pieces = self._chunks(text)
        for chat_id in self.alert_recipients():
            for i, piece in enumerate(pieces):
                for attempt, wait in enumerate((0, 5, 20, 60, 180)):
                    if wait:
                        await asyncio.sleep(wait)
                    try:
                        await self._bot.send_message(chat_id=chat_id, text=piece, parse_mode="HTML",
                                                     reply_markup=markup if i == len(pieces) - 1 else None,
                                                     disable_notification=silent, disable_web_page_preview=True)
                        break
                    except Exception as e:
                        if "parse entities" in str(e).lower() or "chat not found" in str(e).lower():
                            log.warning(f"Telegram alert to {chat_id} failed (not retrying): {e}")
                            break
                        log.warning(f"Telegram alert to {chat_id} failed (try {attempt + 1}/5): {e}")

    # ── cross-channel results from the message bus ──────────────

    async def _receive_bus_message(self, msg):
        if not self._bot or msg.source == "telegram":
            return
        result = msg.result or {}
        if result.get("requires_confirmation"):
            for chat_id in self.alert_recipients():
                await self._send_confirmation_to_chat(chat_id, result, msg.source, msg.text)
            return
        output = result.get("result", result.get("error", "No output"))
        if isinstance(output, (dict, list)):
            output = json.dumps(output, indent=2, default=str)[:3000]
        icon = "✅" if result.get("success") else "❌"
        body = (f"📨 <b>From {html.escape(msg.source.upper())}</b>\n"
                f"<b>Command:</b> <code>{html.escape(str(msg.text))}</code>\n"
                f"{icon} <b>Result:</b>\n<pre>{html.escape(str(output)[:2000])}</pre>")
        for chat_id in self.alert_recipients():
            try:
                await self._bot.send_message(chat_id=chat_id, text=body, parse_mode="HTML")
            except Exception as e:
                log.warning(f"Failed to send to Telegram chat {chat_id}: {e}")

    def _confirmation_markup(self, confirmation_id: str):
        return self._keyboard([[
            {"text": "✅ Approve", "callback_data": f"approve:{confirmation_id}"},
            {"text": "❌ Reject", "callback_data": f"reject:{confirmation_id}"},
        ]])

    def _confirmation_text(self, result: Dict, source: str = "", text: str = "") -> str:
        reasons = result.get("reasons") or ["sensitive action detected"]
        reason_text = "\n".join(f"• {r}" for r in reasons[:4])
        origin = f" (from {html.escape(source.upper())})" if source else ""
        command = f"<b>Command:</b> <code>{html.escape(str(text)[:200])}</code>\n" if text else ""
        return (f"⚠️ <b>Approval Required</b>{origin}\n"
                f"<b>ID:</b> <code>{html.escape(result.get('confirmation_id', ''))}</code>\n"
                f"{command}<b>Reason(s):</b>\n{html.escape(reason_text)}")

    async def _send_confirmation_to_chat(self, chat_id: int, result: Dict, source: str, text: str):
        await self._bot.send_message(chat_id=chat_id, text=self._confirmation_text(result, source, text),
                                     parse_mode="HTML",
                                     reply_markup=self._confirmation_markup(result.get("confirmation_id", "")))

    # ── lifecycle ────────────────────────────────────────────────

    def start(self):
        if self._running:
            return
        try:
            import telegram  # noqa: F401
        except ImportError:
            log.error("python-telegram-bot not installed. Run: pip install python-telegram-bot")
            return
        self._running = True
        self._thread = threading.Thread(target=self._run_bot, daemon=True, name="telegram-bot")
        self._thread.start()
        log.info("🤖 Telegram bot starting in background...")

    def _run_bot(self):
        from telegram.ext import Application, CallbackQueryHandler, CommandHandler, MessageHandler, filters

        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)

        async def _main():
            app = Application.builder().token(self.bot_token).build()
            self._app = app
            self._bot = app.bot
            app.add_handler(CommandHandler("start", self._cmd_start))
            app.add_handler(CommandHandler("help", self._cmd_help))
            app.add_handler(CommandHandler("status", self._cmd_status))
            app.add_handler(CommandHandler("plugins", self._cmd_plugins))
            app.add_handler(CommandHandler("kill", self._cmd_kill))
            app.add_handler(CommandHandler("resume", self._cmd_resume))
            app.add_handler(CommandHandler("mode", self._cmd_mode))
            app.add_handler(CommandHandler("id", self._cmd_id))
            app.add_handler(CommandHandler("lead", self._cmd_lead))
            app.add_handler(CommandHandler("wa", self._cmd_wa))
            app.add_handler(CommandHandler("calls", self._cmd_calls))
            app.add_handler(CommandHandler("deals", self._cmd_deals))
            # Forward any message (Discord copy, Reddit link, group chat) to the bot = capture it as a lead
            app.add_handler(MessageHandler(filters.FORWARDED & ~filters.COMMAND, self._handle_forward))
            app.add_handler(CallbackQueryHandler(self._handle_callback,
                                                 pattern=r"^(approve|reject|oa|os|ws|dnc|pay|mo|cl|pt|rs|rx):"))
            app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self._handle_message))
            await app.initialize()
            await app.start()
            await app.updater.start_polling(drop_pending_updates=True)
            log.info("📱 Telegram bot polling started")
            try:
                while self._running:
                    await asyncio.sleep(1)
            finally:
                await app.updater.stop()
                await app.stop()
                await app.shutdown()

        # A slow or dropped connection at start-up used to kill the bot for the whole session (no alerts, no
        # commands) while email kept going. Keep retrying until it connects: 10s, 30s, 60s, then every 2 minutes.
        waits = [10, 30, 60]
        attempt = 0
        while self._running:
            try:
                self._loop.run_until_complete(_main())
                return
            except Exception as e:
                wait = waits[attempt] if attempt < len(waits) else 120
                attempt += 1
                self._app = self._bot = None
                log.warning(f"Telegram bot couldn't connect ({type(e).__name__}: {str(e)[:120]}); "
                            f"retrying in {wait}s (attempt {attempt})")
                time.sleep(wait)

    def stop(self):
        self._running = False

    # ── command handlers ─────────────────────────────────────────

    async def _cmd_start(self, update, context):
        user = update.effective_user
        if not await self._is_authorized(update):
            await update.message.reply_text(
                f"⛔ Not authorized.\nYour Telegram user ID is {user.id if user else '?'} — "
                f"add it to TELEGRAM_ALLOWED_USER_IDS in .env on the Autocrat machine.")
            return
        await update.message.reply_text(
            f"👋 Hey {html.escape(user.first_name or 'there')} — Autocrat is listening.\n"
            f"Alerts and approvals will arrive here. /help for commands.", parse_mode="HTML")

    def _capture_lead(self, text: str, actor: str) -> Dict[str, Any]:
        engine = self.engine
        plugin = engine.plugins.get("triage_brain") if engine is not None else None
        if plugin is None:
            return {"success": False, "error": "triage_brain plugin isn't loaded (check AUTOCRAT_PLUGIN_PATHS)"}
        from nexus.core.context import acting_as
        with acting_as(actor):
            return plugin.execute("lead_capture", {"text": text})

    async def _cmd_lead(self, update, context):
        """/lead <text or link>, or reply /lead to a forwarded message."""
        if not await self._is_authorized(update):
            return
        text = (update.message.text or "").partition(" ")[2].strip()
        replied = update.message.reply_to_message
        if not text and replied:
            text = (replied.text or replied.caption or "").strip()
        if not text:
            await update.message.reply_text("Paste the post or link after /lead, or forward the message to me.")
            return
        await update.effective_chat.send_action("typing")
        result = await asyncio.get_running_loop().run_in_executor(None, self._capture_lead, text, self._actor(update))
        await self._reply_result(update.message, result)

    async def _handle_forward(self, update, context):
        if not await self._is_authorized(update):
            return
        text = (update.message.text or update.message.caption or "").strip()
        if not text:
            await update.message.reply_text("That forwarded message has no text I can read.")
            return
        await update.effective_chat.send_action("typing")
        result = await asyncio.get_running_loop().run_in_executor(None, self._capture_lead, text, self._actor(update))
        await self._reply_result(update.message, result)

    async def _cmd_wa(self, update, context):
        """/wa <message with {name}> + a list of numbers → one tap-to-send wa.me button each. No LLM."""
        if not await self._is_authorized(update):
            return
        text = (update.message.text or "").split(None, 1)[1] if len((update.message.text or "").split(None, 1)) > 1 else ""
        if not text.strip() and not update.message.reply_to_message:
            await self._send_ready_whatsapp(update)      # plain /wa: the drafts Autocrat already wrote
            return
        replied = update.message.reply_to_message
        if replied and (replied.text or replied.caption):
            text = (text + "\n" + (replied.text or replied.caption)).strip()
        from nexus.core.lead_state import contact_key
        from nexus.core.secrets import get_secret
        from nexus.integrations.wa_links import build
        store = self.store
        blocked = (lambda n: store.is_suppressed(contact_key("whatsapp", "+" + n))) if store else None
        out = build(text, get_secret("WA_LINKS_DEFAULT_CC", "971") or "971", blocked)
        if not out["links"]:
            await update.message.reply_text(
                "Send it like this (numbers on their own lines, {name} optional):\n\n"
                "/wa Hi {name}, quick one about your listings...\nOmar Khan, 050 123 4567\n+971 55 765 4321")
            return
        if store:
            store.audit(self._actor(update), "wa_links", {"count": len(out["links"])})
        preview = out["message"][:300] or "(no message, just opens the chat)"
        head = f"📲 {len(out['links'])} chat(s) ready. Tap, check, send.\nMessage: {preview}"
        if out["skipped"]:
            head += "\nSkipped: " + ", ".join(f"{c['name'] or c['number']} ({why})" for c, why in out["skipped"][:10])
        for start in range(0, len(out["links"]), 40):
            rows = [[{"text": f"{start + i + 1}. {(l['name'] or '+' + l['number'])[:28]}", "url": l["url"]}]
                    for i, l in enumerate(out["links"][start:start + 40])]
            await update.message.reply_text(head if start == 0 else f"📲 continued ({start + 1}+)",
                                            reply_markup=self._keyboard(rows))

    def ready_whatsapp(self, limit: int = 40) -> List[Dict[str, Any]]:
        """Staged/approved WhatsApp drafts with a phone, oldest first, as wa.me links."""
        from urllib.parse import quote
        from nexus.integrations.email_channel import split_subject
        from nexus.integrations.wa_links import normalize
        store = self.store
        if store is None:
            return []
        out = []
        for status in ("approved", "staged"):
            for o in store.list_outreach(status, channel="whatsapp", limit=200):
                lead = store.get_lead(o["lead_id"]) or {}
                # The draft's own number first (a second touch goes to a mobile while the lead's contact is an email).
                ckey = o.get("contact_key") or ""
                number = normalize(ckey[len("phone:"):]) if ckey.startswith("phone:") else ""
                number = number or normalize(lead.get("contact") or "")
                if not number:
                    continue
                _, body = split_subject(o["draft"])
                out.append({"id": o["id"], "who": lead.get("company") or lead.get("name") or "+" + number,
                            "name": lead.get("name") or "", "number": number, "created": o["created_at"],
                            "from": (lead.get("data") or {}).get("send_from") or "",
                            "url": f"https://wa.me/{number}?text={quote(body)}"})
        out.sort(key=lambda x: x["created"])
        return out[:limit]

    async def _send_ready_whatsapp(self, update) -> None:
        ready = await asyncio.get_running_loop().run_in_executor(None, self.ready_whatsapp)
        if not ready:
            await update.message.reply_text("No WhatsApp drafts waiting. New ones arrive with each broker session "
                                            "(Dubai 10:00-19:00).")
            return
        await update.message.reply_text(f"📲 {len(ready)} WhatsApp message(s) ready. Tap a name: WhatsApp opens with "
                                         "the message written. Send it, then tap ✔ so it's never sent twice.")
        for start in range(0, len(ready), 20):
            rows = [[{"text": f"{start + i + 1}. {r['who'][:26]}", "url": r["url"]},
                     {"text": "✔ Sent", "callback_data": f"ws:{r['id']}"}]
                    for i, r in enumerate(ready[start:start + 20])]
            await update.message.reply_text(f"Batch {start // 20 + 1}", reply_markup=self._keyboard(rows))

    def money_action(self, kind: str, lead_id: str, code: str, actor: str) -> Dict[str, Any]:
        """Buttons that move money or calls: deposit request, call debrief, call outcome, pre-call note."""
        from nexus.integrations import calls, deals, precall
        from nexus.integrations.email_channel import active_worker
        worker, store = active_worker(), self.store
        if kind == "pay":
            return {"note": deals.request_payment(store, worker, lead_id, actor=actor)["note"]}
        if kind == "mo":
            return deals.apply_meeting_outcome(store, worker, lead_id, code, actor=actor)
        if kind == "cl":
            return {"note": calls.log_call(store, worker, lead_id, code, actor=actor)}
        if kind in ("rs", "rx"):
            if worker is None:
                return {"note": "Email channel isn't running."}
            return {"note": worker.send_reply_draft(lead_id, actor) if kind == "rs" else worker.skip_reply_draft(lead_id, actor)}
        return {"note": precall.send_teaser(store, worker, lead_id, actor=actor)}

    async def _cmd_calls(self, update, context):
        """/calls: who to ring right now (their business hours), what to say, one tap to log the result."""
        if not await self._is_authorized(update):
            return
        from nexus.integrations import calls
        sheet = await asyncio.get_running_loop().run_in_executor(None, lambda: calls.call_sheet(self.store))
        if not sheet:
            waiting = await asyncio.get_running_loop().run_in_executor(
                None, lambda: len(calls.call_sheet(self.store, ignore_hours=True, limit=500)))
            await update.message.reply_text(
                "📞 Nobody to ring right now." + (f" {waiting} number(s) are waiting for their business hours "
                                                 "(US Eastern 9am-5pm = 7:30pm-3:30am your time)." if waiting else
                                                 " The sheet fills from lists with phone numbers (and hot replies)."))
            return
        await update.message.reply_text(f"📞 {len(sheet)} to ring now. Tap the number to dial, then tap how it went.")
        for i, item in enumerate(sheet, 1):
            await update.message.reply_text(calls.sheet_card(item, i), parse_mode="HTML",
                                            disable_web_page_preview=True,
                                            reply_markup=self._keyboard(calls.card_buttons(item["lead"]["id"])))

    async def _cmd_deals(self, update, context):
        """/deals: deposits waiting, calls booked, hot leads to strike, money in."""
        if not await self._is_authorized(update):
            return
        from nexus.integrations import deals
        text = await asyncio.get_running_loop().run_in_executor(None, lambda: deals.money_board(self.store))
        await update.message.reply_text(text[:3900], parse_mode="HTML", disable_web_page_preview=True)

    def mark_whatsapp_sent(self, outreach_id: str, actor: str) -> str:
        """You sent it by hand: approve (if needed) and mark sent in one step."""
        store = self.store
        out = store.get_outreach(outreach_id)
        if not out:
            return "Unknown draft."
        if out["status"] == "sent":
            return "Already marked sent."
        if out["status"] == "staged":
            store.approve_outreach(outreach_id, actor=actor)
        lead = store.get_lead(out["lead_id"])
        if lead and lead["state"] == "STAGED":
            store.transition(lead["id"], "APPROVED", actor=actor)
        store.mark_outreach_sent(outreach_id, actor=actor)
        who = (lead or {}).get("company") or (lead or {}).get("name") or "them"
        if lead and "@" in (lead.get("contact") or ""):
            # They also get our emails: a WhatsApp answer must stop those.
            return (f"✔ Sent to {who}. If they answer on WhatsApp, type: advance {lead['id']} to replied "
                    f"(stops the follow-up emails). If they say no: dnc {lead['id']}")
        return f"✔ Sent to {who}."

    async def _cmd_id(self, update, context):
        """Show IDs for setup. Works for anyone; IDs aren't secrets and grant nothing."""
        user = update.effective_user
        chat = update.effective_chat
        authorized = self.is_authorized_ids(user.id if user else None, chat.id if chat else None)
        await update.message.reply_text(
            f"Your user ID: {user.id if user else '?'}\n"
            f"This chat ID: {chat.id if chat else '?'}\n"
            f"Authorized: {'yes' if authorized else 'no'}\n\n"
            "Put user IDs in TELEGRAM_ALLOWED_USER_IDS. For alerts in a group, put the group's "
            "chat ID (starts with -100) in TELEGRAM_ALLOWED_CHAT_IDS.")

    async def _cmd_help(self, update, context):
        if not await self._is_authorized(update):
            return
        await update.message.reply_text(
            "<b>🧠 Autocrat — Telegram deck</b>\n\n"
            "/status — run mode, kill switch, pipeline counts\n"
            "/kill [reason] — stop all outbound + workers now\n"
            "/resume — release the kill switch (asks for approval)\n"
            "/mode dry_run | /mode live — switch run mode (live asks for approval)\n"
            "/plugins — loaded plugins\n"
            "/lead &lt;text or link&gt; — capture a lead you spotted (or just forward the message here)\n"
            "/wa &lt;message, {name} optional&gt; + numbers on new lines — tap-to-send WhatsApp links (you press send)\n"
            "/deals — money board: deposits waiting, calls booked, hot leads to strike\n"
            "/calls — who to ring right now, what to say, one tap to log how it went\n"
            "<code>invoice &lt;lead id&gt; [amount] [USD]</code> — email the deposit request (Wise / bank)\n"
            "<code>email &lt;lead id&gt; name@company.com</code> — save an email you got on a call\n"
            "<code>paid &lt;lead id&gt; 625 USD</code> — log a deposit (add <code>final</code> when fully paid)\n"
            "<code>radar</code> / <code>radar run hn</code> — radar status / run a source now\n\n"
            "Or type any command, e.g. <code>leads staged</code>, <code>pipeline</code>.",
            parse_mode="HTML")

    async def _reply_result(self, message, result: Dict[str, Any]):
        if result.get("requires_confirmation"):
            await message.reply_text(self._confirmation_text(result), parse_mode="HTML",
                                     reply_markup=self._confirmation_markup(result.get("confirmation_id", "")))
            return
        output = result.get("result", result.get("error", "No output"))
        if isinstance(output, (dict, list)):
            output = json.dumps(output, indent=2, default=str)
        icon = "✅" if result.get("success") else "❌"
        url_line = f"\n🔗 {result['url']}" if result.get("url") else ""
        await message.reply_text(f"{icon} {str(output)[:3500]}{url_line}")

    async def _run_in_thread(self, text: str, actor: str) -> Dict[str, Any]:
        return await asyncio.get_running_loop().run_in_executor(None, self._execute, text, actor)

    async def _cmd_status(self, update, context):
        if not await self._is_authorized(update):
            return
        await self._reply_result(update.message, await self._run_in_thread("pipeline", self._actor(update)))

    async def _cmd_plugins(self, update, context):
        if not await self._is_authorized(update):
            return
        if self.engine is None:
            await update.message.reply_text("❌ Engine not connected")
            return
        lines = [f"{p.icon} <b>{html.escape(p.name)}</b> — {len(p.get_commands())} commands"
                 for p in self.engine.plugins.values()]
        await update.message.reply_text("\n".join(lines) or "No plugins", parse_mode="HTML")

    async def _cmd_kill(self, update, context):
        if not await self._is_authorized(update):
            return
        actor = self._actor(update)
        reason = " ".join(context.args) if context.args else "telegram /kill"
        self._audit(actor, "kill_switch", {"reason": reason})
        if self.engine is not None:
            self.engine.runtime.engage_kill_switch(reason=reason, actor=actor)
        await update.message.reply_text("🛑 Kill switch engaged.")

    async def _cmd_resume(self, update, context):
        if not await self._is_authorized(update):
            return
        await self._reply_result(update.message, await self._run_in_thread("resume", self._actor(update)))

    async def _cmd_mode(self, update, context):
        if not await self._is_authorized(update):
            return
        mode = (context.args[0] if context.args else "").lower()
        text = f"run mode {mode}" if mode in ("dry_run", "live") else "run mode"
        await self._reply_result(update.message, await self._run_in_thread(text, self._actor(update)))

    async def _handle_message(self, update, context):
        if not await self._is_authorized(update):
            await update.message.reply_text("⛔ Not authorized")
            return
        text = (update.message.text or "").strip()
        if not text:
            return
        await update.effective_chat.send_action("typing")
        user = update.effective_user
        if self.bus:
            msg = await asyncio.get_running_loop().run_in_executor(
                None, lambda: self.bus.send(text=text, source="telegram",
                                            channel_id=str(update.effective_chat.id),
                                            user=f"{user.id}:{user.first_name or ''}",
                                            reply_to="telegram"))
            result = msg.result
        else:
            result = await self._run_in_thread(text, self._actor(update))
        await self._reply_result(update.message, result)

    async def _handle_callback(self, update, context):
        query = update.callback_query
        await query.answer()
        if not await self._is_authorized(update):
            await query.edit_message_reply_markup(reply_markup=None)
            await query.message.reply_text("⛔ Not authorized")
            return
        actor = self._actor(update)
        kind, _, ident = (query.data or "").partition(":")
        original = query.message.text_html if query.message else ""

        if kind in ("approve", "reject"):
            result = await self._run_in_thread(f"{kind} {ident}", actor)
            icon = "✅" if result.get("success") else "❌"
            output = result.get("result", result.get("error", "No output"))
            await query.edit_message_text(f"{icon} {html.escape(str(output))[:3500]}\n— {html.escape(actor)}",
                                          parse_mode="HTML")
            return

        if kind in ("pay", "mo", "cl", "pt", "rs", "rx"):
            lead_id, _, code = ident.partition(":")
            self._audit(actor, f"button_{kind}", {"lead_id": lead_id, "code": code})
            try:
                res = await asyncio.get_running_loop().run_in_executor(None, self.money_action, kind, lead_id, code,
                                                                       actor)
            except Exception as e:
                res = {"note": f"❌ {e}", "buttons": []}
            try:   # one tap only: the same card can't send twice
                await query.edit_message_reply_markup(reply_markup=None)
            except Exception:
                pass
            await query.message.reply_text(str(res.get("note") or "Done.")[:3900], disable_web_page_preview=True,
                                           reply_markup=self._keyboard(res.get("buttons") or []))
            return

        if kind == "ws":
            self._audit(actor, "whatsapp_sent_by_hand", {"outreach_id": ident})
            try:
                note = await asyncio.get_running_loop().run_in_executor(None, self.mark_whatsapp_sent, ident, actor)
            except Exception as e:
                note = f"Couldn't mark it: {e}"
            await query.message.reply_text(note)
            return

        if kind == "oa":
            self._audit(actor, "outreach_approve", {"outreach_id": ident})
            result = await self._run_in_thread(f"approve outreach {ident}", actor)
            if not result.get("success"):
                await query.message.reply_text(f"❌ {result.get('error')}")
                return
            links = result.get("links") or {}
            await query.edit_message_text(
                f"{original}\n\n✅ Approved by {html.escape(actor)}",
                parse_mode="HTML", disable_web_page_preview=True,
                reply_markup=self._keyboard(approved_buttons(ident, links)))
            return

        if kind == "os":
            self._audit(actor, "outreach_mark_sent", {"outreach_id": ident})
            result = await self._run_in_thread(f"sent {ident}", actor)
            icon = "📤" if result.get("success") else "❌"
            await query.edit_message_text(
                f"{original}\n\n{icon} {html.escape(str(result.get('result') or result.get('error')))}",
                parse_mode="HTML", disable_web_page_preview=True)
            return

        if kind == "dnc":
            self._audit(actor, "lead_dnc", {"lead_id": ident})
            result = await self._run_in_thread(f"dnc {ident}", actor)
            await query.edit_message_text(
                f"{original}\n\n🚫 {html.escape(str(result.get('result') or result.get('error')))}",
                parse_mode="HTML")
