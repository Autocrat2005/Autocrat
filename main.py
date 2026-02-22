"""
Autocrat — Main Bootstrap
Initializes the engine, loads plugins, starts the web server.
Now with: Message Bus, Telegram Bot, Heartbeat, Cross-Channel routing.

Usage:
    python main.py          → Start web server + CLI
    python main.py --cli    → CLI only (no web server)
    python main.py --web    → Web server only (no CLI)
    python main.py --tunnel → Expose via ngrok tunnel
"""

import sys
import os
import argparse
import threading
import uvicorn

# Ensure project root is in path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from nexus.core.engine import NexusEngine
from nexus.core.config import Config
from nexus.core.message_bus import MessageBus
from nexus.core.heartbeat import Heartbeat
from nexus.core.logger import get_logger

log = get_logger("main")

BANNER = """
\033[36m
  █████╗ ██╗   ██╗████████╗ ██████╗  ██████╗██████╗  █████╗ ████████╗
 ██╔══██╗██║   ██║╚══██╔══╝██╔═══██╗██╔════╝██╔══██╗██╔══██╗╚══██╔══╝
 ███████║██║   ██║   ██║   ██║   ██║██║     ██████╔╝███████║   ██║
 ██╔══██║██║   ██║   ██║   ██║   ██║██║     ██╔══██╗██╔══██║   ██║
 ██║  ██║╚██████╔╝   ██║   ╚██████╔╝╚██████╗██║  ██║██║  ██║   ██║
 ╚═╝  ╚═╝ ╚═════╝    ╚═╝    ╚═════╝  ╚═════╝╚═╝  ╚═╝╚═╝  ╚═╝   ╚═╝
\033[0m
\033[2m  AI-Powered Desktop Automation OS v1.0.0\033[0m
"""


def main():
    parser = argparse.ArgumentParser(description="Autocrat — AI-Powered Desktop Automation OS")
    parser.add_argument("--cli", action="store_true", help="CLI only mode (no web server)")
    parser.add_argument("--web", action="store_true", help="Web server only (no CLI)")
    parser.add_argument("--tunnel", action="store_true", help="Expose via ngrok tunnel (access from phone anywhere)")
    parser.add_argument("--host", default="0.0.0.0", help="Web server host (default: 0.0.0.0 — accessible from LAN/phone)")
    parser.add_argument("--port", type=int, default=9000, help="Web server port (default: 9000)")
    parser.add_argument("--no-telegram", action="store_true", help="Disable Telegram bot even if configured")
    parser.add_argument("--no-heartbeat", action="store_true", help="Disable heartbeat system")
    args = parser.parse_args()

    print(BANNER)

    # ── Initialize engine ──
    engine = NexusEngine()
    engine.load_all_plugins()

    # Connect scheduler & workflow to engine
    if "task_scheduler" in engine.plugins:
        engine.plugins["task_scheduler"].set_engine(engine)
    if "workflow_engine" in engine.plugins:
        engine.plugins["workflow_engine"].set_engine(engine)

    total_commands = sum(len(p.get_commands()) for p in engine.plugins.values())
    log.info(f"Autocrat ready — {len(engine.plugins)} plugins, {total_commands} commands")

    # ── Load config ──
    import yaml
    cfg = {}
    config_path = "nexus_config.yaml"
    if os.path.exists(config_path):
        with open(config_path, "r") as f:
            cfg = yaml.safe_load(f) or {}

    # ── Initialize Message Bus ──
    bus = MessageBus()
    bus.set_engine(engine)
    log.info("📡 Message bus initialized")

    # ── Initialize Telegram Bot ──
    telegram_bot = None
    tg_cfg = cfg.get("telegram", {})
    if tg_cfg.get("bot_token") and not args.no_telegram:
        try:
            from nexus.integrations.telegram_bot import TelegramBot
            telegram_bot = TelegramBot(
                bot_token=tg_cfg["bot_token"],
                allowed_chat_ids=tg_cfg.get("allowed_chat_ids", []),
                message_bus=bus,
            )
            telegram_bot.start()
            log.info("🤖 Telegram bot started")
        except Exception as e:
            log.warning(f"Telegram bot failed to start: {e}")
    else:
        log.info("📱 Telegram not configured (add telegram.bot_token to nexus_config.yaml)")

    # ── Initialize Heartbeat ──
    heartbeat = None
    hb_cfg = cfg.get("heartbeat", {})
    if hb_cfg.get("enabled", True) and not args.no_heartbeat:
        heartbeat = Heartbeat(message_bus=bus, engine=engine)
        hb_tasks = hb_cfg.get("tasks", [])
        if hb_tasks:
            heartbeat.load_from_config(hb_tasks)
        heartbeat.start()
        log.info(f"💓 Heartbeat started — {len(heartbeat.tasks)} tasks")

    # Start ngrok tunnel if requested
    tunnel_url = None
    if args.tunnel:
        tunnel_url = start_tunnel(args.port)

    if args.cli:
        # CLI only
        run_cli(engine)
    elif args.web:
        # Web server only
        run_web(engine, args.host, args.port, bus, heartbeat)
    else:
        # Both: web server in background thread, CLI in main thread
        web_thread = threading.Thread(
            target=run_web, args=(engine, args.host, args.port, bus, heartbeat), daemon=True
        )
        web_thread.start()
        print(f"\n  \033[32m🌐 Local:\033[0m  http://localhost:{args.port}")
        if tunnel_url:
            print(f"  \033[32m📱 Phone:\033[0m  {tunnel_url}")
        if telegram_bot:
            print(f"  \033[32m🤖 Telegram:\033[0m  Bot active — send commands from your phone!")
        if heartbeat:
            print(f"  \033[32m💓 Heartbeat:\033[0m  {len(heartbeat.tasks)} tasks scheduled")
        print(f"  \033[32m📡 Bus:\033[0m    Channels: {', '.join(bus.get_stats()['channels']) or 'none yet'}")
        print(f"  \033[32m⌨️  CLI:\033[0m    Type commands below")
        print(f"  \033[2m  {len(engine.plugins)} plugins loaded, {total_commands} commands\033[0m\n")
        run_cli(engine)


