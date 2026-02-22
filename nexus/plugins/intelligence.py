"""
NEXUS OS — Intelligence Plugin
Smart contextual actions: browser history search, recent Gemini chats,
system diagnostics, quick math, recent files, smart search, and more.
"""

import os
import re
import sqlite3
import shutil
import glob
import math
import json
import time
import webbrowser
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, List, Dict
from nexus.core.plugin import NexusPlugin
from nexus.core.logger import get_logger

log = get_logger("intelligence")


class IntelligencePlugin(NexusPlugin):
    """Smart contextual commands — the brain behind the brain."""

    name = "intelligence"
    icon = "🧪"
    description = "Smart actions: browser history, Gemini chats, diagnostics, math, recent files"
    version = "1.0.0"

    def setup(self):
        # Browser / Gemini
        self.register_command("gemini_chats", self.open_gemini_chats,
                              "Open previous Gemini chats", "gemini chats")
        self.register_command("gemini_last", self.open_gemini_last,
                              "Open last Gemini conversation", "last gemini chat")
        self.register_command("browser_history", self.browser_history,
                              "Search browser history", "browser history [query]")
        self.register_command("recent_tabs", self.recent_tabs,
                              "Show recently visited sites", "recent tabs")
        self.register_command("open_recent_site", self.open_recent_site,
                              "Open a recently visited site by keyword", "open recent <keyword>")

        # Smart Diagnostics
        self.register_command("why_slow", self.why_slow,
                              "Diagnose why PC is slow", "why is my pc slow")
        self.register_command("health_check", self.health_check,
                              "Full system health check with recommendations", "health check")
        self.register_command("disk_hogs", self.disk_hogs,
                              "Find largest files eating disk space", "disk hogs [path]")
        self.register_command("startup_programs", self.startup_programs,
                              "List startup programs slowing boot", "startup programs")

        # Quick Math & Conversions
        self.register_command("calculate", self.calculate,
                              "Quick math calculation", "calc <expression>")
        self.register_command("convert", self.convert_units,
                              "Unit conversion", "convert <value> <from> to <to>")

        # Recent Files
        self.register_command("recent_files", self.recent_files,
                              "Show recently modified files", "recent files [folder]")
        self.register_command("large_files", self.large_files,
                              "Find large files", "large files [folder]")
        self.register_command("duplicate_files", self.find_duplicates,
                              "Find duplicate files by name", "duplicate files [folder]")
        self.register_command("search_files", self.search_files,
                              "Smart search for files by name", "search file <name>")

        # Context-Aware
        self.register_command("open_last_download", self.open_last_download,
                              "Open the most recent download", "open last download")
        self.register_command("open_last_screenshot", self.open_last_screenshot,
                              "Open the most recent screenshot", "open last screenshot")
        self.register_command("daily_summary", self.daily_summary,
                              "Quick summary of your day (time, battery, top apps)", "daily summary")

        # App-Smart
        self.register_command("open_chatgpt", self.open_chatgpt,
                              "Open ChatGPT", "open chatgpt")
        self.register_command("open_github", self.open_github,
                              "Open GitHub", "open github")
        self.register_command("open_stackoverflow", self.open_stackoverflow,
                              "Search StackOverflow", "stackoverflow <query>")
        self.register_command("open_reddit", self.open_reddit,
                              "Open Reddit or a subreddit", "reddit [subreddit]")
        self.register_command("define_word", self.define_word,
                              "Look up a word definition", "define <word>")
        self.register_command("translate", self.translate_text,
                              "Translate text via Google", "translate <text> to <lang>")

    # ══════════════════════════════════════════════════════════════
    # BROWSER / GEMINI
    # ══════════════════════════════════════════════════════════════

    def _get_chrome_history_db(self) -> Optional[str]:
        """Find Chrome's History database file."""
        paths = [
            os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\User Data\Default\History"),
            os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\User Data\Profile 1\History"),
        ]
        for p in paths:
            if os.path.exists(p):
                return p
        return None

    def _get_edge_history_db(self) -> Optional[str]:
        """Find Edge's History database file."""
        paths = [
            os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\Edge\User Data\Default\History"),
            os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\Edge\User Data\Profile 1\History"),
        ]
        for p in paths:
            if os.path.exists(p):
                return p
        return None

    def _query_browser_history(self, query: str = "", limit: int = 20) -> List[Dict]:
        """Query Chrome or Edge history database."""
        results = []
        for db_finder in [self._get_chrome_history_db, self._get_edge_history_db]:
            db_path = db_finder()
            if not db_path:
                continue
            # Copy DB since Chrome locks it
            tmp = db_path + ".nexus_tmp"
            try:
                shutil.copy2(db_path, tmp)
                conn = sqlite3.connect(tmp)
                cursor = conn.cursor()
                if query:
                    cursor.execute(
                        "SELECT url, title, visit_count, last_visit_time FROM urls "
                        "WHERE (title LIKE ? OR url LIKE ?) "
                        "ORDER BY last_visit_time DESC LIMIT ?",
                        (f"%{query}%", f"%{query}%", limit)
                    )
                else:
                    cursor.execute(
                        "SELECT url, title, visit_count, last_visit_time FROM urls "
                        "ORDER BY last_visit_time DESC LIMIT ?",
                        (limit,)
                    )
                for url, title, visits, last_visit in cursor.fetchall():
                    # Chrome timestamps are microseconds since 1601-01-01
                    try:
                        ts = datetime(1601, 1, 1) + timedelta(microseconds=last_visit)
                        time_str = ts.strftime("%Y-%m-%d %H:%M")
                    except Exception:
                        time_str = "?"
                    results.append({
                        "title": title or "(untitled)",
                        "url": url,
                        "visits": visits,
                        "last_visited": time_str,
                    })
                conn.close()
            except Exception as e:
                log.warning(f"Browser history query failed: {e}")
            finally:
                try:
                    os.remove(tmp)
                except Exception:
                    pass
            if results:
                break
        return results

    def open_gemini_chats(self, **kwargs):
        """Open the Gemini app page which shows all previous chats."""
        webbrowser.open("https://gemini.google.com/app")
        return {
            "success": True,
            "result": "🤖 Opening Gemini — your previous chats are in the sidebar",
            "hint": "Click any conversation in the left sidebar to continue it",
        }

    def open_gemini_last(self, **kwargs):
        """Open the most recent Gemini conversation from browser history."""
        history = self._query_browser_history("gemini.google.com/app", limit=10)
        gemini_chats = [h for h in history if "gemini.google.com/app" in h["url"]
                        and h["url"] != "https://gemini.google.com/app"]
        if gemini_chats:
            last = gemini_chats[0]
            webbrowser.open(last["url"])
            return {
                "success": True,
                "result": f"🤖 Opening your last Gemini chat:\n   \"{last['title']}\"\n   Last visited: {last['last_visited']}",
            }
        # Fallback: just open Gemini
        webbrowser.open("https://gemini.google.com/app")
        return {
            "success": True,
            "result": "🤖 Couldn't find a specific chat — opening Gemini main page\n   💡 Your chats are in the left sidebar",
        }

    def browser_history(self, query="", **kwargs):
        """Search browser history (Chrome/Edge)."""
        results = self._query_browser_history(query, limit=15)
        if not results:
            return {"success": True, "result": "No browser history found" + (f" for '{query}'" if query else "")}
        return {
            "success": True,
            "result": results,
            "hint": f"Found {len(results)} results" + (f" matching '{query}'" if query else ""),
        }

    def recent_tabs(self, **kwargs):
        """Show the 15 most recently visited sites."""
        results = self._query_browser_history("", limit=15)
        if not results:
            return {"success": True, "result": "No recent browsing history found"}
        lines = []
        for r in results:
            lines.append(f"  {r['last_visited']}  {r['title'][:50]}")
            lines.append(f"                  {r['url'][:70]}")
        return {"success": True, "result": "\n".join(lines)}

    def open_recent_site(self, query="", **kwargs):
        """Open a recently visited site matching a keyword."""
        if not query:
            return {"success": False, "error": "What site should I look for? e.g. 'open recent github'"}
        results = self._query_browser_history(query, limit=5)
        if not results:
            return {"success": False, "error": f"No recent sites found matching '{query}'"}
        best = results[0]
        webbrowser.open(best["url"])
        return {
            "success": True,
            "result": f"🌐 Opening: {best['title']}\n   {best['url']}",
        }

    # ══════════════════════════════════════════════════════════════
    # SMART DIAGNOSTICS
    # ══════════════════════════════════════════════════════════════

    def why_slow(self, **kwargs):
        """Diagnose why the PC is slow — check CPU, RAM, disk hogs."""
        import psutil

        issues = []
        recommendations = []

        # CPU check
        cpu = psutil.cpu_percent(interval=1.5)
        if cpu > 85:
            issues.append(f"🔴 CPU at {cpu}% — heavily loaded")
            # Find top CPU processes
            procs = sorted(psutil.process_iter(['name', 'cpu_percent']),
                          key=lambda p: p.info.get('cpu_percent', 0) or 0, reverse=True)[:5]
            hogs = [f"    {p.info['name']}: {p.info['cpu_percent']}%" for p in procs if (p.info.get('cpu_percent') or 0) > 5]
            if hogs:
                issues.append("  Top CPU consumers:\n" + "\n".join(hogs))
                recommendations.append(f"💡 Consider closing '{procs[0].info['name']}' to free CPU")
        elif cpu > 60:
            issues.append(f"🟡 CPU at {cpu}% — moderately busy")
        else:
            issues.append(f"🟢 CPU at {cpu}% — looks fine")

        # RAM check
        mem = psutil.virtual_memory()
        if mem.percent > 85:
            issues.append(f"🔴 RAM at {mem.percent}% — {mem.available / (1024**3):.1f} GB free")
            procs = sorted(psutil.process_iter(['name', 'memory_percent']),
                          key=lambda p: p.info.get('memory_percent', 0) or 0, reverse=True)[:5]
            hogs = [f"    {p.info['name']}: {(p.info['memory_percent'] or 0):.1f}%" for p in procs if (p.info.get('memory_percent') or 0) > 3]
            if hogs:
                issues.append("  Top RAM consumers:\n" + "\n".join(hogs))
            recommendations.append("💡 Close unused browser tabs and heavy apps")
        elif mem.percent > 70:
            issues.append(f"🟡 RAM at {mem.percent}% — {mem.available / (1024**3):.1f} GB free")
        else:
            issues.append(f"🟢 RAM at {mem.percent}% — plenty free")

        # Disk check
        for partition in psutil.disk_partitions():
            try:
                usage = psutil.disk_usage(partition.mountpoint)
                if usage.percent > 90:
                    issues.append(f"🔴 Disk {partition.device} at {usage.percent}% — only {usage.free / (1024**3):.1f} GB free!")
                    recommendations.append(f"💡 Free up space on {partition.device} — run 'organize downloads' or 'disk hogs'")
            except Exception:
                pass

        # Disk I/O check
        try:
            io1 = psutil.disk_io_counters()
            time.sleep(0.5)
            io2 = psutil.disk_io_counters()
            write_rate = (io2.write_bytes - io1.write_bytes) / 0.5 / (1024**2)
            if write_rate > 100:
                issues.append(f"🟡 High disk activity: {write_rate:.0f} MB/s writes")
                recommendations.append("💡 A program may be downloading or writing large files")
        except Exception:
            pass

        # Startup programs
        try:
            startup_count = len(self._get_startup_items())
            if startup_count > 15:
                recommendations.append(f"💡 {startup_count} startup programs — try disabling some in Task Manager > Startup")
        except Exception:
            pass

        result = "🔍 PC Slowness Diagnosis\n" + "=" * 40 + "\n\n"
        result += "\n".join(issues)
        if recommendations:
            result += "\n\n📋 Recommendations:\n" + "\n".join(recommendations)
        else:
            result += "\n\n✅ Everything looks healthy — your PC should be running fine!"

        return {"success": True, "result": result}

    def health_check(self, **kwargs):
        """Comprehensive system health check."""
        import psutil

        checks = []

        # CPU
        cpu = psutil.cpu_percent(interval=1)
        status = "🟢" if cpu < 60 else "🟡" if cpu < 85 else "🔴"
        checks.append(f"{status} CPU: {cpu}%")

        # RAM
        mem = psutil.virtual_memory()
        status = "🟢" if mem.percent < 70 else "🟡" if mem.percent < 85 else "🔴"
        checks.append(f"{status} RAM: {mem.percent}% ({mem.available / (1024**3):.1f} GB free)")

        # Disk
        for part in psutil.disk_partitions():
            try:
                u = psutil.disk_usage(part.mountpoint)
                status = "🟢" if u.percent < 75 else "🟡" if u.percent < 90 else "🔴"
                checks.append(f"{status} {part.device}: {u.percent}% ({u.free / (1024**3):.1f} GB free)")
            except Exception:
                pass

        # Battery
        bat = psutil.sensors_battery()
        if bat:
            status = "🟢" if bat.percent > 30 else "🟡" if bat.percent > 15 else "🔴"
            plug = "⚡ plugged in" if bat.power_plugged else "🔋 on battery"
            checks.append(f"{status} Battery: {bat.percent}% ({plug})")

        # Temperature (if available)
        try:
            temps = psutil.sensors_temperatures()
            if temps:
                for name, entries in temps.items():
                    for entry in entries:
                        if entry.current > 80:
                            checks.append(f"🔴 Temperature: {name} at {entry.current}°C — overheating!")
                        elif entry.current > 65:
                            checks.append(f"🟡 Temperature: {name} at {entry.current}°C — warm")
        except Exception:
            pass

        # Process count
        proc_count = len(list(psutil.process_iter()))
        status = "🟢" if proc_count < 200 else "🟡" if proc_count < 350 else "🔴"
        checks.append(f"{status} Running processes: {proc_count}")

        # Uptime
        boot = datetime.fromtimestamp(psutil.boot_time())
        uptime = datetime.now() - boot
        hours = uptime.total_seconds() / 3600
        if hours > 72:
            checks.append(f"🟡 Uptime: {hours:.0f}h — consider restarting")
        else:
            checks.append(f"🟢 Uptime: {hours:.0f}h")

        result = "🏥 System Health Check\n" + "=" * 40 + "\n\n" + "\n".join(checks)

        score = sum(1 for c in checks if c.startswith("🟢"))
        total = len(checks)
        result += f"\n\n📊 Score: {score}/{total} healthy"

        return {"success": True, "result": result}

    def disk_hogs(self, query="", **kwargs):
        """Find the largest files in a directory."""
        search_path = query or os.path.expanduser("~")
        if not os.path.exists(search_path):
            return {"success": False, "error": f"Path not found: {search_path}"}

        large_files = []
        try:
            for root, dirs, files in os.walk(search_path):
                # Skip system/hidden directories
                dirs[:] = [d for d in dirs if not d.startswith('.') and d not in
                          ['node_modules', '.git', '__pycache__', 'AppData', '$Recycle.Bin', 'Windows']]
                for f in files:
                    try:
                        fp = os.path.join(root, f)
                        size = os.path.getsize(fp)
                        if size > 50 * 1024 * 1024:  # >50MB
                            large_files.append((fp, size))
                    except (OSError, PermissionError):
                        continue
                if len(large_files) > 200:
                    break
        except Exception as e:
            return {"success": False, "error": str(e)}

        large_files.sort(key=lambda x: x[1], reverse=True)
        top = large_files[:20]

        if not top:
            return {"success": True, "result": f"No files larger than 50MB found in {search_path}"}

        lines = [f"📦 Largest files in {search_path}:", ""]
        for fp, size in top:
            if size > 1024**3:
                sz = f"{size / (1024**3):.1f} GB"
            else:
                sz = f"{size / (1024**2):.0f} MB"
            lines.append(f"  {sz:>10}  {fp}")

        return {"success": True, "result": "\n".join(lines)}

    def _get_startup_items(self) -> List[str]:
        """Get list of startup programs from registry."""
        items = []
        try:
            import winreg
            for hive, path in [
                (winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run"),
                (winreg.HKEY_LOCAL_MACHINE, r"Software\Microsoft\Windows\CurrentVersion\Run"),
            ]:
                try:
                    key = winreg.OpenKey(hive, path)
                    i = 0
                    while True:
                        try:
                            name, value, _ = winreg.EnumValue(key, i)
                            items.append(f"{name}: {value[:60]}")
                            i += 1
                        except OSError:
                            break
                    winreg.CloseKey(key)
                except Exception:
                    pass
        except ImportError:
            pass
        return items

    def startup_programs(self, **kwargs):
        """List startup programs that run when Windows boots."""
        items = self._get_startup_items()
        if not items:
            return {"success": True, "result": "No startup programs found (or unable to read registry)"}
        result = f"🚀 Startup Programs ({len(items)} found):\n" + "=" * 40 + "\n\n"
        result += "\n".join(f"  • {item}" for item in items)
        result += "\n\n💡 Disable unwanted ones in Task Manager > Startup tab"
        return {"success": True, "result": result}

    # ══════════════════════════════════════════════════════════════
    # QUICK MATH & CONVERSIONS
    # ══════════════════════════════════════════════════════════════

    def calculate(self, query="", **kwargs):
        """Evaluate a math expression safely."""
        if not query:
            return {"success": False, "error": "What should I calculate? e.g. 'calc 2^10 + 15'"}

        # Clean up the expression
        expr = query.strip()
        expr = expr.replace("^", "**").replace("×", "*").replace("÷", "/")
        expr = re.sub(r'\b(sqrt|sin|cos|tan|log|log2|log10|abs|pi|e)\b',
                      lambda m: f"math.{m.group()}" if m.group() not in ('pi', 'e') else f"math.{m.group()}", expr)

        # Safe evaluation — only allow math operations
        allowed = set("0123456789+-*/.()% ")
        safe_expr = expr.replace("math.", "").replace("sqrt", "").replace("sin", "").replace("cos", "")
        safe_expr = safe_expr.replace("tan", "").replace("log", "").replace("log2", "").replace("log10", "")
        safe_expr = safe_expr.replace("abs", "").replace("pi", "").replace("e", "")

        # Validate: only math characters should remain after stripping known functions
        if not all(c in allowed for c in safe_expr):
            return {"success": False, "error": f"Expression contains invalid characters. Only math operations allowed."}

        # Block attribute access and dunder methods
        if '__' in expr or 'import' in expr or 'exec' in expr or 'eval' in expr:
            return {"success": False, "error": "Expression not allowed for security reasons."}

        try:
            result = eval(expr, {"__builtins__": {}, "math": math}, {})
            if isinstance(result, float):
                # Clean display
                if result == int(result) and abs(result) < 1e15:
                    result = int(result)
                else:
                    result = round(result, 10)
            return {"success": True, "result": f"🔢 {query} = {result}"}
        except Exception as e:
            return {"success": False, "error": f"Couldn't calculate: {e}"}

    def convert_units(self, query="", **kwargs):
        """Convert between common units."""
        if not query:
            return {"success": False, "error": "Usage: convert 5 km to miles"}

        conversions = {
            ("km", "miles"): 0.621371, ("miles", "km"): 1.60934,
            ("m", "feet"): 3.28084, ("feet", "m"): 0.3048,
            ("cm", "inches"): 0.393701, ("inches", "cm"): 2.54,
            ("kg", "lbs"): 2.20462, ("lbs", "kg"): 0.453592,
            ("g", "oz"): 0.035274, ("oz", "g"): 28.3495,
            ("c", "f"): None, ("f", "c"): None,  # Special handling
            ("l", "gal"): 0.264172, ("gal", "l"): 3.78541,
            ("mb", "gb"): 0.001, ("gb", "mb"): 1000,
            ("gb", "tb"): 0.001, ("tb", "gb"): 1000,
            ("kb", "mb"): 0.001, ("mb", "kb"): 1000,
            ("usd", "inr"): 83.5, ("inr", "usd"): 0.012,
        }

        match = re.match(r"([\d.]+)\s*(\w+)\s+(?:to|in|as)\s+(\w+)", query, re.IGNORECASE)
        if not match:
            return {"success": False, "error": "Format: convert <number> <from_unit> to <to_unit>"}

        value = float(match.group(1))
        from_u = match.group(2).lower()
        to_u = match.group(3).lower()

        # Temperature special cases
        if from_u == "c" and to_u == "f":
            result = value * 9 / 5 + 32
            return {"success": True, "result": f"🌡️ {value}°C = {result:.1f}°F"}
        if from_u == "f" and to_u == "c":
            result = (value - 32) * 5 / 9
            return {"success": True, "result": f"🌡️ {value}°F = {result:.1f}°C"}

        key = (from_u, to_u)
        if key not in conversions:
            available = ", ".join(f"{a}→{b}" for a, b in conversions.keys() if conversions[(a, b)] is not None)
            return {"success": False, "error": f"Unknown conversion. Available: {available}"}

        factor = conversions[key]
        result = value * factor
        return {"success": True, "result": f"📐 {value} {from_u} = {result:.4g} {to_u}"}

    # ══════════════════════════════════════════════════════════════
    # RECENT FILES & SEARCH
    # ══════════════════════════════════════════════════════════════

    def recent_files(self, query="", **kwargs):
        """List recently modified files in a folder (default: home)."""
        search_path = query or os.path.expanduser("~")
        if not os.path.exists(search_path):
            return {"success": False, "error": f"Path not found: {search_path}"}

        files = []
        try:
            for item in Path(search_path).iterdir():
                if item.is_file() and not item.name.startswith('.'):
                    try:
                        mtime = item.stat().st_mtime
                        files.append((item, mtime))
                    except Exception:
                        pass
        except Exception as e:
            return {"success": False, "error": str(e)}

        files.sort(key=lambda x: x[1], reverse=True)
        top = files[:20]

        if not top:
            return {"success": True, "result": f"No files found in {search_path}"}

        lines = [f"📂 Recent files in {search_path}:", ""]
        for fp, mtime in top:
            dt = datetime.fromtimestamp(mtime)
            size = fp.stat().st_size
            sz = f"{size / 1024:.0f} KB" if size < 1024 * 1024 else f"{size / (1024**2):.1f} MB"
            lines.append(f"  {dt.strftime('%Y-%m-%d %H:%M')}  {sz:>10}  {fp.name}")

        return {"success": True, "result": "\n".join(lines)}

    def large_files(self, query="", **kwargs):
        """Alias for disk_hogs but searches Downloads & Desktop by default."""
        if not query:
            downloads = os.path.expanduser("~/Downloads")
            desktop = os.path.expanduser("~/Desktop")
            all_large = []
            for folder in [downloads, desktop]:
                if os.path.exists(folder):
                    for item in Path(folder).rglob("*"):
                        if item.is_file():
                            try:
                                size = item.stat().st_size
                                if size > 10 * 1024 * 1024:  # >10MB
                                    all_large.append((str(item), size))
                            except Exception:
                                pass
            all_large.sort(key=lambda x: x[1], reverse=True)
            top = all_large[:20]
            if not top:
                return {"success": True, "result": "No large files (>10MB) in Downloads/Desktop"}
            lines = ["📦 Large files in Downloads & Desktop:", ""]
            for fp, size in top:
                sz = f"{size / (1024**3):.1f} GB" if size > 1024**3 else f"{size / (1024**2):.0f} MB"
                lines.append(f"  {sz:>10}  {os.path.basename(fp)}")
            return {"success": True, "result": "\n".join(lines)}
        return self.disk_hogs(query=query)

    def find_duplicates(self, query="", **kwargs):
        """Find files with duplicate names in a folder."""
        search_path = query or os.path.expanduser("~/Downloads")
        if not os.path.exists(search_path):
            return {"success": False, "error": f"Path not found: {search_path}"}

        from collections import Counter
        names = []
        file_map = {}
        try:
            for item in Path(search_path).rglob("*"):
                if item.is_file():
                    name = item.name.lower()
                    names.append(name)
                    file_map.setdefault(name, []).append(str(item))
        except Exception as e:
            return {"success": False, "error": str(e)}

        counter = Counter(names)
        dupes = {name: count for name, count in counter.items() if count > 1}

        if not dupes:
            return {"success": True, "result": f"✅ No duplicate files found in {search_path}"}

        lines = [f"🔍 Duplicate files in {search_path}:", ""]
        for name, count in sorted(dupes.items(), key=lambda x: x[1], reverse=True)[:15]:
            lines.append(f"  {name} — {count} copies")
            for path in file_map[name][:3]:
                lines.append(f"    → {path}")

        return {"success": True, "result": "\n".join(lines)}

    def search_files(self, query="", **kwargs):
        """Search for files by name across common locations."""
        if not query:
            return {"success": False, "error": "What file should I search for?"}

        locations = [
            os.path.expanduser("~/Desktop"),
            os.path.expanduser("~/Downloads"),
            os.path.expanduser("~/Documents"),
            os.path.expanduser("~"),
        ]

        found = []
        for loc in locations:
            if not os.path.exists(loc):
                continue
            try:
                for item in Path(loc).rglob(f"*{query}*"):
                    if item.is_file():
                        found.append(str(item))
                        if len(found) >= 20:
                            break
            except Exception:
                pass
            if len(found) >= 20:
                break

        if not found:
            return {"success": True, "result": f"No files matching '{query}' found"}

        lines = [f"🔍 Files matching '{query}':", ""]
        for fp in found:
            lines.append(f"  📄 {fp}")
        return {"success": True, "result": "\n".join(lines)}

    def open_last_download(self, **kwargs):
        """Open the most recently downloaded file."""
        downloads = os.path.expanduser("~/Downloads")
        if not os.path.exists(downloads):
            return {"success": False, "error": "Downloads folder not found"}

        files = [(f, f.stat().st_mtime) for f in Path(downloads).iterdir()
                 if f.is_file() and not f.name.startswith('.')]
        if not files:
            return {"success": True, "result": "Downloads folder is empty"}

        latest = max(files, key=lambda x: x[1])
        os.startfile(str(latest[0]))
        dt = datetime.fromtimestamp(latest[1])
        return {
            "success": True,
            "result": f"📥 Opening last download: {latest[0].name}\n   Downloaded: {dt.strftime('%Y-%m-%d %H:%M')}",
        }

    def open_last_screenshot(self, **kwargs):
        """Open the most recent screenshot."""
        locations = [
            os.path.expanduser("~/Pictures/Screenshots"),
            os.path.expanduser("~/Desktop"),
            os.path.expanduser("~/OneDrive/Pictures/Screenshots"),
            "screenshots",  # Project local
        ]

        for loc in locations:
            if not os.path.exists(loc):
                continue
            images = [f for f in Path(loc).iterdir()
                      if f.is_file() and f.suffix.lower() in ('.png', '.jpg', '.jpeg', '.bmp')]
            if images:
                latest = max(images, key=lambda f: f.stat().st_mtime)
                os.startfile(str(latest))
                return {
                    "success": True,
                    "result": f"📸 Opening screenshot: {latest.name}",
                }

        return {"success": False, "error": "No screenshots found"}

    # ══════════════════════════════════════════════════════════════
    # CONTEXT-AWARE / DAILY SUMMARY
    # ══════════════════════════════════════════════════════════════

    def daily_summary(self, **kwargs):
        """Quick summary of system status, time, weather-like info."""
        import psutil

        now = datetime.now()
        hour = now.hour
        greeting = "Good morning" if hour < 12 else "Good afternoon" if hour < 17 else "Good evening"

        lines = [f"👋 {greeting}! Here's your daily summary:", ""]

        # Time & date
        lines.append(f"  🕐 {now.strftime('%I:%M %p, %A %B %d, %Y')}")

        # System health snapshot
        cpu = psutil.cpu_percent(interval=0.5)
        mem = psutil.virtual_memory()
        lines.append(f"  💻 CPU: {cpu}%  |  RAM: {mem.percent}% ({mem.available / (1024**3):.1f} GB free)")

        # Battery
        bat = psutil.sensors_battery()
        if bat:
            plug = "⚡ charging" if bat.power_plugged else "🔋 on battery"
            lines.append(f"  🔋 Battery: {bat.percent}% ({plug})")

        # Uptime
        boot = datetime.fromtimestamp(psutil.boot_time())
        uptime = now - boot
        hours = uptime.total_seconds() / 3600
        lines.append(f"  ⏱️ Uptime: {hours:.1f} hours")

        # Top running apps
        procs = sorted(psutil.process_iter(['name', 'memory_percent']),
                      key=lambda p: p.info.get('memory_percent', 0) or 0, reverse=True)[:5]
        lines.append(f"\n  📊 Top running apps:")
        for p in procs:
            pct = p.info.get('memory_percent', 0) or 0
            lines.append(f"    • {p.info['name']}: {pct:.1f}% RAM")

        # Recent downloads
        downloads = os.path.expanduser("~/Downloads")
        if os.path.exists(downloads):
            today_files = []
            for f in Path(downloads).iterdir():
                if f.is_file():
                    try:
                        mtime = datetime.fromtimestamp(f.stat().st_mtime)
                        if mtime.date() == now.date():
                            today_files.append(f.name)
                    except Exception:
                        pass
            if today_files:
                lines.append(f"\n  📥 Downloaded today: {len(today_files)} files")
                for name in today_files[:3]:
                    lines.append(f"    • {name}")

        return {"success": True, "result": "\n".join(lines)}

    # ══════════════════════════════════════════════════════════════
    # APP-SMART: WEB SHORTCUTS
    # ══════════════════════════════════════════════════════════════

    def open_chatgpt(self, **kwargs):
        """Open ChatGPT."""
        webbrowser.open("https://chat.openai.com")
        return {"success": True, "result": "💬 Opening ChatGPT..."}

    def open_github(self, **kwargs):
        """Open GitHub."""
        webbrowser.open("https://github.com")
        return {"success": True, "result": "🐙 Opening GitHub..."}

    def open_stackoverflow(self, query="", **kwargs):
        """Search StackOverflow."""
        if query:
            import urllib.parse
            url = f"https://stackoverflow.com/search?q={urllib.parse.quote(query)}"
        else:
            url = "https://stackoverflow.com"
        webbrowser.open(url)
        return {"success": True, "result": f"📚 Opening StackOverflow{': ' + query if query else ''}"}

    def open_reddit(self, query="", **kwargs):
        """Open Reddit or a specific subreddit."""
        if query:
            sub = query.strip().lstrip("r/")
            url = f"https://www.reddit.com/r/{sub}"
        else:
            url = "https://www.reddit.com"
        webbrowser.open(url)
        return {"success": True, "result": f"📱 Opening Reddit{': r/' + query if query else ''}"}

    def define_word(self, query="", **kwargs):
        """Look up a word definition online."""
        if not query:
            return {"success": False, "error": "What word should I define?"}
        import urllib.parse
        url = f"https://www.google.com/search?q=define+{urllib.parse.quote(query)}"
        webbrowser.open(url)
        return {"success": True, "result": f"📖 Looking up definition of '{query}'..."}

    def translate_text(self, query="", **kwargs):
        """Translate text using Google Translate."""
        if not query:
            return {"success": False, "error": "Usage: translate hello to spanish"}
        import urllib.parse
        # Try to extract target language
        match = re.match(r"(.+?)\s+to\s+(\w+)$", query, re.IGNORECASE)
        if match:
            text = match.group(1)
            lang = match.group(2)
            url = f"https://translate.google.com/?sl=auto&tl={lang}&text={urllib.parse.quote(text)}"
        else:
            url = f"https://translate.google.com/?sl=auto&tl=en&text={urllib.parse.quote(query)}"
        webbrowser.open(url)
        return {"success": True, "result": f"🌍 Opening Google Translate..."}
