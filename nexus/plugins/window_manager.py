"""
NEXUS OS — Window Manager Plugin
Control windows: list, focus, close, minimize, maximize, move, resize, snap.
"""

import ctypes
import ctypes.wintypes
from typing import Any, Dict, List, Optional
from nexus.core.plugin import NexusPlugin

try:
    import win32gui
    import win32con
    import win32process
    HAS_WIN32 = True
except ImportError:
    HAS_WIN32 = False


class WindowManagerPlugin(NexusPlugin):
    name = "window_manager"
    description = "Find, focus, move, resize, snap, minimize, maximize, and close windows"
    icon = "🪟"

    def setup(self):
        self.register_command("list", self.list_windows,
                              "List all visible windows", "list windows", ["windows", "ls windows"])
        self.register_command("focus", self.focus_window,
                              "Bring a window to the foreground", "focus <title>", ["activate", "switch to"])
        self.register_command("close", self.close_window,
                              "Close a window", "close <title>")
        self.register_command("minimize", self.minimize_window,
                              "Minimize a window", "minimize <title>", ["min"])
        self.register_command("maximize", self.maximize_window,
                              "Maximize a window", "maximize <title>", ["max"])
        self.register_command("move", self.move_window,
                              "Move a window to x,y", "move <title> <x> <y>")
        self.register_command("resize", self.resize_window,
                              "Resize a window", "resize <title> <w> <h>")
        self.register_command("snap", self.snap_window,
                              "Snap window to screen edge", "snap <title> left|right|top|bottom")

    def _get_windows(self) -> List[Dict[str, Any]]:
        """Get all visible windows with their info."""
        if not HAS_WIN32:
            return []
        windows = []

        def callback(hwnd, _):
            if win32gui.IsWindowVisible(hwnd):
                title = win32gui.GetWindowText(hwnd)
                if title and title.strip():
                    try:
                        rect = win32gui.GetWindowRect(hwnd)
                        _, pid = win32process.GetWindowThreadProcessId(hwnd)
                        windows.append({
                            "hwnd": hwnd,
                            "title": title,
                            "pid": pid,
                            "x": rect[0],
                            "y": rect[1],
                            "width": rect[2] - rect[0],
                            "height": rect[3] - rect[1],
                        })
                    except Exception:
                        pass
            return True

        win32gui.EnumWindows(callback, None)
        return windows

    def _find_window(self, title: str) -> Optional[int]:
        """Find a window handle by partial title match."""
        title_lower = title.lower()
        for w in self._get_windows():
            if title_lower in w["title"].lower():
                return w["hwnd"]
        return None

    def list_windows(self, **kwargs):
        windows = self._get_windows()
        display = []
        for w in windows:
            display.append({
                "title": w["title"],
                "pid": w["pid"],
                "position": f"{w['x']},{w['y']}",
                "size": f"{w['width']}x{w['height']}",
            })
        return {"success": True, "result": display, "count": len(display)}

    def focus_window(self, title: str = "", **kwargs):
        hwnd = self._find_window(title)
        if not hwnd:
            return {"success": False, "error": f"Window '{title}' not found"}
        try:
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
            win32gui.SetForegroundWindow(hwnd)
            return {"success": True, "result": f"Focused: {win32gui.GetWindowText(hwnd)}"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def close_window(self, title: str = "", **kwargs):
        hwnd = self._find_window(title)
        if not hwnd:
            return {"success": False, "error": f"Window '{title}' not found"}
        try:
            win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
            return {"success": True, "result": f"Closed: {title}"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def minimize_window(self, title: str = "", **kwargs):
        hwnd = self._find_window(title)
        if not hwnd:
            return {"success": False, "error": f"Window '{title}' not found"}
        win32gui.ShowWindow(hwnd, win32con.SW_MINIMIZE)
        return {"success": True, "result": f"Minimized: {title}"}

    def maximize_window(self, title: str = "", **kwargs):
        hwnd = self._find_window(title)
        if not hwnd:
            return {"success": False, "error": f"Window '{title}' not found"}
        win32gui.ShowWindow(hwnd, win32con.SW_MAXIMIZE)
        return {"success": True, "result": f"Maximized: {title}"}

    def move_window(self, title: str = "", x: int = 0, y: int = 0, **kwargs):
        hwnd = self._find_window(title)
        if not hwnd:
            return {"success": False, "error": f"Window '{title}' not found"}
        rect = win32gui.GetWindowRect(hwnd)
        w = rect[2] - rect[0]
        h = rect[3] - rect[1]
        win32gui.MoveWindow(hwnd, int(x), int(y), w, h, True)
        return {"success": True, "result": f"Moved '{title}' to ({x}, {y})"}

    def resize_window(self, title: str = "", w: int = 800, h: int = 600, **kwargs):
        hwnd = self._find_window(title)
        if not hwnd:
            return {"success": False, "error": f"Window '{title}' not found"}
        rect = win32gui.GetWindowRect(hwnd)
        win32gui.MoveWindow(hwnd, rect[0], rect[1], int(w), int(h), True)
        return {"success": True, "result": f"Resized '{title}' to {w}x{h}"}

    def snap_window(self, title: str = "", direction: str = "left", **kwargs):
        hwnd = self._find_window(title)
        if not hwnd:
            return {"success": False, "error": f"Window '{title}' not found"}
        try:
            user32 = ctypes.windll.user32
            screen_w = user32.GetSystemMetrics(0)
            screen_h = user32.GetSystemMetrics(1)
        except Exception:
            screen_w, screen_h = 1920, 1080

        positions = {
            "left": (0, 0, screen_w // 2, screen_h),
            "right": (screen_w // 2, 0, screen_w // 2, screen_h),
            "top": (0, 0, screen_w, screen_h // 2),
            "bottom": (0, screen_h // 2, screen_w, screen_h // 2),
        }
        pos = positions.get(direction, positions["left"])
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        win32gui.MoveWindow(hwnd, pos[0], pos[1], pos[2], pos[3], True)
        return {"success": True, "result": f"Snapped '{title}' to {direction}"}
