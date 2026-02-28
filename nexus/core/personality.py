"""
Autocrat — Personality Engine
JARVIS-class voice layer. Every interaction sounds like talking to a
brilliant, dry-witted AI butler who genuinely enjoys running your PC.
"""

import random
import psutil
import platform
from datetime import datetime
from typing import Any, Dict, List, Optional


# ── JARVIS-style greetings (time-aware) ──────────────────────────────
_GREETINGS_MORNING = [
    "Good morning, sir. All systems are online and running diagnostics.",
    "Morning. I've already checked the vitals — we're looking clean.",
    "Good morning. Core is warm, plugins loaded, ready for your orders.",
    "Rise and shine. I've been up for hours, naturally.",
]

_GREETINGS_AFTERNOON = [
    "Good afternoon, sir. Standing by for instructions.",
    "Afternoon. Everything's nominal — what are we building today?",
    "Good afternoon. I've been keeping things tidy while you were away.",
]

_GREETINGS_EVENING = [
    "Good evening. All subsystems are green. What do you need?",
    "Evening, sir. The system's running smooth — fire away.",
    "Good evening. I took the liberty of running a quick health check. All clear.",
]

_GREETINGS_NIGHT = [
    "Burning the midnight oil? I'll keep the lights on.",
    "Late night session. Don't worry — I don't need sleep.",
    "Working late, sir? I'll keep things running smooth.",
    "Still here. I'll sleep when you do. Which is to say, never.",
]

# ── Action acknowledgements ──────────────────────────────────────────
_ACK_SUCCESS = [
    "Done.",
    "Taken care of, sir.",
    "Handled.",
    "That's done.",
    "Consider it done.",
    "Right away.",
    "As you wish.",
]

_ACK_MULTI = [
    "All {n} tasks completed. Anything else?",
    "{n} steps executed in parallel — all successful.",
    "That's {n} for {n}, sir. Clean sweep.",
    "Handled {n} operations simultaneously. Efficient, if I say so myself.",
]

_ACK_FAIL = [
    "That didn't go as planned.",
    "I ran into a problem with that one.",
    "I'm afraid that didn't work, sir.",
    "Hmm. That one fought back.",
    "We've got an issue.",
]

# ── Narration templates ──────────────────────────────────────────────
_NARRATION = {
    "kill": "I've terminated {target}.",
    "launch": "Launching {target} now.",
    "open": "Opening {target} for you.",
    "screenshot": "Screenshot captured and saved.",
    "mute": "Audio muted.",
    "unmute": "Volume's back.",
    "volume_up": "Turning it up.",
    "volume_down": "Bringing it down.",
    "set_volume": "Volume set to {target}.",
    "brightness_up": "Brightening up.",
    "brightness_down": "Dimming the display.",
    "minimize": "Window minimized.",
    "maximize": "Window maximized.",
    "minimize_all": "All windows minimized. Clean slate.",
    "focus": "Brought {target} to the front.",
    "create_file": "File created.",
    "write_file": "File updated.",
    "delete": "Deleted. It's gone.",
    "copy": "Copied to clipboard.",
    "paste": "Pasted.",
    "shutdown": "Initiating shutdown sequence. It's been a pleasure, sir.",
    "restart": "Restarting now. I'll be right back.",
    "sleep": "Putting the system to sleep. Sweet dreams.",
    "lock": "Workstation locked. No one's getting in.",
    "health_check": "I've run a full diagnostic. Here's the report.",
    "why_slow": "Let me check... Here's what I found.",
    "daily_summary": "Your daily briefing, sir.",
    "web_search_google": "Here's what I found on Google.",
    "web_search_youtube": "Searching YouTube for you.",
    "run": "Command executed.",
}

# ── Idle commentary (when user hasn't typed in a while) ──────────────
_IDLE_REMARKS = [
    "Still here if you need me.",
    "Standing by, sir.",
    "Systems idle. Everything's green.",
    "Quiet shift. I've been optimizing in the background.",
    "Ready when you are.",
]

