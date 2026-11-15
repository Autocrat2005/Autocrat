"""
Autocrat — Configuration Manager
YAML-based config with defaults, auto-creation, and hot reload.
"""

import copy
import os
import yaml
from typing import Any, Dict, Optional
from nexus.core.logger import get_logger
from nexus.core.secrets import SECRET_CONFIG_PATHS, get_secret

log = get_logger("config")

DEFAULT_CONFIG = {
    "nexus": {
        "name": "Autocrat",
        "version": "1.0.0",
        "log_level": "INFO",
    },
    "server": {
        "host": "127.0.0.1",
        "port": 9000,
        "cors_origins": ["http://localhost:9000", "http://127.0.0.1:9000", "http://localhost:5173"],
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
            "pipeline",
        ],
        "settings": {},
    },
    "scheduler": {
        "jobs": [],
    },
    "workflows": {
        "directory": "workflows",
    },
    "ai": {},
    "llm": {
        # task → "provider:model" or a list tried in order (fallback chain).
        # Spend order: free local prefilter → Claude Haiku 5.5 (cheapest per token
        # per current listings) → DeepSeek Flash (overflow) → local Llama (last resort).
        "routes": {
            "prefilter": "ollama:llama3.2:3b",
            "default": ["anthropic:claude-haiku-5-5", "deepseek:deepseek-flash", "ollama:llama3.2:3b"],
            "triage": ["anthropic:claude-haiku-5-5", "deepseek:deepseek-flash", "ollama:llama3.2:3b"],
            "spec": ["deepseek:deepseek-flash", "anthropic:claude-haiku-5-5", "ollama:llama3.2:3b"],
            # Prospect-facing text never falls back to the small local model.
            "copy": ["anthropic:claude-haiku-5-5", "deepseek:deepseek-flash"],
            "closer": ["anthropic:claude-haiku-5-5", "deepseek:deepseek-flash"],
            "local": "ollama:llama3.2:3b",
        },
        # USD per 1M tokens. VERIFY on the providers' price pages (third-party
        # listings checked 2026-11-15). DeepSeek uses its peak rate to stay conservative.
        # cache_read / cache_write apply to Anthropic prompt caching.
        "pricing": {
            "claude-haiku-5-5": {"input": 0.10, "output": 0.50, "cache_read": 0.01, "cache_write": 0.125},
            "deepseek-flash": {"input": 0.30, "output": 1.20},
        },
        # Prepaid credit per provider, paced over a rolling 30-day window.
        "provider_budgets": {"anthropic": 7.8, "deepseek": 4.9},
        "monthly_budget_usd": 12.7,
        "budget_window_days": 30,
        "daily_burst": 2.0,
        # Haiku 5.5 bills 5x for prompts over 100K tokens: never send one.
        "max_prompt_tokens": {"claude-haiku-5-5": 90000},
        # Cache Anthropic system prompts at least this long (chars); shorter ones
        # aren't worth the cache-write premium.
        "anthropic_cache_min_chars": 4000,
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

        # Merge defaults for missing keys. We deliberately do NOT save here:
        # rewriting the tracked YAML on every boot dirtied the repo and would
        # persist anything placed in it (including secrets).
        self.data = self._deep_merge(DEFAULT_CONFIG, self.data)
        self._strip_secrets(self.data, warn=True)

    def _deep_merge(self, defaults: dict, overrides: dict) -> dict:
        """Recursively merge overrides into defaults."""
        result = defaults.copy()
        for key, value in overrides.items():
            if key in result and isinstance(result[key], dict) and isinstance(value, dict):
                result[key] = self._deep_merge(result[key], value)
            else:
                result[key] = value
        return result

    @staticmethod
    def _strip_secrets(data: dict, warn: bool = False) -> None:
        """Remove secret paths from a config dict (secrets come from env only)."""
        for path in SECRET_CONFIG_PATHS:
            parent = data
            for key in path[:-1]:
                parent = parent.get(key) if isinstance(parent, dict) else None
                if parent is None:
                    break
            if isinstance(parent, dict) and path[-1] in parent:
                value = parent.pop(path[-1])
                if warn and value:
                    log.warning(
                        f"Ignoring secret in nexus_config.yaml at {'.'.join(path)} — "
                        f"set {SECRET_CONFIG_PATHS[path]} in .env instead"
                    )
            if isinstance(parent, dict) and not parent and len(path) > 1 and path[0] == "auth":
                data.pop("auth", None)

    def get(self, *keys: str, default: Any = None) -> Any:
        """Get a nested config value. Usage: config.get('server', 'port')

        Secret paths (see nexus.core.secrets.SECRET_CONFIG_PATHS) resolve from
        the environment, never from YAML.
        """
        env_name = SECRET_CONFIG_PATHS.get(tuple(keys))
        if env_name:
            return get_secret(env_name) or default
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
        """Persist config to disk (secrets are always stripped first)."""
        try:
            data = copy.deepcopy(self.data)
            self._strip_secrets(data)
            with open(self.config_path, "w", encoding="utf-8") as f:
                yaml.dump(data, f, default_flow_style=False, sort_keys=False)
        except Exception as e:
            log.error(f"Failed to save config: {e}")

    @classmethod
    def reset_for_tests(cls):
        cls._instance = None

    def reload(self):
        """Re-read config from disk."""
        self._loaded = False
        self.data = {}
        self._load()
