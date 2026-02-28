"""
Autocrat — Keyboard & Mouse Plugin
Simulate keyboard input, hotkeys, mouse clicks, moves, scrolls, and drags.
"""

from typing import Any, Dict
from nexus.core.plugin import NexusPlugin

try:
    import pyautogui
    pyautogui.FAILSAFE = True
    pyautogui.PAUSE = 0.05
    HAS_PYAUTOGUI = True
except ImportError:
    HAS_PYAUTOGUI = False


class KeyboardMousePlugin(NexusPlugin):
    name = "keyboard_mouse"
    description = "Simulate keyboard input, hotkeys, mouse clicks, moves, scrolls, and drags"
    icon = "🖱️"

    def setup(self):
        self.register_command("type", self.type_text,
                              "Type text via keyboard", "type <text>", ["write", "input"])
        self.register_command("hotkey", self.send_hotkey,
                              "Press a keyboard shortcut", "hotkey <combo>", ["shortcut"])
        self.register_command("press", self.press_key,
                              "Press a single key", "press <key>")
        self.register_command("click", self.click,
                              "Click at current or specified position", "click [x y]")
        self.register_command("doubleclick", self.doubleclick,
                              "Double click", "doubleclick [x y]")
        self.register_command("rightclick", self.rightclick,
                              "Right click", "rightclick [x y]")
        self.register_command("move_mouse", self.move_mouse,
                              "Move mouse to coordinates", "move mouse <x> <y>", ["mousemove"])
        self.register_command("scroll", self.scroll,
                              "Scroll up (positive) or down (negative)", "scroll <amount>")
        self.register_command("drag", self.drag,
                              "Drag from one point to another", "drag <x1> <y1> to <x2> <y2>")

    def _check(self):
        if not HAS_PYAUTOGUI:
            return {"success": False, "error": "pyautogui not installed"}
        return None

    def type_text(self, text: str = "", **kwargs):
        err = self._check()
        if err:
            return err
        pyautogui.typewrite(text, interval=0.02) if text.isascii() else pyautogui.write(text)
        return {"success": True, "result": f"Typed: {text[:50]}{'...' if len(text) > 50 else ''}"}

    def send_hotkey(self, combo: str = "", **kwargs):
        err = self._check()
        if err:
            return err
        # Parse combo like "ctrl+c", "alt+tab", "ctrl+shift+s"
        keys = [k.strip().lower() for k in combo.replace("+", " ").split()]
        # Map common names
        key_map = {"ctrl": "ctrl", "control": "ctrl", "alt": "alt", "shift": "shift",
                    "win": "win", "windows": "win", "tab": "tab", "enter": "enter",
                    "escape": "esc", "esc": "esc", "delete": "delete", "del": "delete",
                    "backspace": "backspace", "space": "space"}
        mapped = [key_map.get(k, k) for k in keys]
        pyautogui.hotkey(*mapped)
        return {"success": True, "result": f"Hotkey: {'+'.join(mapped)}"}

    def press_key(self, key: str = "", **kwargs):
        err = self._check()
        if err:
            return err
        pyautogui.press(key)
        return {"success": True, "result": f"Pressed: {key}"}

    def click(self, x: int = None, y: int = None, **kwargs):
        err = self._check()
        if err:
            return err
        if x is not None and y is not None:
            pyautogui.click(int(x), int(y))
            return {"success": True, "result": f"Clicked at ({x}, {y})"}
        else:
            pyautogui.click()
            pos = pyautogui.position()
            return {"success": True, "result": f"Clicked at ({pos.x}, {pos.y})"}

    def doubleclick(self, x: int = None, y: int = None, **kwargs):
        err = self._check()
        if err:
            return err
        if x is not None and y is not None:
            pyautogui.doubleClick(int(x), int(y))
        else:
            pyautogui.doubleClick()
        return {"success": True, "result": "Double-clicked"}

    def rightclick(self, x: int = None, y: int = None, **kwargs):
        err = self._check()
        if err:
            return err
        if x is not None and y is not None:
            pyautogui.rightClick(int(x), int(y))
        else:
            pyautogui.rightClick()
        return {"success": True, "result": "Right-clicked"}

    def move_mouse(self, x: int = 0, y: int = 0, **kwargs):
        err = self._check()
        if err:
            return err
        pyautogui.moveTo(int(x), int(y), duration=0.3)
        return {"success": True, "result": f"Mouse moved to ({x}, {y})"}

    def scroll(self, amount: int = 3, **kwargs):
        err = self._check()
        if err:
            return err
        pyautogui.scroll(int(amount))
        direction = "up" if int(amount) > 0 else "down"
        return {"success": True, "result": f"Scrolled {direction} by {abs(int(amount))}"}

    def drag(self, x1: int = 0, y1: int = 0, x2: int = 0, y2: int = 0, **kwargs):
        err = self._check()
        if err:
            return err
        pyautogui.moveTo(int(x1), int(y1))
        pyautogui.drag(int(x2) - int(x1), int(y2) - int(y1), duration=0.5)
        return {"success": True, "result": f"Dragged from ({x1},{y1}) to ({x2},{y2})"}
