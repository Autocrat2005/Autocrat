"""
Autocrat — request context (who is acting).

engine.execute(text, actor=...) sets the actor for the duration of the call so
plugins and the audit log can attribute approvals ("telegram:12345:Yash",
"ws:hud", "local") without every handler taking an extra argument.
"""

from __future__ import annotations

import contextvars
from contextlib import contextmanager
from typing import Iterator

_actor: contextvars.ContextVar[str] = contextvars.ContextVar("autocrat_actor", default="local")


def current_actor() -> str:
    return _actor.get()


@contextmanager
def acting_as(actor: str) -> Iterator[None]:
    token = _actor.set(actor or "local")
    try:
        yield
    finally:
        _actor.reset(token)
