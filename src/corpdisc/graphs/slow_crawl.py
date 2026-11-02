"""Slow crawl controller: the hooks ``Supervisor.run`` calls when slow crawl mode is on (slow-crawl plan D10, D11).

One supervisor loop serves both modes. In default mode ``Supervisor.slow`` is None and none of this runs. The controller
owns the runtime deadline, the stop checks, chunked sleeping until the next eligible task, the 429 cool-down delay, the
heartbeat in the session file and the periodic progress report. It never touches task state itself: the supervisor does.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from corpdisc.clock import Sleeper
from corpdisc.logging_setup import get_logger
from corpdisc.services.instance_lock import InstanceLock, read_session
from corpdisc.services.progress import build_progress, format_progress

if TYPE_CHECKING:
    from corpdisc.agents.ratelimit import SlowCrawlLimiter
    from corpdisc.graphs.deps import ResearchDeps

log = get_logger("graphs.slow_crawl")

MAX_RUNTIME = "MAX_RUNTIME"
INTERRUPTED = "INTERRUPTED"
CHECKPOINT_EVERY_S = 600.0  # PRAGMA wal_checkpoint(PASSIVE) about every 10 minutes of waiting


def _print(message: str) -> None:
    print(message, flush=True)


class SlowCrawlController:
    def __init__(self, deps: ResearchDeps, *, limiter: SlowCrawlLimiter, sleeper: Sleeper, deadline: float,
                 stop_check: Callable[[], str | None] | None = None, lock: InstanceLock | None = None,
                 emit: Callable[[str], None] = _print) -> None:
        self.d = deps
        self.limiter = limiter
        self._sleeper = sleeper
        self.deadline = deadline  # monotonic seconds: no new work starts after it
        self._stop_check = stop_check or (lambda: None)
        self.lock = lock
        self._emit = emit
        s = deps.settings
        self.chunk_s = min(max(s.slow_crawl_sleep_chunk_seconds, 1.0), 60.0)
        self.progress_every_s = s.slow_crawl_progress_interval_seconds
        self._last_report = deps.clock.monotonic()
        self._slept_since_checkpoint = 0.0
        self._rl0 = 0
        self._refusals0 = 0
        self.http_429_day = 0
        self.http_429_minute = 0

    # ------------------------------------------------------------------ session file
    def _update(self, **fields: Any) -> None:
        if self.lock is not None:
            self.lock.update(**fields)

    def start(self) -> None:
        """Write the session baselines and the wall-clock deadline into the session file."""
        d = self.d
        fields: dict[str, Any] = {"state": "RUNNING", "sleeping_until": None, "http_429_day": 0, "http_429_minute": 0,
                                  "deadline_at": d.clock.now() + timedelta(seconds=max(self.deadline - d.clock.monotonic(), 0.0))}
        session = d.budget.session
        if session is not None:
            fields["tokens_baseline"] = session.baseline_tokens
            fields["cost_baseline"] = session.baseline_cost
        self._update(**fields)
        self._last_report = d.clock.monotonic()

    # ------------------------------------------------------------------ stop checks
    def should_stop(self) -> str | None:
        """``INTERRUPTED`` after a shutdown request, ``MAX_RUNTIME`` after the deadline, else None."""
        if self._stop_check():
            return INTERRUPTED
        if self.d.clock.monotonic() >= self.deadline:
            return MAX_RUNTIME
        return None

    # ------------------------------------------------------------------ per-task bookkeeping
    def begin_task(self) -> None:
        self._rl0, self._refusals0 = self.limiter.rate_limited, self.limiter.refusals

    def refusal(self) -> str | None:
        """Why the limiter refused a request during the current task (``MAX_RUNTIME`` or ``INTERRUPTED``), else None."""
        if self.limiter.refusals <= self._refusals0:
            return None
        return MAX_RUNTIME if self.limiter.last_refusal == MAX_RUNTIME else INTERRUPTED

    def quota_scope(self) -> str | None:
        """``day`` / ``minute`` when a provider 429 happened during the current task, else None."""
        return self.limiter.last_429_scope if self.limiter.rate_limited > self._rl0 else None

    def end_task(self) -> None:
        """Count this task's 429s (a daily 429 ends a request chain at once, so it is the last one) and heartbeat."""
        n = self.limiter.rate_limited - self._rl0
        if n > 0:
            if self.limiter.last_429_scope == "day":
                self.http_429_day += 1
                self.http_429_minute += n - 1
            else:
                self.http_429_minute += n
        self._update(state="RUNNING", sleeping_until=None, http_429_day=self.http_429_day,
                     http_429_minute=self.http_429_minute, last_429_scope=self.limiter.last_429_scope)
        self.report()

    def rate_limit_delay(self, retry_after_s: float | None, *, hit_429: bool) -> float:
        """Seconds before a rate-limited task may run again.

        The limiter cool-down was started by the 429 itself (Retry-After plus jitter), so what is left of it is exactly the
        provider's wait minus the time the task already spent waiting on it. Without a 429 in this task the stated
        ``Retry-After`` counts too. Never zero: a task that returns before its cool-down ends waits at least the base delay.
        """
        left = self.limiter.cooldown_remaining()
        delay = left if hit_429 else max(retry_after_s or 0.0, left)
        return delay if delay > 0 else self.d.settings.queue_retry_base_seconds

    # ------------------------------------------------------------------ waiting
    def idle_wait(self, ready_at: datetime) -> tuple[str | None, float]:
        """Sleep, in chunks of at most ``SLOW_CRAWL_SLEEP_CHUNK_SECONDS``, until ``ready_at``.

        Returns ``(None, waited)`` when the next task is due, else ``(stop_reason, waited)``. ``MAX_RUNTIME`` is returned at
        once when ``ready_at`` lies beyond the deadline (sleeping to it would only delay the stop).
        """
        clock = self.d.clock
        remaining = (ready_at - clock.now()).total_seconds()
        if remaining > 0 and clock.monotonic() + remaining >= self.deadline:
            return MAX_RUNTIME, 0.0
        self._update(state="SLEEPING", sleeping_until=ready_at)
        log.info("slow crawl: sleeping until %s", ready_at.isoformat(timespec="seconds"))
        waited = 0.0
        try:
            while True:
                if (why := self.should_stop()) is not None:
                    return why, waited
                remaining = (ready_at - clock.now()).total_seconds()
                if remaining <= 0:
                    return None, waited
                chunk = min(self.chunk_s, remaining + 0.001)
                self._sleeper.sleep(chunk)
                waited += chunk
                self._slept_since_checkpoint += chunk
                self._update(state="SLEEPING", sleeping_until=ready_at)  # heartbeat
                self._checkpoint()
                self.report()
        except KeyboardInterrupt:  # second signal while sleeping: no task is running, nothing to requeue
            return INTERRUPTED, waited
        finally:
            self._update(state="RUNNING", sleeping_until=None)

    def _checkpoint(self) -> None:
        if self._slept_since_checkpoint < CHECKPOINT_EVERY_S:
            return
        self._slept_since_checkpoint = 0.0
        conn = self.d.conn
        if conn.in_transaction:
            return
        try:
            conn.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchall()
        except Exception:  # noqa: BLE001 - housekeeping only
            log.warning("wal checkpoint failed", exc_info=True)

    # ------------------------------------------------------------------ reporting
    def report(self, *, force: bool = False) -> None:
        """Print the progress snapshot every ``SLOW_CRAWL_PROGRESS_INTERVAL_SECONDS`` (or now when forced)."""
        d = self.d
        now = d.clock.monotonic()
        if not force and now - self._last_report < self.progress_every_s:
            return
        self._last_report = now
        try:
            session = read_session(d.settings.db_path) if self.lock is not None else None
            self._emit(format_progress(build_progress(d.conn, d.run_id, d.settings, now=d.clock.now(), session=session)))
        except Exception:  # noqa: BLE001 - monitoring must never stop the crawl
            log.warning("progress report failed", exc_info=True)

    def on_stop(self, stopped_reason: str) -> None:
        """Final progress print on every stop; the session file shows STOPPING until the lock is released."""
        self._update(state="STOPPING", sleeping_until=None, http_429_day=self.http_429_day,
                     http_429_minute=self.http_429_minute, last_429_scope=self.limiter.last_429_scope)
        log.info("slow crawl stopping: %s", stopped_reason)
        self.report(force=True)
