"""
Autocrat — AI Brain (ML-Powered)
Intent classification using sentence embeddings + cosine similarity.
Maps natural language → structured intent → plugin action.

The INTENT_CATALOG below serves as *training examples*, NOT an exhaustive list.
The ML model generalises from these examples to understand commands it has
never seen before. The more examples per intent, the better the model
understands the concept.
"""

import re
import time
from typing import Dict, List, Optional, Tuple
from nexus.core.logger import get_logger

log = get_logger("brain")

# ─── Intent Definitions ──────────────────────────────────────────────────────
# Each intent has: patterns (training phrases), action (plugin.command), param_extractor

INTENT_CATALOG = [
    # ── Web / Search ──────────────────────────────────────────────
    {
        "intent": "web_search_youtube",
        "phrases": [
            "search youtube for", "search on youtube", "find on youtube",
            "play on youtube", "youtube search", "look up on youtube",
            "open youtube and search", "watch on youtube",
        ],
        "action": "smart_actions.web_search_youtube",
        "extract": r"(?:search|find|look up|play|watch)(?:\s+(?:on|for|in))?\s+(?:youtube\s+(?:for\s+)?)?(.+)",
    },
    {
        "intent": "web_search_google",
        "phrases": [
            "search google for", "google search", "search for", "look up",
            "google", "find on google", "search the web for", "search online",
        ],
        "action": "smart_actions.web_search_google",
        "extract": r"(?:search|find|look up|google)(?:\s+(?:on|for|the web for|online|in google))?\s*(.+)",
    },
    {
        "intent": "open_website",
        "phrases": [
            "open website", "go to website", "navigate to", "open url",
            "browse to", "visit", "go to",
        ],
        "action": "smart_actions.open_website",
        "extract": r"(?:open|go to|navigate to|browse to|visit)\s+(?:website\s+)?(.+)",
    },

    # ── Media Control ─────────────────────────────────────────────
    {
        "intent": "media_play_pause",
        "phrases": [
            "play music", "pause music", "play pause", "resume music",
            "toggle playback", "play song", "pause song", "play spotify",
            "pause spotify", "play media", "pause media",
        ],
        "action": "smart_actions.media_play_pause",
        "extract": None,
    },
    {
        "intent": "media_next",
        "phrases": [
            "next song", "skip song", "next track", "skip track",
            "play next", "next music", "skip", "forward song",
        ],
        "action": "smart_actions.media_next",
        "extract": None,
    },
    {
        "intent": "media_previous",
        "phrases": [
            "previous song", "last song", "go back song", "previous track",
            "play previous", "back track",
        ],
        "action": "smart_actions.media_prev",
        "extract": None,
    },

    # ── System Queries (natural language) ─────────────────────────
    {
        "intent": "check_cpu",
        "phrases": [
            "what's using my cpu", "show cpu usage", "cpu heavy processes",
            "what's eating my cpu", "why is my pc slow", "check cpu",
            "which process is using cpu", "cpu hog", "what's making my pc lag",
        ],
        "action": "process_controller.list",
        "extract": None,
    },
    {
        "intent": "check_ram",
        "phrases": [
            "what's using my ram", "what's eating my memory", "show ram usage",
            "memory hog", "which process uses most memory", "ram heavy",
            "check memory usage", "why is my ram full",
        ],
        "action": "process_controller.list",
        "extract": None,
    },
    {
        "intent": "system_overview",
        "phrases": [
            "how is my system doing", "system status", "pc health",
            "computer status", "show me system info", "how's my pc",
            "machine status", "system overview", "computer health check",
        ],
        "action": "system_info.full",
        "extract": None,
    },
    {
        "intent": "check_battery",
        "phrases": [
            "how much battery", "battery left", "check battery",
            "battery status", "am i plugged in", "battery percentage",
            "how much charge", "power status",
        ],
        "action": "system_info.battery",
        "extract": None,
    },
    {
        "intent": "check_storage",
        "phrases": [
            "how much space left", "disk space", "storage left",
            "is my disk full", "check storage", "drive space",
            "how much free space", "disk usage",
        ],
        "action": "system_info.disk",
        "extract": None,
    },

    # ── Volume & Display (natural language) ───────────────────────
    {
        "intent": "volume_up",
        "phrases": [
            "turn up volume", "louder", "increase volume", "volume up",
            "make it louder", "raise volume", "crank it up",
            "i can't hear", "too quiet",
        ],
        "action": "smart_actions.volume_up",
        "extract": None,
    },
    {
        "intent": "volume_down",
        "phrases": [
            "turn down volume", "quieter", "decrease volume", "volume down",
            "make it quieter", "lower volume", "too loud",
        ],
        "action": "smart_actions.volume_down",
        "extract": None,
    },
    {
        "intent": "volume_mute",
        "phrases": [
            "mute", "mute everything", "silence", "shut up",
            "mute audio", "mute sound", "no sound", "mute volume",
        ],
        "action": "volume_display.mute",
        "extract": None,
    },
    {
        "intent": "volume_unmute",
        "phrases": [
            "unmute", "unmute audio", "unmute sound", "turn sound on",
            "restore audio", "enable sound", "turn volume back on",
        ],
        "action": "volume_display.unmute",
        "extract": None,
    },
    {
        "intent": "volume_set",
        "phrases": [
            "set volume to", "volume at", "change volume to",
            "make volume", "put volume at",
        ],
        "action": "volume_display.set_volume",
        "extract": r"(?:set |change |make |put )?volume\s*(?:to |at )?\s*(\d+)",
    },
    {
        "intent": "brightness_up",
        "phrases": [
            "brighter", "increase brightness", "screen brighter",
            "make screen brighter", "brighten", "more brightness",
            "i can't see the screen",
        ],
        "action": "smart_actions.brightness_up",
        "extract": None,
    },
    {
        "intent": "brightness_down",
        "phrases": [
            "darker", "decrease brightness", "dim screen", "make screen darker",
            "dimmer", "reduce brightness", "less brightness",
            "too bright", "my eyes hurt",
        ],
        "action": "smart_actions.brightness_down",
        "extract": None,
    },

    # ── App Management (natural language) ─────────────────────────
    {
        "intent": "open_app",
        "phrases": [
            "open", "launch", "start", "run app", "fire up",
            "open application", "start application",
        ],
        "action": "app_launcher.open",
        "extract": r"(?:open|launch|start|fire up|run)\s+(?:app(?:lication)?\s+)?(.+)",
    },
    {
        "intent": "close_app",
        "phrases": [
            "close", "kill", "stop", "quit", "exit app",
            "shut down app", "end task", "force close",
        ],
        "action": "process_controller.kill",
        "extract": r"(?:close|kill|stop|quit|exit|shut down|end|force close)\s+(?:app(?:lication)?\s+)?(.+)",
    },
    {
        "intent": "switch_app",
        "phrases": [
            "switch to", "go to", "focus on", "bring up",
            "show me", "alt tab to", "change to",
            "switch to chrome", "switch to firefox", "switch to notepad",
        ],
        "action": "window_manager.focus",
        "extract": r"(?:switch to|go to|focus on|bring up|show me|alt tab to|change to)\s+(.+)",
    },
    {
        "intent": "switch_tab",
        "phrases": [
            "switch to tab", "go to tab", "open tab", "change tab",
            "switch chrome tab", "switch browser tab",
            "go to the tab", "switch to the tab",
            "switch to notebooklm tab", "switch to gmail tab",
            "switch to youtube tab", "switch to google tab",
            "open the tab with", "find the tab",
            "chrome tab", "browser tab",
        ],
        "action": "smart_actions.switch_tab",
        "extract": r"(?:switch|go|change|open|find)\s+(?:to\s+)?(?:the\s+)?(?:chrome\s+|browser\s+)?tab\s+(?:to\s+|with\s+|for\s+)?(.+)",
    },

    # ── File Operations (natural language) ────────────────────────
    {
        "intent": "find_file",
        "phrases": [
            "find file", "where is", "search for file", "locate file",
            "find a file named", "look for file",
        ],
        "action": "file_ops.find",
        "extract": r"(?:find|where is|search for|locate|look for)\s+(?:file\s+)?(?:named\s+)?(.+)",
    },

    # ── Screenshots (natural language) ────────────────────────────
    {
        "intent": "take_screenshot",
        "phrases": [
            "take a screenshot", "capture screen", "screenshot",
            "take a pic of my screen", "screen capture", "snap screen",
            "save my screen", "print screen",
        ],
        "action": "screen_intel.screenshot",
        "extract": None,
    },

    # ── Clipboard (natural language) ──────────────────────────────
    {
        "intent": "clipboard_read",
        "phrases": [
            "what's in my clipboard", "show clipboard", "paste content",
            "what did i copy", "clipboard contents", "read clipboard",
        ],
        "action": "clipboard.get",
        "extract": None,
    },
    {
        "intent": "clipboard_write",
        "phrases": [
            "copy this to clipboard", "set clipboard to", "copy text",
            "put in clipboard",
        ],
        "action": "clipboard.set",
        "extract": r"(?:copy|set clipboard to|put in clipboard)\s+(.+)",
    },

    # ── Window Management (natural language) ──────────────────────
    {
        "intent": "minimize_all",
        "phrases": [
            "minimize everything", "minimize all", "show desktop",
            "clear my screen", "hide all windows", "go to desktop",
        ],
        "action": "smart_actions.minimize_all",
        "extract": None,
    },
    {
        "intent": "list_windows",
        "phrases": [
            "what windows are open", "show open windows", "list windows",
            "what's open", "which apps are open", "show all windows",
        ],
        "action": "window_manager.list",
        "extract": None,
    },

    # ── Shell (natural language) ──────────────────────────────────
    {
        "intent": "run_shell",
        "phrases": [
            "run command", "execute command", "terminal command",
            "run in terminal", "command line", "powershell",
        ],
        "action": "shell_executor.run",
        "extract": r"(?:run|execute)\s+(?:command\s+)?(.+)",
    },

    # ── AI / Gemini ──────────────────────────────────────────────
    {
        "intent": "ask_ai",
        "phrases": [
            "ask gemini", "hey gemini", "ask ai", "ask google ai",
            "gemini", "ask chatbot", "ai question", "ask about",
            "what is", "explain", "tell me about", "how does",
            "why does", "can you explain",
        ],
        "action": "smart_actions.ask_gemini",
        "extract": r"(?:ask|hey|tell me about|explain|what is|how does|why does|can you explain)\s+(?:gemini|ai|google ai|about)?\s*(.+)",
    },

    # ── Security ─────────────────────────────────────────────────
    {
        "intent": "lock_pc",
        "phrases": [
            "lock my computer", "lock pc", "lock screen", "lock my laptop",
            "lock the pc", "lock this computer", "secure my pc",
        ],
        "action": "smart_actions.lock_pc",
        "extract": None,
    },

    # ── Power Management ─────────────────────────────────────────
    {
        "intent": "shutdown_pc",
        "phrases": [
            "shut down", "shutdown", "power off", "turn off pc",
            "turn off computer", "shut down my computer", "power down",
        ],
        "action": "power_tools.shutdown",
        "extract": None,
    },
    {
        "intent": "restart_pc",
        "phrases": [
            "restart", "reboot", "restart my computer", "reboot pc",
            "restart the pc", "reboot my laptop",
        ],
        "action": "power_tools.restart",
        "extract": None,
    },
    {
        "intent": "sleep_pc",
        "phrases": [
            "put pc to sleep", "sleep mode", "put computer to sleep",
            "go to sleep", "sleep my pc", "standby",
        ],
        "action": "power_tools.sleep",
        "extract": None,
    },
    {
        "intent": "hibernate_pc",
        "phrases": [
            "hibernate", "hibernate my pc", "hibernate computer",
            "put pc in hibernation", "hibernation mode",
        ],
        "action": "power_tools.hibernate",
        "extract": None,
    },
    {
        "intent": "logoff_pc",
        "phrases": [
            "log off", "logoff", "sign out", "log me out",
            "sign me out", "log out of windows",
        ],
        "action": "power_tools.logoff",
        "extract": None,
    },
    {
        "intent": "cancel_shutdown",
        "phrases": [
            "cancel shutdown", "abort shutdown", "stop shutdown",
            "don't shut down", "cancel restart", "abort reboot",
        ],
        "action": "power_tools.cancel_shutdown",
        "extract": None,
    },

    # ── Notes ─────────────────────────────────────────────────────
    {
        "intent": "save_note",
        "phrases": [
            "save a note", "take a note", "remember this", "note this",
            "write down", "save this note", "jot down", "make a note",
            "remind me", "note save",
        ],
        "action": "power_tools.note_save",
        "extract": r"(?:save a note|take a note|remember this|note this|write down|jot down|make a note|remind me|note save)\s*[:]*\s*(.+)",
    },
    {
        "intent": "list_notes",
        "phrases": [
            "show my notes", "list notes", "what are my notes",
            "show notes", "read my notes", "note list",
        ],
        "action": "power_tools.note_list",
        "extract": None,
    },

    # ── Timer ─────────────────────────────────────────────────────
    {
        "intent": "set_timer",
        "phrases": [
            "set a timer", "timer for", "countdown", "set timer",
            "remind me in", "alarm in", "wake me in",
        ],
        "action": "power_tools.timer",
        "extract": r"(?:set a timer|timer for|countdown|set timer|remind me in|alarm in|wake me in)\s+(?:for\s+)?(\d+.+)",
    },

    # ── Networking ────────────────────────────────────────────────
    {
        "intent": "wifi_info",
        "phrases": [
            "wifi info", "show wifi", "what wifi am i connected to",
            "wifi status", "wifi details", "my wifi", "network name",
            "am i connected to wifi", "what's my wifi",
        ],
        "action": "power_tools.wifi",
        "extract": None,
    },
    {
        "intent": "ip_address",
        "phrases": [
            "what's my ip", "show my ip", "ip address", "my ip address",
            "what is my ip address", "show ip", "get my ip",
        ],
        "action": "power_tools.ip_address",
        "extract": None,
    },

    # ── Window Multitasking ───────────────────────────────────────
    {
        "intent": "snap_window_left",
        "phrases": [
            "snap window left", "put window on left", "move window left half",
            "tile left", "split screen left",
        ],
        "action": "smart_actions.snap_left",
        "extract": None,
    },
    {
        "intent": "snap_window_right",
        "phrases": [
            "snap window right", "put window on right", "move window right half",
            "tile right", "split screen right",
        ],
        "action": "smart_actions.snap_right",
        "extract": None,
    },

    # ── File Organization ────────────────────────────────────────
    {
        "intent": "organize_downloads",
        "phrases": [
            "organize my downloads", "clean up downloads", "sort downloads",
            "organize download folder", "tidy downloads",
            "clean downloads folder", "sort my files",
        ],
        "action": "smart_actions.organize_downloads",
        "extract": None,
    },
    {
        "intent": "organize_desktop",
        "phrases": [
            "organize my desktop", "clean up desktop", "sort desktop",
            "tidy desktop", "clean desktop",
        ],
        "action": "smart_actions.organize_desktop",
        "extract": None,
    },

    # ── Recycle Bin ───────────────────────────────────────────────
    {
        "intent": "empty_recycle",
        "phrases": [
            "empty recycle bin", "clear recycle bin", "empty trash",
            "clear trash", "delete recycle bin", "clean recycle bin",
        ],
        "action": "power_tools.empty_recycle",
        "extract": None,
    },

    # ── Pin Window ───────────────────────────────────────────────
    {
        "intent": "pin_window",
        "phrases": [
            "pin window", "always on top", "keep on top",
            "pin this window", "stay on top", "window on top",
        ],
        "action": "power_tools.pin_window",
        "extract": None,
    },

    # ── Night Light ──────────────────────────────────────────────
    {
        "intent": "night_light",
        "phrases": [
            "night light", "blue light filter", "night mode",
            "eye protection", "warm screen", "toggle night light",
        ],
        "action": "power_tools.night_light",
        "extract": None,
    },

    # ── Conversational / Greetings ───────────────────────────────
    {
        "intent": "greeting",
        "phrases": [
            "hello", "hi", "hey", "good morning", "good evening",
            "good afternoon", "howdy", "what's up", "sup",
            "yo", "hey nexus", "hello nexus",
        ],
        "action": "smart_actions.greet",
        "extract": None,
    },
    {
        "intent": "whats_the_time",
        "phrases": [
            "what time is it", "what's the time", "current time",
            "tell me the time", "show time", "what time",
        ],
        "action": "smart_actions.current_time",
        "extract": None,
    },
    {
        "intent": "whats_the_date",
        "phrases": [
            "what's the date", "what date is it", "current date",
            "today's date", "what day is it", "show date",
        ],
        "action": "smart_actions.current_date",
        "extract": None,
    },

    # ── Quick Web Shortcuts ──────────────────────────────────────
    {
        "intent": "open_email",
        "phrases": [
            "open email", "check email", "open gmail", "check my email",
            "go to gmail", "open my mail", "inbox",
        ],
        "action": "smart_actions.open_email",
        "extract": None,
    },
    {
        "intent": "open_maps",
        "phrases": [
            "open maps", "google maps", "show me a map",
            "navigate to", "directions to", "find directions",
        ],
        "action": "smart_actions.open_maps",
        "extract": r"(?:navigate to|directions to|find directions)\s+(.+)",
    },
    {
        "intent": "search_wikipedia",
        "phrases": [
            "search wikipedia", "look up on wikipedia", "wikipedia",
            "wiki search", "find on wikipedia",
        ],
        "action": "smart_actions.search_wikipedia",
        "extract": r"(?:search|look up|find)\s+(?:on\s+)?(?:wikipedia|wiki)\s+(?:for\s+)?(.+)",
    },
    {
        "intent": "search_amazon",
        "phrases": [
            "search amazon", "find on amazon", "amazon search",
            "shop for", "buy", "look up on amazon",
        ],
        "action": "smart_actions.search_amazon",
        "extract": r"(?:search|find|look up|shop for|buy)\s+(?:on\s+)?(?:amazon\s+)?(?:for\s+)?(.+)",
    },

    # ── Undo / Redo ──────────────────────────────────────────────
    {
        "intent": "undo",
        "phrases": [
            "undo", "undo that", "go back", "ctrl z", "reverse that",
        ],
        "action": "smart_actions.undo",
        "extract": None,
    },
    {
        "intent": "redo",
        "phrases": [
            "redo", "redo that", "ctrl y", "redo last action",
        ],
        "action": "smart_actions.redo",
        "extract": None,
    },

    # ── Select All / Copy / Paste ────────────────────────────────
    {
        "intent": "select_all",
        "phrases": [
            "select all", "select everything", "highlight all",
            "ctrl a", "mark all",
        ],
        "action": "smart_actions.select_all",
        "extract": None,
    },
    {
        "intent": "copy_selection",
        "phrases": [
            "copy this", "copy that", "ctrl c", "copy selected",
        ],
        "action": "smart_actions.copy",
        "extract": None,
    },
    {
        "intent": "paste",
        "phrases": [
            "paste", "paste that", "ctrl v", "paste here",
        ],
        "action": "smart_actions.paste",
        "extract": None,
    },

    # ── Save / Print ─────────────────────────────────────────────
    {
        "intent": "save_file",
        "phrases": [
            "save", "save this", "ctrl s", "save file", "save document",
        ],
        "action": "smart_actions.save",
        "extract": None,
    },
    {
        "intent": "switch_window",
        "phrases": [
            "alt tab", "switch window", "next window",
            "cycle windows", "change window",
        ],
        "action": "smart_actions.alt_tab",
        "extract": None,
    },

    # ── Task Manager ─────────────────────────────────────────────
    {
        "intent": "open_task_manager",
        "phrases": [
            "open task manager", "show task manager", "task manager",
            "what's running", "show running processes",
        ],
        "action": "smart_actions.open_task_manager",
        "extract": None,
    },

    # ── Refresh ──────────────────────────────────────────────────
    {
        "intent": "refresh",
        "phrases": [
            "refresh", "reload page", "refresh page", "f5",
            "reload this page", "ctrl r",
        ],
        "action": "smart_actions.refresh",
        "extract": None,
    },

    # ══════════════════════════════════════════════════════════════
    # ── Intelligence Plugin — Smart Contextual Commands ──────────
    # ══════════════════════════════════════════════════════════════

    # ── Gemini / AI Chat ─────────────────────────────────────────
    {
        "intent": "gemini_chats",
        "phrases": [
            "open gemini", "open gemini chats", "show gemini chats",
            "my gemini conversations", "go to gemini", "gemini history",
            "previous gemini chats", "access gemini", "gemini app",
        ],
        "action": "intelligence.gemini_chats",
        "extract": None,
    },
    {
        "intent": "gemini_last",
        "phrases": [
            "last gemini chat", "previous gemini chat", "open last gemini",
            "continue gemini chat", "go back to gemini chat", "resume gemini",
            "my last chat with gemini", "access previous chat with gemini",
            "open my previous gemini conversation", "where was i on gemini",
            "previous chat with gemini", "last conversation with gemini",
        ],
        "action": "intelligence.gemini_last",
        "extract": None,
    },
    {
        "intent": "open_chatgpt",
        "phrases": [
            "open chatgpt", "go to chatgpt", "chatgpt", "open chat gpt",
            "access chatgpt", "talk to chatgpt",
        ],
        "action": "intelligence.open_chatgpt",
        "extract": None,
    },

    # ── Browser History ──────────────────────────────────────────
    {
        "intent": "browser_history",
        "phrases": [
            "browser history", "search history", "my history",
            "what did i browse", "show browser history", "browsing history",
            "search my history for", "check history for",
        ],
        "action": "intelligence.browser_history",
        "extract": r"(?:search|check|show|find in|browse)?\s*(?:my\s+)?(?:browser\s+)?history\s+(?:for\s+)?(.+)",
    },
    {
        "intent": "recent_tabs",
        "phrases": [
            "recent tabs", "recently visited", "recent sites",
            "what sites did i visit", "show recent pages",
            "last visited sites", "recent browsing",
        ],
        "action": "intelligence.recent_tabs",
        "extract": None,
    },
    {
        "intent": "open_recent_site",
        "phrases": [
            "open recent", "open last visited", "go back to that site",
            "reopen that page", "open the page i was on",
        ],
        "action": "intelligence.open_recent_site",
        "extract": r"(?:open|go back to|reopen)\s+(?:recent|last visited|that)\s+(?:site|page)?\s*(.+)?",
    },

    # ── Smart Diagnostics ────────────────────────────────────────
    {
        "intent": "why_slow",
        "phrases": [
            "why is my pc slow", "why is it slow", "computer is slow",
            "laptop slow", "system slow", "why is my computer lagging",
            "pc is lagging", "everything is slow", "running slow",
            "diagnose slow", "why so slow", "my pc is freezing",
        ],
        "action": "intelligence.why_slow",
        "extract": None,
    },
    {
        "intent": "health_check",
        "phrases": [
            "health check", "system health", "check my system",
            "is my pc healthy", "pc checkup", "system status",
            "how is my computer", "system report", "diagnostics",
            "run diagnostics", "full system check",
        ],
        "action": "intelligence.health_check",
        "extract": None,
    },
    {
        "intent": "disk_hogs",
        "phrases": [
            "disk hogs", "what is using disk space", "biggest files",
            "find large files", "whats eating my storage",
            "what is taking up space", "storage hogs",
        ],
        "action": "intelligence.disk_hogs",
        "extract": r"(?:disk hogs|large files|biggest files|storage hogs)(?:\s+(?:in|on|at)\s+)?(.+)?",
    },
    {
        "intent": "startup_programs",
        "phrases": [
            "startup programs", "startup apps", "what runs on startup",
            "boot programs", "list startup apps", "disable startup",
            "why is boot slow", "slow boot", "slow startup",
        ],
        "action": "intelligence.startup_programs",
        "extract": None,
    },

    # ── Quick Math & Conversions ─────────────────────────────────
    {
        "intent": "calculate",
        "phrases": [
            "calculate", "calc", "what is", "how much is",
            "math", "compute", "evaluate", "solve",
        ],
        "action": "intelligence.calculate",
        "extract": r"(?:calculate|calc|compute|evaluate|solve|what is|how much is)\s+(.+)",
    },
    {
        "intent": "convert_units",
        "phrases": [
            "convert", "how many", "how much is in",
            "what is in", "to miles", "to km", "to celsius",
            "to fahrenheit", "to pounds", "to kg",
        ],
        "action": "intelligence.convert",
        "extract": r"(?:convert\s+)?(.+)",
    },

    # ── Recent Files & Search ────────────────────────────────────
    {
        "intent": "recent_files",
        "phrases": [
            "recent files", "recently modified files", "latest files",
            "what did i work on", "show recent files",
            "recently edited", "last modified files",
        ],
        "action": "intelligence.recent_files",
        "extract": r"(?:recent|latest|last modified)\s+files\s+(?:in|on|at)\s+(.+)",
    },
    {
        "intent": "search_files",
        "phrases": [
            "search file", "find file", "where is file",
            "locate file", "search for file", "find a file named",
        ],
        "action": "intelligence.search_files",
        "extract": r"(?:search|find|locate|where is)\s+(?:a\s+)?(?:file\s+)?(?:named|called)?\s*(.+)",
    },
    {
        "intent": "large_files",
        "phrases": [
            "large files", "big files", "huge files",
            "files taking space", "whats big in downloads",
        ],
        "action": "intelligence.large_files",
        "extract": r"(?:large|big|huge)\s+files\s+(?:in|on|at)\s+(.+)",
    },
    {
        "intent": "duplicate_files",
        "phrases": [
            "duplicate files", "find duplicates", "duplicate check",
            "are there duplicates", "duplicate finder",
        ],
        "action": "intelligence.find_duplicates",
        "extract": r"(?:duplicate|find duplicates)(?:\s+(?:in|on|at)\s+)?(.+)?",
    },
    {
        "intent": "open_last_download",
        "phrases": [
            "open last download", "latest download", "recent download",
            "open my last download", "what did i download",
            "open newest download", "last downloaded file",
        ],
        "action": "intelligence.open_last_download",
        "extract": None,
    },
    {
        "intent": "open_last_screenshot",
        "phrases": [
            "open last screenshot", "latest screenshot", "recent screenshot",
            "show last screenshot", "open my screenshot",
        ],
        "action": "intelligence.open_last_screenshot",
        "extract": None,
    },

    # ── Daily Summary & Context ──────────────────────────────────
    {
        "intent": "daily_summary",
        "phrases": [
            "daily summary", "my day", "day summary",
            "how is my day going", "quick summary", "morning summary",
            "give me a summary", "summarize my system",
        ],
        "action": "intelligence.daily_summary",
        "extract": None,
    },

    # ── Web Shortcuts ────────────────────────────────────────────
    {
        "intent": "open_github",
        "phrases": [
            "open github", "go to github", "github",
        ],
        "action": "intelligence.open_github",
        "extract": None,
    },
    {
        "intent": "open_stackoverflow",
        "phrases": [
            "stackoverflow", "stack overflow", "search stackoverflow",
            "ask stackoverflow",
        ],
        "action": "intelligence.open_stackoverflow",
        "extract": r"(?:search\s+)?(?:stack\s*overflow)\s*(?:for\s+)?(.+)?",
    },
    {
        "intent": "open_reddit",
        "phrases": [
            "open reddit", "go to reddit", "reddit",
            "browse reddit", "show reddit",
        ],
        "action": "intelligence.open_reddit",
        "extract": r"(?:open|go to|browse|show)\s+(?:reddit)\s*(?:r/)?(.+)?",
    },
    {
        "intent": "define_word",
        "phrases": [
            "define", "definition of", "what does mean",
            "meaning of", "whats the meaning of",
        ],
        "action": "intelligence.define_word",
        "extract": r"(?:define|definition of|meaning of|what does)\s+(.+?)(?:\s+mean)?$",
    },
    {
        "intent": "translate",
        "phrases": [
            "translate", "how do you say", "translation of",
            "translate to", "say in spanish", "say in french",
            "say in hindi",
        ],
        "action": "intelligence.translate_text",
        "extract": r"(?:translate|how do you say)\s+(.+)",
    },

    # ── Workflow Engine ──────────────────────────────────────────
    {
        "intent": "workflow_list",
        "phrases": [
            "list workflows", "show workflows", "my workflows",
            "what workflows do i have", "all workflows",
        ],
        "action": "workflow_engine.list",
        "extract": None,
    },
    {
        "intent": "workflow_run",
        "phrases": [
            "run workflow", "execute workflow", "start workflow",
            "play workflow",
        ],
        "action": "workflow_engine.run",
        "extract": r"(?:run|execute|start|play)\s+workflow\s+(.+)",
    },
    {
        "intent": "workflow_record",
        "phrases": [
            "record workflow", "create workflow", "new workflow",
            "start recording workflow", "make a workflow",
        ],
        "action": "workflow_engine.start_recording",
        "extract": r"(?:record|create|new|make)\s+(?:a\s+)?workflow\s+(?:called\s+|named\s+)?(.+)",
    },

    # ── File Ops (expanded) ──────────────────────────────────────
    {
        "intent": "copy_file",
        "phrases": [
            "copy file", "copy this file", "duplicate file",
            "copy file to", "cp file",
        ],
        "action": "file_ops.copy",
        "extract": r"copy\s+(?:file\s+)?(.+)\s+to\s+(.+)",
    },
    {
        "intent": "move_file",
        "phrases": [
            "move file", "move this file", "relocate file",
            "move file to", "mv file",
        ],
        "action": "file_ops.move",
        "extract": r"move\s+(?:file\s+)?(.+)\s+to\s+(.+)",
    },
    {
        "intent": "delete_file",
        "phrases": [
            "delete file", "remove file", "trash file",
            "delete this file", "rm file",
        ],
        "action": "file_ops.delete",
        "extract": r"(?:delete|remove|trash|rm)\s+(?:file\s+)?(.+)",
    },
    {
        "intent": "dir_tree",
        "phrases": [
            "show directory tree", "folder tree", "show file tree",
            "directory structure", "tree view", "show tree",
        ],
        "action": "file_ops.tree",
        "extract": r"(?:show\s+)?(?:directory\s+|folder\s+|file\s+)?tree\s+(?:of\s+|for\s+)?(.+)?",
    },
    {
        "intent": "organize_folder",
        "phrases": [
            "organize folder", "organize directory", "clean up folder",
            "sort files in folder", "tidy up folder",
        ],
        "action": "file_ops.organize",
        "extract": r"(?:organize|clean up|sort files in|tidy up)\s+(?:folder\s+|directory\s+)?(.+)",
    },

    # ── System Info (individual) ─────────────────────────────────
    {
        "intent": "network_info",
        "phrases": [
            "network info", "network status", "show network",
            "internet speed", "network details", "connection info",
        ],
        "action": "system_info.network",
        "extract": None,
    },
    {
        "intent": "uptime_info",
        "phrases": [
            "uptime", "how long has pc been on", "system uptime",
            "when was last reboot", "computer uptime",
        ],
        "action": "system_info.uptime",
        "extract": None,
    },
    {
        "intent": "memory_info",
        "phrases": [
            "memory info", "ram info", "ram usage", "how much ram",
            "show memory", "memory details",
        ],
        "action": "system_info.memory",
        "extract": None,
    },
    {
        "intent": "disk_info",
        "phrases": [
            "disk info", "disk usage", "storage info", "how much storage",
            "show disk space", "disk space",
        ],
        "action": "system_info.disk",
        "extract": None,
    },
    {
        "intent": "cpu_info",
        "phrases": [
            "cpu info", "processor info", "cpu usage", "how busy is cpu",
            "show cpu", "cpu details",
        ],
        "action": "system_info.cpu",
        "extract": None,
    },

    # ── Keyboard/Mouse ───────────────────────────────────────────
    {
        "intent": "type_text",
        "phrases": [
            "type", "type this", "type out", "write this",
            "enter text", "input text",
        ],
        "action": "keyboard_mouse.type",
        "extract": r"(?:type|type this|type out|write this|enter text|input text)\s+(.+)",
    },
    {
        "intent": "press_hotkey",
        "phrases": [
            "press", "hotkey", "keyboard shortcut", "press keys",
            "key combo", "key combination",
        ],
        "action": "keyboard_mouse.hotkey",
        "extract": r"(?:press|hotkey|key combo)\s+(.+)",
    },

    # ── Scheduler ────────────────────────────────────────────────
    {
        "intent": "schedule_command",
        "phrases": [
            "schedule", "schedule command", "run later",
            "do this later", "schedule task",
        ],
        "action": "task_scheduler.schedule",
        "extract": r"schedule\s+(.+)",
    },
    {
        "intent": "list_scheduled",
        "phrases": [
            "list scheduled", "show scheduled", "my schedules",
            "what is scheduled", "scheduled tasks",
        ],
        "action": "task_scheduler.list",
        "extract": None,
    },

    # ── Core Builder (Meta-Plugin) ───────────────────────────────
    {
        "intent": "build_plugin",
        "phrases": [
            "build a plugin", "create a plugin", "generate a plugin",
            "make a plugin", "write a plugin", "build plugin",
            "create a tool", "generate a tool", "make a tool",
            "i need a plugin that", "can you build a plugin",
            "create a new plugin", "build me a tool",
            "write a module for", "generate a module",
        ],
        "action": "core_builder.build_plugin",
        "extract": r"(?:build|create|generate|make|write)\s+(?:a\s+)?(?:plugin|tool|module)\s+(?:that\s+|to\s+|for\s+|which\s+)?(.+)",
    },
    {
        "intent": "list_generated_plugins",
        "phrases": [
            "list generated plugins", "show generated plugins",
            "my generated plugins", "list built plugins",
            "what plugins have i built", "show my plugins",
        ],
        "action": "core_builder.list_generated",
        "extract": None,
    },
    {
        "intent": "heal_plugin",
        "phrases": [
            "heal plugin", "fix plugin", "debug plugin",
            "repair plugin", "auto fix plugin", "auto debug plugin",
        ],
        "action": "core_builder.heal_plugin",
        "extract": r"(?:heal|fix|debug|repair)\s+(?:the\s+)?plugin\s+(.+)",
    },
    {
        "intent": "reload_plugin",
        "phrases": [
            "reload plugin", "hotswap plugin", "hot swap plugin",
            "refresh plugin", "reload generated plugin",
        ],
        "action": "core_builder.reload_generated",
        "extract": r"(?:reload|hotswap|hot-swap|refresh)\s+(?:the\s+)?(?:generated\s+)?plugin\s+(.+)",
    },
    {
        "intent": "grant_plugin_permission",
        "phrases": [
            "grant permission", "allow network access", "permit domain",
            "allow plugin access", "grant network permission",
            "approve domain access", "allow once", "allow always",
        ],
        "action": "core_builder.grant_permission",
        "extract": r"(?:grant\s+permission|allow|permit)\s+(?:network\s+)?(?:access\s+)?(?:for\s+)?(\S+)\s+(once|always|block)",
    },
]


