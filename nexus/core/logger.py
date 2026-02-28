"""
Autocrat — Structured Logging System
Color output, file rotation, WebSocket broadcast.
"""

import logging
import os
import sys
import json
from datetime import datetime
from logging.handlers import RotatingFileHandler
from typing import Callable, Optional, List


class WebSocketLogHandler(logging.Handler):
    """Broadcasts log records over WebSocket to the web UI."""

    def __init__(self):
        super().__init__()
        self._listeners: List[Callable] = []

    def add_listener(self, callback: Callable):
        self._listeners.append(callback)

    def remove_listener(self, callback: Callable):
        self._listeners = [l for l in self._listeners if l != callback]

    def emit(self, record: logging.LogRecord):
        try:
            entry = {
                "timestamp": datetime.fromtimestamp(record.created).isoformat(),
                "level": record.levelname,
                "module": record.name,
                "message": record.getMessage(),
            }
            for listener in self._listeners:
                try:
                    listener(entry)
                except Exception:
                    pass
        except Exception:
            self.handleError(record)


class ColorFormatter(logging.Formatter):
    """ANSI color formatter for terminal output."""

    COLORS = {
        "DEBUG": "\033[36m",      # Cyan
        "INFO": "\033[32m",       # Green
        "WARNING": "\033[33m",    # Yellow
        "ERROR": "\033[31m",      # Red
        "CRITICAL": "\033[1;31m", # Bold Red
    }
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"

    def format(self, record: logging.LogRecord) -> str:
        color = self.COLORS.get(record.levelname, self.RESET)
        level = f"{color}{record.levelname:<8}{self.RESET}"
        timestamp = f"{self.DIM}{datetime.fromtimestamp(record.created).strftime('%H:%M:%S')}{self.RESET}"
        module = f"{self.BOLD}{record.name}{self.RESET}"
        message = record.getMessage()
        return f"  {timestamp}  {level}  {module} → {message}"


class NexusLogger:
    """Central logging facility for Autocrat."""

    _instance: Optional["NexusLogger"] = None
    _initialized = False

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self, log_dir: str = "logs", level: int = logging.DEBUG):
        if NexusLogger._initialized:
            return
        NexusLogger._initialized = True

        self.log_dir = log_dir
        os.makedirs(log_dir, exist_ok=True)

        # Root logger
        self.root = logging.getLogger("nexus")
        self.root.setLevel(level)
        self.root.handlers.clear()

        # Console handler (color)
        console = logging.StreamHandler(sys.stdout)
        console.setLevel(logging.INFO)
        console.setFormatter(ColorFormatter())
        self.root.addHandler(console)

        # File handler (rotating)
        log_file = os.path.join(log_dir, "nexus.log")
        file_handler = RotatingFileHandler(
            log_file, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
        )
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(
            logging.Formatter("%(asctime)s | %(levelname)-8s | %(name)s | %(message)s")
        )
        self.root.addHandler(file_handler)

        # WebSocket handler
        self.ws_handler = WebSocketLogHandler()
        self.ws_handler.setLevel(logging.DEBUG)
        self.root.addHandler(self.ws_handler)

    def get(self, name: str) -> logging.Logger:
        """Get a child logger for a specific module."""
        return self.root.getChild(name)

    def add_ws_listener(self, callback: Callable):
        self.ws_handler.add_listener(callback)

    def remove_ws_listener(self, callback: Callable):
        self.ws_handler.remove_listener(callback)


# Convenience
def get_logger(name: str = "core") -> logging.Logger:
    """Get a NEXUS logger. Initializes the logging system on first call."""
    nexus_logger = NexusLogger()
    return nexus_logger.get(name)
