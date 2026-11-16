"""
Autocrat — Lead state machine.

Happy path:
  DISCOVERED → TRIAGED → BUILDING → STAGED → APPROVED → SENT
             → OPENED → FORWARDED → REPLIED → DEPOSIT_OPENED → WON

Exits:  ARCHIVED (triage/build rejected), LOST (went cold), DNC (do-not-contact,
        reachable from anywhere, permanent, and suppresses the contact forever).

Engagement states (SENT…DEPOSIT_OPENED) come from telemetry that can arrive
out of order. Moving "backwards" among them (an OPENED beacon after a REPLIED)
is recorded as a *signal* event without changing state, so state never
regresses and alerts still fire.
"""

from __future__ import annotations

import re
from enum import Enum
from typing import Dict, FrozenSet, Tuple


class LeadState(str, Enum):
    DISCOVERED = "DISCOVERED"
    TRIAGED = "TRIAGED"
    BUILDING = "BUILDING"
    STAGED = "STAGED"
    APPROVED = "APPROVED"
    SENT = "SENT"
    OPENED = "OPENED"
    FORWARDED = "FORWARDED"
    REPLIED = "REPLIED"
    DEPOSIT_OPENED = "DEPOSIT_OPENED"
    WON = "WON"
    LOST = "LOST"
    ARCHIVED = "ARCHIVED"
    DNC = "DNC"


S = LeadState
TERMINAL: FrozenSet[LeadState] = frozenset({S.WON, S.LOST, S.ARCHIVED, S.DNC})

ENGAGEMENT_RANK: Dict[LeadState, int] = {
    S.SENT: 0, S.OPENED: 1, S.FORWARDED: 2, S.REPLIED: 3, S.DEPOSIT_OPENED: 4,
}

_PRE_SEND: Dict[LeadState, FrozenSet[LeadState]] = {
    S.DISCOVERED: frozenset({S.TRIAGED, S.ARCHIVED}),
    S.TRIAGED: frozenset({S.BUILDING, S.STAGED, S.ARCHIVED}),  # STAGED = draft without a demo
    S.BUILDING: frozenset({S.STAGED, S.TRIAGED, S.ARCHIVED}),  # TRIAGED = build failed, retry
    S.STAGED: frozenset({S.APPROVED, S.ARCHIVED}),
    S.APPROVED: frozenset({S.SENT, S.STAGED}),                  # STAGED = approval withdrawn
}


def allowed_targets(current: LeadState) -> FrozenSet[LeadState]:
    """States reachable from `current` as real transitions."""
    if current in TERMINAL:
        return frozenset()
    targets = set(_PRE_SEND.get(current, frozenset()))
    if current in ENGAGEMENT_RANK:
        rank = ENGAGEMENT_RANK[current]
        targets |= {s for s, r in ENGAGEMENT_RANK.items() if r > rank}
        targets |= {S.LOST}
        if current in (S.REPLIED, S.DEPOSIT_OPENED):
            targets |= {S.WON}
    targets.add(S.DNC)
    return frozenset(targets)


class TransitionKind(str, Enum):
    TRANSITION = "transition"   # state changes
    SIGNAL = "signal"           # recorded, state unchanged (out-of-order telemetry)


class InvalidTransition(ValueError):
    pass


def classify_transition(current: LeadState, target: LeadState) -> Tuple[TransitionKind, str]:
    """Decide whether current → target is a transition, a signal, or invalid."""
    current, target = LeadState(current), LeadState(target)
    if current == S.DNC:
        raise InvalidTransition("Lead is DNC — permanently closed")
    if target in allowed_targets(current):
        return TransitionKind.TRANSITION, f"{current.value} → {target.value}"
    if current in ENGAGEMENT_RANK and target in ENGAGEMENT_RANK:
        return TransitionKind.SIGNAL, f"{target.value} signal while {current.value}"
    if current in TERMINAL and target in ENGAGEMENT_RANK:
        return TransitionKind.SIGNAL, f"{target.value} signal on closed lead ({current.value})"
    raise InvalidTransition(f"Illegal transition {current.value} → {target.value}")


# ─── Contact normalisation (the unit of "never message twice") ──────────────

def contact_key(channel: str, value: str) -> str:
    """Normalise a contact handle so the same person maps to one key."""
    channel = (channel or "").strip().lower()
    value = (value or "").strip()
    if not value:
        return ""
    if channel in ("whatsapp", "wa", "phone", "sms", "call"):
        digits = re.sub(r"\D", "", value)
        if digits.startswith("00"):          # 00971… is the same number as +971…
            digits = digits[2:]
        return f"phone:+{digits}" if digits else ""
    if channel in ("x", "twitter"):
        return f"x:{value.lstrip('@').lower()}"
    if channel == "reddit":
        return f"reddit:{re.sub(r'^/?u/', '', value).lower()}"
    if channel == "email":
        return f"email:{value.lower()}"
    if channel == "discord":
        return f"discord:{value.lower()}"
    if channel == "hn":
        return f"hn:{value.lower()}"
    if channel in ("post", "link"):
        return f"post:{value.strip()}"
    return f"{channel}:{value.lower()}"
