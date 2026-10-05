"""RunManager storage: the only writer of the ``runs`` row's lifecycle fields (ARCHITECTURE 26.2).

(Token/cost counters on the same row are updated by services/budget.py.)
"""

from __future__ import annotations

import sqlite3

from corpdisc.clock import Clock, iso_utc
from corpdisc.models.domain import RunStatus


class RunRepo:
    def __init__(self, conn: sqlite3.Connection, clock: Clock) -> None:
        self._conn = conn
        self._clock = clock

    def create_run(self, *, root_name: str, root_url: str, root_code: str, config_json: str,
                   llm_token_budget: int | None = None) -> int:
        with self._conn:
            cur = self._conn.execute(
                "INSERT INTO runs (root_name, root_url, root_code, config_json, status, started_at, llm_token_budget) "
                "VALUES (?,?,?,?,?,?,?)",
                (root_name, root_url, root_code, config_json, RunStatus.RUNNING.value,
                 iso_utc(self._clock.now()), llm_token_budget),
            )
        return int(cur.lastrowid)

    def set_status(self, run_id: int, status: RunStatus) -> None:
        done = status in (RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED, RunStatus.BUDGET_EXHAUSTED)
        with self._conn:
            self._conn.execute("UPDATE runs SET status = ?, finished_at = ? WHERE run_id = ?",
                               (status.value, iso_utc(self._clock.now()) if done else None, run_id))

    def get_status(self, run_id: int) -> RunStatus:
        row = self._conn.execute("SELECT status FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if row is None:
            raise LookupError(f"unknown run {run_id}")
        return RunStatus(row[0])
