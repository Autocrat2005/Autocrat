"""
NEXUS OS — Screen Intelligence Plugin
Screenshot, OCR, screen region capture, find-on-screen.
"""

import os
import time
import base64
import io
from typing import Any, Dict
from nexus.core.plugin import NexusPlugin

try:
    import pyautogui
    HAS_PYAUTOGUI = True
except ImportError:
    HAS_PYAUTOGUI = False

try:
    from PIL import Image
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

try:
    import pytesseract
    HAS_TESSERACT = True
except ImportError:
    HAS_TESSERACT = False


class ScreenIntelPlugin(NexusPlugin):
    name = "screen_intel"
    description = "Screenshot, OCR text extraction, and visual element detection"
    icon = "📸"

    def setup(self):
        self.screenshots_dir = "screenshots"
        os.makedirs(self.screenshots_dir, exist_ok=True)

        self.register_command("screenshot", self.take_screenshot,
                              "Capture full screen", "screenshot [save <path>]", ["capture", "ss"])
        self.register_command("screenshot_region", self.screenshot_region,
                              "Capture screen region", "screenshot region <x> <y> <w> <h>")
        self.register_command("ocr", self.ocr_screen,
                              "Extract text from full screen", "ocr", ["read screen"])
        self.register_command("ocr_region", self.ocr_region,
                              "Extract text from screen region", "ocr region <x> <y> <w> <h>")
        self.register_command("find_on_screen", self.find_on_screen,
                              "Locate an image element on screen", "find on screen <image_path>")

    def take_screenshot(self, path: str = "", **kwargs):
        if not HAS_PYAUTOGUI:
            return {"success": False, "error": "pyautogui not installed"}

        if not path:
            timestamp = time.strftime("%Y%m%d_%H%M%S")
            path = os.path.join(self.screenshots_dir, f"screenshot_{timestamp}.png")

        try:
            img = pyautogui.screenshot()
            img.save(path)

            # Also create base64 for web UI preview
            buffer = io.BytesIO()
            img.thumbnail((400, 225))  # Small preview
            img.save(buffer, format="PNG")
            preview = base64.b64encode(buffer.getvalue()).decode()

            return {
                "success": True,
                "result": f"Screenshot saved: {path}",
                "path": os.path.abspath(path),
                "preview": f"data:image/png;base64,{preview}",
                "size": f"{img.size[0]}x{img.size[1]}",
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    def screenshot_region(self, x: int = 0, y: int = 0, w: int = 400, h: int = 300, **kwargs):
        if not HAS_PYAUTOGUI:
            return {"success": False, "error": "pyautogui not installed"}

        try:
            timestamp = time.strftime("%Y%m%d_%H%M%S")
            path = os.path.join(self.screenshots_dir, f"region_{timestamp}.png")

            img = pyautogui.screenshot(region=(int(x), int(y), int(w), int(h)))
            img.save(path)

            buffer = io.BytesIO()
            img.save(buffer, format="PNG")
            preview = base64.b64encode(buffer.getvalue()).decode()

            return {
                "success": True,
                "result": f"Region screenshot saved: {path}",
                "path": os.path.abspath(path),
                "preview": f"data:image/png;base64,{preview}",
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    def ocr_screen(self, **kwargs):
        if not HAS_PYAUTOGUI:
            return {"success": False, "error": "pyautogui not installed"}
        if not HAS_TESSERACT:
            return {"success": False, "error": "pytesseract not installed. Install Tesseract OCR and pytesseract."}

        try:
            img = pyautogui.screenshot()
            text = pytesseract.image_to_string(img)
            return {"success": True, "result": text.strip(), "char_count": len(text.strip())}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def ocr_region(self, x: int = 0, y: int = 0, w: int = 400, h: int = 300, **kwargs):
        if not HAS_PYAUTOGUI:
            return {"success": False, "error": "pyautogui not installed"}
        if not HAS_TESSERACT:
            return {"success": False, "error": "pytesseract not installed"}

        try:
            img = pyautogui.screenshot(region=(int(x), int(y), int(w), int(h)))
            text = pytesseract.image_to_string(img)
            return {"success": True, "result": text.strip()}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def find_on_screen(self, image_path: str = "", **kwargs):
        if not HAS_PYAUTOGUI:
            return {"success": False, "error": "pyautogui not installed"}

        if not os.path.exists(image_path):
            return {"success": False, "error": f"Image not found: {image_path}"}

        try:
            location = pyautogui.locateOnScreen(image_path, confidence=0.8)
            if location:
                center = pyautogui.center(location)
                return {
                    "success": True,
                    "result": {
                        "found": True,
                        "x": center.x,
                        "y": center.y,
                        "region": {"left": location.left, "top": location.top,
                                   "width": location.width, "height": location.height},
                    },
                }
            return {"success": True, "result": {"found": False, "message": "Image not found on screen"}}
        except Exception as e:
            return {"success": False, "error": str(e)}