def start_tunnel(port):
    """Start ngrok tunnel and return the public URL."""
    try:
        from pyngrok import ngrok
        log.info("Starting ngrok tunnel...")
        tunnel = ngrok.connect(port, "http")
        public_url = tunnel.public_url
        # Force HTTPS
        if public_url.startswith("http://"):
            public_url = public_url.replace("http://", "https://", 1)
        log.info(f"🌍 Tunnel active: {public_url}")
        print(f"\n  \033[35m{'='*55}\033[0m")
        print(f"  \033[35m📱 PHONE ACCESS URL:\033[0m \033[1;36m{public_url}\033[0m")
        print(f"  \033[2m  Open this URL on ANY device, ANY network!\033[0m")
        print(f"  \033[35m{'='*55}\033[0m")
        return public_url
    except Exception as e:
        log.error(f"Tunnel failed: {e}")
        print(f"\n  \033[31m✗ Tunnel failed: {e}\033[0m")
        print(f"  \033[33m💡 Run: ngrok config add-authtoken <YOUR_TOKEN>\033[0m")
        print(f"  \033[33m   Get a free token at: https://dashboard.ngrok.com\033[0m")
        return None


def run_web(engine, host, port, bus=None, heartbeat=None):
    """Start the FastAPI web server."""
    from nexus.web.server import app, set_engine, set_message_bus, set_heartbeat
    set_engine(engine)
    if bus:
        set_message_bus(bus)
    if heartbeat:
        set_heartbeat(heartbeat)

    config = uvicorn.Config(
        app,
        host=host,
        port=port,
        log_level="warning",
        access_log=False,
    )
    server = uvicorn.Server(config)
    server.run()


def run_cli(engine):
    """Interactive CLI loop."""
    from nexus.cli import format_result

    while True:
        try:
            cmd = input("\033[35m❯ \033[0m").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n\033[2mGoodbye! 👋\033[0m")
            break

        if not cmd:
            continue

        if cmd.lower() in ("exit", "quit", "q"):
            print("\033[2mShutting down Autocrat... 👋\033[0m")
            break

        result = engine.execute(cmd)

        if result.get("success"):
            data = result.get("result", result.get("results"))
            if data is not None:
                print(format_result(data))

            duration = result.get("duration_ms")
            if duration is not None:
                print(f"\033[2m  ⏱ {duration}ms\033[0m")
        else:
            error = result.get("error", "Unknown error")
            print(f"\033[31m  ✗ {error}\033[0m")
            hint = result.get("hint")
            if hint:
                print(f"\033[33m  💡 {hint}\033[0m")

        print()


if __name__ == "__main__":
    main()