# ── Witty observations on system state ───────────────────────────────
_HIGH_CPU_REMARKS = [
    "CPU's running hot — {cpu}%. Something's working hard.",
    "Your processor is at {cpu}%. Shall I investigate?",
    "I'm seeing {cpu}% CPU. Want me to find out what's eating cycles?",
]

_LOW_BATTERY_REMARKS = [
    "Fair warning — battery's at {bat}%. Might want to plug in.",
    "Battery's getting low, sir. {bat}% and dropping.",
    "We're at {bat}% battery. I'd recommend finding a charger.",
]

_HIGH_MEMORY_REMARKS = [
    "Memory is at {mem}%. Getting a bit crowded in there.",
    "RAM's sitting at {mem}%. The system's carrying some weight.",
]

# ── Suggestion phrases ───────────────────────────────────────────────
_SUGGEST_PREFIX = [
    "You might also want to",
    "While you're at it, you could",
    "Shall I also",
    "Quick suggestion:",
    "Related:",
]

# ── Thinking/processing phrases ──────────────────────────────────────
_THINKING = [
    "Working on it...",
    "Let me handle that...",
    "Processing...",
    "One moment...",
    "On it...",
]

# ── Error recovery phrases ───────────────────────────────────────────
_RECOVERY = {
    "not loaded": "That plugin isn't loaded. Try 'plugins' to see what's available.",
    "not found": "I couldn't find that. Could you be more specific?",
    "blocked": "That action is blocked by your safety policy. You can modify it in the config.",
    "timeout": "That took too long. The service might be down.",
    "permission": "I don't have permission for that. Try running as administrator.",
}


