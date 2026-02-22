"""
NEXUS OS — Event Bus
Pub/sub system for inter-module communication.
"""

import asyncio
from typing import Any, Callable, Dict, List, Optional
from collections import defaultdict
from nexus.core.logger import get_logger

log = get_logger("events")


class EventBus:
    """Asynchronous pub/sub event bus."""

    _instance: Optional["EventBus"] = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        if hasattr(self, "_initialized"):
            return
        self._initialized = True
        self._listeners: Dict[str, List[Callable]] = defaultdict(list)
        self._history: List[Dict[str, Any]] = []
        self._max_history = 500

    def on(self, event: str, callback: Callable):
        """Subscribe to an event."""
        self._listeners[event].append(callback)
        log.debug(f"Listener registered for '{event}'")

    def off(self, event: str, callback: Callable):
        """Unsubscribe from an event."""
        self._listeners[event] = [l for l in self._listeners[event] if l != callback]

    def emit(self, event: str, data: Any = None):
        """Emit an event synchronously."""
        entry = {"event": event, "data": data}
        self._history.append(entry)
        if len(self._history) > self._max_history:
            self._history = self._history[-self._max_history:]

        for listener in self._listeners.get(event, []):
            try:
                result = listener(data)
                # If the listener returns a coroutine, schedule it
                if asyncio.iscoroutine(result):
                    try:
                        loop = asyncio.get_running_loop()
                        loop.create_task(result)
                    except RuntimeError:
                        pass
            except Exception as e:
                log.error(f"Event listener error for '{event}': {e}")

        # Also notify wildcard listeners
        for listener in self._listeners.get("*", []):
            try:
                listener(entry)
            except Exception as e:
                log.error(f"Wildcard listener error: {e}")

    async def emit_async(self, event: str, data: Any = None):
        """Emit an event with async listener support."""
        entry = {"event": event, "data": data}
        self._history.append(entry)

        for listener in self._listeners.get(event, []) + self._listeners.get("*", []):
            try:
                result = listener(data if event != "*" else entry)
                if asyncio.iscoroutine(result):
                    await result
            except Exception as e:
                log.error(f"Async event listener error for '{event}': {e}")

    def get_history(self, event: Optional[str] = None, limit: int = 50) -> List[Dict]:
        """Get event history, optionally filtered by event name."""
        if event:
            return [e for e in self._history if e["event"] == event][-limit:]
        return self._history[-limit:]

    def clear(self):
        """Clear all listeners and history."""
        self._listeners.clear()
        self._history.clear()
