"""
Autocrat — Runtime control: run mode + global kill switch.

  RUN_MODE=dry_run  external/financial side effects are simulated, never sent
  RUN_MODE=live     side effects execute (still behind the approval gate)

Kill switch: when engaged, every destructive/external/financial command is
refused and background workers are stopped. It persists across restarts in
the store (kv table) so a crash can't silently re-arm outbound activity.
"""

from __future__ import annotations

import threading
from typing import Callable, List, Optional

from nexus.core.events import EventBus
from nexus.core.logger import get_logger
from nexus.core.secrets import get_secret

log = get_logger("runtime")

RUN_MODES = ("dry_run", "live")

# Side-effect classes (declared per command via register_command(side_effect=...))
SIDE_EFFECT_NONE = "none"                # read-only
SIDE_EFFECT_LOCAL = "local"              # changes this machine (default for legacy commands)
SIDE_EFFECT_INTERNAL = "internal"        # writes Autocrat's own state (store, files it owns)
SIDE_EFFECT_DESTRUCTIVE = "destructive"  # irreversible local change (delete, kill, shell)
SIDE_EFFECT_EXTERNAL = "external"        # contacts a third party (message, post, deploy)
SIDE_EFFECT_FINANCIAL = "financial"      # money: payment links, invoices
SIDE_EFFECTS = (
    SIDE_EFFECT_NONE, SIDE_EFFECT_LOCAL, SIDE_EFFECT_INTERNAL,
    SIDE_EFFECT_DESTRUCTIVE, SIDE_EFFECT_EXTERNAL, SIDE_EFFECT_FINANCIAL,
)
REQUIRES_APPROVAL = {SIDE_EFFECT_DESTRUCTIVE, SIDE_EFFECT_EXTERNAL, SIDE_EFFECT_FINANCIAL}
SIMULATED_IN_DRY_RUN = {SIDE_EFFECT_EXTERNAL, SIDE_EFFECT_FINANCIAL}
BLOCKED_BY_KILL_SWITCH = {SIDE_EFFECT_DESTRUCTIVE, SIDE_EFFECT_EXTERNAL, SIDE_EFFECT_FINANCIAL}


class RuntimeControl:
    """Process-wide run mode + kill switch (singleton)."""

    _instance: Optional["RuntimeControl"] = None
    _instance_lock = threading.Lock()

    def __new__(cls):
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        if getattr(self, "_initialized", False):
            return
        self._initialized = True
        self._lock = threading.Lock()
        mode = (get_secret("RUN_MODE", "dry_run") or "dry_run").strip().lower()
        if mode not in RUN_MODES:
            log.warning(f"Unknown RUN_MODE '{mode}' — falling back to dry_run")
            mode = "dry_run"
        self._run_mode = mode
        self._killed = False
        self._kill_reason = ""
        self._store = None
        self._on_kill: List[Callable[[], None]] = []

    # ── Persistence ──────────────────────────────────────────────

    def attach_store(self, store) -> None:
        """Load persisted kill-switch state from the store."""
        self._store = store
        try:
            if store.kv_get("kill_switch") == "engaged":
                self._killed = True
                self._kill_reason = store.kv_get("kill_reason") or "persisted"
                log.warning(f"Kill switch is ENGAGED (persisted): {self._kill_reason}")
        except Exception as e:
            log.warning(f"Could not read persisted kill switch: {e}")

    def _persist(self) -> None:
        if not self._store:
            return
        try:
            self._store.kv_set("kill_switch", "engaged" if self._killed else "released")
            self._store.kv_set("kill_reason", self._kill_reason)
        except Exception as e:
            log.warning(f"Could not persist kill switch: {e}")

    # ── Run mode ─────────────────────────────────────────────────

    @property
    def run_mode(self) -> str:
        return self._run_mode

    @property
    def is_dry_run(self) -> bool:
        return self._run_mode != "live"

    def set_run_mode(self, mode: str, actor: str = "system") -> str:
        mode = (mode or "").strip().lower()
        if mode not in RUN_MODES:
            raise ValueError(f"run mode must be one of {RUN_MODES}")
        with self._lock:
            previous, self._run_mode = self._run_mode, mode
        if previous != mode:
            log.warning(f"RUN_MODE {previous} → {mode} (by {actor})")
            EventBus().emit("control.run_mode", {"run_mode": mode, "previous": previous, "actor": actor})
        return mode

    # ── Kill switch ──────────────────────────────────────────────

    @property
    def killed(self) -> bool:
        return self._killed

    def on_kill(self, callback: Callable[[], None]) -> None:
        self._on_kill.append(callback)

    def engage_kill_switch(self, reason: str = "", actor: str = "system") -> None:
        with self._lock:
            self._killed = True
            self._kill_reason = reason or "manual"
        self._persist()
        log.warning(f"🛑 KILL SWITCH ENGAGED by {actor}: {self._kill_reason}")
        for callback in list(self._on_kill):
            try:
                callback()
            except Exception as e:
                log.error(f"Kill-switch callback failed: {e}")
        EventBus().emit("control.kill_switch", {"engaged": True, "reason": self._kill_reason, "actor": actor})

    def release_kill_switch(self, actor: str = "system") -> None:
        with self._lock:
            self._killed = False
            self._kill_reason = ""
        self._persist()
        log.warning(f"Kill switch released by {actor}")
        EventBus().emit("control.kill_switch", {"engaged": False, "actor": actor})

    # ── Gate ─────────────────────────────────────────────────────

    def check(self, side_effect: str) -> Optional[str]:
        """Return a refusal reason if this side effect may not run right now."""
        if self._killed and side_effect in BLOCKED_BY_KILL_SWITCH:
            return f"Kill switch engaged ({self._kill_reason}) — {side_effect} actions are blocked"
        return None

    def status(self) -> dict:
        return {
            "run_mode": self._run_mode,
            "kill_switch": self._killed,
            "kill_reason": self._kill_reason,
        }

    @classmethod
    def reset_for_tests(cls):
        with cls._instance_lock:
            cls._instance = None