class NexusBrain:
    """
    ML-Powered AI Brain.
    
    Pipeline:
      1. Sentence-transformer encodes user input → embedding vector
      2. Cosine similarity against pre-computed intent embeddings
      3. Behavioral boost from learner (frequently used intents score higher)
      4. Dynamic threshold: high-confidence → instant match,
         medium-confidence → verify with keyword overlap
      5. Falls back to enhanced Jaccard if model unavailable
    
    The model generalises from the INTENT_CATALOG examples, so it can
    understand phrasings that are NOT hardcoded (e.g. "show me stuff on youtube"
    matches web_search_youtube even though that exact phrase doesn't exist).
    """

    # Confidence thresholds
    HIGH_CONFIDENCE = 0.52     # Accept immediately
    MEDIUM_CONFIDENCE = 0.38   # Accept if keyword overlap confirms
    FALLBACK_THRESHOLD = 0.25  # For Jaccard fallback mode

    def __init__(self):
        self.model = None
        self.intent_embeddings = None
        self.catalog = INTENT_CATALOG
        self._ready = False
        self._loading = False
        self._phrase_to_intent = {}
        self._all_phrases = []
        self._intent_keywords = {}   # intent → set of important keywords
        self._build_keyword_index()

    @property
    def is_ready(self) -> bool:
        """Whether the ML brain model is loaded and ready."""
        return self._ready

    def _build_keyword_index(self):
        """Pre-build a keyword index for each intent from its phrases."""
        stopwords = {"a", "the", "to", "for", "on", "in", "my", "is", "it", "do",
                     "can", "you", "me", "i", "and", "or", "of", "up", "at", "with"}
        for intent_def in self.catalog:
            words = set()
            for phrase in intent_def["phrases"]:
                for w in phrase.lower().split():
                    if w not in stopwords and len(w) > 1:
                        words.add(w)
            self._intent_keywords[intent_def["intent"]] = words

    def initialize(self):
        """Load the sentence transformer model and pre-compute intent embeddings."""
        if self._ready or self._loading:
            return
        self._loading = True

        try:
            from sentence_transformers import SentenceTransformer
            log.info("Loading AI brain model (all-MiniLM-L6-v2)...")
            start = time.time()
            self.model = SentenceTransformer("all-MiniLM-L6-v2")

            # Pre-compute embeddings for all intent phrases
            all_phrases = []
            self._phrase_to_intent = {}
            for intent_def in self.catalog:
                for phrase in intent_def["phrases"]:
                    all_phrases.append(phrase)
                    self._phrase_to_intent[phrase] = intent_def

            self.intent_embeddings = self.model.encode(all_phrases, convert_to_tensor=True)
            self._all_phrases = all_phrases

            elapsed = time.time() - start
            log.info(f"🧠 AI brain ready — {len(all_phrases)} training phrases, "
                     f"{len(self.catalog)} intents in {elapsed:.1f}s")
            self._ready = True
        except ImportError:
            log.warning("sentence-transformers not installed — brain using fallback mode")
            self._ready = False
        except Exception as e:
            log.error(f"Brain init failed: {e}")
            self._ready = False
        finally:
            self._loading = False

    def classify(self, text: str, boost_map: Optional[Dict[str, float]] = None) -> Optional[Dict]:
        """
        Classify natural language text into an intent using ML embeddings.
        
        The model generalises — it understands phrasings NOT in the catalog
        because it matches semantic meaning, not just keywords.
        """
        if not self._ready:
            return self._fallback_classify(text)

        try:
            import torch
            from sentence_transformers import util

            # Encode user input
            query_embedding = self.model.encode(text, convert_to_tensor=True)

            # Compute cosine similarities against all training phrases
            scores = util.cos_sim(query_embedding, self.intent_embeddings)[0]

            # Apply behavioral boost (frequently used intents get a nudge)
            if boost_map:
                for i, phrase in enumerate(self._all_phrases):
                    intent_def = self._phrase_to_intent[phrase]
                    intent_name = intent_def["intent"]
                    if intent_name in boost_map:
                        scores[i] = scores[i] + boost_map[intent_name] * 0.08

            # Get best match
            best_idx = torch.argmax(scores).item()
            best_score = scores[best_idx].item()
            best_phrase = self._all_phrases[best_idx]
            best_intent = self._phrase_to_intent[best_phrase]

            # ── Tiered confidence check ──
            accepted = False

            if best_score >= self.HIGH_CONFIDENCE:
                accepted = True
            elif best_score >= self.MEDIUM_CONFIDENCE:
                # Medium confidence — verify with keyword overlap
                text_words = set(text.lower().split())
                intent_kw = self._intent_keywords.get(best_intent["intent"], set())
                if intent_kw:
                    kw_overlap = len(text_words & intent_kw) / max(len(intent_kw), 1)
                    if kw_overlap >= 0.2:
                        accepted = True
                        best_score = min(best_score + kw_overlap * 0.1, 0.95)

            if not accepted:
                return None

            # Extract parameters
            params = self._extract_params(text, best_intent)

            return {
                "intent": best_intent["intent"],
                "action": best_intent["action"],
                "params": params,
                "confidence": round(best_score, 3),
                "matched_phrase": best_phrase,
                "original": text,
            }

        except Exception as e:
            log.error(f"Brain classify error: {e}")
            return self._fallback_classify(text)

    def _extract_params(self, text: str, intent_def: dict) -> dict:
        """Extract parameters from the user's text using regex, then smart fallbacks."""
        params = {}
        extractor = intent_def.get("extract")

        intent_name = intent_def.get("intent", "")

        # Domain-specific noise patterns to strip from extracted queries
        _domain_clean = {
            "web_search_youtube": [r"\b(?:on|from|in)\s+youtube\b", r"\byoutube\b"],
            "web_search_google":  [r"\b(?:on|from|in)\s+google\b", r"\bgoogle\b"],
            "open_website":       [r"\b(?:the\s+)?(?:website|site|page)\b"],
        }

        def _clean_query(raw: str) -> str:
            """Iteratively strip leading noise words and domain-specific phrases."""
            # Strip leading filler words iteratively
            prev = None
            while raw != prev:
                prev = raw
                raw = re.sub(
                    r"^(?:me|my|a|an|the|some|good|best|great|nice|top|really|"
                    r"for|about|on|in|of|preferably|please)\s+",
                    "", raw, flags=re.IGNORECASE,
                ).strip()
            # Strip trailing filler words
            raw = re.sub(
                r"\s+(?:please|preferably|if\s+possible|maybe)$",
                "", raw, flags=re.IGNORECASE,
            ).strip()
            # Strip domain-specific platform references from the middle
            for pattern in _domain_clean.get(intent_name, []):
                raw = re.sub(pattern, "", raw, flags=re.IGNORECASE).strip()
            # Collapse multiple spaces
            raw = re.sub(r"\s{2,}", " ", raw).strip()
            return raw

        # ── Method 1: Regex extractor ──
        if extractor:
            match = re.search(extractor, text, re.IGNORECASE)
            if match:
                raw = _clean_query(match.group(1).strip())
                if raw:
                    params["query"] = raw
                    return params

        # ── Method 2: Phrase-prefix stripping ──
        text_lower = text.lower().strip()
        for phrase in intent_def["phrases"]:
            if text_lower.startswith(phrase):
                remainder = text[len(phrase):].strip()
                if remainder:
                    remainder = _clean_query(remainder)
                    if remainder:
                        params["query"] = remainder
                        return params

        # ── Method 3: Smart keyword stripping (ML fallback) ──
        # Remove words that are part of the intent "machinery" and keep the content
        intent_name = intent_def.get("intent", "")
        noise_words = {
            "find", "me", "search", "for", "on", "a", "the", "some", "good",
            "show", "open", "look", "up", "please", "can", "you", "i", "want",
            "to", "do", "go", "play", "watch", "get", "my", "let", "tell",
            "give", "see", "check", "run", "start", "launch", "hey", "nexus",
            "could", "would", "like", "need", "just", "about", "how", "is",
            "whats", "what", "what's", "it", "this", "that",
        }
        # Also remove words that appear in the intent keywords (they're action words, not content)
        intent_kw = self._intent_keywords.get(intent_name, set())

        # For specific intent types, add domain-specific noise words
        domain_noise = {
            "web_search_youtube": {"youtube", "video", "videos", "yt"},
            "web_search_google": {"google", "web", "online", "internet"},
            "browser_history": {"browser", "history", "browsing"},
            "open_website": {"website", "site", "url", "page"},
            "calculate": {"calculate", "calc", "compute", "evaluate", "solve", "equals"},
            "open_stackoverflow": {"stackoverflow", "stack", "overflow"},
            "open_reddit": {"reddit"},
            "define_word": {"define", "definition", "meaning"},
            "translate": {"translate", "translation"},
            "search_files": {"file", "files", "named", "called"},
        }
        extra_noise = domain_noise.get(intent_name, set())

        words = text.split()
        content_words = []
        for w in words:
            w_clean = w.lower().strip(".,!?\"'")
            if w_clean not in noise_words and w_clean not in extra_noise:
                content_words.append(w)

        query = " ".join(content_words).strip()
        query = re.sub(r"^(for|about|on|the|some|a)\s+", "", query, flags=re.IGNORECASE).strip()

        if query:
            params["query"] = query

        return params

    def _fallback_classify(self, text: str) -> Optional[Dict]:
        """Enhanced fallback: word overlap + substring + partial matching."""
        text_lower = text.lower().strip()
        text_words = set(text_lower.split())

        best_match = None
        best_score = 0

        for intent_def in self.catalog:
            for phrase in intent_def["phrases"]:
                phrase_lower = phrase.lower()
                phrase_words = set(phrase_lower.split())

                score = 0

                # Method 1: substring containment
                if phrase_lower in text_lower:
                    score = len(phrase_lower) / max(len(text_lower), 1)
                elif text_lower in phrase_lower:
                    score = len(text_lower) / max(len(phrase_lower), 1) * 0.8

                # Method 2: Jaccard word overlap
                if phrase_words and text_words:
                    intersection = text_words & phrase_words
                    union = text_words | phrase_words
                    jaccard = len(intersection) / max(len(union), 1)
                    phrase_coverage = len(intersection) / max(len(phrase_words), 1)
                    combined = jaccard * 0.4 + phrase_coverage * 0.6
                    score = max(score, combined)

                if score > best_score:
                    best_score = score
                    best_match = intent_def

        if best_match and best_score > self.FALLBACK_THRESHOLD:
            params = self._extract_params(text, best_match)
            return {
                "intent": best_match["intent"],
                "action": best_match["action"],
                "params": params,
                "confidence": round(min(best_score, 0.95), 3),
                "matched_phrase": "(fallback)",
                "original": text,
            }

        return None

    def get_suggestions(self, partial: str, top_k: int = 5) -> List[Dict]:
        """Get intent suggestions for partial input (autocomplete)."""
        if not self._ready:
            return self._fallback_suggestions(partial, top_k)

        try:
            import torch
            from sentence_transformers import util

            query_emb = self.model.encode(partial, convert_to_tensor=True)
            scores = util.cos_sim(query_emb, self.intent_embeddings)[0]

            seen = set()
            suggestions = []
            top_indices = torch.argsort(scores, descending=True)

            for idx in top_indices:
                phrase = self._all_phrases[idx.item()]
                intent_def = self._phrase_to_intent[phrase]
                intent_name = intent_def["intent"]

                if intent_name not in seen:
                    seen.add(intent_name)
                    suggestions.append({
                        "intent": intent_name,
                        "example": phrase,
                        "confidence": round(scores[idx.item()].item(), 3),
                    })

                if len(suggestions) >= top_k:
                    break

            return suggestions
        except Exception:
            return self._fallback_suggestions(partial, top_k)

    def _fallback_suggestions(self, partial: str, top_k: int = 5) -> List[Dict]:
        """Simple keyword-based suggestions when model isn't available."""
        partial_lower = partial.lower()
        scored = []
        seen = set()
        for intent_def in self.catalog:
            for phrase in intent_def["phrases"]:
                if partial_lower in phrase.lower() and intent_def["intent"] not in seen:
                    seen.add(intent_def["intent"])
                    scored.append({
                        "intent": intent_def["intent"],
                        "example": phrase,
                        "confidence": 0.5,
                    })
                    break
        return scored[:top_k]