class Personality:
    """JARVIS-class voice layer for Autocrat."""

    def __init__(self, name: str = "Autocrat"):
        self.name = name
        self._session_start = datetime.now()
        self._command_count = 0
        self._last_remark_time = datetime.now()

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
        lines.append(f"  ┌─ System Status ──────────────────────")
        lines.append(f"  │ Modules    : {plugins_count} plugins, {commands_count} commands")
        lines.append(f"  │ AI Brain   : {'◉ online' if brain_ready else '◎ warming up...'}")
        lines.append(f"  │ CPU        : {cpu:.0f}%")
        lines.append(f"  │ Memory     : {mem.percent:.0f}% ({mem.used // (1024**3)}/{mem.total // (1024**3)} GB)")
        if bat and bat.percent is not None:
            plug = "⚡ charging" if bat.power_plugged else "🔋 on battery"
            lines.append(f"  │ Battery    : {bat.percent:.0f}% ({plug})")
        lines.append(f"  │ Platform   : {platform.system()} {platform.release()}")
        lines.append(f"  └────────────────────────────────────────")

        # Proactive observation
        if cpu > 70:
            lines.append(f"\n  ⚠ CPU's already running warm at {cpu:.0f}%.")
        if mem.percent > 85:
            lines.append(f"\n  ⚠ Memory is tight — {mem.percent:.0f}% used.")
        if bat and bat.percent is not None and bat.percent < 20 and not bat.power_plugged:
            lines.append(f"\n  ⚠ Battery's low at {bat.percent:.0f}%. Plugin recommended.")

        lines.append("")
        lines.append("What can I do for you?")

        return "\n".join(lines)

    # ── Narrate an action result ─────────────────────────────────
    def narrate(self, action: str, result: Dict[str, Any],
                target: str = "", duration_ms: float = 0) -> str:
        """Turn a raw action result into a JARVIS-style spoken response."""
        self._command_count += 1
        success = result.get("success", False)

        if not success:
            msg = self._narrate_failure(result)
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
            if narration in _ACK_SUCCESS:
                narration = data
            elif data != narration:
                narration += f"\n{data}"
        elif data and isinstance(data, dict):
            pass  # Formatter handles structured data

        if duration_ms and duration_ms > 500:
            narration += f"  ({duration_ms:.0f}ms)"

        return narration

    def _narrate_failure(self, result: Dict[str, Any]) -> str:
        """Generate a JARVIS-style error response with smart recovery hints."""
        error = result.get("error", "")
        error_lower = error.lower()

        # Try smart recovery messages
        for key, recovery in _RECOVERY.items():
            if key in error_lower:
                return f"{random.choice(_ACK_FAIL)} {recovery}"

        msg = random.choice(_ACK_FAIL)
        if error:
            msg += f" {error}"
        hint = result.get("hint")
        if hint:
            msg += f"\n  💡 {hint}"
        return msg

    def narrate_multi(self, results: List[Dict], duration_ms: float = 0) -> str:
        """Narrate a multi-step result."""
        self._command_count += 1
        n = len(results)
        successes = sum(1 for r in results if r.get("success", False))

        if successes == n:
            msg = random.choice(_ACK_MULTI).format(n=n)
        elif successes == 0:
            msg = f"All {n} actions failed. That's unusual — want me to investigate?"
        else:
            msg = f"{successes} of {n} succeeded. {n - successes} ran into trouble."

        if duration_ms:
            msg += f"  ({duration_ms:.0f}ms)"

        return msg

    # ── Proactive observations ───────────────────────────────────
    def get_proactive_remark(self) -> Optional[str]:
        """Check system state and return a proactive JARVIS-style remark if warranted.
        Returns None if nothing noteworthy is happening."""
        now = datetime.now()
        # Don't be annoying — max one remark every 5 minutes
        if (now - self._last_remark_time).total_seconds() < 300:
            return None

        try:
            cpu = psutil.cpu_percent(interval=0.1)
            mem = psutil.virtual_memory()
            bat = psutil.sensors_battery()

            if cpu > 85:
                self._last_remark_time = now
                return random.choice(_HIGH_CPU_REMARKS).format(cpu=f"{cpu:.0f}")

            if bat and bat.percent is not None and bat.percent < 15 and not bat.power_plugged:
                self._last_remark_time = now
                return random.choice(_LOW_BATTERY_REMARKS).format(bat=f"{bat.percent:.0f}")

            if mem.percent > 90:
                self._last_remark_time = now
                return random.choice(_HIGH_MEMORY_REMARKS).format(mem=f"{mem.percent:.0f}")
        except Exception:
            pass

        return None

    def get_thinking_phrase(self) -> str:
        """Return a random 'thinking' phrase for slow operations."""
        return random.choice(_THINKING)

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

    # ── Milestone responses ──────────────────────────────────────
    def milestone_remark(self) -> Optional[str]:
        """Return a witty remark at command milestones."""
        n = self._command_count
        milestones = {
            10: "That's 10 commands in. We're warming up.",
            25: "25 commands. You're putting me through my paces.",
            50: "50 commands this session. I'd ask for a raise, but I work for free.",
            100: "100 commands. This has been a productive session, sir.",
            200: "200 commands. At this rate, I might need a coffee. If I could drink one.",
        }
        return milestones.get(n)

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

    def session_summary(self) -> str:
        """Return a session summary like a briefing."""
        dur = self.session_duration()
        return (
            f"Session active for {dur}. "
            f"{self._command_count} commands executed."
        )

    def farewell(self) -> str:
        """JARVIS-style goodbye."""
        dur = self.session_duration()
        n = self._command_count
        farewells = [
            f"Shutting down after {dur}. {n} commands served. Until next time, sir.",
            f"Powering off. {dur} of uptime, {n} tasks handled. It's been a pleasure.",
            f"Going offline. {n} commands in {dur} — not a bad session.",
            f"Systems going dark. {dur} logged. Rest well, sir.",
            f"Signing off. {n} commands in {dur}. I'll keep the lights off.",
        ]
        return random.choice(farewells)
