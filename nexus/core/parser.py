"""
NEXUS OS — Command Parser
Parses natural text commands into (plugin, action, args) tuples.
Supports direct commands, aliases, fuzzy matching, and chained commands.
"""

import re
from typing import Any, Dict, List, Optional, Tuple
from dataclasses import dataclass
from nexus.core.logger import get_logger

log = get_logger("parser")


@dataclass
class ParsedCommand:
    """Result of parsing a text command."""
    plugin: str
    action: str
    args: Dict[str, Any]
    raw: str = ""
    confidence: float = 1.0


# ─── Command Patterns ────────────────────────────────────────────────────────
# Maps text patterns to (plugin, action) with named capture groups for args.
COMMAND_PATTERNS = [
    # ── Window Manager ──
    (r"^list\s+windows?$", "window_manager", "list", {}),
    (r"^focus\s+(?:window\s+)?(?P<title>.+)$", "window_manager", "focus", {}),
    (r"^close\s+(?:window\s+)?(?P<title>.+)$", "window_manager", "close", {}),
    (r"^minimize\s+(?:window\s+)?(?P<title>.+)$", "window_manager", "minimize", {}),
    (r"^maximize\s+(?:window\s+)?(?P<title>.+)$", "window_manager", "maximize", {}),
    (r"^move\s+(?:window\s+)?(?P<title>.+?)\s+(?P<x>\d+)\s+(?P<y>\d+)$", "window_manager", "move", {}),
    (r"^resize\s+(?:window\s+)?(?P<title>.+?)\s+(?P<w>\d+)\s+(?P<h>\d+)$", "window_manager", "resize", {}),
    (r"^snap\s+(?:window\s+)?(?P<title>.+?)\s+(?P<direction>left|right|top|bottom)$", "window_manager", "snap", {}),

    # ── Process Controller ──
    (r"^list\s+process(?:es)?$", "process_controller", "list", {}),
    (r"^(?:kill|stop|end)\s+(?:process\s+)?(?P<target>.+)$", "process_controller", "kill", {}),
    (r"^start\s+(?:process\s+)?(?P<path>.+)$", "process_controller", "start", {}),
    (r"^monitor\s+(?:process\s+)?(?P<target>.+)$", "process_controller", "monitor", {}),
    (r"^(?:process\s+)?tree\s+(?P<pid>\d+)$", "process_controller", "tree", {}),

    # ── File Ops ──
    (r"^find\s+(?P<pattern>.+?)\s+in\s+(?P<directory>.+)$", "file_ops", "find", {}),
    (r"^find\s+(?:files?\s+(?:named?\s+)?)?(?P<pattern>[^\s]+\.[^\s]+.*)$", "file_ops", "find", {}),
    (r"^find\s+(?:files?\s+)?(?P<pattern>\S+)\s*$", "file_ops", "find", {}),
    (r"^(?:move|mv)\s+(?P<src>.+?)\s+(?:to\s+)?(?P<dst>.+)$", "file_ops", "move", {}),
    (r"^(?:copy|cp)\s+(?P<src>.+?)\s+(?:to\s+)?(?P<dst>.+)$", "file_ops", "copy", {}),
    (r"^(?:delete|rm|remove)\s+(?P<path>.+)$", "file_ops", "delete", {}),
    (r"^organize\s+(?P<directory>.+)$", "file_ops", "organize", {}),
    (r"^watch\s+(?P<directory>.+)$", "file_ops", "watch", {}),
    (r"^(?:size|du)\s+(?P<path>.+)$", "file_ops", "size", {}),
    (r"^tree\s+(?P<directory>.+)$", "file_ops", "tree", {}),

    # ── Keyboard / Mouse ──
    (r"^type\s+(?P<text>.+)$", "keyboard_mouse", "type", {}),
    (r"^hotkey\s+(?P<combo>.+)$", "keyboard_mouse", "hotkey", {}),
    (r"^press\s+(?P<key>.+)$", "keyboard_mouse", "press", {}),
    (r"^click\s+(?P<x>\d+)\s+(?P<y>\d+)$", "keyboard_mouse", "click", {}),
    (r"^click$", "keyboard_mouse", "click", {}),
    (r"^doubleclick(?:\s+(?P<x>\d+)\s+(?P<y>\d+))?$", "keyboard_mouse", "doubleclick", {}),
    (r"^rightclick(?:\s+(?P<x>\d+)\s+(?P<y>\d+))?$", "keyboard_mouse", "rightclick", {}),
    (r"^(?:move\s+mouse|mousemove)\s+(?P<x>\d+)\s+(?P<y>\d+)$", "keyboard_mouse", "move_mouse", {}),
    (r"^scroll\s+(?P<amount>-?\d+)$", "keyboard_mouse", "scroll", {}),
    (r"^drag\s+(?P<x1>\d+)\s+(?P<y1>\d+)\s+(?:to\s+)?(?P<x2>\d+)\s+(?P<y2>\d+)$", "keyboard_mouse", "drag", {}),

    # ── Screen Intel ──
    (r"^screenshot\s+save\s+(?P<path>.+)$", "screen_intel", "screenshot", {}),
    (r"^screenshot\s+region\s+(?P<x>\d+)\s+(?P<y>\d+)\s+(?P<w>\d+)\s+(?P<h>\d+)$", "screen_intel", "screenshot_region", {}),
    (r"^screenshot$", "screen_intel", "screenshot", {}),
    (r"^ocr\s+region\s+(?P<x>\d+)\s+(?P<y>\d+)\s+(?P<w>\d+)\s+(?P<h>\d+)$", "screen_intel", "ocr_region", {}),
    (r"^ocr$", "screen_intel", "ocr", {}),
    (r"^find\s+on\s+screen\s+(?P<image_path>.+)$", "screen_intel", "find_on_screen", {}),

    # ── App Launcher ──
    (r"^open\s+url\s+(?P<url>.+)$", "app_launcher", "open_url", {}),
    (r"^open\s+folder\s+(?P<path>.+)$", "app_launcher", "open_folder", {}),
    (r"^open\s+(?:last|latest|recent)\s+download$", "intelligence", "open_last_download", {}),
    (r"^open\s+(?:last|latest|recent)\s+screenshot$", "intelligence", "open_last_screenshot", {}),
    (r"^open\s+youtube$", "smart_actions", "open_website", {"query": "youtube.com"}),
    (r"^open\s+(?P<app_name>.+)$", "app_launcher", "open", {}),
    (r"^(?:installed\s+)?apps$", "app_launcher", "installed_apps", {}),

    # ── Clipboard ──
    (r"^clipboard\s+set\s+(?P<text>.+)$", "clipboard", "set", {}),
    (r"^clipboard\s+history$", "clipboard", "history", {}),
    (r"^clipboard\s+clear$", "clipboard", "clear", {}),
    (r"^clipboard$", "clipboard", "get", {}),

    # ── System Info ──
    (r"^sysinfo$", "system_info", "full", {}),
    (r"^cpu$", "system_info", "cpu", {}),
    (r"^memory$", "system_info", "memory", {}),
    (r"^disk$", "system_info", "disk", {}),
    (r"^battery$", "system_info", "battery", {}),
    (r"^network$", "system_info", "network", {}),
    (r"^uptime$", "system_info", "uptime", {}),

    # ── Volume / Display ──
    (r"^volume\s+(?P<level>\d+)$", "volume_display", "set_volume", {}),
    (r"^volume\s+(?P<direction>up|down)$", "volume_display", "volume_adjust", {}),
    (r"^mute$", "volume_display", "mute", {}),
    (r"^unmute$", "volume_display", "unmute", {}),
    (r"^brightness\s+(?P<level>\d+)$", "volume_display", "set_brightness", {}),
    (r"^brightness\s+(?P<direction>up|down)$", "volume_display", "brightness_adjust", {}),

    # ── Shell Executor ──
    (r"^(?:shell|run|exec|cmd)\s+(?P<command>.+)$", "shell_executor", "run", {}),
    (r"^powershell\s+(?P<script>.+)$", "shell_executor", "powershell", {}),

    # ── Task Scheduler ──
    (r"^schedule\s+(?P<command>.+?)\s+every\s+(?P<interval>.+)$", "task_scheduler", "schedule_interval", {}),
    (r"^schedule\s+(?P<command>.+?)\s+at\s+(?P<time>.+)$", "task_scheduler", "schedule_at", {}),
    (r"^list\s+schedules?$", "task_scheduler", "list", {}),
    (r"^cancel\s+schedule\s+(?P<job_id>.+)$", "task_scheduler", "cancel", {}),

    # ── Codeforces ──
    (r"^(?:download\s+)?(?:latest|recent)\s+(?:codeforces\s+)?div\.?\s*2\s+(?:problems?|contest)\s+(?:to\s+)?(?P<workspace>.+)$", "comet_web_agent", "codeforces_latest_div2_download", {}),
    (r"^codeforces?\s+div\.?\s*2\s+(?:download|problems?)\s*(?:to\s+(?P<workspace>.+))?$", "comet_web_agent", "codeforces_latest_div2_download", {}),
    (r"^cf\s+div2\s*(?:download|problems?)?\s*(?:to\s+(?P<workspace>.+))?$", "comet_web_agent", "codeforces_latest_div2_download", {}),

    # ── Core Builder (Meta-Plugin) ──
    (r"^(?:build|create|generate|make|write)\s+(?:a\s+)?plugin\s+(?:that\s+|to\s+|for\s+|which\s+)?(?P<description>.+)$", "core_builder", "build_plugin", {}),
    (r"^(?:build|generate)\s+(?:a\s+)?(?:new\s+)?(?:tool|module)\s+(?:that\s+|to\s+|for\s+|which\s+)?(?P<description>.+)$", "core_builder", "build_plugin", {}),
    (r"^list\s+(?:generated|built|my)\s+plugins?$", "core_builder", "list_generated", {}),
    (r"^(?:unload|remove)\s+(?:generated\s+)?plugin\s+(?P<name>.+)$", "core_builder", "unload_generated", {}),
    (r"^(?:reload|hotswap|hot-swap)\s+(?:generated\s+)?plugin\s+(?P<name>.+)$", "core_builder", "reload_generated", {}),
    (r"^(?:heal|fix|debug|repair)\s+plugin\s+(?P<name>.+)$", "core_builder", "heal_plugin", {}),
    (r"^grant\s+permission\s+(?P<name>\S+)\s+(?P<mode>once|always|block)$", "core_builder", "grant_permission", {}),
    (r"^(?:allow|permit)\s+(?:network\s+)?(?:access\s+)?(?:for\s+)?(?P<name>\S+)\s+(?P<mode>once|always|block)$", "core_builder", "grant_permission", {}),

    # ── Workflow Engine ──
    (r"^workflow\s+create\s+(?P<name>.+)$", "workflow_engine", "create", {}),
    (r"^workflow\s+add\s+(?P<command>.+)$", "workflow_engine", "add_step", {}),
    (r"^workflow\s+save$", "workflow_engine", "save", {}),
    (r"^workflow\s+generate\s+(?P<description>.+)$", "workflow_engine", "generate", {}),
    (r"^workflow\s+run_dynamic\s+(?P<name>\S+)\s+(?P<_rest>.+)$", "workflow_engine", "run_dynamic", {}),
    (r"^workflow\s+run\s+(?P<name>.+)$", "workflow_engine", "run", {}),
    (r"^workflow\s+list$", "workflow_engine", "list", {}),
    (r"^workflow\s+delete\s+(?P<name>.+)$", "workflow_engine", "delete", {}),

    # ── Power Tools ──
    (r"^shutdown(?:\s+(?P<query>\d+))?$", "power_tools", "shutdown", {}),
    (r"^restart$", "power_tools", "restart", {}),
    (r"^sleep$", "power_tools", "sleep", {}),
    (r"^hibernate$", "power_tools", "hibernate", {}),
    (r"^logoff$", "power_tools", "logoff", {}),
    (r"^cancel\s+shutdown$", "power_tools", "cancel_shutdown", {}),
    (r"^note\s+save\s+(?P<query>.+)$", "power_tools", "note_save", {}),
    (r"^note\s+list$", "power_tools", "note_list", {}),
    (r"^(?:notes|my\s+notes)$", "power_tools", "note_list", {}),
    (r"^note\s+read\s+(?P<query>\d+)$", "power_tools", "note_read", {}),
    (r"^note\s+delete\s+(?P<query>\d+)$", "power_tools", "note_delete", {}),
    (r"^timer\s+(?P<query>.+)$", "power_tools", "timer", {}),
    (r"^wifi$", "power_tools", "wifi", {}),
    (r"^ip(?:\s+address)?$", "power_tools", "ip_address", {}),
    (r"^night\s*light$", "power_tools", "night_light", {}),
    (r"^pin\s+window$", "power_tools", "pin_window", {}),
    (r"^empty\s+recycle(?:\s+bin)?$", "power_tools", "empty_recycle", {}),
    (r"^(?:open\s+fav|favorites?)\s*(?P<query>.*)$", "power_tools", "open_fav", {}),

    # ── Smart Action Shortcuts ──
    (r"^(?:snap|tile)\s+left$", "smart_actions", "snap_left", {}),
    (r"^(?:snap|tile)\s+right$", "smart_actions", "snap_right", {}),
    (r"^alt\s*tab$", "smart_actions", "alt_tab", {}),
    (r"^task\s*manager$", "smart_actions", "open_task_manager", {}),
    (r"^undo$", "smart_actions", "undo", {}),
    (r"^redo$", "smart_actions", "redo", {}),
    (r"^select\s+all$", "smart_actions", "select_all", {}),
    (r"^(?:organize|clean)\s+downloads$", "smart_actions", "organize_downloads", {}),
    (r"^(?:organize|clean)\s+desktop$", "smart_actions", "organize_desktop", {}),
    (r"^(?:what\s+time|time)$", "smart_actions", "current_time", {}),
    (r"^(?:what\s+date|date|today)$", "smart_actions", "current_date", {}),
    (r"^(?:new\s+tab)$", "smart_actions", "new_tab", {}),
    (r"^(?:close\s+tab)$", "smart_actions", "close_tab", {}),
    (r"^(?:switch|go|change)\s+(?:to\s+)?(?:the\s+)?(?:chrome\s+|browser\s+)?tab\s+(?:to\s+|with\s+|for\s+|on\s+)?(?P<query>.+)$", "smart_actions", "switch_tab", {}),
    (r"^switch\s+(?:to\s+)?(?P<query>.+?)\s+tab(?:\s+(?:on|in)\s+chrome)?$", "smart_actions", "switch_tab", {}),
    (r"^react\s+plan\s+(?P<task>.+?)(?:\s+start_url\s+(?P<start_url>\S+))?$", "comet_web_agent", "react_plan", {}),
    (r"^comet\s+plan\s+(?P<task>.+?)(?:\s+start_url\s+(?P<start_url>\S+))?$", "comet_web_agent", "react_plan", {}),
    (r"^play\s*pause$", "smart_actions", "media_play_pause", {}),
    (r"^next\s+(?:song|track)$", "smart_actions", "media_next", {}),
    (r"^(?:prev|previous)\s+(?:song|track)$", "smart_actions", "media_prev", {}),
    (r"^lock(?:\s+pc)?$", "smart_actions", "lock_pc", {}),
    (r"^refresh$", "smart_actions", "refresh", {}),
    (r"^save$", "smart_actions", "save", {}),
    (r"^search\s+youtube\s+(?:for\s+)?(?P<query>.+)$", "smart_actions", "web_search_youtube", {}),
    (r"^youtube$", "smart_actions", "open_website", {"query": "youtube.com"}),
    (r"^search\s+(?:for\s+)?file\s+(?P<query>.+)$", "intelligence", "search_files", {}),
    (r"^(?:google|search)\s+(?P<query>.+)$", "smart_actions", "web_search_google", {}),
    (r"^set\s+(?:volume|vol)\s+(?:to\s+)?(?P<level>\d+)$", "volume_display", "set_volume", {}),
    (r"^set\s+brightness\s+(?:to\s+)?(?P<level>\d+)$", "volume_display", "set_brightness", {}),
    (r"^ask\s+(?:gemini|ai)\s+(?P<query>.+)$", "smart_actions", "ask_gemini", {}),
    (r"^(?:email|gmail|open\s+email)$", "smart_actions", "open_email", {}),
    (r"^maps(?:\s+(?P<query>.+))?$", "smart_actions", "open_maps", {}),
    (r"^(?:wikipedia|wiki)\s+(?P<query>.+)$", "smart_actions", "search_wikipedia", {}),
    (r"^amazon\s+(?P<query>.+)$", "smart_actions", "search_amazon", {}),

    # ── Intelligence Plugin ──
    # Gemini / AI Chat
    (r"^(?:open\s+)?gemini(?:\s+chats?)?$", "intelligence", "gemini_chats", {}),
    (r"^(?:last|previous|recent)\s+gemini\s+(?:chat|conversation)$", "intelligence", "gemini_last", {}),
    (r"^(?:open\s+)?(?:my\s+)?(?:previous|last)\s+(?:chat|conversation)\s+(?:with|on)\s+gemini$", "intelligence", "gemini_last", {}),
    (r"^(?:access|continue|resume)\s+(?:my\s+)?gemini\s+(?:chat|conversation)$", "intelligence", "gemini_last", {}),
    (r"^(?:open\s+)?chatgpt$", "intelligence", "open_chatgpt", {}),
    (r"^(?:open\s+)?chat\s*gpt$", "intelligence", "open_chatgpt", {}),

    # Browser History
    (r"^(?:browser|browsing)\s+history(?:\s+(?:for|about)\s+(?P<query>.+))?$", "intelligence", "browser_history", {}),
    (r"^search\s+(?:my\s+)?history\s+(?:for\s+)?(?P<query>.+)$", "intelligence", "browser_history", {}),
    (r"^recent\s+(?:tabs|sites|pages)$", "intelligence", "recent_tabs", {}),
    (r"^(?:recently|last)\s+visited(?:\s+sites)?$", "intelligence", "recent_tabs", {}),
    (r"^open\s+recent\s+(?:site\s+)?(?P<query>.+)$", "intelligence", "open_recent_site", {}),

    # Smart Diagnostics
    (r"^why\s+(?:is\s+)?(?:my\s+)?(?:pc|computer|laptop|system)\s+(?:so\s+)?slow", "intelligence", "why_slow", {}),
    (r"^(?:pc|computer|laptop)\s+(?:is\s+)?(?:slow|lagging|freezing)", "intelligence", "why_slow", {}),
    (r"^(?:diagnose|fix)\s+(?:my\s+)?(?:slow|lag)", "intelligence", "why_slow", {}),
    (r"^health\s*check$", "intelligence", "health_check", {}),
    (r"^system\s+(?:health|check|status|diagnostics?|report)$", "intelligence", "health_check", {}),
    (r"^(?:run\s+)?diagnostics?$", "intelligence", "health_check", {}),
    (r"^disk\s+hogs?(?:\s+(?:in\s+)?(?P<query>.+))?$", "intelligence", "disk_hogs", {}),
    (r"^(?:what(?:'s| is)\s+)?(?:using|eating|taking)\s+(?:my\s+)?(?:disk|storage)\s+space", "intelligence", "disk_hogs", {}),
    (r"^startup\s+(?:programs?|apps?)$", "intelligence", "startup_programs", {}),
    (r"^(?:what\s+runs?\s+on\s+)?startup$", "intelligence", "startup_programs", {}),
    (r"^(?:why\s+is\s+)?(?:boot|startup)\s+slow$", "intelligence", "startup_programs", {}),

    # Math & Conversions
    (r"^(?:calc(?:ulate)?|compute|evaluate|solve)\s+(?P<query>.+)$", "intelligence", "calculate", {}),
    (r"^(?:what(?:'s| is)|how much is)\s+(?P<query>[\d\s+\-*/^().]+.*)$", "intelligence", "calculate", {}),
    (r"^convert\s+(?P<query>.+)$", "intelligence", "convert", {}),
    (r"^(?P<query>[\d.]+\s*\w+\s+(?:to|in)\s+\w+)$", "intelligence", "convert", {}),

    # Recent / Smart Files
    (r"^recent\s+files?(?:\s+(?:in\s+)?(?P<query>.+))?$", "intelligence", "recent_files", {}),
    (r"^(?:find|search|locate)\s+file\s+(?:named?\s+)?(?P<query>.+)$", "intelligence", "search_files", {}),
    (r"^(?:where\s+is)\s+(?:my\s+)?(?:file\s+)?(?P<query>.+)$", "intelligence", "search_files", {}),
    (r"^large\s+files?(?:\s+(?:in\s+)?(?P<query>.+))?$", "intelligence", "large_files", {}),
    (r"^(?:find\s+)?duplicate\s+files?(?:\s+(?:in\s+)?(?P<query>.+))?$", "intelligence", "find_duplicates", {}),
    (r"^(?:open\s+)?(?:last|latest|recent)\s+download$", "intelligence", "open_last_download", {}),
    (r"^(?:open\s+)?(?:last|latest|recent)\s+screenshot$", "intelligence", "open_last_screenshot", {}),

    # Daily Summary
    (r"^(?:daily|morning|quick)\s+summary$", "intelligence", "daily_summary", {}),
    (r"^(?:how\s+is\s+)?my\s+day$", "intelligence", "daily_summary", {}),
    (r"^summarize\s+(?:my\s+)?(?:system|day)$", "intelligence", "daily_summary", {}),

    # Web Shortcuts
    (r"^(?:open\s+)?github$", "intelligence", "open_github", {}),
    (r"^(?:open\s+)?(?:stack\s*overflow)(?:\s+(?P<query>.+))?$", "intelligence", "open_stackoverflow", {}),
    (r"^(?:open\s+)?reddit(?:\s+(?:r/)?(?P<query>\w+))?$", "intelligence", "open_reddit", {}),
    (r"^define\s+(?P<query>\w+)$", "intelligence", "define_word", {}),
    (r"^(?:definition|meaning)\s+(?:of\s+)?(?P<query>\w+)$", "intelligence", "define_word", {}),
    (r"^translate\s+(?P<query>.+)$", "intelligence", "translate_text", {}),

    # ── Meta / Help ──
    (r"^!toggle\s+mock_nav$", "_meta", "toggle_mock_navigation", {}),
    (r"^enable\s+mock\s+navigation\s+mode$", "_meta", "enable_mock_navigation", {}),
    (r"^disable\s+mock\s+navigation\s+mode$", "_meta", "disable_mock_navigation", {}),
    (r"^mock\s+navigation\s+status$", "_meta", "mock_navigation_status", {}),
    (r"^help\s+(?P<plugin>.+)$", "_meta", "help_plugin", {}),
    (r"^help$", "_meta", "help", {}),
    (r"^plugins$", "_meta", "plugins", {}),
    (r"^history$", "_meta", "history", {}),
    (r"^status$", "_meta", "status", {}),
    (r"^suggest(?:ions)?$", "_meta", "suggestions", {}),
]


