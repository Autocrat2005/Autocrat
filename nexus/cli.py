"""
Autocrat — CLI Interface
JARVIS-class terminal interface with personality, narration, and style.
"""

import sys
import os
import time
import threading

# Add parent to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nexus.core.engine import NexusEngine
from nexus.core.logger import get_logger

log = get_logger("cli")

# ── ANSI Helpers ──────────────────────────────────────────────────────
CYAN   = "\033[36m"
GREEN  = "\033[32m"
RED    = "\033[31m"
YELLOW = "\033[33m"
DIM    = "\033[2m"
BOLD   = "\033[1m"
BLUE   = "\033[34m"
MAGENTA = "\033[35m"
RESET  = "\033[0m"

BANNER = f"""
{CYAN}
  █████╗ ██╗   ██╗████████╗ ██████╗  ██████╗██████╗  █████╗ ████████╗
 ██╔══██╗██║   ██║╚══██╔══╝██╔═══██╗██╔════╝██╔══██╗██╔══██╗╚══██╔══╝
 ███████║██║   ██║   ██║   ██║   ██║██║     ██████╔╝███████║   ██║
 ██╔══██║██║   ██║   ██║   ██║   ██║██║     ██╔══██╗██╔══██║   ██║
 ██║  ██║╚██████╔╝   ██║   ╚██████╔╝╚██████╗██║  ██║██║  ██║   ██║
 ╚═╝  ╚═╝ ╚═════╝    ╚═╝    ╚═════╝  ╚═════╝╚═╝  ╚═╝╚═╝  ╚═╝   ╚═╝
{RESET}
 {DIM} Your personal AI assistant.  v2.0.0{RESET}
"""


def format_result(data, indent=0):
    """Format a result dict for terminal display."""
    pad = "  " * indent
    lines = []

    if isinstance(data, str):
        return f"{pad}{data}"

    if isinstance(data, list):
        if not data:
            return f"{pad}{DIM}(empty){RESET}"
        if isinstance(data[0], dict):
            return format_table(data, indent)
        return "\n".join(f"{pad}{item}" for item in data)

    if isinstance(data, dict):
        for key, val in data.items():
            if isinstance(val, dict):
                lines.append(f"{pad}{BOLD}{key}:{RESET}")
                lines.append(format_result(val, indent + 1))
            elif isinstance(val, list):
                lines.append(f"{pad}{BOLD}{key}:{RESET}")
                lines.append(format_result(val, indent + 1))
            else:
                lines.append(f"{pad}{DIM}{key}:{RESET} {val}")
        return "\n".join(lines)

    return f"{pad}{data}"


def format_table(items, indent=0):
    """Format a list of dicts as an aligned table."""
    if not items:
        return "(empty)"

    pad = "  " * indent
    keys = list(items[0].keys())

    widths = {}
    for k in keys:
        widths[k] = max(len(str(k)), max(len(str(item.get(k, ""))) for item in items[:30]))
        widths[k] = min(widths[k], 45)

    header = pad + "  ".join(k.ljust(widths[k]) for k in keys)
    sep = pad + "  ".join("─" * widths[k] for k in keys)

    rows = []
    for item in items[:30]:
        row = pad + "  ".join(str(item.get(k, "")).ljust(widths[k])[:widths[k]] for k in keys)
        rows.append(row)

    result = f"{BOLD}{header}{RESET}\n{DIM}{sep}{RESET}\n" + "\n".join(rows)
    if len(items) > 30:
        result += f"\n{pad}... and {len(items) - 30} more"
    return result


