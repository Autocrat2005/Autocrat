"""
Autocrat — Personality Engine
Gives the assistant a JARVIS-like voice. Wraps raw results in natural,
conversational narration so every interaction feels like talking to an AI
assistant, not reading JSON output.
"""

import random
import psutil
import platform
from datetime import datetime
from typing import Any, Dict, List, Optional


# ── Greeting phrases ─────────────────────────────────────────────────
_GREETINGS_MORNING = [
    "Good morning. Systems are online.",
    "Morning. All systems nominal.",
    "Good morning. I've run a quick diagnostic — everything looks clean.",
]

_GREETINGS_AFTERNOON = [
    "Good afternoon. Standing by.",
    "Afternoon. All plugins loaded and ready.",
    "Good afternoon. What are we working on?",
]

_GREETINGS_EVENING = [
    "Good evening. Systems are online.",
    "Evening. Ready when you are.",
    "Good evening. All subsystems operational.",
]

_GREETINGS_NIGHT = [
    "Burning the midnight oil? I'm here.",
    "Late session. Systems are standing by.",
    "Working late — I'll keep things running smooth.",
]

# ── Action acknowledgements ──────────────────────────────────────────
_ACK_SUCCESS = [
    "Done.",
    "Taken care of.",
    "Executed successfully.",
    "All set.",
    "Handled.",
]

_ACK_MULTI = [
    "All {n} actions completed.",
    "{n} tasks executed in parallel — all successful.",
    "Handled {n} steps simultaneously.",
]

_ACK_FAIL = [
    "That didn't work.",
    "Something went wrong.",
    "I couldn't complete that.",
    "That action failed.",
]

# ── Narration templates for common actions ───────────────────────────
_NARRATION = {
    "kill": "Terminated {target}.",
    "launch": "Opening {target}.",
    "open": "Launching {target} now.",
    "screenshot": "Screenshot captured.",
    "mute": "Volume muted.",
    "unmute": "Volume restored.",
    "volume_up": "Volume increased.",
    "volume_down": "Volume decreased.",
    "brightness_up": "Brightness turned up.",
    "brightness_down": "Brightness lowered.",
    "minimize": "Window minimized.",
    "maximize": "Window maximized.",
    "minimize_all": "All windows minimized.",
    "focus": "Brought {target} to focus.",
    "create_file": "File created.",
    "write_file": "File updated.",
    "delete": "Deleted.",
    "copy": "Copied to clipboard.",
    "paste": "Pasted from clipboard.",
    "shutdown": "Initiating shutdown sequence.",
    "restart": "Restarting the system.",
    "sleep": "Putting the system to sleep.",
    "lock": "Workstation locked.",
    "health_check": "Here's your system health report.",
    "why_slow": "I've run a diagnostic. Here's what I found.",
    "daily_summary": "Here's your daily briefing.",
}

# ── Suggestion phrases ───────────────────────────────────────────────
_SUGGEST_PREFIX = [
    "You might also want to",
    "Next, you could",
    "Related action:",
    "Quick follow-up:",
]


