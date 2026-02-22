"""
NEXUS OS — Clipboard Plugin
Read, write, and track clipboard history.
"""

import time
from typing import Any, Dict, List
from nexus.core.plugin import NexusPlugin

try:
    import pyperclip
    HAS_PYPERCLIP = True
except ImportError:
    HAS_PYPERCLIP = False

try:
    import win32clipboard
    HAS_WIN32CLIP = True
except ImportError:
    HAS_WIN32CLIP = False


class ClipboardPlugin(NexusPlugin):
    name = "clipboard"
    description = "Read, write, and track clipboard contents"
    icon = "📋"

    def setup(self):
        self._history: List[Dict[str, Any]] = []
        self._max_history = 50

        self.register_command("get", self.get_clipboard,
                              "Show current clipboard contents", "clipboard", ["paste", "show clipboard"])
        self.register_command("set", self.set_clipboard,
                              "Copy text to clipboard", "clipboard set <text>", ["copy to clipboard"])
        self.register_command("history", self.get_history,
                              "Show clipboard history", "clipboard history")
        self.register_command("clear", self.clear_clipboard,
                              "Clear clipboard", "clipboard clear")

    def _get_text(self) -> str:
        """Get clipboard text using available backend."""
        if HAS_PYPERCLIP:
            try:
                return pyperclip.paste()
            except Exception:
                pass

        if HAS_WIN32CLIP:
            try:
                win32clipboard.OpenClipboard()
                try:
                    data = win32clipboard.GetClipboardData(win32clipboard.CF_UNICODETEXT)
                    return data
                except TypeError:
                    return ""
                finally:
                    win32clipboard.CloseClipboard()
            except Exception:
                pass

        return ""

    def _set_text(self, text: str):
        """Set clipboard text."""
        if HAS_PYPERCLIP:
            pyperclip.copy(text)
            return

        if HAS_WIN32CLIP:
            win32clipboard.OpenClipboard()
            try:
                win32clipboard.EmptyClipboard()
                win32clipboard.SetClipboardText(text, win32clipboard.CF_UNICODETEXT)
            finally:
                win32clipboard.CloseClipboard()

    def _record(self, text: str):
        """Save to history."""
        if text and (not self._history or self._history[-1]["text"] != text):
            self._history.append({
                "text": text[:500],
                "time": time.strftime("%H:%M:%S"),
                "length": len(text),
            })
            if len(self._history) > self._max_history:
                self._history = self._history[-self._max_history:]

    def get_clipboard(self, **kwargs):
        text = self._get_text()
        self._record(text)
        if text:
            return {"success": True, "result": text, "length": len(text)}
        return {"success": True, "result": "(clipboard is empty)", "length": 0}

    def set_clipboard(self, text: str = "", **kwargs):
        try:
            self._set_text(text)
            self._record(text)
            preview = text[:100] + "..." if len(text) > 100 else text
            return {"success": True, "result": f"Copied to clipboard: {preview}"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def get_history(self, **kwargs):
        if not self._history:
            return {"success": True, "result": "(no clipboard history yet)"}
        return {"success": True, "result": list(reversed(self._history)), "count": len(self._history)}

    def clear_clipboard(self, **kwargs):
        try:
            self._set_text("")
            return {"success": True, "result": "Clipboard cleared"}
        except Exception as e:
            return {"success": False, "error": str(e)}
