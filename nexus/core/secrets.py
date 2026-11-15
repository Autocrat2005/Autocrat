"""
Autocrat — Secrets & environment loading.

Rules:
  * Secrets live in environment variables, loaded from `.env` (gitignored).
  * `.env.example` documents every variable with placeholder values.
  * Secrets are NEVER read from or written back to nexus_config.yaml.

Set AUTOCRAT_ENV_FILE to point at a different env file (tests use this).
"""

from __future__ import annotations

import os
import secrets as _secrets
import threading
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from nexus.core.logger import get_logger

log = get_logger("secrets")

PROJECT_ROOT = Path(__file__).resolve().parents[2]

# Config paths that used to hold secrets in YAML → the env var that replaces them.
# Config.get() resolves these from the environment; Config.save() strips them.
SECRET_CONFIG_PATHS: Dict[Tuple[str, ...], str] = {
    ("auth", "token"): "AUTOCRAT_AUTH_TOKEN",
    ("ai", "gemini_api_key"): "GEMINI_API_KEY",
    ("ai", "anthropic_api_key"): "ANTHROPIC_API_KEY",
    ("telegram", "bot_token"): "TELEGRAM_BOT_TOKEN",
}

_lock = threading.Lock()
_loaded_from: Optional[Path] = None


def env_file_path() -> Path:
    override = os.environ.get("AUTOCRAT_ENV_FILE")
    return Path(override) if override else PROJECT_ROOT / ".env"


def _parse_env_line(line: str) -> Optional[Tuple[str, str]]:
    line = line.strip()
    if not line or line.startswith("#") or "=" not in line:
        return None
    if line.startswith("export "):
        line = line[len("export "):]
    key, _, value = line.partition("=")
    key = key.strip()
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        value = value[1:-1]
    else:
        # Strip trailing inline comment on unquoted values
        hash_pos = value.find(" #")
        if hash_pos != -1:
            value = value[:hash_pos].rstrip()
    return (key, value) if key else None


def load_env(path: Optional[Path] = None, override: bool = False) -> Dict[str, str]:
    """Load KEY=VALUE pairs into os.environ (existing env wins unless override)."""
    global _loaded_from
    path = Path(path) if path else env_file_path()
    with _lock:
        if _loaded_from == path and not override:
            return {}
        loaded: Dict[str, str] = {}
        if path.exists():
            for raw in path.read_text(encoding="utf-8").splitlines():
                parsed = _parse_env_line(raw)
                if not parsed:
                    continue
                key, value = parsed
                if override or key not in os.environ:
                    os.environ[key] = value
                loaded[key] = value
        _loaded_from = path
        return loaded


def get_secret(name: str, default: str = "") -> str:
    load_env()
    return os.environ.get(name, default) or default


def env_list(name: str) -> List[str]:
    raw = get_secret(name)
    return [item.strip() for item in raw.split(",") if item.strip()]


def env_bool(name: str, default: bool = False) -> bool:
    raw = get_secret(name)
    if not raw:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def mask(value: str) -> str:
    """Safe-to-log representation of a secret."""
    if not value:
        return "<unset>"
    return f"{value[:2]}…[{len(value)} chars]"


def generate_token(nbytes: int = 32) -> str:
    return _secrets.token_urlsafe(nbytes)


def _upsert_env_value(path: Path, key: str, value: str) -> None:
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    replaced = False
    for i, raw in enumerate(lines):
        parsed = _parse_env_line(raw)
        if parsed and parsed[0] == key:
            lines[i] = f"{key}={value}"
            replaced = True
    if not replaced:
        lines.append(f"{key}={value}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def auth_token() -> str:
    """Return the control-plane auth token, creating one in .env if missing.

    Fails closed: there is always a token, so protected endpoints are never open.
    """
    token = get_secret("AUTOCRAT_AUTH_TOKEN")
    if token:
        return token
    token = generate_token()
    path = env_file_path()
    with _lock:
        try:
            _upsert_env_value(path, "AUTOCRAT_AUTH_TOKEN", token)
            log.warning(f"No AUTOCRAT_AUTH_TOKEN set — generated a new one and saved it to {path}")
        except OSError as e:
            log.warning(f"Generated an in-memory auth token (could not write {path}: {e})")
        os.environ["AUTOCRAT_AUTH_TOKEN"] = token
    return token


def reset_for_tests() -> None:
    """Forget which env file was loaded (tests switch AUTOCRAT_ENV_FILE)."""
    global _loaded_from
    with _lock:
        _loaded_from = None
