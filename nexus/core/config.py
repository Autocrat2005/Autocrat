"""
NEXUS OS — Configuration Manager
YAML-based config with defaults, auto-creation, and hot reload.
"""

import os
import yaml
from typing import Any, Dict, Optional
from nexus.core.logger import get_logger

log = get_logger("config")

DEFAULT_CONFIG = {
    "nexus": {
        "name": "NEXUS OS",
        "version": "1.0.0",
        "log_level": "INFO",
    },
    "server": {
        "host": "127.0.0.1",
        "port": 9000,
        "cors_origins": ["*"],
    },
    "plugins": {
        "enabled": [
            "window_manager",
            "process_controller",
            "file_ops",
            "keyboard_mouse",
            "screen_intel",
            "app_launcher",
            "clipboard",
            "system_info",
            "volume_display",
            "shell_executor",
            "task_scheduler",
            "workflow_engine",
            "smart_actions",
            "power_tools",
            "intelligence",
        ],
        "settings": {},
    },
    "scheduler": {
        "jobs": [],
    },
    "workflows": {
        "directory": "workflows",
    },
    "ai": {
        "gemini_api_key": "",
    },
    "system": {
        "safe_mode": False,
        "safe_mode_allow_actions": [
            "system_info.full",
            "system_info.cpu",
            "system_info.memory",
            "system_info.disk",
            "system_info.network",
            "system_info.battery",
            "system_info.uptime",
            "comet_web_agent.react_plan",
        ],
    },
    "safety": {
        "confirm_destructive": True,
        "blocked_actions": [
            "power_tools.shutdown",
            "power_tools.restart",
            "power_tools.sleep",
            "power_tools.hibernate",
            "power_tools.logoff",
        ],
        "web": {
            "allowlist_domains": ["codeforces.com", "github.com", "localhost"],
            "blocked_selector_terms": ["delete", "remove", "drop", "erase", "destroy", "terminate", "submit"],
        },
    },
}


class Config:
    """YAML configuration manager with auto-defaults."""

    _instance: Optional["Config"] = None

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self, config_path: str = "nexus_config.yaml"):
        if getattr(self, "_loaded", False):
            return
        self._loaded = True
        self.config_path = config_path
        self.data: Dict[str, Any] = {}
        self._load()

    def _load(self):
        """Load config from file, or create default if missing."""
        if os.path.exists(self.config_path):
            try:
                with open(self.config_path, "r", encoding="utf-8") as f:
                    self.data = yaml.safe_load(f) or {}
                log.info(f"Config loaded from {self.config_path}")
            except Exception as e:
                log.error(f"Failed to load config: {e}")
                self.data = {}
        else:
            log.info("No config found, creating defaults...")
            self.data = {}

        # Merge defaults for missing keys
        self.data = self._deep_merge(DEFAULT_CONFIG, self.data)
        self.save()

    def _deep_merge(self, defaults: dict, overrides: dict) -> dict:
        """Recursively merge overrides into defaults."""
        result = defaults.copy()
        for key, value in overrides.items():
            if key in result and isinstance(result[key], dict) and isinstance(value, dict):
                result[key] = self._deep_merge(result[key], value)
            else:
                result[key] = value
        return result

    def get(self, *keys: str, default: Any = None) -> Any:
        """Get a nested config value. Usage: config.get('server', 'port')"""
        current = self.data
        for key in keys:
            if isinstance(current, dict) and key in current:
                current = current[key]
            else:
                return default
        return current

    def set(self, *keys_and_value):
        """Set a nested config value. Last arg is the value."""
        if len(keys_and_value) < 2:
            raise ValueError("Need at least one key and a value")
        keys = keys_and_value[:-1]
        value = keys_and_value[-1]
        current = self.data
        for key in keys[:-1]:
            current = current.setdefault(key, {})
        current[keys[-1]] = value

    def save(self):
        """Persist config to disk."""
        try:
            with open(self.config_path, "w", encoding="utf-8") as f:
                yaml.dump(self.data, f, default_flow_style=False, sort_keys=False)
        except Exception as e:
            log.error(f"Failed to save config: {e}")

    def reload(self):
        """Re-read config from disk."""
        self._loaded = False
        self.data = {}
        self._load()
