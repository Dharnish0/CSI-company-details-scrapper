"""Gate rejection persistence (ARCHITECTURE.md 22.11).

The gate_rejections table records each deterministic gate that blocked an item,
including correlation/run/task information and a human-readable detail string.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from corpdisc.clock import Clock, iso_utc


@dataclass(frozen=True)
class GateRejectionRepo:
    """Write gate rejections to SQLite; read-only for inspection."""

    _conn: sqlite3.Connection
    _clock: Clock

    def write(
        self,
        *,
        run_id: int,
        gate_code: str,
        item_ref: str,
        detail: str,
        invocation_id: int | None = None,
    ) -> int:
        """Insert a rejection and return its rejection_id."""
        sql = (
            "INSERT INTO gate_rejections (run_id, invocation_id, gate_code, item_ref, detail, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)"
        )
        with self._conn:
            cursor = self._conn.execute(
                sql,
                (run_id, invocation_id, gate_code, item_ref, detail, iso_utc(self._clock.now())),
            )
            return cursor.lastrowid or 0
