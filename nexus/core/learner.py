"""
Autocrat — Behavioral Learner
SQLite-backed pattern tracker that learns from user behavior.
Tracks command frequency, time patterns, and sequences.
"""

import os
import sqlite3
import time
import json
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
from nexus.core.logger import get_logger

log = get_logger("learner")


class BehaviorLearner:
    """Tracks user behavior patterns and provides adaptive suggestions."""

    def __init__(self, db_path: str = "nexus_brain.db"):
        self.db_path = db_path
        self.conn = None
        self._init_db()
        log.info("Behavioral learner initialized")

    def _init_db(self):
        """Create SQLite tables for behavior tracking."""
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        cursor = self.conn.cursor()

        cursor.executescript("""
            CREATE TABLE IF NOT EXISTS command_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                hour INTEGER NOT NULL,
                day_of_week INTEGER NOT NULL,
                command TEXT NOT NULL,
                intent TEXT,
                success INTEGER DEFAULT 1,
                duration_ms REAL,
                active_window TEXT
            );

            CREATE TABLE IF NOT EXISTS intent_frequency (
                intent TEXT PRIMARY KEY,
                count INTEGER DEFAULT 0,
                last_used TEXT,
                total_duration_ms REAL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS command_chains (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                prev_command TEXT NOT NULL,
                next_command TEXT NOT NULL,
                count INTEGER DEFAULT 1,
                UNIQUE(prev_command, next_command)
            );

            CREATE TABLE IF NOT EXISTS time_patterns (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                hour INTEGER NOT NULL,
                day_of_week INTEGER NOT NULL,
                intent TEXT NOT NULL,
                count INTEGER DEFAULT 1,
                UNIQUE(hour, day_of_week, intent)
            );

            CREATE TABLE IF NOT EXISTS context_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                hour INTEGER NOT NULL,
                day_of_week INTEGER NOT NULL,
                active_window TEXT,
                cpu_percent REAL,
                ram_percent REAL,
                metadata TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_log_hour ON command_log(hour);
            CREATE INDEX IF NOT EXISTS idx_log_intent ON command_log(intent);
            CREATE INDEX IF NOT EXISTS idx_chains_prev ON command_chains(prev_command);
            CREATE INDEX IF NOT EXISTS idx_ctx_time ON context_snapshots(hour, day_of_week);
            CREATE INDEX IF NOT EXISTS idx_ctx_window ON context_snapshots(active_window);
        """)

        self.conn.commit()

    def record(self, command: str, intent: str = None, success: bool = True,
               duration_ms: float = None, active_window: str = None):
        """Record a command execution for learning."""
        now = datetime.now()
        hour = now.hour
        dow = now.weekday()

        cursor = self.conn.cursor()

        # Log the command
        cursor.execute("""
            INSERT INTO command_log (timestamp, hour, day_of_week, command, intent, success, duration_ms, active_window)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (now.isoformat(), hour, dow, command, intent, int(success), duration_ms, active_window))

        # Update intent frequency
        if intent:
            cursor.execute("""
                INSERT INTO intent_frequency (intent, count, last_used, total_duration_ms)
                VALUES (?, 1, ?, ?)
                ON CONFLICT(intent) DO UPDATE SET
                    count = count + 1,
                    last_used = excluded.last_used,
                    total_duration_ms = total_duration_ms + COALESCE(excluded.total_duration_ms, 0)
            """, (intent, now.isoformat(), duration_ms or 0))

            # Update time patterns
            cursor.execute("""
                INSERT INTO time_patterns (hour, day_of_week, intent, count)
                VALUES (?, ?, ?, 1)
                ON CONFLICT(hour, day_of_week, intent) DO UPDATE SET count = count + 1
            """, (hour, dow, intent))

        # Update command chains (what command follows what)
        last_cmd = self._get_last_command(cursor)
        if last_cmd and last_cmd != command:
            cursor.execute("""
                INSERT INTO command_chains (prev_command, next_command, count)
                VALUES (?, ?, 1)
                ON CONFLICT(prev_command, next_command) DO UPDATE SET count = count + 1
            """, (last_cmd, command))

        self.conn.commit()

    def _get_last_command(self, cursor) -> Optional[str]:
        """Get the most recently logged command."""
        cursor.execute("""
            SELECT command FROM command_log
            ORDER BY id DESC LIMIT 1 OFFSET 1
        """)
        row = cursor.fetchone()
        return row["command"] if row else None

    def get_boost_map(self) -> Dict[str, float]:
        """
        Get intent frequency boost map for the brain's classifier.
        More frequently used intents get a small similarity boost.
        """
        cursor = self.conn.cursor()
        cursor.execute("SELECT intent, count FROM intent_frequency ORDER BY count DESC")

        boost = {}
        rows = cursor.fetchall()
        if not rows:
            return boost

        max_count = rows[0]["count"]
        for row in rows:
            # Normalize to 0-1 range
            boost[row["intent"]] = row["count"] / max_count if max_count > 0 else 0

        return boost

    def get_success_rates(self) -> Dict[str, float]:
        """Get per-intent success rates for confidence calibration.

        Returns a dict of intent → success_ratio (0.0 to 1.0).
        Only includes intents with >= 3 data points for statistical significance.
        """
        cursor = self.conn.cursor()
        cursor.execute("""
            SELECT intent,
                   COUNT(*) as total,
                   SUM(CASE WHEN success = 1 THEN 1 ELSE 0 END) as successes
            FROM command_log
            WHERE intent IS NOT NULL
            GROUP BY intent
            HAVING total >= 3
        """)
        rates = {}
        for row in cursor.fetchall():
            total = row["total"]
            successes = row["successes"]
            rates[row["intent"]] = successes / total if total > 0 else 0.5
        return rates

    def get_time_suggestions(self, top_k: int = 3) -> List[Dict]:
        """
        Get suggestions based on current time patterns.
        "You usually do X around this time"
        """
        now = datetime.now()
        hour = now.hour
        dow = now.weekday()

        cursor = self.conn.cursor()

        # Look for patterns at this hour (±1 hour) and day
        cursor.execute("""
            SELECT intent, SUM(count) as total
            FROM time_patterns
            WHERE hour BETWEEN ? AND ?
            AND day_of_week = ?
            GROUP BY intent
            ORDER BY total DESC
            LIMIT ?
        """, (hour - 1, hour + 1, dow, top_k))

        suggestions = []
        for row in cursor.fetchall():
            if row["total"] >= 2:  # Need at least 2 occurrences to suggest
                suggestions.append({
                    "intent": row["intent"],
                    "reason": f"You usually do this around {hour}:00",
                    "frequency": row["total"],
                })

        return suggestions

    def get_chain_suggestions(self, last_command: str, top_k: int = 3) -> List[Dict]:
        """
        Get suggestions based on command sequences.
        "After X, you usually do Y"
        """
        cursor = self.conn.cursor()
        cursor.execute("""
            SELECT next_command, count
            FROM command_chains
            WHERE prev_command = ?
            ORDER BY count DESC
            LIMIT ?
        """, (last_command, top_k))

        suggestions = []
        for row in cursor.fetchall():
            if row["count"] >= 2:
                suggestions.append({
                    "command": row["next_command"],
                    "reason": f"You usually do this after '{last_command}'",
                    "frequency": row["count"],
                })

        return suggestions

    def get_frequent_commands(self, top_k: int = 10) -> List[Dict]:
        """Get the most frequently used commands."""
        cursor = self.conn.cursor()
        cursor.execute("""
            SELECT command, COUNT(*) as count,
                   AVG(duration_ms) as avg_duration
            FROM command_log
            GROUP BY command
            ORDER BY count DESC
            LIMIT ?
        """, (top_k,))

        return [dict(row) for row in cursor.fetchall()]

    def get_stats(self) -> Dict:
        """Get learning statistics."""
        cursor = self.conn.cursor()

        cursor.execute("SELECT COUNT(*) as total FROM command_log")
        total = cursor.fetchone()["total"]

        cursor.execute("SELECT COUNT(DISTINCT intent) as intents FROM command_log WHERE intent IS NOT NULL")
        intents = cursor.fetchone()["intents"]

        cursor.execute("SELECT COUNT(*) as chains FROM command_chains WHERE count >= 2")
        chains = cursor.fetchone()["chains"]

        cursor.execute("SELECT COUNT(*) as patterns FROM time_patterns WHERE count >= 2")
        patterns = cursor.fetchone()["patterns"]

        cursor.execute("SELECT COUNT(*) as snapshots FROM context_snapshots")
        snapshots = cursor.fetchone()["snapshots"]

        return {
            "total_commands_learned": total,
            "unique_intents_seen": intents,
            "learned_sequences": chains,
            "time_patterns": patterns,
            "context_snapshots": snapshots,
        }

    def record_context_snapshot(
        self,
        active_window: str = "",
        cpu_percent: float = None,
        ram_percent: float = None,
        metadata: Dict = None,
    ):
        """Record passive context snapshots for proactive predictions."""
        now = datetime.now()
        hour = now.hour
        dow = now.weekday()

        cursor = self.conn.cursor()
        cursor.execute(
            """
            INSERT INTO context_snapshots
                (timestamp, hour, day_of_week, active_window, cpu_percent, ram_percent, metadata)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                now.isoformat(),
                hour,
                dow,
                (active_window or "")[:300],
                cpu_percent,
                ram_percent,
                json.dumps(metadata or {}),
            ),
        )
        self.conn.commit()

    def get_proactive_nudges(self, now: datetime = None, min_count: int = 4) -> List[Dict]:
        """
        Detect recurring workflow windows for current hour/day and propose nudges.
        """
        now = now or datetime.now()
        hour = now.hour
        dow = now.weekday()

        cursor = self.conn.cursor()
        cursor.execute(
            """
            SELECT active_window, COUNT(*) as cnt,
                   AVG(COALESCE(cpu_percent,0)) as avg_cpu,
                   AVG(COALESCE(ram_percent,0)) as avg_ram
            FROM context_snapshots
            WHERE day_of_week = ?
              AND hour BETWEEN ? AND ?
              AND COALESCE(active_window, '') != ''
            GROUP BY active_window
            HAVING cnt >= ?
            ORDER BY cnt DESC
            LIMIT 5
            """,
            (dow, max(hour - 1, 0), min(hour + 1, 23), min_count),
        )

        nudges = []
        for row in cursor.fetchall():
            window_title = row["active_window"]
            title_l = window_title.lower()

            suggested_action = None
            if "visual studio code" in title_l or "vscode" in title_l:
                suggested_action = "workflow_engine.run_workflow"
            elif "chrome" in title_l:
                suggested_action = "smart_actions.switch_tab"

            nudges.append({
                "active_window": window_title,
                "frequency": row["cnt"],
                "avg_cpu": round(float(row["avg_cpu"] or 0), 1),
                "avg_ram": round(float(row["avg_ram"] or 0), 1),
                "suggested_action": suggested_action,
                "message": f"Pattern detected: you often use '{window_title}' around {hour:02d}:00.",
            })

        return nudges

    def close(self):
        """Close the database connection."""
        if self.conn:
            self.conn.close()
