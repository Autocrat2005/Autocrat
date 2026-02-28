"""
Autocrat — Power Tools Plugin
Power management, quick notes, timers, and system shortcuts.
"""

import os
import json
import time
import threading
import subprocess
import ctypes
from datetime import datetime
from pathlib import Path
from nexus.core.plugin import NexusPlugin
from nexus.core.logger import get_logger
from nexus.core.config import Config

log = get_logger("power_tools")

NOTES_DIR = Path("nexus_data/notes")
NOTES_DIR.mkdir(parents=True, exist_ok=True)


class PowerToolsPlugin(NexusPlugin):
    """Power management, notes, timers, and system utilities."""

    name = "power_tools"
    icon = "⚡"
    description = "Power management, notes, timers & system utilities"
    version = "1.0.0"

    def setup(self):
        self.config = Config()

        # Power
        self.register_command("shutdown", self.shutdown,
                              "Shutdown PC", "shutdown [delay_seconds]")
        self.register_command("restart", self.restart,
                              "Restart PC", "restart")
        self.register_command("sleep", self.sleep_pc,
                              "Put PC to sleep", "sleep")
        self.register_command("hibernate", self.hibernate,
                              "Hibernate PC", "hibernate")
        self.register_command("logoff", self.logoff,
                              "Log out current user", "logoff")
        self.register_command("cancel_shutdown", self.cancel_shutdown,
                              "Cancel scheduled shutdown", "cancel shutdown")

        # Notes
        self.register_command("note_save", self.note_save,
                              "Save a quick note", "note save <text>")
        self.register_command("note_list", self.note_list,
                              "List all notes", "note list")
        self.register_command("note_read", self.note_read,
                              "Read a note by number", "note read <number>")
        self.register_command("note_delete", self.note_delete,
                              "Delete a note", "note delete <number>")

        # Timer
        self.register_command("timer", self.set_timer,
                              "Set a countdown timer", "timer <seconds> [message]")

        # System info shortcuts
        self.register_command("wifi", self.wifi_info,
                              "Show Wi-Fi info", "wifi")
        self.register_command("ip_address", self.ip_address,
                              "Show IP address", "ip address")
        self.register_command("uptime", self.uptime,
                              "Show system uptime", "uptime")
        self.register_command("night_light", self.night_light,
                              "Toggle night light", "night light")
        self.register_command("pin_window", self.pin_window,
                              "Keep current window always on top", "pin window")
        self.register_command("empty_recycle", self.empty_recycle,
                              "Empty the recycle bin", "empty recycle bin")
        self.register_command("open_fav", self.open_favorite,
                              "Open a favorite app", "open <app_name>")

    def _blocked_power_action(self, action: str):
        blocked = self.config.get("safety", "blocked_actions") or []
        blocked_set = {str(a).strip().lower() for a in blocked if str(a).strip()}
        action_name = f"power_tools.{action}".lower()
        if action_name in blocked_set:
            return {
                "success": False,
                "blocked": True,
                "error": "Action blocked by safety policy",
                "blocked_actions": [action_name],
            }
        return None

    # ── Power Management ──────────────────────────────────────────

    def shutdown(self, query="", **kwargs):
        """Shutdown the PC (with optional delay in seconds)."""
        guard = self._blocked_power_action("shutdown")
        if guard:
            return guard
        delay = 30
        if query and query.strip().isdigit():
            delay = int(query.strip())
        subprocess.Popen(f"shutdown /s /t {delay}", shell=True)
        return {"success": True, "result": f"⏻ Shutting down in {delay} seconds... (type 'cancel shutdown' to abort)"}

    def restart(self, **kwargs):
        """Restart the PC."""
        guard = self._blocked_power_action("restart")
        if guard:
            return guard
        subprocess.Popen("shutdown /r /t 10", shell=True)
        return {"success": True, "result": "🔄 Restarting in 10 seconds... (type 'cancel shutdown' to abort)"}

    def sleep_pc(self, **kwargs):
        """Put PC to sleep."""
        guard = self._blocked_power_action("sleep")
        if guard:
            return guard
        subprocess.Popen("rundll32.exe powrprof.dll,SetSuspendState 0,1,0", shell=True)
        return {"success": True, "result": "😴 Going to sleep..."}

    def hibernate(self, **kwargs):
        """Hibernate the PC."""
        guard = self._blocked_power_action("hibernate")
        if guard:
            return guard
        subprocess.Popen("shutdown /h", shell=True)
        return {"success": True, "result": "❄️ Hibernating..."}

    def logoff(self, **kwargs):
        """Log off current user."""
        guard = self._blocked_power_action("logoff")
        if guard:
            return guard
        subprocess.Popen("shutdown /l", shell=True)
        return {"success": True, "result": "👋 Logging off..."}

    def cancel_shutdown(self, **kwargs):
        """Cancel a scheduled shutdown/restart."""
        subprocess.Popen("shutdown /a", shell=True)
        return {"success": True, "result": "✅ Shutdown/restart cancelled"}

    # ── Quick Notes ───────────────────────────────────────────────

    def note_save(self, query="", **kwargs):
        """Save a quick note."""
        if not query:
            return {"success": False, "error": "What should I save?"}

        notes = self._load_notes()
        notes.append({
            "text": query,
            "time": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "id": len(notes) + 1,
        })
        self._save_notes(notes)
        return {"success": True, "result": f"📝 Note #{len(notes)} saved: {query[:80]}"}

    def note_list(self, **kwargs):
        """List all saved notes."""
        notes = self._load_notes()
        if not notes:
            return {"success": True, "result": "📭 No notes saved yet. Try: note save <your text>"}

        lines = ["📝 Your Notes:\n"]
        for i, note in enumerate(notes, 1):
            lines.append(f"  #{i}  [{note['time']}]  {note['text'][:60]}")
        return {"success": True, "result": "\n".join(lines)}

    def note_read(self, query="", **kwargs):
        """Read a specific note."""
        notes = self._load_notes()
        try:
            idx = int(query.strip()) - 1
            note = notes[idx]
            return {"success": True, "result": f"📝 Note #{idx+1} ({note['time']}):\n{note['text']}"}
        except (ValueError, IndexError):
            return {"success": False, "error": f"Note not found. You have {len(notes)} notes."}

    def note_delete(self, query="", **kwargs):
        """Delete a note by number."""
        notes = self._load_notes()
        try:
            idx = int(query.strip()) - 1
            removed = notes.pop(idx)
            self._save_notes(notes)
            return {"success": True, "result": f"🗑️ Deleted note: {removed['text'][:50]}"}
        except (ValueError, IndexError):
            return {"success": False, "error": f"Note not found. You have {len(notes)} notes."}

    def _load_notes(self):
        notes_file = NOTES_DIR / "notes.json"
        if notes_file.exists():
            return json.loads(notes_file.read_text())
        return []

    def _save_notes(self, notes):
        notes_file = NOTES_DIR / "notes.json"
        notes_file.write_text(json.dumps(notes, indent=2))

    # ── Timer ─────────────────────────────────────────────────────

    def set_timer(self, query="", **kwargs):
        """Set a countdown timer."""
        if not query:
            return {"success": False, "error": "How many seconds? e.g. timer 300"}

        parts = query.strip().split(None, 1)
        try:
            seconds = int(parts[0])
        except ValueError:
            return {"success": False, "error": "Give me a number of seconds, e.g. timer 60"}

        message = parts[1] if len(parts) > 1 else "Timer done!"

        def _timer_thread():
            time.sleep(seconds)
            # Show Windows toast notification
            try:
                from plyer import notification
                notification.notify(
                    title="⏰ NEXUS Timer",
                    message=message,
                    timeout=10,
                )
            except ImportError:
                # Fallback: use PowerShell toast
                ps_cmd = f'powershell -Command "Add-Type -AssemblyName System.Windows.Forms; [System.Windows.Forms.MessageBox]::Show(\'{message}\', \'NEXUS Timer\', \'OK\', \'Information\')"'
                subprocess.Popen(ps_cmd, shell=True)
            log.info(f"⏰ Timer fired: {message}")

        t = threading.Thread(target=_timer_thread, daemon=True)
        t.start()

        if seconds >= 60:
            display = f"{seconds // 60}m {seconds % 60}s"
        else:
            display = f"{seconds}s"
        return {"success": True, "result": f"⏰ Timer set for {display}: {message}"}

    # ── System Utilities ──────────────────────────────────────────

    def wifi_info(self, **kwargs):
        """Show current Wi-Fi connection info."""
        try:
            result = subprocess.run(
                "netsh wlan show interfaces",
                capture_output=True, text=True, shell=True
            )
            lines = result.stdout.strip().split("\n")
            info = {}
            for line in lines:
                if ":" in line:
                    key, _, val = line.partition(":")
                    key = key.strip()
                    val = val.strip()
                    if key in ["SSID", "Signal", "Radio type", "Band", "Channel",
                               "Receive rate (Mbps)", "Transmit rate (Mbps)", "State"]:
                        info[key] = val

            if not info:
                return {"success": True, "result": "📡 Not connected to Wi-Fi"}

            lines_out = ["📡 Wi-Fi Connection:\n"]
            for k, v in info.items():
                lines_out.append(f"  {k}: {v}")
            return {"success": True, "result": "\n".join(lines_out)}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def ip_address(self, **kwargs):
        """Show local and public IP address."""
        import socket
        try:
            # Local IP
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            local_ip = s.getsockname()[0]
            s.close()

            # Public IP
            try:
                import urllib.request
                public_ip = urllib.request.urlopen("https://api.ipify.org", timeout=3).read().decode()
            except Exception:
                public_ip = "unavailable"

            return {
                "success": True,
                "result": f"🌐 IP Address:\n  Local:  {local_ip}\n  Public: {public_ip}"
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    def uptime(self, **kwargs):
        """Show system uptime."""
        try:
            uptime_ms = ctypes.windll.kernel32.GetTickCount64()
            seconds = uptime_ms // 1000
            hours = seconds // 3600
            minutes = (seconds % 3600) // 60
            return {
                "success": True,
                "result": f"⏱️ System uptime: {hours}h {minutes}m"
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    def night_light(self, **kwargs):
        """Toggle Windows night light."""
        try:
            subprocess.Popen(
                'powershell -Command "Start-Process ms-settings:nightlight"',
                shell=True
            )
            return {"success": True, "result": "🌙 Night light settings opened"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def pin_window(self, **kwargs):
        """Toggle always-on-top for current window."""
        try:
            import pyautogui
            # Use AutoHotkey-style approach via PowerShell
            ps = '''
            Add-Type @"
            using System;
            using System.Runtime.InteropServices;
            public class WinAPI {
                [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
                [DllImport("user32.dll")] public static extern bool SetWindowPos(IntPtr hWnd, IntPtr hWndInsertAfter, int X, int Y, int cx, int cy, uint uFlags);
            }
"@
            $hwnd = [WinAPI]::GetForegroundWindow()
            $HWND_TOPMOST = [IntPtr]::new(-1)
            [WinAPI]::SetWindowPos($hwnd, $HWND_TOPMOST, 0, 0, 0, 0, 0x0003)
            '''
            subprocess.run(["powershell", "-Command", ps], capture_output=True)
            return {"success": True, "result": "📌 Window pinned on top"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def empty_recycle(self, **kwargs):
        """Empty the recycle bin."""
        try:
            ps = 'Clear-RecycleBin -Force -ErrorAction SilentlyContinue'
            subprocess.run(["powershell", "-Command", ps], capture_output=True)
            return {"success": True, "result": "🗑️ Recycle bin emptied"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def open_favorite(self, query="", **kwargs):
        """Open a favorite app by name."""
        FAVORITES = {
            "spotify": "spotify:",
            "discord": "discord:",
            "whatsapp": "https://web.whatsapp.com",
            "telegram": "https://web.telegram.org",
            "twitter": "https://x.com",
            "x": "https://x.com",
            "instagram": "https://instagram.com",
            "reddit": "https://reddit.com",
            "github": "https://github.com",
            "chatgpt": "https://chat.openai.com",
            "gemini": "https://gemini.google.com",
            "youtube": "https://youtube.com",
            "gmail": "https://mail.google.com",
            "drive": "https://drive.google.com",
            "maps": "https://maps.google.com",
            "netflix": "https://netflix.com",
            "calculator": "calc.exe",
            "notepad": "notepad.exe",
            "paint": "mspaint.exe",
            "explorer": "explorer.exe",
            "settings": "ms-settings:",
            "store": "ms-windows-store:",
            "camera": "microsoft.windows.camera:",
            "snip": "ms-screenclip:",
        }

        if not query:
            apps = ", ".join(sorted(FAVORITES.keys()))
            return {"success": True, "result": f"🚀 Available favorites:\n{apps}"}

        app = query.strip().lower()
        target = FAVORITES.get(app)

        if not target:
            # Try fuzzy match
            for name, url in FAVORITES.items():
                if app in name or name in app:
                    target = url
                    app = name
                    break

        if target:
            import webbrowser
            if target.endswith(".exe"):
                subprocess.Popen(target, shell=True)
            else:
                webbrowser.open(target)
            return {"success": True, "result": f"🚀 Opening {app.title()}"}
        else:
            return {"success": False, "error": f"Unknown app: {query}. Type 'open fav' to see available apps."}
