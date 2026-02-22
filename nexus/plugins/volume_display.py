"""
NEXUS OS — Volume & Display Plugin
Control system volume and screen brightness.
"""

from typing import Any, Dict
from nexus.core.plugin import NexusPlugin

# Volume control via pycaw
try:
    from ctypes import cast, POINTER
    from comtypes import CLSCTX_ALL
    from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
    HAS_PYCAW = True
except ImportError:
    HAS_PYCAW = False

# Brightness control
try:
    import screen_brightness_control as sbc
    HAS_SBC = True
except ImportError:
    HAS_SBC = False


class VolumeDisplayPlugin(NexusPlugin):
    name = "volume_display"
    description = "Control system volume and screen brightness"
    icon = "🔊"

    def setup(self):
        self.register_command("set_volume", self.set_volume,
                              "Set volume (0-100)", "volume <level>")
        self.register_command("volume_adjust", self.volume_adjust,
                              "Volume up or down by 10%", "volume up|down")
        self.register_command("mute", self.mute,
                              "Mute audio", "mute")
        self.register_command("unmute", self.unmute,
                              "Unmute audio", "unmute")
        self.register_command("set_brightness", self.set_brightness,
                              "Set brightness (0-100)", "brightness <level>")
        self.register_command("brightness_adjust", self.brightness_adjust,
                              "Brightness up or down by 10%", "brightness up|down")

    def _get_volume_interface(self):
        """Get the Windows audio endpoint volume interface."""
        devices = AudioUtilities.GetSpeakers()
        interface = devices.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
        return cast(interface, POINTER(IAudioEndpointVolume))

    def set_volume(self, level: int = 50, **kwargs):
        if not HAS_PYCAW:
            return {"success": False, "error": "pycaw not installed"}
        try:
            vol = self._get_volume_interface()
            level = max(0, min(100, int(level)))
            vol.SetMasterVolumeLevelScalar(level / 100.0, None)
            return {"success": True, "result": f"Volume set to {level}%"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def volume_adjust(self, direction: str = "up", **kwargs):
        if not HAS_PYCAW:
            return {"success": False, "error": "pycaw not installed"}
        try:
            vol = self._get_volume_interface()
            current = round(vol.GetMasterVolumeLevelScalar() * 100)
            step = 10 if direction == "up" else -10
            new_level = max(0, min(100, current + step))
            vol.SetMasterVolumeLevelScalar(new_level / 100.0, None)
            return {"success": True, "result": f"Volume: {current}% → {new_level}%"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def mute(self, **kwargs):
        if not HAS_PYCAW:
            return {"success": False, "error": "pycaw not installed"}
        try:
            vol = self._get_volume_interface()
            vol.SetMute(1, None)
            return {"success": True, "result": "🔇 Audio muted"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def unmute(self, **kwargs):
        if not HAS_PYCAW:
            return {"success": False, "error": "pycaw not installed"}
        try:
            vol = self._get_volume_interface()
            vol.SetMute(0, None)
            return {"success": True, "result": "🔊 Audio unmuted"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def set_brightness(self, level: int = 50, **kwargs):
        if not HAS_SBC:
            return {"success": False, "error": "screen_brightness_control not installed"}
        try:
            level = max(0, min(100, int(level)))
            sbc.set_brightness(level)
            return {"success": True, "result": f"Brightness set to {level}%"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def brightness_adjust(self, direction: str = "up", **kwargs):
        if not HAS_SBC:
            return {"success": False, "error": "screen_brightness_control not installed"}
        try:
            current = sbc.get_brightness()[0]
            step = 10 if direction == "up" else -10
            new_level = max(0, min(100, current + step))
            sbc.set_brightness(new_level)
            return {"success": True, "result": f"Brightness: {current}% → {new_level}%"}
        except Exception as e:
            return {"success": False, "error": str(e)}
