"""
NEXUS OS — Telegram Bot Integration
Connects NEXUS to Telegram: send commands from your phone, get results back.
Also receives forwarded results from other channels (web, VS Code, heartbeat).

Setup:
  1. Message @BotFather on Telegram → /newbot → get your bot token
  2. Add the token to nexus_config.yaml under telegram.bot_token
  3. Message your bot /start to get your chat_id
  4. Add allowed chat_ids to config for security
"""

import asyncio
import threading
import html
from typing import Optional, Dict, List, Set
from nexus.core.logger import get_logger

log = get_logger("telegram")


class TelegramBot:
    """
    Telegram bot that bridges NEXUS commands.
    Runs in its own thread with its own event loop.
    """

    def __init__(self, bot_token: str, allowed_chat_ids: List[str] = None,
                 message_bus=None):
        self.bot_token = bot_token
        self.allowed_chat_ids: Set[str] = set(str(c) for c in (allowed_chat_ids or []))
        self.bus = message_bus
        self._bot = None
        self._app = None
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._running = False

        # Register with message bus for cross-channel results
        if self.bus:
            self.bus.register_channel("telegram", self._receive_bus_message)

    async def _receive_bus_message(self, msg):
        """Called by message bus when other channels produce results."""
        if not self._bot or not self.allowed_chat_ids:
            return

        result = msg.result or {}
        source = msg.source
        text = msg.text

        # Confirmation prompt from another channel (web/vscode/etc)
        if result.get("requires_confirmation"):
            for chat_id in self.allowed_chat_ids:
                await self._send_confirmation_to_chat(int(chat_id), result, source, text)
            return

        # Format the forwarded result
        output = result.get("result", result.get("error", "No output"))
        if isinstance(output, (dict, list)):
            import json
            output = json.dumps(output, indent=2, default=str)[:3000]

        success = "✅" if result.get("success") else "❌"
        header = f"📨 <b>From {source.upper()}</b>\n"
        body = (
            f"{header}"
            f"<b>Command:</b> <code>{html.escape(str(text))}</code>\n"
            f"{success} <b>Result:</b>\n"
            f"<pre>{html.escape(str(output)[:2000])}</pre>"
        )

        for chat_id in self.allowed_chat_ids:
            try:
                await self._bot.send_message(
                    chat_id=int(chat_id),
                    text=body,
                    parse_mode="HTML",
                )
            except Exception as e:
                log.warning(f"Failed to send to Telegram chat {chat_id}: {e}")

    async def _send_confirmation_to_chat(self, chat_id: int, result: Dict, source: str, text: str):
        """Send approve/reject buttons directly via bot API."""
        try:
            from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        except Exception:
            await self._bot.send_message(
                chat_id=chat_id,
                text=(
                    "⚠️ Approval required for a sensitive command.\n"
                    f"Reply with: {result.get('approve_command', 'approve <id>')}"
                ),
            )
            return

        confirmation_id = result.get("confirmation_id", "")
        reasons = result.get("reasons") or []
        reason_text = "\n".join(f"• {r}" for r in reasons[:4]) if reasons else "• sensitive action detected"

        body = (
            f"⚠️ <b>Approval Required</b> (from {source.upper()})\n"
            f"<b>ID:</b> <code>{html.escape(confirmation_id)}</code>\n"
            f"<b>Command:</b> <code>{html.escape(str(text)[:200])}</code>\n"
            f"<b>Reason(s):</b>\n{html.escape(reason_text)}"
        )
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Approve", callback_data=f"approve:{confirmation_id}"),
                InlineKeyboardButton("❌ Reject", callback_data=f"reject:{confirmation_id}"),
            ]
        ])
        await self._bot.send_message(chat_id=chat_id, text=body, parse_mode="HTML", reply_markup=keyboard)

    def start(self):
        """Start the bot in a background thread."""
        if self._running:
            return

        try:
            from telegram import Update, Bot
            from telegram.ext import (
                Application, CommandHandler, MessageHandler,
                filters, ContextTypes,
            )
        except ImportError:
            log.error("python-telegram-bot not installed. Run: pip install python-telegram-bot")
            return

        self._running = True
        self._thread = threading.Thread(target=self._run_bot, daemon=True)
        self._thread.start()
        log.info("🤖 Telegram bot starting in background...")

    def _run_bot(self):
        """Bot event loop (runs in its own thread)."""
        from telegram import Update, Bot
        from telegram.ext import (
            Application, CommandHandler, MessageHandler, CallbackQueryHandler,
            filters, ContextTypes,
        )

        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)

        async def _main():
            app = Application.builder().token(self.bot_token).build()
            self._app = app
            self._bot = app.bot

            # Command handlers
            app.add_handler(CommandHandler("start", self._cmd_start))
            app.add_handler(CommandHandler("help", self._cmd_help))
            app.add_handler(CommandHandler("status", self._cmd_status))
            app.add_handler(CommandHandler("plugins", self._cmd_plugins))
            app.add_handler(CallbackQueryHandler(self._handle_confirmation_callback, pattern=r"^(approve|reject):"))

            # All other text → NEXUS command
            app.add_handler(MessageHandler(
                filters.TEXT & ~filters.COMMAND, self._handle_message
            ))

            # Start polling
            log.info("📱 Telegram bot polling started")
            await app.initialize()
            await app.start()
            await app.updater.start_polling(drop_pending_updates=True)

            # Keep running
            try:
                while self._running:
                    await asyncio.sleep(1)
            finally:
                await app.updater.stop()
                await app.stop()
                await app.shutdown()

        self._loop.run_until_complete(_main())

    def stop(self):
        """Stop the bot."""
        self._running = False
        if self._loop:
            self._loop.call_soon_threadsafe(self._loop.stop)

    # ── Telegram Handlers ──────────────────────────────────────────

    async def _is_authorized(self, update) -> bool:
        """Check if the user is allowed to use the bot."""
        chat_id = str(update.effective_chat.id)
        if not self.allowed_chat_ids:
            # No whitelist = first user auto-registers
            self.allowed_chat_ids.add(chat_id)
            log.info(f"Auto-registered Telegram chat: {chat_id}")
            return True
        return chat_id in self.allowed_chat_ids

    async def _cmd_start(self, update, context):
        """Handle /start command."""
        chat_id = update.effective_chat.id
        name = update.effective_user.first_name or "there"

        welcome = (
            f"👋 Hey {name}! I'm <b>NEXUS OS</b>.\n\n"
            f"🆔 Your chat ID: <code>{chat_id}</code>\n\n"
            f"Send me any command and I'll execute it on your PC:\n"
            f"• <code>volume 50</code>\n"
            f"• <code>screenshot</code>\n"
            f"• <code>search youtube for lofi</code>\n"
            f"• <code>lock pc</code>\n"
            f"• <code>sysinfo</code>\n"
            f"• Or any natural language!\n\n"
            f"Type /help for all commands."
        )
        await update.message.reply_text(welcome, parse_mode="HTML")

        if not await self._is_authorized(update):
            await update.message.reply_text(
                "⚠️ You're not authorized. Add your chat ID to "
                "nexus_config.yaml → telegram.allowed_chat_ids"
            )

    async def _cmd_help(self, update, context):
        """Handle /help command."""
        if not await self._is_authorized(update):
            return

        help_text = (
            "<b>🧠 NEXUS OS — Telegram Control</b>\n\n"
            "<b>Commands:</b>\n"
            "/status — System status\n"
            "/plugins — List plugins\n"
            "/help — This help\n\n"
            "<b>Just type anything to execute:</b>\n"
            "<code>open chrome</code>\n"
            "<code>set volume to 30</code>\n"
            "<code>search youtube for python tutorial</code>\n"
            "<code>take screenshot</code>\n"
            "<code>what time is it</code>\n"
            "<code>minimize all</code>\n"
            "<code>daily summary</code>\n\n"
            "💡 You'll also receive results from web dashboard & VS Code commands here!"
        )
        await update.message.reply_text(help_text, parse_mode="HTML")

    async def _cmd_status(self, update, context):
        """Handle /status — quick system info."""
        if not await self._is_authorized(update):
            return

        await update.message.reply_text("⏳ Fetching system status...")

        if self.bus and self.bus._engine:
            result = self.bus._engine.execute("sysinfo")
            output = result.get("result", "No data")
            if isinstance(output, (dict, list)):
                import json
                output = json.dumps(output, indent=2, default=str)[:3000]
            await update.message.reply_text(
                f"<pre>{html.escape(str(output)[:3500])}</pre>",
                parse_mode="HTML",
            )
        else:
            await update.message.reply_text("❌ Engine not connected")

    async def _cmd_plugins(self, update, context):
        """Handle /plugins — list loaded plugins."""
        if not await self._is_authorized(update):
            return

        if self.bus and self.bus._engine:
            lines = []
            for p in self.bus._engine.plugins.values():
                cmds = len(p.get_commands())
                lines.append(f"{p.icon} <b>{p.name}</b> — {cmds} commands")
            text = "\n".join(lines)
            await update.message.reply_text(text, parse_mode="HTML")
        else:
            await update.message.reply_text("❌ Engine not connected")

    async def _handle_message(self, update, context):
        """Handle any text message as a NEXUS command."""
        if not await self._is_authorized(update):
            await update.message.reply_text("⛔ Not authorized")
            return

        text = update.message.text.strip()
        if not text:
            return

        chat_id = str(update.effective_chat.id)
        user = update.effective_user.first_name or "Telegram"

        # Send typing indicator
        await update.effective_chat.send_action("typing")

        # Execute via message bus (or direct engine)
        if self.bus:
            msg = self.bus.send(
                text=text,
                source="telegram",
                channel_id=chat_id,
                user=user,
                reply_to="telegram",  # Reply to self
            )
            result = msg.result
        elif self.bus and self.bus._engine:
            result = self.bus._engine.execute(text)
        else:
            result = {"success": False, "error": "Engine not connected"}

        # Format result
        success = result.get("success", False)
        icon = "✅" if success else "❌"

        # Destructive action confirmation flow
        if result.get("requires_confirmation"):
            await self._send_confirmation_prompt(update.message, result)
            return

        output = result.get("result", result.get("error", "No output"))

        # Handle AI conversational responses
        if result.get("ai_source"):
            await update.message.reply_text(str(output))
            return

        # Format structured output
        if isinstance(output, dict):
            import json
            output = json.dumps(output, indent=2, default=str)
        elif isinstance(output, list):
            import json
            output = json.dumps(output, indent=2, default=str)

        output_str = str(output)[:3500]

        # Check for URL in result
        url = result.get("url")
        url_line = f"\n🔗 {url}" if url else ""

        duration = result.get("duration_ms")
        dur_line = f"\n⏱ {duration}ms" if duration else ""

        reply = f"{icon} {output_str}{url_line}{dur_line}"

        await update.message.reply_text(reply)

    async def _send_confirmation_prompt(self, message, result: Dict):
        """Send inline approval buttons for pending destructive actions."""
        try:
            from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        except Exception:
            await message.reply_text(
                "⚠️ Confirmation required, but telegram inline buttons unavailable. "
                f"Reply with: {result.get('approve_command')}"
            )
            return

        confirmation_id = result.get("confirmation_id", "")
        reasons = result.get("reasons") or []
        reason_text = "\n".join(f"• {r}" for r in reasons[:4]) if reasons else "• sensitive action detected"

        text = (
            "⚠️ <b>Approval Required</b>\n"
            f"<b>ID:</b> <code>{confirmation_id}</code>\n"
            f"<b>Reason(s):</b>\n{html.escape(reason_text)}\n\n"
            "Approve execution?"
        )

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Approve", callback_data=f"approve:{confirmation_id}"),
                InlineKeyboardButton("❌ Reject", callback_data=f"reject:{confirmation_id}"),
            ]
        ])

        await message.reply_text(text, parse_mode="HTML", reply_markup=keyboard)

    async def _handle_confirmation_callback(self, update, context):
        """Handle inline approve/reject callbacks."""
        query = update.callback_query
        await query.answer()

        if not await self._is_authorized(update):
            await query.edit_message_text("⛔ Not authorized")
            return

        data = query.data or ""
        try:
            decision, confirmation_id = data.split(":", 1)
        except ValueError:
            await query.edit_message_text("❌ Invalid confirmation payload")
            return

        command = f"approve {confirmation_id}" if decision == "approve" else f"reject {confirmation_id}"

        if not self.bus:
            await query.edit_message_text("❌ Engine not connected")
            return

        msg = self.bus.send(
            text=command,
            source="telegram",
            channel_id=str(update.effective_chat.id),
            user=update.effective_user.first_name or "Telegram",
            reply_to="telegram",
        )
        result = msg.result or {}
        success = result.get("success", False)
        output = result.get("result", result.get("error", "No output"))
        icon = "✅" if success else "❌"
        await query.edit_message_text(f"{icon} {output}")
