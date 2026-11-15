"""
Autocrat — Main Bootstrap
Initializes the engine, loads plugins, starts the web server.
Now with: Message Bus, Telegram Bot, Heartbeat, Cross-Channel routing.

Usage:
    python main.py          → Start web server + CLI (profile from AUTOCRAT_PROFILE, default full)
    python main.py --core   → Always-on core only: pipeline + revenue plugins, web server, no CLI,
                              no desktop plugins. Same command on the laptop (dev) and the Oracle VM.
    python main.py --cli    → CLI only (no web server)
    python main.py --web    → Web server only (no CLI)
    python main.py --tunnel → Expose via ngrok tunnel (avoid: exposes the control plane)

Secrets come from .env (see .env.example). RUN_MODE defaults to dry_run.
"""

import sys
import os
import argparse
import threading
import uvicorn

# Ensure project root is in path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from nexus.core.secrets import get_secret, load_env

load_env()  # .env before anything reads configuration

from nexus.core.engine import NexusEngine
from nexus.core.config import Config
from nexus.core.message_bus import MessageBus
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
\033[2m  Your PC, on autopilot.  v2.0.0\033[0m
"""


def main():
    # Windows uses cp1252 for redirected stdout; the banner/emoji would crash a
    # service or log-file run. Force UTF-8 with replacement instead.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    parser = argparse.ArgumentParser(description="Autocrat — AI-Powered Desktop Automation OS")
    parser.add_argument("--cli", action="store_true", help="CLI only mode (no web server)")
    parser.add_argument("--web", action="store_true", help="Web server only (no CLI)")
    parser.add_argument("--tunnel", action="store_true", help="Expose via ngrok tunnel (access from phone anywhere)")
    parser.add_argument("--core", action="store_true",
                        help="Run the always-on core (profile=core, web only, no desktop plugins)")
    parser.add_argument("--profile", choices=["core", "desk", "full"], default=None,
                        help="Plugin profile (default: AUTOCRAT_PROFILE or full)")
    parser.add_argument("--host", default="127.0.0.1",
                        help="Web server host (default: 127.0.0.1; put Caddy/Cloudflare in front for remote access)")
    parser.add_argument("--port", type=int, default=9000, help="Web server port (default: 9000)")
    parser.add_argument("--no-telegram", action="store_true", help="Disable Telegram bot even if configured")
    parser.add_argument("--no-heartbeat", action="store_true", help="Disable heartbeat system")
    args = parser.parse_args()

    print(BANNER)

    profile = "core" if args.core else args.profile
    if profile:
        os.environ["AUTOCRAT_PROFILE"] = profile
    if profile == "core":
        args.web = True
        os.environ.setdefault("AUTOCRAT_BRAIN", "0")  # the core doesn't need the desktop intent model
        args.no_heartbeat = True  # built-in heartbeat tasks probe the Windows desktop
    if args.host not in ("127.0.0.1", "localhost", "::1"):
        log.warning(f"Binding to {args.host} exposes the control plane beyond this machine")

    # ── Initialize engine ──
    engine = NexusEngine()
    engine.load_all_plugins(profile=profile)
    _ = engine.store  # open the WAL store and restore a persisted kill switch
    engine.start_workers()
    log.info(f"Run mode: {engine.runtime.run_mode} · kill switch: "
             f"{'ENGAGED' if engine.runtime.killed else 'off'} · profile: {engine.profile}")

    # Connect scheduler & workflow to engine
    if "task_scheduler" in engine.plugins:
        engine.plugins["task_scheduler"].set_engine(engine)
    if "workflow_engine" in engine.plugins:
        engine.plugins["workflow_engine"].set_engine(engine)

    total_commands = sum(len(p.get_commands()) for p in engine.plugins.values())
    log.info(f"Autocrat ready — {len(engine.plugins)} plugins, {total_commands} commands")
    # \u2500\u2500 JARVIS-style startup greeting \u2500\u2500
    greeting = engine.personality.greeting(
        plugins_count=len(engine.plugins),
        commands_count=total_commands,
        brain_ready=engine.brain._ready,
    )
    print(f"\033[36m{greeting}\033[0m")
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
    tg_token = get_secret("TELEGRAM_BOT_TOKEN")
    if tg_token and not args.no_telegram:
        try:
            from nexus.integrations.telegram_bot import TelegramBot
            telegram_bot = TelegramBot(bot_token=tg_token, message_bus=bus, store=engine.store)
            telegram_bot.start()
            log.info("🤖 Telegram bot started")
        except Exception as e:
            log.warning(f"Telegram bot failed to start: {e}")
    else:
        log.info("📱 Telegram not configured (set TELEGRAM_BOT_TOKEN + TELEGRAM_ALLOWED_USER_IDS in .env)")

    # ── Initialize Heartbeat ──
    heartbeat = None
    hb_cfg = cfg.get("heartbeat", {})
    if hb_cfg.get("enabled", True) and not args.no_heartbeat:
        from nexus.core.heartbeat import Heartbeat  # lazy: the core profile doesn't need it
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
    """Interactive CLI loop — JARVIS-style."""
    from nexus.cli import format_result

    while True:
        try:
            cmd = input("\033[35m❯ \033[0m").strip()
        except (EOFError, KeyboardInterrupt):
            print(f"\n\033[2m{engine.personality.farewell()}\033[0m")
            break

        if not cmd:
            continue

        if cmd.lower() in ("exit", "quit", "q"):
            print(f"\033[2m{engine.personality.farewell()}\033[0m")
            break

        result = engine.execute(cmd)

        if result.get("success"):
            data = result.get("result", result.get("results"))

            # Multi-step result narration
            if result.get("chain_length") and result.get("results"):
                narration = engine.personality.narrate_multi(
                    result["results"],
                    duration_ms=result.get("duration_ms", 0),
                )
                print(f"\033[36m  {narration}\033[0m")
                for i, r in enumerate(result["results"], 1):
                    sub_data = r.get("result", r.get("results"))
                    if sub_data is not None:
                        print(f"\033[2m  [{i}]\033[0m {format_result(sub_data)}")
            elif data is not None:
                # Single result — use personality narration for simple confirmations
                if isinstance(data, str) and len(data) < 200:
                    print(f"\033[36m  {data}\033[0m")
                else:
                    print(format_result(data))

            duration = result.get("duration_ms")
            if duration is not None and duration > 100:
                print(f"\033[2m  ⏱ {duration}ms\033[0m")

            # Proactive suggestions
            suggestions = result.get("suggestions")
            if suggestions:
                hint = engine.personality.suggest(suggestions)
                if hint:
                    print(f"\033[33m{hint}\033[0m")
        else:
            error = result.get("error", "Unknown error")
            print(f"\033[31m  ✗ {error}\033[0m")
            hint = result.get("hint")
            if hint:
                print(f"\033[33m  💡 {hint}\033[0m")
            suggestions = result.get("suggestions")
            if suggestions:
                sugg_text = engine.personality.suggest(suggestions)
                if sugg_text:
                    print(f"\033[33m{sugg_text}\033[0m")

        print()


if __name__ == "__main__":
    main()

