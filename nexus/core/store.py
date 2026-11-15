"""
Autocrat — SQLite store (WAL mode) for the revenue pipeline.

Tables
  leads          one row per prospect; `dedupe_key` UNIQUE so a lead is found once
  lead_events    append-only history (transitions, signals, notes) — drives HUD replay
  outreach       staged/approved/sent messages; DB-level guarantees nobody is messaged twice
  suppression    do-not-contact list keyed by normalised contact
  artifacts      demos, screenshots, specs per lead
  budget_ledger  every LLM call with tokens + cost
  audit_log      who approved/killed/changed what
  kv             small runtime flags (kill switch)

Concurrency: one connection per thread, WAL + busy_timeout, writes in
BEGIN IMMEDIATE transactions. Events are emitted on the EventBus after commit.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

from nexus.core.events import EventBus
from nexus.core.lead_state import (
    ENGAGEMENT_RANK, InvalidTransition, LeadState, TransitionKind,
    classify_transition, contact_key as make_contact_key,
)
from nexus.core.logger import get_logger
from nexus.core.secrets import PROJECT_ROOT, get_secret

log = get_logger("store")


class AlreadyContacted(RuntimeError):
    """Raised when an action would message a lead/contact a second time."""


class Suppressed(RuntimeError):
    """Raised when the contact is on the do-not-contact list."""


MIGRATIONS: List[str] = [
    # v1
    """
    CREATE TABLE IF NOT EXISTS kv (
        key TEXT PRIMARY KEY,
        value TEXT,
        updated_at REAL NOT NULL
    );
    CREATE TABLE IF NOT EXISTS leads (
        id TEXT PRIMARY KEY,
        dedupe_key TEXT NOT NULL UNIQUE,
        source TEXT NOT NULL,
        source_ref TEXT,
        name TEXT,
        company TEXT,
        channel TEXT,
        contact TEXT,
        contact_key TEXT,
        market TEXT,
        niche TEXT,
        url TEXT,
        score_intent REAL,
        score_proto REAL,
        state TEXT NOT NULL DEFAULT 'DISCOVERED',
        data TEXT NOT NULL DEFAULT '{}',
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_leads_state ON leads(state);
    CREATE INDEX IF NOT EXISTS idx_leads_contact ON leads(contact_key);

    CREATE TABLE IF NOT EXISTS lead_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        lead_id TEXT NOT NULL REFERENCES leads(id),
        ts REAL NOT NULL,
        type TEXT NOT NULL,
        from_state TEXT,
        to_state TEXT,
        actor TEXT,
        payload TEXT NOT NULL DEFAULT '{}'
    );
    CREATE INDEX IF NOT EXISTS idx_events_lead ON lead_events(lead_id, id);

    CREATE TABLE IF NOT EXISTS outreach (
        id TEXT PRIMARY KEY,
        lead_id TEXT NOT NULL REFERENCES leads(id),
        channel TEXT NOT NULL,
        kind TEXT NOT NULL DEFAULT 'first_touch',
        contact_key TEXT NOT NULL,
        idempotency_key TEXT NOT NULL UNIQUE,
        draft TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'staged',
        approved_by TEXT,
        sent_by TEXT,
        created_at REAL NOT NULL,
        approved_at REAL,
        sent_at REAL
    );
    -- The hard guarantee: one sent message of each kind per contact, ever.
    CREATE UNIQUE INDEX IF NOT EXISTS uq_outreach_contact_sent
        ON outreach(contact_key, kind) WHERE status IN ('sending', 'sent');

    CREATE TABLE IF NOT EXISTS suppression (
        contact_key TEXT PRIMARY KEY,
        reason TEXT,
        created_at REAL NOT NULL
    );

    CREATE TABLE IF NOT EXISTS artifacts (
        id TEXT PRIMARY KEY,
        lead_id TEXT NOT NULL REFERENCES leads(id),
        kind TEXT NOT NULL,
        uri TEXT,
        meta TEXT NOT NULL DEFAULT '{}',
        created_at REAL NOT NULL
    );

    CREATE TABLE IF NOT EXISTS budget_ledger (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts REAL NOT NULL,
        provider TEXT NOT NULL,
        model TEXT NOT NULL,
        task TEXT,
        input_tokens INTEGER NOT NULL DEFAULT 0,
        output_tokens INTEGER NOT NULL DEFAULT 0,
        cost_usd REAL NOT NULL DEFAULT 0,
        lead_id TEXT,
        ok INTEGER NOT NULL DEFAULT 1
    );
    CREATE INDEX IF NOT EXISTS idx_budget_ts ON budget_ledger(ts);

    CREATE TABLE IF NOT EXISTS audit_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts REAL NOT NULL,
        actor TEXT NOT NULL,
        action TEXT NOT NULL,
        detail TEXT NOT NULL DEFAULT '{}'
    );
    """,
]


def _now() -> float:
    return time.time()


def _new_id() -> str:
    # Always contains a hex letter, so the parser never coerces an ID to int.
    while True:
        candidate = uuid.uuid4().hex[:12]
        if not candidate.isdigit():
            return candidate


def _row(row: Optional[sqlite3.Row]) -> Optional[Dict[str, Any]]:
    if row is None:
        return None
    d = dict(row)
    for key in ("data", "payload", "meta", "detail"):
        if key in d and isinstance(d[key], str):
            try:
                d[key] = json.loads(d[key])
            except json.JSONDecodeError:
                pass
    return d


def default_db_path() -> Path:
    raw = get_secret("AUTOCRAT_DB_PATH", ".autocrat/autocrat.db")
    path = Path(raw)
    return path if path.is_absolute() else PROJECT_ROOT / path


class Store:
    def __init__(self, path: Optional[os.PathLike] = None, events: Optional[EventBus] = None):
        self.path = Path(path) if path else default_db_path()
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._write_lock = threading.Lock()
        self._events = events or EventBus()
        self._connections: List[sqlite3.Connection] = []
        self._conns_lock = threading.Lock()
        self.migrate()

    # ── Connection management ────────────────────────────────────

    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(str(self.path), timeout=10, isolation_level=None,
                                   check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA synchronous=NORMAL")
            self._local.conn = conn
            with self._conns_lock:
                self._connections.append(conn)
        return conn

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Serialised write transaction (BEGIN IMMEDIATE)."""
        conn = self._conn()
        with self._write_lock:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise

    def journal_mode(self) -> str:
        return self._conn().execute("PRAGMA journal_mode").fetchone()[0]

    def close(self) -> None:
        with self._conns_lock:
            for conn in self._connections:
                try:
                    conn.close()
                except Exception:
                    pass
            self._connections.clear()
        self._local = threading.local()

    def migrate(self) -> int:
        conn = self._conn()
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        for idx in range(version, len(MIGRATIONS)):
            with self._write_lock:
                conn.executescript("BEGIN;" + MIGRATIONS[idx] + f"; PRAGMA user_version={idx + 1}; COMMIT;")
            log.info(f"Store migrated to v{idx + 1}")
        return len(MIGRATIONS)

    # ── KV + audit ───────────────────────────────────────────────

    def kv_get(self, key: str) -> Optional[str]:
        row = self._conn().execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def kv_set(self, key: str, value: str) -> None:
        with self.transaction() as c:
            c.execute(
                "INSERT INTO kv(key, value, updated_at) VALUES(?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                (key, value, _now()),
            )

    def audit(self, actor: str, action: str, detail: Optional[Dict[str, Any]] = None) -> None:
        with self.transaction() as c:
            c.execute("INSERT INTO audit_log(ts, actor, action, detail) VALUES(?,?,?,?)",
                      (_now(), actor, action, json.dumps(detail or {}, default=str)))

    def audit_entries(self, limit: int = 50) -> List[Dict[str, Any]]:
        rows = self._conn().execute(
            "SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [_row(r) for r in rows]

    # ── Suppression ──────────────────────────────────────────────

    def is_suppressed(self, contact_key: str) -> bool:
        if not contact_key:
            return False
        row = self._conn().execute(
            "SELECT 1 FROM suppression WHERE contact_key=?", (contact_key,)).fetchone()
        return row is not None

    def suppress(self, contact_key: str, reason: str = "") -> None:
        if not contact_key:
            return
        with self.transaction() as c:
            c.execute("INSERT OR IGNORE INTO suppression(contact_key, reason, created_at) VALUES(?,?,?)",
                      (contact_key, reason, _now()))

    # ── Leads ────────────────────────────────────────────────────

    def upsert_lead(self, *, source: str, dedupe_key: str, name: str = "", company: str = "",
                    channel: str = "", contact: str = "", market: str = "", niche: str = "",
                    url: str = "", source_ref: str = "", data: Optional[Dict[str, Any]] = None,
                    actor: str = "system") -> Tuple[Dict[str, Any], bool]:
        """Create a lead, or return the existing one with the same dedupe_key."""
        if not dedupe_key:
            raise ValueError("dedupe_key is required")
        ckey = make_contact_key(channel, contact)
        now = _now()
        lead_id = _new_id()
        with self.transaction() as c:
            existing = c.execute("SELECT * FROM leads WHERE dedupe_key=?", (dedupe_key,)).fetchone()
            if existing:
                return _row(existing), False
            c.execute(
                "INSERT INTO leads(id, dedupe_key, source, source_ref, name, company, channel, contact,"
                " contact_key, market, niche, url, state, data, created_at, updated_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (lead_id, dedupe_key, source, source_ref, name, company, channel, contact, ckey,
                 market, niche, url, LeadState.DISCOVERED.value, json.dumps(data or {}, default=str),
                 now, now),
            )
            c.execute(
                "INSERT INTO lead_events(lead_id, ts, type, from_state, to_state, actor, payload)"
                " VALUES(?,?,?,?,?,?,?)",
                (lead_id, now, "created", None, LeadState.DISCOVERED.value, actor,
                 json.dumps({"source": source}, default=str)),
            )
            lead = _row(c.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone())
        self._events.emit("lead.created", {"lead_id": lead_id, "lead": lead, "actor": actor})
        return lead, True

    def find_lead_by_contact(self, contact_key: str) -> Optional[Dict[str, Any]]:
        """Most recently updated lead for a normalised contact (e.g. 'email:jane@x.com')."""
        return _row(self._conn().execute(
            "SELECT * FROM leads WHERE contact_key=? ORDER BY updated_at DESC LIMIT 1", (contact_key,)).fetchone())

    def list_outreach(self, status: str, channel: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        sql, params = "SELECT * FROM outreach WHERE status=?", [status]
        if channel:
            sql += " AND channel=?"
            params.append(channel)
        rows = self._conn().execute(sql + " ORDER BY created_at LIMIT ?", (*params, limit)).fetchall()
        return [_row(r) for r in rows]

    def get_lead_by_dedupe_key(self, dedupe_key: str) -> Optional[Dict[str, Any]]:
        return _row(self._conn().execute("SELECT * FROM leads WHERE dedupe_key=?", (dedupe_key,)).fetchone())

    def get_lead(self, lead_id: str) -> Optional[Dict[str, Any]]:
        return _row(self._conn().execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone())

    def list_leads(self, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        if state:
            rows = self._conn().execute(
                "SELECT * FROM leads WHERE state=? ORDER BY updated_at DESC LIMIT ?",
                (LeadState(state).value, limit)).fetchall()
        else:
            rows = self._conn().execute(
                "SELECT * FROM leads ORDER BY updated_at DESC LIMIT ?", (limit,)).fetchall()
        return [_row(r) for r in rows]

    def update_lead_fields(self, lead_id: str, **fields: Any) -> Dict[str, Any]:
        allowed = {"name", "company", "market", "niche", "url", "score_intent", "score_proto", "data"}
        bad = set(fields) - allowed
        if bad:
            raise ValueError(f"Cannot update fields: {sorted(bad)}")
        if "data" in fields:
            fields["data"] = json.dumps(fields["data"], default=str)
        sets = ", ".join(f"{k}=?" for k in fields)
        with self.transaction() as c:
            c.execute(f"UPDATE leads SET {sets}, updated_at=? WHERE id=?",
                      (*fields.values(), _now(), lead_id))
        return self.get_lead(lead_id)

    def lead_events(self, lead_id: str) -> List[Dict[str, Any]]:
        rows = self._conn().execute(
            "SELECT * FROM lead_events WHERE lead_id=? ORDER BY id", (lead_id,)).fetchall()
        return [_row(r) for r in rows]

    def log_event(self, lead_id: str, event_type: str, payload: Optional[Dict[str, Any]] = None,
                  actor: str = "system") -> int:
        """Append a non-state event (e.g. message_in / message_out) to a lead's history."""
        lead = self.get_lead(lead_id)
        if not lead:
            raise KeyError(f"Unknown lead {lead_id}")
        with self.transaction() as c:
            cur = c.execute(
                "INSERT INTO lead_events(lead_id, ts, type, from_state, to_state, actor, payload)"
                " VALUES(?,?,?,?,?,?,?)",
                (lead_id, _now(), event_type, lead["state"], lead["state"], actor,
                 json.dumps(payload or {}, default=str)))
            event_id = cur.lastrowid
        self._events.emit(f"lead.{event_type}", {"lead_id": lead_id, "event_id": event_id,
                                                 "payload": payload or {}, "actor": actor})
        return event_id

    def transition(self, lead_id: str, to_state: str, *, actor: str = "system",
                   reason: str = "", payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Move a lead through the state machine (or record an out-of-order signal)."""
        target = LeadState(to_state)
        now = _now()
        with self.transaction() as c:
            row = c.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()
            if not row:
                raise KeyError(f"Unknown lead {lead_id}")
            current = LeadState(row["state"])
            kind, description = classify_transition(current, target)  # may raise InvalidTransition
            if target == LeadState.SENT and kind == TransitionKind.TRANSITION:
                sent = c.execute("SELECT COUNT(*) FROM outreach WHERE lead_id=? AND status='sent'",
                                 (lead_id,)).fetchone()[0]
                if not sent:
                    raise InvalidTransition("SENT requires a sent outreach record (use mark_outreach_sent)")
            event_payload = dict(payload or {})
            if reason:
                event_payload["reason"] = reason
            if kind == TransitionKind.TRANSITION:
                c.execute("UPDATE leads SET state=?, updated_at=? WHERE id=?", (target.value, now, lead_id))
                if target == LeadState.DNC and row["contact_key"]:
                    c.execute("INSERT OR IGNORE INTO suppression(contact_key, reason, created_at)"
                              " VALUES(?,?,?)", (row["contact_key"], reason or "DNC", now))
                    c.execute("UPDATE outreach SET status='cancelled' WHERE lead_id=? AND status IN"
                              " ('staged','approved')", (lead_id,))
            cur = c.execute(
                "INSERT INTO lead_events(lead_id, ts, type, from_state, to_state, actor, payload)"
                " VALUES(?,?,?,?,?,?,?)",
                (lead_id, now, kind.value, current.value, target.value, actor,
                 json.dumps(event_payload, default=str)),
            )
            event_id = cur.lastrowid
        result = {
            "lead_id": lead_id,
            "kind": kind.value,
            "from": current.value,
            "to": target.value,
            "state": target.value if kind == TransitionKind.TRANSITION else current.value,
            "changed": kind == TransitionKind.TRANSITION,
            "actor": actor,
            "event_id": event_id,
            "description": description,
            "payload": event_payload,
        }
        self._events.emit("lead.transition" if result["changed"] else "lead.signal", result)
        return result

    # ── Outreach (idempotent, never twice) ───────────────────────

    def stage_outreach(self, lead_id: str, channel: str, draft: str, *, kind: str = "first_touch",
                       contact: Optional[str] = None, actor: str = "system") -> Dict[str, Any]:
        """Stage a message. Same (lead, kind) twice returns the existing record."""
        lead = self.get_lead(lead_id)
        if not lead:
            raise KeyError(f"Unknown lead {lead_id}")
        ckey = make_contact_key(channel, contact or lead.get("contact") or "")
        if not ckey:
            raise ValueError("Outreach needs a contact (phone/handle/email)")
        if self.is_suppressed(ckey):
            raise Suppressed(f"{ckey} is on the do-not-contact list")
        idem = f"{lead_id}:{kind}"
        now = _now()
        with self.transaction() as c:
            existing = c.execute("SELECT * FROM outreach WHERE idempotency_key=?", (idem,)).fetchone()
            if existing:
                return _row(existing)
            prior = c.execute(
                "SELECT lead_id FROM outreach WHERE contact_key=? AND kind=? AND status IN ('sending','sent')",
                (ckey, kind)).fetchone()
            if prior:
                raise AlreadyContacted(f"{ckey} was already contacted ({kind}) via lead {prior[0]}")
            oid = _new_id()
            c.execute(
                "INSERT INTO outreach(id, lead_id, channel, kind, contact_key, idempotency_key, draft,"
                " status, created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (oid, lead_id, channel, kind, ckey, idem, draft, "staged", now),
            )
            out = _row(c.execute("SELECT * FROM outreach WHERE id=?", (oid,)).fetchone())
        self._events.emit("outreach.staged", {"lead_id": lead_id, "outreach": out, "actor": actor})
        return out

    def get_outreach(self, outreach_id: str) -> Optional[Dict[str, Any]]:
        return _row(self._conn().execute("SELECT * FROM outreach WHERE id=?", (outreach_id,)).fetchone())

    def outreach_for_lead(self, lead_id: str) -> List[Dict[str, Any]]:
        rows = self._conn().execute("SELECT * FROM outreach WHERE lead_id=? ORDER BY created_at",
                                    (lead_id,)).fetchall()
        return [_row(r) for r in rows]

    def approve_outreach(self, outreach_id: str, actor: str) -> Dict[str, Any]:
        with self.transaction() as c:
            cur = c.execute("UPDATE outreach SET status='approved', approved_by=?, approved_at=?"
                            " WHERE id=? AND status='staged'", (actor, _now(), outreach_id))
            out = _row(c.execute("SELECT * FROM outreach WHERE id=?", (outreach_id,)).fetchone())
        if not out:
            raise KeyError(f"Unknown outreach {outreach_id}")
        if cur.rowcount == 0 and out["status"] != "approved":
            raise AlreadyContacted(f"Outreach {outreach_id} is '{out['status']}', not staged")
        self._events.emit("outreach.approved", {"lead_id": out["lead_id"], "outreach": out, "actor": actor})
        return out

    def mark_outreach_sent(self, outreach_id: str, actor: str) -> Dict[str, Any]:
        """Atomically flip approved → sent. A second call raises AlreadyContacted."""
        now = _now()
        try:
            with self.transaction() as c:
                row = c.execute("SELECT * FROM outreach WHERE id=?", (outreach_id,)).fetchone()
                if not row:
                    raise KeyError(f"Unknown outreach {outreach_id}")
                if row["status"] != "approved":
                    raise AlreadyContacted(f"Outreach {outreach_id} is '{row['status']}', not approved")
                if self.is_suppressed(row["contact_key"]):
                    raise Suppressed(f"{row['contact_key']} is on the do-not-contact list")
                c.execute("UPDATE outreach SET status='sent', sent_by=?, sent_at=? WHERE id=? AND status='approved'",
                          (actor, now, outreach_id))
                # Advance the lead in the same transaction so state and outreach never disagree.
                lead = c.execute("SELECT state FROM leads WHERE id=?", (row["lead_id"],)).fetchone()
                transition = None
                if lead and lead["state"] == LeadState.APPROVED.value:
                    c.execute("UPDATE leads SET state=?, updated_at=? WHERE id=?",
                              (LeadState.SENT.value, now, row["lead_id"]))
                    cur = c.execute(
                        "INSERT INTO lead_events(lead_id, ts, type, from_state, to_state, actor, payload)"
                        " VALUES(?,?,?,?,?,?,?)",
                        (row["lead_id"], now, TransitionKind.TRANSITION.value, LeadState.APPROVED.value,
                         LeadState.SENT.value, actor, json.dumps({"outreach_id": outreach_id})))
                    transition = {
                        "lead_id": row["lead_id"], "kind": "transition", "from": "APPROVED", "to": "SENT",
                        "state": "SENT", "changed": True, "actor": actor, "event_id": cur.lastrowid,
                        "description": "APPROVED → SENT", "payload": {"outreach_id": outreach_id},
                    }
        except sqlite3.IntegrityError as e:
            raise AlreadyContacted(f"Contact already messaged: {e}") from e
        out = self.get_outreach(outreach_id)
        self._events.emit("outreach.sent", {"lead_id": out["lead_id"], "outreach": out, "actor": actor})
        if transition:
            self._events.emit("lead.transition", transition)
        return out

    # ── Artifacts ────────────────────────────────────────────────

    def add_artifact(self, lead_id: str, kind: str, uri: str = "", meta: Optional[Dict[str, Any]] = None) -> str:
        aid = _new_id()
        with self.transaction() as c:
            c.execute("INSERT INTO artifacts(id, lead_id, kind, uri, meta, created_at) VALUES(?,?,?,?,?,?)",
                      (aid, lead_id, kind, uri, json.dumps(meta or {}, default=str), _now()))
        return aid

    # ── Budget ledger ────────────────────────────────────────────

    def record_llm_usage(self, *, provider: str, model: str, task: str = "", input_tokens: int = 0,
                         output_tokens: int = 0, cost_usd: float = 0.0, lead_id: Optional[str] = None,
                         ok: bool = True) -> None:
        with self.transaction() as c:
            c.execute(
                "INSERT INTO budget_ledger(ts, provider, model, task, input_tokens, output_tokens,"
                " cost_usd, lead_id, ok) VALUES(?,?,?,?,?,?,?,?,?)",
                (_now(), provider, model, task, int(input_tokens), int(output_tokens),
                 float(cost_usd), lead_id, 1 if ok else 0),
            )

    def spend_since(self, since_ts: float, provider: Optional[str] = None) -> Dict[str, Any]:
        sql = ("SELECT COALESCE(SUM(cost_usd),0), COALESCE(SUM(input_tokens),0),"
               " COALESCE(SUM(output_tokens),0), COUNT(*) FROM budget_ledger WHERE ts>=?")
        params: Tuple[Any, ...] = (since_ts,)
        if provider:
            sql += " AND provider=?"
            params += (provider,)
        row = self._conn().execute(sql, params).fetchone()
        return {"cost_usd": round(row[0], 6), "input_tokens": row[1], "output_tokens": row[2], "calls": row[3]}

    def month_spend(self) -> Dict[str, Any]:
        t = time.gmtime()
        start = time.mktime((t.tm_year, t.tm_mon, 1, 0, 0, 0, 0, 0, -1))
        return self.spend_since(start)


# ─── Process-wide default store ──────────────────────────────────────────────

_default: Optional[Store] = None
_default_lock = threading.Lock()


def get_store() -> Store:
    global _default
    with _default_lock:
        if _default is None:
            _default = Store()
        return _default


def set_store(store: Optional[Store]) -> None:
    global _default
    with _default_lock:
        _default = store


__all__ = [
    "Store", "get_store", "set_store", "AlreadyContacted", "Suppressed",
    "InvalidTransition", "LeadState", "ENGAGEMENT_RANK",
]