def run_cli():
    """Main CLI loop with JARVIS personality."""
    print(BANNER)

    engine = NexusEngine()
    engine.load_all_plugins()

    # Connect scheduler & workflow to engine
    if "task_scheduler" in engine.plugins:
        engine.plugins["task_scheduler"].set_engine(engine)
    if "workflow_engine" in engine.plugins:
        engine.plugins["workflow_engine"].set_engine(engine)

    # ── JARVIS startup briefing ──
    personality = engine.personality
    total_cmds = sum(len(p.get_commands()) for p in engine.plugins.values())
    greeting = personality.greeting(
        plugins_count=len(engine.plugins),
        commands_count=total_cmds,
        brain_ready=engine.brain._ready,
    )
    print(f"\n{CYAN}{greeting}{RESET}\n")

    # Background thread for proactive remarks
    _proactive_stop = threading.Event()

    def _proactive_loop():
        while not _proactive_stop.is_set():
            _proactive_stop.wait(60)  # check every 60 seconds
            if _proactive_stop.is_set():
                break
            remark = personality.get_proactive_remark()
            if remark:
                print(f"\n  {YELLOW}⚡ {remark}{RESET}\n{MAGENTA}❯ {RESET}", end="", flush=True)

    proactive_thread = threading.Thread(target=_proactive_loop, daemon=True)
    proactive_thread.start()

    while True:
        try:
            cmd = input(f"{MAGENTA}❯ {RESET}").strip()
        except (EOFError, KeyboardInterrupt):
            print(f"\n{DIM}{personality.farewell()}{RESET}")
            _proactive_stop.set()
            break

        if not cmd:
            continue

        if cmd.lower() in ("exit", "quit", "q"):
            print(f"\n{DIM}{personality.farewell()}{RESET}")
            _proactive_stop.set()
            break

        if cmd.lower() in ("session", "session status", "uptime"):
            print(f"\n  {BLUE}{personality.session_summary()}{RESET}\n")
            continue

        # Execute through engine
        result = engine.execute(cmd)

        if result.get("success"):
            # Narrate the result through personality
            action = ""
            target = ""
            if result.get("ai_match"):
                action = result["ai_match"].get("intent", "")
                pct = round(result["ai_match"]["confidence"] * 100)
                print(f"  {DIM}🧠 {action} ({pct}% confidence){RESET}")
            if result.get("ai_source"):
                # Conversational AI response — display with style
                response = result.get("result", "")
                print(f"\n  {CYAN}{response}{RESET}")
            else:
                # Action result — narrate and display data
                data = result.get("result", result.get("results"))
                narration = personality.narrate(
                    action=action,
                    result=result,
                    target=target,
                    duration_ms=result.get("duration_ms", 0),
                )
                # If narration differs from raw data, show narration
                if data is not None and narration != str(data):
                    if isinstance(data, (dict, list)):
                        print(f"  {GREEN}{narration}{RESET}")
                        print(format_result(data))
                    else:
                        print(f"  {GREEN}{narration}{RESET}")
                elif data is not None:
                    print(format_result(data))
                else:
                    print(f"  {GREEN}{narration}{RESET}")

            # Show suggestions
            suggestions = result.get("suggestions")
            if suggestions:
                suggestion_text = personality.suggest(suggestions)
                if suggestion_text:
                    print(f"{DIM}{suggestion_text}{RESET}")

            # Milestone remarks (witty at 10, 25, 50, 100 commands)
            milestone = personality.milestone_remark()
            if milestone:
                print(f"\n  {BLUE}💬 {milestone}{RESET}")

            duration = result.get("duration_ms")
            if duration is not None and duration > 100:
                print(f"{DIM}  ⏱ {duration}ms{RESET}")
        else:
            # Error — narrate with personality
            error_narration = personality.narrate(action="", result=result)
            print(f"  {RED}{error_narration}{RESET}")

            # Show clickable suggestions for failures
            suggestions = result.get("suggestions")
            if suggestions:
                suggestion_text = personality.suggest(suggestions)
                if suggestion_text:
                    print(f"{YELLOW}{suggestion_text}{RESET}")

            if result.get("requires_confirmation"):
                cid = result.get("confirmation_id", "")
                print(f"\n  {YELLOW}⚠ This requires confirmation.{RESET}")
                print(f"  {DIM}Say '{GREEN}approve {cid}{RESET}{DIM}' or '{RED}reject {cid}{RESET}{DIM}'{RESET}")

        print()


if __name__ == "__main__":
    run_cli()
