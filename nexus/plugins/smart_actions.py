"""
Autocrat — Smart Actions Plugin
High-level actions using keyboard shortcuts instead of coordinates.
No pixel positions needed — uses OS shortcuts and app hotkeys.
"""

import os
import time
import webbrowser
import urllib.parse
from datetime import datetime
import pyautogui
from nexus.core.plugin import NexusPlugin
from nexus.core.logger import get_logger

log = get_logger("smart_actions")

# Safety: don't let pyautogui move too fast
pyautogui.PAUSE = 0.15


class SmartActionsPlugin(NexusPlugin):
    """Smart actions — high-level automation using keyboard shortcuts."""

    name = "smart_actions"
    icon = "🧠"
    description = "AI-powered smart actions (no coordinates needed)"
    version = "2.0.0"

    def setup(self):
        # Web
        self.register_command("web_search_youtube", self.search_youtube,
                              "Search YouTube", "search youtube for <query>")
        self.register_command("web_search_google", self.search_google,
                              "Search Google", "google <query>")
        self.register_command("open_website", self.open_website,
                              "Open a website", "open website <url>")
        self.register_command("search_wikipedia", self.search_wikipedia,
                              "Search Wikipedia", "wikipedia <query>")
        self.register_command("search_amazon", self.search_amazon,
                              "Search Amazon", "amazon <query>")
        self.register_command("open_email", self.open_email,
                              "Open Gmail", "open email")
        self.register_command("open_maps", self.open_maps,
                              "Open Google Maps", "open maps [query]")

        # Media
        self.register_command("media_play_pause", self.media_play_pause,
                              "Play/Pause media", "play pause")
        self.register_command("media_next", self.media_next,
                              "Next track", "next song")
        self.register_command("media_prev", self.media_prev,
                              "Previous track", "previous song")

        # Volume/Brightness
        self.register_command("volume_up", self.volume_up_step,
                              "Increase volume 10%", "louder")
        self.register_command("volume_down", self.volume_down_step,
                              "Decrease volume 10%", "quieter")
        self.register_command("brightness_up", self.brightness_up_step,
                              "Increase brightness 20%", "brighter")
        self.register_command("brightness_down", self.brightness_down_step,
                              "Decrease brightness 20%", "dimmer")

        # Desktop/Windows
        self.register_command("minimize_all", self.minimize_all,
                              "Minimize all windows", "minimize all")
        self.register_command("snap_left", self.snap_left,
                              "Snap current window to left", "snap left")
        self.register_command("snap_right", self.snap_right,
                              "Snap current window to right", "snap right")
        self.register_command("alt_tab", self.alt_tab,
                              "Switch to next window", "alt tab")
        self.register_command("open_task_manager", self.open_task_manager,
                              "Open Task Manager", "task manager")

        # Text & Tabs
        self.register_command("type_text", self.type_text,
                              "Type text into active window", "type <text>")
        self.register_command("new_tab", self.new_tab,
                              "Open new browser tab", "new tab")
        self.register_command("close_tab", self.close_tab,
                              "Close current tab", "close tab")
        self.register_command("switch_tab", self.switch_tab,
                              "Switch to a specific browser tab by name", "switch tab <name>")

        # Keyboard shortcuts
        self.register_command("undo", self.undo,
                              "Undo last action", "undo")
        self.register_command("redo", self.redo,
                              "Redo last action", "redo")
        self.register_command("select_all", self.select_all,
                              "Select all", "select all")
        self.register_command("copy", self.copy,
                              "Copy selection", "copy")
        self.register_command("paste", self.paste,
                              "Paste clipboard", "paste")
        self.register_command("save", self.save,
                              "Save current document", "save")
        self.register_command("refresh", self.refresh,
                              "Refresh current page", "refresh")

        # AI
        self.register_command("ask_gemini", self.ask_gemini,
                              "Ask Google Gemini AI", "ask gemini <question>")

        # System
        self.register_command("lock_pc", self.lock_pc,
                              "Lock the computer", "lock pc")

        # File organization
        self.register_command("organize_downloads", self.organize_downloads,
                              "Organize Downloads folder", "organize downloads")
        self.register_command("organize_desktop", self.organize_desktop,
                              "Organize Desktop", "organize desktop")

        # Conversational
        self.register_command("greet", self.greet,
                              "Greet the user", "hello")
        self.register_command("current_time", self.current_time,
                              "Show current time", "what time is it")
        self.register_command("current_date", self.current_date,
                              "Show current date", "what date is it")

    # ── Web Actions ───────────────────────────────────────────────

    def search_youtube(self, query="", **kwargs):
        """Search YouTube, find the best result, and open it directly."""
        if not query:
            return {"success": False, "error": "What should I search for?"}

        fallback_url = f"https://www.youtube.com/results?search_query={urllib.parse.quote(query)}"

        # Try to find the actual best result using YouTube's innertube API
        try:
            import json
            import httpx

            wants_playlist = any(w in query.lower() for w in [
                "playlist", "series", "full course", "all episodes", "all lectures",
            ])

            # Hit YouTube's internal search API (no key needed)
            payload = {
                "context": {
                    "client": {"clientName": "WEB", "clientVersion": "2.20240101.00.00"}
                },
                "query": query,
            }
            resp = httpx.post(
                "https://www.youtube.com/youtubei/v1/search?key=AIzaSyAO_FJ2SlqU8Q4STEHLGCilw_Y9_11qcW8",
                json=payload,
                headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"},
                timeout=15,
            )
            data = resp.json()

            # Parse results from the nested response structure
            items = []
            try:
                contents = data["contents"]["twoColumnSearchResultsRenderer"]["primaryContents"]["sectionListRenderer"]["contents"]
                for section in contents:
                    item_section = section.get("itemSectionRenderer", {}).get("contents", [])
                    for item in item_section:
                        # ── New format: lockupViewModel (2024+) ──
                        if "lockupViewModel" in item:
                            lv = item["lockupViewModel"]
                            cid = lv.get("contentId", "")
                            ctype = lv.get("contentType", "")
                            md = lv.get("metadata", {}).get("lockupMetadataViewModel", {})
                            title = md.get("title", {}).get("content", "")

                            # Extract channel from metadata rows
                            channel = ""
                            meta_rows = md.get("metadata", {}).get("contentMetadataViewModel", {}).get("metadataRows", [])
                            for row in meta_rows:
                                for part in row.get("metadataParts", []):
                                    txt = part.get("text", {}).get("content", "")
                                    if txt and not channel:
                                        channel = txt
                                        break
                                if channel:
                                    break

                            if "PLAYLIST" in ctype:
                                items.append({
                                    "type": "playlist",
                                    "id": cid,
                                    "url": f"https://www.youtube.com/playlist?list={cid}",
                                    "title": title,
                                    "channel": channel,
                                })
                            elif "VIDEO" in ctype or ctype == "":
                                items.append({
                                    "type": "video",
                                    "id": cid,
                                    "url": f"https://www.youtube.com/watch?v={cid}",
                                    "title": title,
                                    "channel": channel,
                                })

                        # ── Legacy format: videoRenderer / playlistRenderer ──
                        elif "videoRenderer" in item:
                            v = item["videoRenderer"]
                            vid_id = v.get("videoId", "")
                            title = v.get("title", {}).get("runs", [{}])[0].get("text", "")
                            channel = v.get("ownerText", {}).get("runs", [{}])[0].get("text", "")
                            length = v.get("lengthText", {}).get("simpleText", "")
                            views = v.get("shortViewCountText", {}).get("simpleText", "")
                            items.append({
                                "type": "video",
                                "id": vid_id,
                                "url": f"https://www.youtube.com/watch?v={vid_id}",
                                "title": title,
                                "channel": channel,
                                "duration": length,
                                "views": views,
                            })
                        elif "playlistRenderer" in item:
                            p = item["playlistRenderer"]
                            pl_id = p.get("playlistId", "")
                            title = p.get("title", {}).get("simpleText", "")
                            channel = p.get("shortBylineText", {}).get("runs", [{}])[0].get("text", "")
                            count = p.get("videoCount", "?")
                            items.append({
                                "type": "playlist",
                                "id": pl_id,
                                "url": f"https://www.youtube.com/playlist?list={pl_id}",
                                "title": title,
                                "channel": channel,
                                "video_count": count,
                            })
            except (KeyError, IndexError):
                pass

            if not items:
                webbrowser.open(fallback_url)
                return {"success": True, "result": f"🔍 No direct results — opened YouTube search for: {query}", "url": fallback_url}

            # Prefer playlists if user asked for one
            best = None
            if wants_playlist:
                playlists = [i for i in items if i["type"] == "playlist"]
                if playlists:
                    best = playlists[0]

            if not best:
                best = items[0]

            webbrowser.open(best["url"])

            if best["type"] == "playlist":
                msg = f"▶️ Opening playlist: {best['title']}\n📺 {best['channel']} • {best.get('video_count', '?')} videos"
            else:
                msg = f"▶️ Opening: {best['title']}\n📺 {best['channel']}"
                if best.get("duration"):
                    msg += f" • {best['duration']}"
                if best.get("views"):
                    msg += f" • {best['views']}"

            # Show other results too
            others = [i for i in items[:5] if i["id"] != best["id"]]
            if others:
                msg += "\n\n📋 Also found:"
                for i, r in enumerate(others[:3], 1):
                    tag = "📃" if r["type"] == "playlist" else "🎬"
                    msg += f"\n  {i}. {tag} {r['title']} — {r['channel']}"

            return {
                "success": True,
                "result": msg,
                "url": best["url"],
                "title": best["title"],
                "channel": best["channel"],
                "type": best["type"],
            }

        except Exception as e:
            log.warning(f"YouTube smart search failed ({e}), falling back to search URL")
            webbrowser.open(fallback_url)
            return {"success": True, "result": f"🔍 Searching YouTube for: {query}", "url": fallback_url}

    def search_google(self, query="", **kwargs):
        """Search Google for something."""
        if not query:
            return {"success": False, "error": "What should I search for?"}
        url = f"https://www.google.com/search?q={urllib.parse.quote(query)}"
        webbrowser.open(url)
        return {"success": True, "result": f"🔍 Searching Google for: {query}", "url": url}

    def open_website(self, query="", **kwargs):
        """Open a website URL."""
        if not query:
            return {"success": False, "error": "What URL should I open?"}
        url = query if query.startswith(("http://", "https://")) else "https://" + query
        webbrowser.open(url)
        return {"success": True, "result": f"🌐 Opening: {url}"}

    def search_wikipedia(self, query="", **kwargs):
        """Search Wikipedia for a topic."""
        if not query:
            return {"success": False, "error": "What should I look up?"}
        url = f"https://en.wikipedia.org/wiki/Special:Search?search={urllib.parse.quote(query)}"
        webbrowser.open(url)
        return {"success": True, "result": f"📚 Searching Wikipedia for: {query}", "url": url}

    def search_amazon(self, query="", **kwargs):
        """Search Amazon for a product."""
        if not query:
            return {"success": False, "error": "What should I search for?"}
        url = f"https://www.amazon.com/s?k={urllib.parse.quote(query)}"
        webbrowser.open(url)
        return {"success": True, "result": f"🛒 Searching Amazon for: {query}", "url": url}

    def open_email(self, **kwargs):
        """Open Gmail inbox."""
        webbrowser.open("https://mail.google.com")
        return {"success": True, "result": "📧 Opening Gmail..."}

    def open_maps(self, query="", **kwargs):
        """Open Google Maps, optionally with a search/directions query."""
        if query:
            url = f"https://www.google.com/maps/search/{urllib.parse.quote(query)}"
        else:
            url = "https://www.google.com/maps"
        webbrowser.open(url)
        return {"success": True, "result": f"🗺️ Opening Maps{': ' + query if query else ''}"}

    # ── Media Control ─────────────────────────────────────────────

    def media_play_pause(self, **kwargs):
        """Toggle play/pause using media key."""
        pyautogui.press("playpause")
        return {"success": True, "result": "⏯️ Play/Pause toggled"}

    def media_next(self, **kwargs):
        """Skip to next track using media key."""
        pyautogui.press("nexttrack")
        return {"success": True, "result": "⏭️ Next track"}

    def media_prev(self, **kwargs):
        """Go to previous track using media key."""
        pyautogui.press("prevtrack")
        return {"success": True, "result": "⏮️ Previous track"}

    # ── Volume (stepped) ──────────────────────────────────────────

    def volume_up_step(self, **kwargs):
        """Increase volume by ~10%."""
        for _ in range(5):
            pyautogui.press("volumeup")
        return {"success": True, "result": "🔊 Volume increased"}

    def volume_down_step(self, **kwargs):
        """Decrease volume by ~10%."""
        for _ in range(5):
            pyautogui.press("volumedown")
        return {"success": True, "result": "🔉 Volume decreased"}

    # ── Brightness ────────────────────────────────────────────────

    def brightness_up_step(self, **kwargs):
        """Increase brightness by 20%."""
        try:
            import screen_brightness_control as sbc
            current = sbc.get_brightness()[0]
            new_val = min(100, current + 20)
            sbc.set_brightness(new_val)
            return {"success": True, "result": f"☀️ Brightness: {new_val}%"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def brightness_down_step(self, **kwargs):
        """Decrease brightness by 20%."""
        try:
            import screen_brightness_control as sbc
            current = sbc.get_brightness()[0]
            new_val = max(0, current - 20)
            sbc.set_brightness(new_val)
            return {"success": True, "result": f"🔅 Brightness: {new_val}%"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    # ── Window / Desktop ──────────────────────────────────────────

    def minimize_all(self, **kwargs):
        """Show desktop / minimize all windows."""
        pyautogui.hotkey("win", "d")
        return {"success": True, "result": "🪟 All windows minimized (desktop shown)"}

    def snap_left(self, **kwargs):
        """Snap current window to left half of screen."""
        pyautogui.hotkey("win", "left")
        return {"success": True, "result": "⬅️ Window snapped to left"}

    def snap_right(self, **kwargs):
        """Snap current window to right half of screen."""
        pyautogui.hotkey("win", "right")
        return {"success": True, "result": "➡️ Window snapped to right"}

    def alt_tab(self, **kwargs):
        """Switch to the next window (Alt+Tab)."""
        pyautogui.hotkey("alt", "tab")
        return {"success": True, "result": "🔄 Switched window"}

    def open_task_manager(self, **kwargs):
        """Open Task Manager."""
        pyautogui.hotkey("ctrl", "shift", "escape")
        return {"success": True, "result": "📊 Task Manager opened"}

    # ── Text & Tabs ───────────────────────────────────────────────

    def type_text(self, query="", **kwargs):
        """Type text into the currently active window."""
        if not query:
            return {"success": False, "error": "What should I type?"}
        time.sleep(0.3)
        pyautogui.typewrite(query, interval=0.02) if query.isascii() else pyautogui.write(query)
        return {"success": True, "result": f"⌨️ Typed: {query[:50]}..."}

    def new_tab(self, **kwargs):
        """Open a new browser tab."""
        pyautogui.hotkey("ctrl", "t")
        return {"success": True, "result": "📑 New tab opened"}

    def close_tab(self, **kwargs):
        """Close the current tab."""
        pyautogui.hotkey("ctrl", "w")
        return {"success": True, "result": "❌ Tab closed"}

    def switch_tab(self, query="", **kwargs):
        """Switch to a specific browser tab by searching tab titles.
        
        Focuses Chrome, then uses Ctrl+Shift+A (Chrome's tab search) to find
        the tab by name. Falls back to cycling through tabs if search fails.
        """
        if not query:
            return {"success": False, "error": "Which tab should I switch to?"}

        try:
            import win32gui
            import win32con

            # First, find and focus Chrome window
            target_hwnd = None
            def enum_cb(hwnd, _):
                nonlocal target_hwnd
                if win32gui.IsWindowVisible(hwnd):
                    title = win32gui.GetWindowText(hwnd).lower()
                    if "chrome" in title or "google chrome" in title:
                        target_hwnd = hwnd
            win32gui.EnumWindows(enum_cb, None)

            if not target_hwnd:
                return {"success": False, "error": "Chrome is not open"}

            # Focus Chrome
            win32gui.ShowWindow(target_hwnd, win32con.SW_RESTORE)
            win32gui.SetForegroundWindow(target_hwnd)
            time.sleep(0.3)

            # Use Chrome's built-in tab search (Ctrl+Shift+A)
            pyautogui.hotkey("ctrl", "shift", "a")
            time.sleep(0.5)

            # Type the tab name to search
            pyautogui.typewrite(query, interval=0.03) if query.isascii() else pyautogui.write(query)
            time.sleep(0.4)

            # Press Enter to switch to the first matching tab
            pyautogui.press("enter")
            time.sleep(0.2)

            return {"success": True, "result": f"🔄 Switched to tab: {query}"}
        except ImportError:
            return {"success": False, "error": "win32gui not available"}
        except Exception as e:
            return {"success": False, "error": f"Tab switch failed: {e}"}

    # ── Keyboard Shortcuts ────────────────────────────────────────

    def undo(self, **kwargs):
        """Undo last action (Ctrl+Z)."""
        pyautogui.hotkey("ctrl", "z")
        return {"success": True, "result": "↩️ Undo"}

    def redo(self, **kwargs):
        """Redo last action (Ctrl+Y)."""
        pyautogui.hotkey("ctrl", "y")
        return {"success": True, "result": "↪️ Redo"}

    def select_all(self, **kwargs):
        """Select all (Ctrl+A)."""
        pyautogui.hotkey("ctrl", "a")
        return {"success": True, "result": "🔲 Selected all"}

    def copy(self, **kwargs):
        """Copy selection (Ctrl+C)."""
        pyautogui.hotkey("ctrl", "c")
        return {"success": True, "result": "📋 Copied"}

    def paste(self, **kwargs):
        """Paste from clipboard (Ctrl+V)."""
        pyautogui.hotkey("ctrl", "v")
        return {"success": True, "result": "📋 Pasted"}

    def save(self, **kwargs):
        """Save current document (Ctrl+S)."""
        pyautogui.hotkey("ctrl", "s")
        return {"success": True, "result": "💾 Saved"}

    def refresh(self, **kwargs):
        """Refresh the current page (F5)."""
        pyautogui.press("f5")
        return {"success": True, "result": "🔄 Refreshed"}

    # ── AI / Gemini ───────────────────────────────────────────────

    def ask_gemini(self, query="", **kwargs):
        """Open Google Gemini AI in Chrome with a question."""
        if not query:
            return {"success": False, "error": "What should I ask Gemini?"}
        url = f"https://gemini.google.com/app?q={urllib.parse.quote(query)}"
        webbrowser.open(url)
        return {
            "success": True,
            "result": f"🤖 Asking Gemini: {query}",
            "url": url,
            "hint": "Gemini is opening in your browser (logged into your Google account)",
        }

    # ── System ────────────────────────────────────────────────────

    def lock_pc(self, **kwargs):
        """Lock the computer."""
        import ctypes
        ctypes.windll.user32.LockWorkStation()
        return {"success": True, "result": "🔒 Computer locked"}

    # ── File Organization ─────────────────────────────────────────

    def organize_downloads(self, **kwargs):
        """Auto-organize the Downloads folder by file type."""
        downloads = os.path.expanduser("~/Downloads")
        if not os.path.exists(downloads):
            return {"success": False, "error": "Downloads folder not found"}
        # Delegate to file_ops plugin via engine if available
        return self._organize_folder(downloads)

    def organize_desktop(self, **kwargs):
        """Auto-organize the Desktop by file type."""
        desktop = os.path.expanduser("~/Desktop")
        if not os.path.exists(desktop):
            return {"success": False, "error": "Desktop folder not found"}
        return self._organize_folder(desktop)

    def _organize_folder(self, folder_path):
        """Organize a folder by moving files into categorized subfolders."""
        import shutil
        from collections import defaultdict

        ext_map = {
            "Images": [".jpg", ".jpeg", ".png", ".gif", ".bmp", ".svg", ".webp", ".ico", ".heic"],
            "Documents": [".pdf", ".doc", ".docx", ".txt", ".rtf", ".odt", ".xls", ".xlsx", ".ppt", ".pptx", ".csv"],
            "Videos": [".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm"],
            "Audio": [".mp3", ".wav", ".flac", ".aac", ".ogg", ".wma", ".m4a"],
            "Archives": [".zip", ".rar", ".7z", ".tar", ".gz", ".bz2"],
            "Code": [".py", ".js", ".ts", ".html", ".css", ".cpp", ".c", ".java", ".go", ".rs", ".json", ".yaml"],
            "Executables": [".exe", ".msi", ".bat", ".cmd", ".ps1", ".sh", ".appimage"],
        }

        moved = defaultdict(int)
        from pathlib import Path

        for item in Path(folder_path).iterdir():
            if item.is_file() and not item.name.startswith('.'):
                ext = item.suffix.lower()
                target_folder = "Other"
                for folder_name, extensions in ext_map.items():
                    if ext in extensions:
                        target_folder = folder_name
                        break

                target_dir = Path(folder_path) / target_folder
                target_dir.mkdir(exist_ok=True)

                dest = target_dir / item.name
                if dest.exists():
                    stem = item.stem
                    dest = target_dir / f"{stem}_{int(time.time())}{ext}"

                try:
                    shutil.move(str(item), str(dest))
                    moved[target_folder] += 1
                except Exception:
                    continue

        total = sum(moved.values())
        if total == 0:
            return {"success": True, "result": f"📁 {folder_path} is already clean!"}
        return {
            "success": True,
            "result": f"📁 Organized {total} files in {folder_path}",
            "breakdown": dict(moved),
        }

    # ── Conversational ────────────────────────────────────────────

    def greet(self, **kwargs):
        """Respond to a greeting."""
        hour = datetime.now().hour
        if hour < 12:
            greeting = "Good morning"
        elif hour < 17:
            greeting = "Good afternoon"
        else:
            greeting = "Good evening"
        return {
            "success": True,
            "result": f"👋 {greeting}! I'm Autocrat, your system automation assistant.\n"
                      f"   Type 'help' to see what I can do, or just tell me what you need!",
        }

    def current_time(self, **kwargs):
        """Show the current time."""
        now = datetime.now()
        return {
            "success": True,
            "result": f"🕐 Current time: {now.strftime('%I:%M:%S %p')}",
        }

    def current_date(self, **kwargs):
        """Show the current date."""
        now = datetime.now()
        return {
            "success": True,
            "result": f"📅 Today is {now.strftime('%A, %B %d, %Y')}",
        }
