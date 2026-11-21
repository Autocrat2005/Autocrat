"""
Meetings, booked fast and free: two concrete times in the prospect's timezone, a meeting link,
a calendar invite (.ics) in the confirmation email, reminders. No booking SaaS, no LLM tokens.

Settings (.env)
  OWNER_AVAILABILITY        "Mon-Sat 10-22"  your free hours (your timezone)
  OWNER_UTC_OFFSET_MINUTES  330              your timezone (IST)
  MEETING_MINUTES           20
  MEETING_LINK              optional fixed link (your Google Meet / Zoom room); else a free Jitsi room per lead
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from nexus.core.secrets import get_secret

DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def _offset(minutes: int) -> timezone:
    return timezone(timedelta(minutes=minutes))


def parse_availability(spec: str) -> Tuple[set, int, int]:
    """'Mon-Sat 10-22' → ({0..5}, 10, 22). Falls back to Mon-Sat 10-22."""
    m = re.match(r"\s*([a-z]{3})\s*-\s*([a-z]{3})\s+(\d{1,2})\s*-\s*(\d{1,2})", (spec or "").lower())
    if not m or m.group(1) not in DAYS or m.group(2) not in DAYS:
        return set(range(6)), 10, 22
    a, b = DAYS.index(m.group(1)), DAYS.index(m.group(2))
    days = set(range(a, b + 1)) if a <= b else set(range(a, 7)) | set(range(0, b + 1))
    return days, int(m.group(3)), int(m.group(4))


def meeting_link(lead_id: str) -> str:
    fixed = (get_secret("MEETING_LINK", "") or "").strip()
    return fixed or f"https://meet.jit.si/windrose-{(lead_id or uuid.uuid4().hex)[:12]}"


def open_slots(their_offset_min: int, *, now: Optional[datetime] = None, busy: Optional[List[datetime]] = None,
               count: int = 4, their_hours: Tuple[int, int] = (9, 18), lead_days: int = 5) -> List[datetime]:
    """Times (UTC) inside BOTH your availability and their business hours (Mon-Fri), at least 3h away,
    spread across days, never within 45 min of a booked call."""
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    owner_off = int(get_secret("OWNER_UTC_OFFSET_MINUTES", "330") or 330)
    days, start_h, end_h = parse_availability(get_secret("OWNER_AVAILABILITY", "Mon-Sat 10-22"))
    minutes = int(get_secret("MEETING_MINUTES", "20") or 20)
    busy = busy or []
    t = (now + timedelta(hours=3)).replace(second=0, microsecond=0)
    t += timedelta(minutes=(30 - t.minute % 30) % 30)
    picked: List[datetime] = []
    per_day: Dict[str, int] = {}
    end = now + timedelta(days=lead_days)
    while t < end and len(picked) < count:
        mine = t.astimezone(_offset(owner_off))
        theirs = t.astimezone(_offset(their_offset_min))
        ok = (mine.weekday() in days and start_h <= mine.hour and (mine + timedelta(minutes=minutes)).hour <= end_h
              and theirs.weekday() < 5 and their_hours[0] <= theirs.hour < their_hours[1]
              and all(abs((t - b).total_seconds()) >= 45 * 60 for b in busy))
        day = theirs.strftime("%Y-%m-%d")
        if ok and per_day.get(day, 0) < 2 and all(abs((t - p).total_seconds()) >= 3 * 3600 for p in picked):
            picked.append(t)
            per_day[day] = per_day.get(day, 0) + 1
        t += timedelta(minutes=30)
    return picked


def describe(t: datetime, offset_min: int) -> str:
    local = t.astimezone(_offset(offset_min))
    return local.strftime("%a %d %b, ") + local.strftime("%I:%M %p").lstrip("0").lower()


def slots_text(slots: List[datetime], their_offset_min: int) -> str:
    return "; ".join(f"{describe(s, their_offset_min)} their time = {s.strftime('%Y-%m-%dT%H:%MZ')}" for s in slots)


def parse_meeting(value: str, offered: Optional[List[datetime]] = None) -> Optional[datetime]:
    """The closer returns the ISO time it confirmed. Accept it only if it's one we offered (±1 min)
    or, failing that list, a sane future time within 14 days."""
    if not value:
        return None
    try:
        t = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    t = t.astimezone(timezone.utc)
    if offered:
        return t if any(abs((t - o).total_seconds()) <= 60 for o in offered) else None
    now = datetime.now(timezone.utc)
    return t if now < t < now + timedelta(days=14) else None


def ics_invite(start: datetime, *, summary: str, link: str, organizer: str, attendee: str,
               description: str = "", minutes: int = 20, uid: str = "") -> str:
    fmt = "%Y%m%dT%H%M%SZ"
    start = start.astimezone(timezone.utc)
    end = start + timedelta(minutes=minutes)
    clean = lambda s: (s or "").replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")  # noqa: E731
    return "\r\n".join([
        "BEGIN:VCALENDAR", "PRODID:-//Windrose Labs//Autocrat//EN", "VERSION:2.0", "METHOD:REQUEST", "BEGIN:VEVENT",
        f"UID:{uid or uuid.uuid4().hex}@autocrat", f"DTSTAMP:{datetime.now(timezone.utc).strftime(fmt)}",
        f"DTSTART:{start.strftime(fmt)}", f"DTEND:{end.strftime(fmt)}", f"SUMMARY:{clean(summary)}",
        f"DESCRIPTION:{clean((description + chr(10) if description else '') + link)}", f"LOCATION:{clean(link)}",
        f"ORGANIZER:mailto:{organizer}", f"ATTENDEE;RSVP=TRUE;ROLE=REQ-PARTICIPANT:mailto:{attendee}",
        "STATUS:CONFIRMED", "BEGIN:VALARM", "TRIGGER:-PT15M", "ACTION:DISPLAY", "DESCRIPTION:Reminder",
        "END:VALARM", "END:VEVENT", "END:VCALENDAR", ""])


def booked_meetings(store, since: Optional[datetime] = None) -> List[Dict[str, Any]]:
    """Upcoming meetings from the event log (newest booking per lead wins)."""
    since = since or datetime.now(timezone.utc) - timedelta(hours=2)
    out: Dict[str, Dict[str, Any]] = {}
    for r in store._conn().execute("SELECT lead_id, payload FROM lead_events WHERE type='meeting_booked' ORDER BY id"):
        import json
        try:
            p = json.loads(r[1] or "{}")
            t = datetime.fromisoformat(p["start"].replace("Z", "+00:00"))
        except (ValueError, KeyError, TypeError):
            continue
        out[r[0]] = {"lead_id": r[0], "start": t, "link": p.get("link", ""), "their_offset": p.get("their_offset", 0)}
    return sorted((m for m in out.values() if m["start"] >= since), key=lambda m: m["start"])
