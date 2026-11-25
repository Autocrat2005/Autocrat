"""
Autocrat — Event Bus
Pub/sub system for inter-module communication.

Thread-safe: `emit()` may be called from any thread (plugin executors, worker
loops, the Telegram thread). Coroutine listeners are scheduled onto the loop
registered with `bind_loop()` — normally the uvicorn loop — instead of being
silently dropped when the emitter isn't on an event loop.

Subscriptions:
  * exact name   — bus.on("lead.transition", fn)  → fn(data)
  * pattern      — bus.on("lead.*", fn) / bus.on("*", fn) → fn(entry)
    where entry = {"event", "data", "ts", "seq"}
"""

import asyncio
import fnmatch
import itertools
import threading
import time
from collections import defaultdict
from typing import Any, Callable, Dict, List, Optional

from nexus.core.logger import get_logger

log = get_logger("events")


class EventBus:
    """Thread-safe pub/sub event bus (process-wide singleton)."""

    _instance: Optional["EventBus"] = None
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
        self._lock = threading.RLock()
        self._listeners: Dict[str, List[Callable]] = defaultdict(list)
        self._history: List[Dict[str, Any]] = []
        self._max_history = 500
        self._seq = itertools.count(1)
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    # ── Loop binding ─────────────────────────────────────────────

    def bind_loop(self, loop: Optional[asyncio.AbstractEventLoop]):
        """Register the loop that coroutine listeners should run on."""
        self._loop = loop

    # ── Subscription ─────────────────────────────────────────────

    def on(self, event: str, callback: Callable):
        """Subscribe to an event name or a glob pattern ("lead.*", "*")."""
        with self._lock:
            self._listeners[event].append(callback)
        log.debug(f"Listener registered for '{event}'")

    def off(self, event: str, callback: Callable):
        """Unsubscribe from an event."""
        with self._lock:
            self._listeners[event] = [l for l in self._listeners[event] if l != callback]

    def _matching(self, event: str):
        """Snapshot (callback, wants_entry) pairs for an event."""
        with self._lock:
            pairs = [(cb, False) for cb in self._listeners.get(event, [])]
            for pattern, callbacks in self._listeners.items():
                if "*" in pattern and fnmatch.fnmatchcase(event, pattern):
                    pairs.extend((cb, True) for cb in callbacks)
        return pairs

    # ── Emission ─────────────────────────────────────────────────

    def _record(self, event: str, data: Any) -> Dict[str, Any]:
        entry = {"event": event, "data": data, "ts": time.time(), "seq": next(self._seq)}
        with self._lock:
            self._history.append(entry)
            if len(self._history) > self._max_history:
                self._history = self._history[-self._max_history:]
        return entry

    def _schedule(self, coro, event: str):
        """Run a coroutine returned by a listener, from whatever thread we're on."""
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is not None:
            running.create_task(coro)
            return
        loop = self._loop
        if loop is not None and loop.is_running() and not loop.is_closed():
            asyncio.run_coroutine_threadsafe(coro, loop)
            return
        coro.close()
        log.warning(f"Dropped async listener for '{event}': no event loop bound (call bind_loop)")

    def emit(self, event: str, data: Any = None) -> Dict[str, Any]:
        """Emit an event. Safe to call from any thread."""
        entry = self._record(event, data)
        for callback, wants_entry in self._matching(event):
            try:
                result = callback(entry if wants_entry else data)
                if asyncio.iscoroutine(result):
                    self._schedule(result, event)
            except Exception as e:
                log.error(f"Event listener error for '{event}': {e}")
        return entry

    async def emit_async(self, event: str, data: Any = None) -> Dict[str, Any]:
        """Emit an event and await coroutine listeners in the current loop."""
        entry = self._record(event, data)
        for callback, wants_entry in self._matching(event):
            try:
                result = callback(entry if wants_entry else data)
                if asyncio.iscoroutine(result):
                    await result
            except Exception as e:
                log.error(f"Async event listener error for '{event}': {e}")
        return entry

    # ── Introspection ────────────────────────────────────────────

    def get_history(self, event: Optional[str] = None, limit: int = 50) -> List[Dict]:
        """Get event history, optionally filtered by name or glob pattern."""
        with self._lock:
            history = list(self._history)
        if event:
            history = [e for e in history if fnmatch.fnmatchcase(e["event"], event)]
        return history[-limit:]

    def clear(self):
        """Clear all listeners and history."""
        with self._lock:
            self._listeners.clear()
            self._history.clear()

    @classmethod
    def reset_for_tests(cls):
        with cls._instance_lock:
            cls._instance = None
