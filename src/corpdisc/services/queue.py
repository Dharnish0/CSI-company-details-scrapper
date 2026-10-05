"""SQLite-backed research queue (decision D2): the work queue AND the visited-entities set.

States: PENDING, RUNNING, DONE, FAILED, SKIPPED (there is no COMPLETED state in the database).
Task ids are the integer primary key in enqueue order. ``priority = depth * 1_000_000 +
discovery_seq`` so processing is breadth-first and stable. ``UNIQUE(run_id, entity_id)`` means an
entity is queued at most once per run. The parent of a task is a read model derived from the
entity's primary relationship, never a stored column.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from corpdisc.clock import Clock, iso_utc, parse_iso_utc
from corpdisc.models.domain import CrawlState, QueueStatus, ResearchTask
from corpdisc.services.urls import domain_of

QUEUE_DEPTH_FACTOR: Final = 1_000_000


def compute_priority(depth: int, discovery_seq: int) -> int:
    if depth < 0:
        raise ValueError("depth must be >= 0")
    if not 0 <= discovery_seq < QUEUE_DEPTH_FACTOR:
        raise ValueError(f"discovery_seq must be in [0, {QUEUE_DEPTH_FACTOR})")
    return depth * QUEUE_DEPTH_FACTOR + discovery_seq


@dataclass(frozen=True)
class ClaimedTask:
    task_id: int
    run_id: int
    entity_id: int
    start_url: str | None
    depth: int
    priority: int
    attempts: int
    parent_entity_id: int | None


def insert_task(
    conn: sqlite3.Connection,
    *,
    run_id: int,
    entity_id: int,
    start_url: str | None,
    depth: int,
    discovery_seq: int,
    enqueued_at: datetime,
    not_before: datetime | None = None,
) -> int | None:
    """Insert a PENDING task without committing. Returns None when (run, entity) is already queued."""
    cur = conn.execute(
        "INSERT INTO research_queue (run_id, entity_id, start_url, depth, priority, status, attempts, "
        "enqueued_at, not_before) VALUES (?,?,?,?,?,'PENDING',0,?,?) ON CONFLICT(run_id, entity_id) DO NOTHING",
        (run_id, entity_id, start_url, depth, compute_priority(depth, discovery_seq), iso_utc(enqueued_at),
         iso_utc(not_before) if not_before else None),
    )
    return int(cur.lastrowid) if cur.rowcount == 1 else None


class QueueService:
    def __init__(self, conn: sqlite3.Connection, clock: Clock, *, max_attempts: int = 3) -> None:
        self._conn = conn
        self._clock = clock
        self.max_attempts = max_attempts

    @contextmanager
    def _tx(self) -> Iterator[None]:
        """BEGIN IMMEDIATE ... COMMIT, or join a transaction the caller already holds."""
        if self._conn.in_transaction:
            yield
            return
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self._conn.rollback()
            raise
        self._conn.commit()

    # ------------------------------------------------------------------ enqueue
    def enqueue(self, *, run_id: int, entity_id: int, start_url: str | None, depth: int,
                not_before: datetime | None = None) -> int | None:
        """Idempotent: returns the new task_id, or None when the entity is already queued/visited."""
        with self._tx():
            row = self._conn.execute(
                "SELECT discovery_seq FROM entities WHERE entity_id = ? AND run_id = ?", (entity_id, run_id)
            ).fetchone()
            if row is None:
                raise LookupError(f"entity {entity_id} does not belong to run {run_id}")
            return insert_task(self._conn, run_id=run_id, entity_id=entity_id, start_url=start_url, depth=depth,
                               discovery_seq=row["discovery_seq"], enqueued_at=self._clock.now(),
                               not_before=not_before)

    # -------------------------------------------------------------------- claim
    def claim_next(self, run_id: int) -> ClaimedTask | None:
        """Atomically move the lowest-priority ready PENDING task to RUNNING."""
        now = self._clock.now()
        with self._tx():
            row = self._conn.execute(
                "SELECT task_id, run_id, entity_id, start_url, depth, priority, attempts FROM research_queue "
                "WHERE run_id = ? AND status = 'PENDING' AND (not_before IS NULL OR not_before <= ?) "
                "ORDER BY priority, task_id LIMIT 1",
                (run_id, iso_utc(now)),
            ).fetchone()
            if row is None:
                return None
            self._conn.execute(
                "UPDATE research_queue SET status = 'RUNNING', attempts = attempts + 1, started_at = ?, "
                "finished_at = NULL WHERE task_id = ? AND status = 'PENDING'",
                (iso_utc(now), row["task_id"]),
            )
            return ClaimedTask(
                task_id=row["task_id"], run_id=row["run_id"], entity_id=row["entity_id"],
                start_url=row["start_url"], depth=row["depth"], priority=row["priority"],
                attempts=row["attempts"] + 1, parent_entity_id=self.parent_entity_id(row["entity_id"]),
            )  # fmt: skip

    # ------------------------------------------------------------------ finish
    def _finish(self, task_id: int, status: QueueStatus, error: str | None, crawl_state: CrawlState | None) -> None:
        with self._tx():
            row = self._conn.execute("SELECT entity_id FROM research_queue WHERE task_id = ?", (task_id,)).fetchone()
            if row is None:
                raise LookupError(f"unknown task {task_id}")
            self._conn.execute(
                "UPDATE research_queue SET status = ?, finished_at = ?, error = ? WHERE task_id = ?",
                (status.value, iso_utc(self._clock.now()), error, task_id),
            )
            if crawl_state is not None:
                self._conn.execute("UPDATE entities SET crawl_state = ? WHERE entity_id = ?",
                                   (crawl_state.value, row["entity_id"]))

    def complete(self, task_id: int, crawl_state: CrawlState | None = None) -> None:
        self._finish(task_id, QueueStatus.DONE, None, crawl_state)

    def skip(self, task_id: int, reason: str, crawl_state: CrawlState | None = CrawlState.SKIPPED) -> None:
        self._finish(task_id, QueueStatus.SKIPPED, reason[:500], crawl_state)

    def fail(self, task_id: int, error: str, *, retry: bool = True) -> QueueStatus:
        """Return to PENDING (attempts preserved) while attempts remain, else FAILED permanently."""
        with self._tx():
            row = self._conn.execute("SELECT attempts FROM research_queue WHERE task_id = ?", (task_id,)).fetchone()
            if row is None:
                raise LookupError(f"unknown task {task_id}")
            if retry and row["attempts"] < self.max_attempts:
                self._conn.execute(
                    "UPDATE research_queue SET status = 'PENDING', error = ?, finished_at = NULL WHERE task_id = ?",
                    (error[:500], task_id),
                )
                return QueueStatus.PENDING
            self._conn.execute(
                "UPDATE research_queue SET status = 'FAILED', error = ?, finished_at = ? WHERE task_id = ?",
                (error[:500], iso_utc(self._clock.now()), task_id),
            )
            return QueueStatus.FAILED

    def defer(self, task_id: int, not_before: datetime) -> None:
        """Per-domain cool-down (ARCHITECTURE 25.6): back to PENDING, not failed, retry budget untouched."""
        with self._tx():
            self._conn.execute(
                "UPDATE research_queue SET status = 'PENDING', not_before = ?, finished_at = NULL, "
                "attempts = MAX(attempts - 1, 0) WHERE task_id = ? AND status = 'RUNNING'",
                (iso_utc(not_before), task_id),
            )

    def defer_domain(self, run_id: int, domain: str, not_before: datetime) -> int:
        """Set not_before on PENDING tasks whose start_url is on ``domain``. Returns rows changed."""
        changed = 0
        with self._tx():
            rows = self._conn.execute(
                "SELECT task_id, start_url FROM research_queue WHERE run_id = ? AND status = 'PENDING'", (run_id,)
            ).fetchall()
            for r in rows:
                if r["start_url"] and domain_of(r["start_url"]) == domain:
                    self._conn.execute("UPDATE research_queue SET not_before = ? WHERE task_id = ?",
                                       (iso_utc(not_before), r["task_id"]))
                    changed += 1
        return changed

    def reset_stale_running(self, run_id: int | None = None) -> int:
        """Resume after a crash: RUNNING tasks go back to PENDING (ARCHITECTURE 25.7)."""
        with self._tx():
            if run_id is None:
                cur = self._conn.execute("UPDATE research_queue SET status = 'PENDING' WHERE status = 'RUNNING'")
            else:
                cur = self._conn.execute(
                    "UPDATE research_queue SET status = 'PENDING' WHERE status = 'RUNNING' AND run_id = ?", (run_id,)
                )
            return cur.rowcount

    # --------------------------------------------------------------------- read
    def counts(self, run_id: int) -> dict[QueueStatus, int]:
        out = dict.fromkeys(QueueStatus, 0)
        for r in self._conn.execute(
            "SELECT status, COUNT(*) AS n FROM research_queue WHERE run_id = ? GROUP BY status", (run_id,)
        ):
            out[QueueStatus(r["status"])] = r["n"]
        return out

    def get(self, task_id: int) -> ResearchTask | None:
        r = self._conn.execute("SELECT * FROM research_queue WHERE task_id = ?", (task_id,)).fetchone()
        if r is None:
            return None

        def ts(v: str | None) -> datetime | None:
            return parse_iso_utc(v) if v else None

        return ResearchTask(
            task_id=r["task_id"], run_id=r["run_id"], entity_id=r["entity_id"], start_url=r["start_url"],
            depth=r["depth"], priority=r["priority"], status=QueueStatus(r["status"]), attempts=r["attempts"],
            enqueued_at=parse_iso_utc(r["enqueued_at"]), started_at=ts(r["started_at"]),
            finished_at=ts(r["finished_at"]), error=r["error"], not_before=ts(r["not_before"]),
        )  # fmt: skip

    def parent_entity_id(self, entity_id: int) -> int | None:
        """Derived from the primary, non-rejected relationship (CONFIRMED first, then oldest)."""
        row = self._conn.execute(
            "SELECT parent_id FROM relationships WHERE child_id = ? AND is_primary = 1 "
            "AND verification_status <> 'REJECTED' "
            "ORDER BY (verification_status = 'CONFIRMED') DESC, relationship_id LIMIT 1",
            (entity_id,),
        ).fetchone()
        return int(row["parent_id"]) if row else None
