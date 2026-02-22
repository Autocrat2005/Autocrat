"""
NEXUS OS — App Launcher Plugin
Open apps, URLs, folders. Detect installed apps.
"""

import os
import subprocess
import winreg
from pathlib import Path
from typing import Any, Dict, List
from nexus.core.plugin import NexusPlugin


class AppLauncherPlugin(NexusPlugin):
    name = "app_launcher"
    description = "Launch apps, open URLs and folders, detect installed programs"
    icon = "🚀"

    def setup(self):
        self._app_cache: List[Dict[str, str]] = []

        self.register_command("open", self.open_app,
                              "Open an application by name", "open <app_name>", ["launch", "start app"])
        self.register_command("open_url", self.open_url,
                              "Open a URL in default browser", "open url <url>", ["browse"])
        self.register_command("open_folder", self.open_folder,
                              "Open a folder in Explorer", "open folder <path>", ["explore"])
        self.register_command("installed_apps", self.list_installed,
                              "List detected installed apps", "installed apps", ["apps"])

    # Common Windows apps with their typical paths or commands
    KNOWN_APPS = {
        "notepad": "notepad.exe",
        "calculator": "calc.exe",
        "calc": "calc.exe",
        "paint": "mspaint.exe",
        "cmd": "cmd.exe",
        "terminal": "wt.exe",
        "powershell": "powershell.exe",
        "explorer": "explorer.exe",
        "task manager": "taskmgr.exe",
        "taskmgr": "taskmgr.exe",
        "control panel": "control.exe",
        "settings": "start ms-settings:",
        "snipping tool": "snippingtool.exe",
        "snip": "snippingtool.exe",
        "wordpad": "wordpad.exe",
        "regedit": "regedit.exe",
        "device manager": "devmgmt.msc",
        "disk management": "diskmgmt.msc",
        "services": "services.msc",
        "event viewer": "eventvwr.msc",
        "chrome": r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        "firefox": r"C:\Program Files\Mozilla Firefox\firefox.exe",
        "edge": "msedge.exe",
        "vscode": "code",
        "code": "code",
        "spotify": "spotify.exe",
        "discord": "discord.exe",
        "steam": "steam.exe",
        "obs": "obs64.exe",
        "vlc": r"C:\Program Files\VideoLAN\VLC\vlc.exe",
    }

    def open_app(self, app_name: str = "", **kwargs):
        name_lower = app_name.lower().strip()

        # Check known apps first
        for key, cmd in self.KNOWN_APPS.items():
            if name_lower in key or key in name_lower:
                try:
                    if cmd.startswith("start "):
                        os.system(cmd)
                    else:
                        subprocess.Popen(cmd, shell=True)
                    return {"success": True, "result": f"Opened: {app_name} ({cmd})"}
                except Exception as e:
                    self.log.debug(f"Failed to open via known path: {e}")

        # Try os.startfile
        try:
            os.startfile(app_name)
            return {"success": True, "result": f"Opened: {app_name}"}
        except Exception:
            pass

        # Try Start-Process via PowerShell
        try:
            subprocess.Popen(f'powershell -Command "Start-Process \'{app_name}\'"', shell=True)
            return {"success": True, "result": f"Opened via PowerShell: {app_name}"}
        except Exception:
            pass

        # Search in Start Menu
        start_menu_dirs = [
            os.path.expandvars(r"%ProgramData%\Microsoft\Windows\Start Menu\Programs"),
            os.path.expandvars(r"%APPDATA%\Microsoft\Windows\Start Menu\Programs"),
        ]

        for sm_dir in start_menu_dirs:
            if os.path.exists(sm_dir):
                for root, dirs, files in os.walk(sm_dir):
                    for f in files:
                        if f.endswith('.lnk') and name_lower in f.lower():
                            full = os.path.join(root, f)
                            try:
                                os.startfile(full)
                                return {"success": True, "result": f"Opened: {f}"}
                            except Exception:
                                continue

        return {"success": False, "error": f"Could not find app: '{app_name}'"}

    def open_url(self, url: str = "", **kwargs):
        if not url.startswith("http"):
            url = "https://" + url
        try:
            import webbrowser
            webbrowser.open(url)
            return {"success": True, "result": f"Opened: {url}"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def open_folder(self, path: str = "", **kwargs):
        p = Path(path).resolve()
        if not p.exists():
            return {"success": False, "error": f"Folder not found: {path}"}
        try:
            os.startfile(str(p))
            return {"success": True, "result": f"Opened: {p}"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def list_installed(self, **kwargs):
        """Detect installed apps from registry and Start Menu."""
        if not self._app_cache:
            self._scan_apps()
        return {"success": True, "result": self._app_cache, "count": len(self._app_cache)}

    def _scan_apps(self):
        """Scan registry for installed apps."""
        self._app_cache = []
        reg_paths = [
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
            (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
        ]

        seen = set()
        for hive, path in reg_paths:
            try:
                key = winreg.OpenKey(hive, path)
                for i in range(winreg.QueryInfoKey(key)[0]):
                    try:
                        subkey_name = winreg.EnumKey(key, i)
                        subkey = winreg.OpenKey(key, subkey_name)
                        try:
                            name = winreg.QueryValueEx(subkey, "DisplayName")[0]
                            if name and name not in seen:
                                seen.add(name)
                                version = ""
                                try:
                                    version = winreg.QueryValueEx(subkey, "DisplayVersion")[0]
                                except (FileNotFoundError, OSError):
                                    pass
                                self._app_cache.append({"name": name, "version": version})
                        except (FileNotFoundError, OSError):
                            pass
                        winreg.CloseKey(subkey)
                    except OSError:
                        continue
                winreg.CloseKey(key)
            except OSError:
                continue

        self._app_cache.sort(key=lambda x: x["name"].lower())