class CommandParser:
    """Parses text commands into structured ParsedCommand objects."""

    CHAIN_SEPARATORS = [" and then ", " && ", " then ", " >> "]

    def parse(self, text: str) -> List[ParsedCommand]:
        """Parse a text command. Supports chained commands."""
        text = text.strip()
        if not text:
            return []

        # Check for chained commands
        for sep in self.CHAIN_SEPARATORS:
            if sep in text.lower():
                parts = re.split(re.escape(sep), text, flags=re.IGNORECASE)
                commands = []
                for part in parts:
                    parsed = self._parse_single(part.strip())
                    if parsed:
                        commands.append(parsed)
                return commands

        # Single command
        parsed = self._parse_single(text)
        return [parsed] if parsed else []

    def _parse_single(self, text: str) -> Optional[ParsedCommand]:
        """Parse a single command text."""
        text_lower = text.lower().strip()

        for pattern, plugin, action, defaults in COMMAND_PATTERNS:
            match = re.match(pattern, text_lower, re.IGNORECASE)
            if match:
                args = {**defaults, **{k: v for k, v in match.groupdict().items() if v is not None}}
                # Convert numeric args
                for k, v in args.items():
                    if isinstance(v, str) and v.isdigit():
                        args[k] = int(v)
                    elif isinstance(v, str):
                        try:
                            args[k] = float(v)
                        except ValueError:
                            pass

                log.debug(f"Parsed: '{text}' → {plugin}.{action}({args})")
                return ParsedCommand(
                    plugin=plugin,
                    action=action,
                    args=args,
                    raw=text,
                    confidence=1.0,
                )

        # Fuzzy fallback — try keyword matching
        return self._fuzzy_match(text_lower, text)

    def _fuzzy_match(self, text_lower: str, original: str) -> Optional[ParsedCommand]:
        """Attempt fuzzy matching for unrecognized commands."""
        # Simple keyword-based fallback
        keyword_map = {
            "window": ("window_manager", "list"),
            "process": ("process_controller", "list"),
            "file": ("file_ops", "find"),
            "screen": ("screen_intel", "screenshot"),
            "volume": ("volume_display", "set_volume"),
            "bright": ("volume_display", "set_brightness"),
            "clip": ("clipboard", "get"),
            "system": ("system_info", "full"),
            "sysinfo": ("system_info", "full"),
        }

        for keyword, (plugin, action) in keyword_map.items():
            if keyword in text_lower:
                log.debug(f"Fuzzy match: '{original}' → {plugin}.{action} (keyword: {keyword})")
                return ParsedCommand(
                    plugin=plugin,
                    action=action,
                    args={"query": original},
                    raw=original,
                    confidence=0.5,
                )

        log.warning(f"No match for command: '{original}'")
        return None
