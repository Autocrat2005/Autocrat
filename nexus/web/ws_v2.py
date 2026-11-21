"""
Autocrat — /ws/v2: authenticated, typed event stream for the HUD and desk↔core link.

Protocol (JSON frames)
  client → {"type": "auth", "token": "<AUTOCRAT_AUTH_TOKEN>", "client": "hud"}   MUST be first, within 5 s
  server → {"type": "welcome", "v": 2, "run_mode", "kill_switch", "profile"}
           or closes with code 4401 (bad/missing auth)

  client → {"type": "subscribe", "topics": ["lead", "outreach", "control"]}   ("*" = everything)
  client → {"type": "command", "id": "c1", "text": "pipeline"}
  client → {"type": "replay", "lead_id": "<id>"}            full event history for one lead
  client → {"type": "ping"}

  server → {"type": "event", "v": 2, "event": "lead.transition", "topic": "lead",
            "seq": 42, "ts": 1700000000.0, "lead_id": "...", "payload": {...}}
  server → {"type": "command_result", "id": "c1", "result": {...}}

Events are produced by any thread (workers, executors, Telegram) via the
EventBus; the hub hands them to each client's queue with call_soon_threadsafe,
so nothing is dropped for being emitted off the event loop.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import threading
from typing import Any, Dict, Optional, Set

from fastapi import WebSocket, WebSocketDisconnect

from nexus.core.events import EventBus
from nexus.core.logger import get_logger

log = get_logger("ws_v2")

AUTH_TIMEOUT_S = 5.0
QUEUE_SIZE = 1000
CLOSE_UNAUTHORIZED = 4401


def _json_safe(value: Any) -> Any:
    return json.loads(json.dumps(value, default=str))


def make_envelope(entry: Dict[str, Any]) -> Dict[str, Any]:
    event = entry.get("event", "")
    data = entry.get("data")
    lead_id = data.get("lead_id") if isinstance(data, dict) else None
    return {
        "type": "event", "v": 2, "event": event, "topic": event.split(".", 1)[0],
        "seq": entry.get("seq"), "ts": entry.get("ts"), "lead_id": lead_id,
        "payload": _json_safe(data),
    }


def _offer(queue: asyncio.Queue, item: Dict[str, Any]) -> None:
    """Runs on the client's loop. Drop the oldest frame rather than block producers."""
    if queue.full():
        try:
            queue.get_nowait()
        except asyncio.QueueEmpty:
            pass
    queue.put_nowait(item)


class _Client:
    def __init__(self, ws: WebSocket, loop: asyncio.AbstractEventLoop, name: str):
        self.ws = ws
        self.loop = loop
        self.name = name
        self.topics: Set[str] = {"*"}
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=QUEUE_SIZE)

    def wants(self, topic: str) -> bool:
        return "*" in self.topics or topic in self.topics


class WsHub:
    def __init__(self, events: Optional[EventBus] = None):
        self._events = events
        self._clients: Dict[int, _Client] = {}
        self._lock = threading.Lock()
        self._subscribed_bus: Optional[EventBus] = None

    def _ensure_subscribed(self):
        bus = self._events or EventBus()
        if self._subscribed_bus is not bus:  # bus singleton may be replaced in tests
            bus.on("*", self._on_event)
            self._subscribed_bus = bus

    def _on_event(self, entry: Dict[str, Any]) -> None:
        envelope = None
        with self._lock:
            clients = list(self._clients.values())
        for client in clients:
            topic = entry.get("event", "").split(".", 1)[0]
            if not client.wants(topic):
                continue
            if envelope is None:
                try:
                    envelope = make_envelope(entry)
                except Exception as e:
                    log.warning(f"Unserialisable event {entry.get('event')}: {e}")
                    return
            try:
                client.loop.call_soon_threadsafe(_offer, client.queue, envelope)
            except RuntimeError:
                pass  # client loop closed; it will be removed on disconnect

    def add(self, client: _Client) -> None:
        self._ensure_subscribed()
        with self._lock:
            self._clients[id(client)] = client

    def remove(self, client: _Client) -> None:
        with self._lock:
            self._clients.pop(id(client), None)

    @property
    def client_count(self) -> int:
        with self._lock:
            return len(self._clients)


hub = WsHub()


async def _authenticate(ws: WebSocket, expected_token: str) -> Optional[str]:
    try:
        raw = await asyncio.wait_for(ws.receive_text(), timeout=AUTH_TIMEOUT_S)
        frame = json.loads(raw)
    except (asyncio.TimeoutError, json.JSONDecodeError, WebSocketDisconnect, RuntimeError):
        return None
    if not isinstance(frame, dict) or frame.get("type") != "auth":
        return None
    token = str(frame.get("token") or "")
    if not expected_token or not hmac.compare_digest(token.encode(), expected_token.encode()):
        return None
    return str(frame.get("client") or "hud")[:32]


async def handle_ws_v2(ws: WebSocket, *, expected_token: str, get_engine) -> None:
    await ws.accept()
    client_name = await _authenticate(ws, expected_token)
    if not client_name:
        try:
            await ws.send_json({"type": "error", "code": CLOSE_UNAUTHORIZED, "error": "unauthorized"})
        except Exception:
            pass
        await ws.close(code=CLOSE_UNAUTHORIZED)
        return

    loop = asyncio.get_running_loop()
    EventBus().bind_loop(loop)
    client = _Client(ws, loop, client_name)
    hub.add(client)
    engine = get_engine()
    welcome = {"type": "welcome", "v": 2, "client": client_name}
    if engine is not None:
        welcome.update(engine.runtime.status())
        welcome["profile"] = getattr(engine, "profile", "full")
    await ws.send_json(welcome)

    async def sender():
        while True:
            frame = await client.queue.get()
            await ws.send_json(frame)

    send_task = asyncio.create_task(sender())
    actor = f"ws:{client_name}"
    try:
        while True:
            raw = await ws.receive_text()
            try:
                frame = json.loads(raw)
            except json.JSONDecodeError:
                await ws.send_json({"type": "error", "error": "frames must be JSON"})
                continue
            kind = frame.get("type")
            if kind == "ping":
                await ws.send_json({"type": "pong"})
            elif kind == "subscribe":
                topics = frame.get("topics") or ["*"]
                client.topics = {str(t) for t in topics}
                await ws.send_json({"type": "subscribed", "topics": sorted(client.topics)})
            elif kind == "command":
                engine = get_engine()
                if engine is None:
                    result = {"success": False, "error": "Engine not initialized"}
                else:
                    text = str(frame.get("text") or "")[:2000]
                    result = await loop.run_in_executor(None, lambda: engine.execute(text, actor=actor))
                await ws.send_json({"type": "command_result", "id": frame.get("id"),
                                    "result": _json_safe(result)})
            elif kind == "replay":
                engine = get_engine()
                lead_id = str(frame.get("lead_id") or "")
                events = engine.store.lead_events(lead_id) if engine is not None else []
                await ws.send_json({"type": "replay", "lead_id": lead_id, "events": _json_safe(events)})
            else:
                await ws.send_json({"type": "error", "error": f"unknown frame type {kind!r}"})
    except WebSocketDisconnect:
        pass
    finally:
        hub.remove(client)
        send_task.cancel()