class Personality:
    """JARVIS-style voice layer for Autocrat."""

    def __init__(self, name: str = "Autocrat"):
        self.name = name
        self._session_start = datetime.now()

    # ── Startup briefing ─────────────────────────────────────────
    def greeting(self, plugins_count: int, commands_count: int,
                 brain_ready: bool = False) -> str:
        """Generate a JARVIS-style startup greeting with system status."""
        hour = datetime.now().hour
        if 5 <= hour < 12:
            greet = random.choice(_GREETINGS_MORNING)
        elif 12 <= hour < 17:
            greet = random.choice(_GREETINGS_AFTERNOON)
        elif 17 <= hour < 22:
            greet = random.choice(_GREETINGS_EVENING)
        else:
            greet = random.choice(_GREETINGS_NIGHT)

        # Quick system snapshot
        cpu = psutil.cpu_percent(interval=0.3)
        mem = psutil.virtual_memory()
        bat = psutil.sensors_battery()

        lines = [greet, ""]
        lines.append(f"  Plugins loaded  : {plugins_count}")
        lines.append(f"  Commands ready  : {commands_count}")
        lines.append(f"  AI Brain        : {'online' if brain_ready else 'warming up...'}")
        lines.append(f"  CPU             : {cpu:.0f}%")
        lines.append(f"  Memory          : {mem.percent:.0f}% ({mem.used // (1024**3)}/{mem.total // (1024**3)} GB)")
        if bat and bat.percent is not None:
            plug = "charging" if bat.power_plugged else "on battery"
            lines.append(f"  Battery         : {bat.percent:.0f}% ({plug})")
        lines.append("")
        lines.append("How can I help?")

        return "\n".join(lines)

    # ── Narrate an action result ─────────────────────────────────
    def narrate(self, action: str, result: Dict[str, Any],
                target: str = "", duration_ms: float = 0) -> str:
        """Turn a raw action result into a JARVIS-style spoken response."""
        success = result.get("success", False)

        if not success:
            msg = random.choice(_ACK_FAIL)
            error = result.get("error", "")
            if error:
                msg += f" {error}"
            hint = result.get("hint")
            if hint:
                msg += f"\n  💡 {hint}"
            return msg

        # Check for known narration template
        action_key = action.split(".")[-1] if "." in action else action
        template = _NARRATION.get(action_key)
        if template and target:
            narration = template.format(target=target)
        elif template:
            narration = template
        else:
            narration = random.choice(_ACK_SUCCESS)

        # Append the actual data if it's meaningful
        data = result.get("result")
        if data and isinstance(data, str) and len(data) > 0:
            # If narration is just "Done." and there's real content, show content
            if narration in _ACK_SUCCESS:
                narration = data
            elif data != narration:
                narration += f"\n{data}"
        elif data and isinstance(data, dict):
            # Don't dump raw dicts — the formatter handles that
            pass

        if duration_ms and duration_ms > 500:
            narration += f"  ({duration_ms:.0f}ms)"

        return narration

    def narrate_multi(self, results: List[Dict], duration_ms: float = 0) -> str:
        """Narrate a multi-step result."""
        n = len(results)
        successes = sum(1 for r in results if r.get("success", False))

        if successes == n:
            msg = random.choice(_ACK_MULTI).format(n=n)
        elif successes == 0:
            msg = f"All {n} actions failed."
        else:
            msg = f"{successes} of {n} actions succeeded."

        if duration_ms:
            msg += f"  ({duration_ms:.0f}ms)"

        return msg

    # ── Proactive suggestions ────────────────────────────────────
    def suggest(self, suggestions: List[str]) -> Optional[str]:
        """Format follow-up suggestions in natural language."""
        if not suggestions:
            return None
        prefix = random.choice(_SUGGEST_PREFIX)
        if len(suggestions) == 1:
            return f"  💡 {prefix} '{suggestions[0]}'"
        top = suggestions[:3]
        items = " · ".join(f"'{s}'" for s in top)
        return f"  💡 {prefix}: {items}"

    # ── Session awareness ────────────────────────────────────────
    def session_duration(self) -> str:
        """How long this session has been active."""
        delta = datetime.now() - self._session_start
        minutes = int(delta.total_seconds() // 60)
        if minutes < 1:
            return "just started"
        elif minutes < 60:
            return f"{minutes} min"
        else:
            hours = minutes // 60
            mins = minutes % 60
            return f"{hours}h {mins}m"

    def farewell(self) -> str:
        """JARVIS-style goodbye."""
        dur = self.session_duration()
        farewells = [
            f"Shutting down. Session lasted {dur}. Until next time.",
            f"Powering off after {dur}. See you soon.",
            f"Goodbye. {dur} session logged.",
            f"Systems going offline. {dur} of uptime. Take care.",
        ]
        return random.choice(farewells)
