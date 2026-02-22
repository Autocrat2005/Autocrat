"""
NEXUS OS — CLI Interface
Rich terminal interface with colored output and command history.
"""

import sys
import os

# Add parent to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nexus.core.engine import NexusEngine
from nexus.core.logger import get_logger

log = get_logger("cli")

BANNER = """
\033[36m
 ███╗   ██╗███████╗██╗  ██╗██╗   ██╗███████╗     ██████╗ ███████╗
 ████╗  ██║██╔════╝╚██╗██╔╝██║   ██║██╔════╝    ██╔═══██╗██╔════╝
 ██╔██╗ ██║█████╗   ╚███╔╝ ██║   ██║███████╗    ██║   ██║███████╗
 ██║╚██╗██║██╔══╝   ██╔██╗ ██║   ██║╚════██║    ██║   ██║╚════██║
 ██║ ╚████║███████╗██╔╝ ██╗╚██████╔╝███████║    ╚██████╔╝███████║
 ╚═╝  ╚═══╝╚══════╝╚═╝  ╚═╝ ╚═════╝ ╚══════╝     ╚═════╝ ╚══════╝
\033[0m
 \033[2m Advanced System Automation Framework v1.0.0\033[0m
 \033[2m Type 'help' for commands • 'plugins' to list modules • 'exit' to quit\033[0m
"""


def format_result(data, indent=0):
    """Format a result dict for terminal display."""
    pad = "  " * indent
    lines = []

    if isinstance(data, str):
        return f"{pad}{data}"

    if isinstance(data, list):
        if not data:
            return f"{pad}(empty)"
        if isinstance(data[0], dict):
            return format_table(data, indent)
        return "\n".join(f"{pad}{item}" for item in data)

    if isinstance(data, dict):
        for key, val in data.items():
            if isinstance(val, dict):
                lines.append(f"{pad}\033[1m{key}:\033[0m")
                lines.append(format_result(val, indent + 1))
            elif isinstance(val, list):
                lines.append(f"{pad}\033[1m{key}:\033[0m")
                lines.append(format_result(val, indent + 1))
            else:
                lines.append(f"{pad}\033[2m{key}:\033[0m {val}")
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

    result = f"\033[1m{header}\033[0m\n\033[2m{sep}\033[0m\n" + "\n".join(rows)
    if len(items) > 30:
        result += f"\n{pad}... and {len(items) - 30} more"
    return result


def run_cli():
    """Main CLI loop."""
    print(BANNER)

    engine = NexusEngine()
    engine.load_all_plugins()

    # Connect scheduler & workflow to engine
    if "task_scheduler" in engine.plugins:
        engine.plugins["task_scheduler"].set_engine(engine)
    if "workflow_engine" in engine.plugins:
        engine.plugins["workflow_engine"].set_engine(engine)

    print(f"\n  \033[32m✓\033[0m {len(engine.plugins)} plugins loaded, "
          f"{sum(len(p.get_commands()) for p in engine.plugins.values())} commands available\n")

    while True:
        try:
            cmd = input("\033[35m❯ \033[0m").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n\033[2mGoodbye! 👋\033[0m")
            break

        if not cmd:
            continue

        if cmd.lower() in ("exit", "quit", "q"):
            print("\033[2mShutting down NEXUS OS... 👋\033[0m")
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
    run_cli()
