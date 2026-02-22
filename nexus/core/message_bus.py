"""
NEXUS OS — Message Bus (Cross-Channel Communication)
Routes commands and results between channels: Web, Telegram, Discord, VS Code.
Any input source can send a command, and results are dispatched to all
configured output channels.
"""

import asyncio
import time
import threading
from collections import defaultdict, deque
from typing import Any, Callable, Dict, List, Optional
from nexus.core.logger import get_logger

log = get_logger("message_bus")


class Message:
    """A message flowing through the bus."""
    __slots__ = ("text", "source", "channel_id", "user", "timestamp",
                 "result", "pending", "id")

    _counter = 0

    def __init__(self, text: str, source: str, channel_id: str = "",
                 user: str = ""):
        Message._counter += 1
        self.id = Message._counter
        self.text = text
        self.source = source          # "web" | "telegram" | "discord" | "vscode" | "heartbeat"
        self.channel_id = channel_id  # chat/channel id within the platform
        self.user = user
        self.timestamp = time.time()
        self.result: Optional[Dict] = None
        self.pending = True


class MessageBus:
    """
    Central message router for NEXUS OS.

    Channels register themselves and provide a callback for receiving results.
    When a command arrives from any source, results are broadcast to all
    subscribers (or a specific reply channel).
    """

    _instance: Optional["MessageBus"] = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        if hasattr(self, "_initialized"):
            return
        self._initialized = True
        self._lock = threading.Lock()
        self._channels: Dict[str, Callable] = {}   # name → async callback(msg)
        self._history: deque = deque(maxlen=200)
        self._engine = None
        self._subscribers: Dict[str, List[Callable]] = defaultdict(list)
        log.info("Message bus initialized")

    def set_engine(self, engine):
        """Connect the NEXUS engine for command execution."""
        self._engine = engine

    def register_channel(self, name: str, callback: Callable):
        """
        Register an output channel.
        callback(msg: Message) will be called with the completed message.
        """
        with self._lock:
            self._channels[name] = callback
        log.info(f"Channel registered: {name}")

    def unregister_channel(self, name: str):
        with self._lock:
            self._channels.pop(name, None)

    def subscribe(self, event: str, callback: Callable):
        """Subscribe to bus events: 'command', 'result', 'broadcast'."""
        self._subscribers[event].append(callback)

    def send(self, text: str, source: str, channel_id: str = "",
             user: str = "", reply_to: str = None) -> Message:
        """
        Send a command into the bus. Executes via engine and dispatches result.

        Args:
            text: The command text
            source: Origin channel ("web", "telegram", "discord", "vscode", "heartbeat")
            channel_id: Chat/channel ID on the platform
            user: Username
            reply_to: If set, only reply to this channel (not broadcast)

        Returns:
            The completed Message with result attached.
        """
        msg = Message(text, source, channel_id, user)
        self._history.append(msg)

        # Notify subscribers
        for cb in self._subscribers.get("command", []):
            try:
                cb(msg)
            except Exception:
                pass

        # Execute
        if self._engine:
            try:
                msg.result = self._engine.execute(text)
            except Exception as e:
                msg.result = {"success": False, "error": str(e)}
        else:
            msg.result = {"success": False, "error": "Engine not connected"}

        msg.pending = False
        msg.result["_source"] = source
        msg.result["_msg_id"] = msg.id

        # Dispatch result
        self._dispatch(msg, reply_to)

        # Notify subscribers
        for cb in self._subscribers.get("result", []):
            try:
                cb(msg)
            except Exception:
                pass

        return msg

    def send_async(self, text: str, source: str, channel_id: str = "",
                   user: str = "", reply_to: str = None):
        """Fire-and-forget version that runs in a thread."""
        t = threading.Thread(
            target=self.send,
            args=(text, source, channel_id, user, reply_to),
            daemon=True,
        )
        t.start()
        return t

    def broadcast(self, text: str, source: str = "system"):
        """Broadcast a notification to all channels (not a command)."""
        msg = Message(text, source)
        msg.result = {"success": True, "result": text, "type": "broadcast"}
        msg.pending = False
        self._dispatch(msg, reply_to=None)

    def _dispatch(self, msg: Message, reply_to: str = None):
        """Send result to channels."""
        with self._lock:
            targets = {reply_to: self._channels[reply_to]} if reply_to and reply_to in self._channels \
                else dict(self._channels)

        for name, callback in targets.items():
            # Don't echo back to source unless it's a broadcast
            if name == msg.source and msg.result.get("type") != "broadcast":
                continue
            try:
                if asyncio.iscoroutinefunction(callback):
                    # Schedule in event loop if one exists
                    try:
                        loop = asyncio.get_running_loop()
                        loop.create_task(callback(msg))
                    except RuntimeError:
                        asyncio.run(callback(msg))
                else:
                    callback(msg)
            except Exception as e:
                log.warning(f"Dispatch to {name} failed: {e}")

    def get_history(self, limit: int = 50) -> List[Dict]:
        """Get recent message history."""
        items = []
        for m in list(self._history)[-limit:]:
            items.append({
                "id": m.id,
                "text": m.text,
                "source": m.source,
                "user": m.user,
                "timestamp": m.timestamp,
                "success": m.result.get("success") if m.result else None,
            })
        return items

    def get_stats(self) -> Dict:
        return {
            "channels": list(self._channels.keys()),
            "total_messages": len(self._history),
        }
