"""Shared fixtures: every test runs in a temp dir with isolated env + singletons."""

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Test runs must never write into the live engine's log (it made real problems hard to read).
import tempfile  # noqa: E402
os.environ.setdefault("AUTOCRAT_LOG_DIR", tempfile.mkdtemp(prefix="autocrat-test-logs-"))

TEST_TOKEN = "test-token-0123456789abcdefghijklmnopqrstuv"


def _reset_singletons():
    from nexus.core import llm_router, secrets, store
    from nexus.core.config import Config
    from nexus.core.events import EventBus
    from nexus.core.runtime import RuntimeControl

    Config.reset_for_tests()
    EventBus.reset_for_tests()
    RuntimeControl.reset_for_tests()
    secrets.reset_for_tests()
    llm_router.set_router(None)
    old = store._default
    store.set_store(None)
    if old is not None:
        old.close()


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AUTOCRAT_ENV_FILE", str(tmp_path / "test.env"))  # nonexistent unless a test writes it
    monkeypatch.setenv("AUTOCRAT_AUTH_TOKEN", TEST_TOKEN)
    monkeypatch.setenv("RUN_MODE", "dry_run")
    monkeypatch.setenv("MX_CHECK", "0")   # no DNS lookups in tests
    monkeypatch.setenv("EMAIL_MIN_GAP_SECONDS", "0")   # tests send in bursts; live mail is paced
    monkeypatch.setenv("EMAIL_MAX_PER_TICK", "1000")
    monkeypatch.setenv("AUTOCRAT_DB_PATH", str(tmp_path / "autocrat.db"))
    monkeypatch.setenv("AUTOCRAT_BRAIN", "0")
    monkeypatch.setenv("AUTOCRAT_PROFILE", "core")
    monkeypatch.setenv("AUTOCRAT_PLUGIN_PATHS", "")
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://127.0.0.1:9")  # nothing listens; fail fast
    for var in ("TELEGRAM_ALLOWED_USER_IDS", "TELEGRAM_ALLOWED_CHAT_IDS", "TELEGRAM_BOT_TOKEN",
                "GEMINI_API_KEY", "ANTHROPIC_API_KEY", "AUTOCRAT_CORS_ORIGINS"):
        monkeypatch.delenv(var, raising=False)
    _reset_singletons()
    yield tmp_path
    _reset_singletons()


@pytest.fixture
def store(tmp_path):
    from nexus.core.store import Store, set_store
    s = Store(tmp_path / "pipeline.db")
    set_store(s)
    return s


@pytest.fixture
def make_engine(monkeypatch):
    """Build a core-profile engine (no desktop plugins, no ML brain, no LLM probe)."""
    from nexus.core.ai_engine import GeminiEngine
    from nexus.core.engine import NexusEngine

    monkeypatch.setattr(GeminiEngine, "_check_ollama_ready", lambda self: False)

    def _make(profile="core"):
        engine = NexusEngine()
        engine.gemini._ready = False  # never call a real LLM in tests
        engine.load_all_plugins(profile=profile)
        return engine

    return _make
