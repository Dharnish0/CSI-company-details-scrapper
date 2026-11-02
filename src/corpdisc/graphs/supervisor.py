"""Supervisor runtime: the deterministic run loop (ARCHITECTURE 24.1, 25.4, 25.7).

Initialises the run, seeds the root, claims tasks from ``research_queue`` and runs ``research_entity`` for each,
then ``finalize``. Only this loop decides whether the run continues: run-wide budgets, the circuit breaker and
``max_tasks`` stop it; an exception in one task fails that task (retried up to ``max_attempts``) and never the run.
Resume = ``resume()``: stale RUNNING tasks return to PENDING, finished tasks are never re-claimed, fetches and
agent outputs are served from cache.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from datetime import timedelta

from corpdisc.clock import Clock, Sleeper
from corpdisc.config import PROVIDER_MODEL_FIELDS, Settings
from corpdisc.contracts.postgate import CommitPlan, CommitRejected, EntityWrite, TaskWrite, WebsiteWrite, EntityRef
from corpdisc.db.repos.runs import RunRepo
from corpdisc.graphs.deps import ResearchDeps
from corpdisc.graphs.finalize import FinalizeGraph, FinalizeResult
from corpdisc.graphs.research import CommitRejectedError, ResearchGraph, TaskCtx
from corpdisc.graphs.slow_crawl import SlowCrawlController
from corpdisc.logging_setup import get_logger
from corpdisc.models.domain import (
    CrawlState,
    EntityType,
    OperatingStatus,
    QueueStatus,
    RunStatus,
    VerificationStatus,
    WebsiteKind,
)
from corpdisc.services.normalize import normalize_name, registrable_domain
from corpdisc.services.urls import canonicalize_url

log = get_logger("graphs.supervisor")

OUTCOME_TO_STATE = {
    "EXPANDED": CrawlState.EXPANDED, "PARTIAL": CrawlState.EXPANDED, "TERMINAL": CrawlState.TERMINAL,
    "UNREACHABLE": CrawlState.UNREACHABLE, "NO_WEBSITE": CrawlState.NO_WEBSITE,
}


@dataclass
class RunSummary:
    status: str
    stopped_reason: str
    processed: int = 0
    outcomes: Counter = field(default_factory=Counter)
    failed_tasks: int = 0
    retries: int = 0  # tasks rescheduled after a transient provider failure
    waited_seconds: float = 0.0  # time slept waiting for rescheduled tasks
    queue_detail: dict[str, int] = field(default_factory=dict)
    finalize: FinalizeResult | None = None


def init_run(repo: RunRepo, settings: Settings, *, run_provider: str | None = None, run_model: str | None = None) -> int:
    """Create the ``runs`` row from settings (retargeting = changing START_URL / ROOT_COMPANY / ROOT_CODE).
    
    Args:
        repo: Run repository
        settings: Base settings from .env
        run_provider: Override provider for this run (stored in config_json)
        run_model: Override model for this run (stored in config_json)
    """
    cfg = {k: v for k, v in settings.model_dump(mode="json").items() if "key" not in k}
    # Add runtime provider/model to config_json for persistence
    provider = (run_provider or settings.llm_provider or "").strip().lower()
    cfg["llm_provider"] = provider
    cfg["llm_model"] = run_model or getattr(settings, PROVIDER_MODEL_FIELDS.get(provider, "llm_model")) or ""
    return repo.create_run(root_name=settings.root_company, root_url=settings.start_url, root_code=settings.root_code,
                           config_json=json.dumps(cfg, sort_keys=True, default=str),
                           llm_token_budget=settings.llm_token_budget)


class Supervisor:
    def __init__(self, deps: ResearchDeps, runs: RunRepo, sleeper: Sleeper | None = None,
                 slow: SlowCrawlController | None = None) -> None:
        self.d = deps
        self.runs = runs
        self.slow = slow  # slow crawl mode hooks; None = the default loop, unchanged
        self.sleeper = sleeper or (deps.clock if isinstance(deps.clock, Sleeper) else None)
        self.research = ResearchGraph(deps)
        self.finalize_graph = FinalizeGraph(deps)
        self._rl_deferrals: dict[int, int] = {}  # task_id -> free (attempt-neutral) 429 deferrals used in this process

    # ------------------------------------------------------------------ seeding
    def seed_root(self) -> int | None:
        """Create the root entity, its website and its depth-0 task. Idempotent: returns None if already seeded."""
        d, s = self.d, self.d.settings
        if d.conn.execute("SELECT 1 FROM entities WHERE run_id=? LIMIT 1", (d.run_id,)).fetchone():
            return None
        url = canonicalize_url(s.start_url)
        root = EntityRef(local_ref="root")
        plan = CommitPlan(
            run_id=d.run_id,
            entities=(EntityWrite(local_ref="root", name=s.root_company, normalized_name=normalize_name(s.root_company),
                                  entity_type=EntityType.PARENT_COMPANY, verification_status=VerificationStatus.CONFIRMED,
                                  confidence=1.0, classification_confidence=1.0, operating_status=OperatingStatus.UNKNOWN,
                                  crawl_state=CrawlState.PENDING, discovery_method="ROOT", is_root=True),),
            websites=(WebsiteWrite(local_ref="rootsite", entity=root, url=url.canonical or s.start_url,
                                   domain=url.domain or "", registrable_domain=registrable_domain(s.start_url) or "",
                                   kind=WebsiteKind.OFFICIAL, resolution_method="CONFIG", is_primary=True),),
            enqueue=(TaskWrite(entity=root, start_url=url.canonical or s.start_url, depth=0),))
        res = d.committer.commit(plan)
        if isinstance(res, CommitRejected):
            raise RuntimeError(f"cannot seed root: {res.failed_invariant}: {res.message}")
        return res.entity_ids["root"]

    # ------------------------------------------------------------------ loop
    def _run_budget_stop(self) -> str | None:
        b = self.d.budget
        if b.degradation_level() == "EXHAUSTED_100":
            return "TOKEN_BUDGET_EXHAUSTED"
        if self.slow is not None:  # session token / cost ceiling: a session already at the ceiling is denied
            v = b.check_session(0, 0)
            if not v.allow:
                return v.reason or "TOKEN_BUDGET_SLOW_CRAWL_CEILING"
        v = b.check_page("*")  # total-page check runs before the per-domain one
        if not v.allow and v.reason == "TOTAL_PAGE_LIMIT":
            return "TOTAL_PAGE_LIMIT"
        return None

    def run(self, *, max_tasks: int | None = None, run_finalize: bool = True) -> RunSummary:
        d = self.d
        d.queue.reset_stale_running(d.run_id)  # 25.7: crashed RUNNING tasks go back to PENDING
        self.runs.set_status(d.run_id, RunStatus.RUNNING)
        summary = RunSummary(status=RunStatus.RUNNING.value, stopped_reason="")
        breaker = d.runner.gateway.breaker
        idle_waits = 0
        if self.slow is not None:
            self.slow.start()
        while True:
            if max_tasks is not None and summary.processed >= max_tasks:
                summary.stopped_reason = "MAX_TASKS"
                return self._stop(summary, None, run_finalize=False)
            if self.slow is not None and (why := self.slow.should_stop()) is not None:  # MAX_RUNTIME / INTERRUPTED
                return self._slow_pause(summary, why)
            if breaker.state.value == "OPEN":
                summary.stopped_reason = "CIRCUIT_OPEN"
                return self._stop(summary, RunStatus.PAUSED, run_finalize=False)
            if (why := self._run_budget_stop()) is not None:
                summary.stopped_reason = why
                return self._stop(summary, RunStatus.BUDGET_EXHAUSTED, run_finalize=run_finalize)
            task = d.queue.claim_next(d.run_id)
            if task is None:
                ready_at = d.queue.next_ready_time(d.run_id)
                if ready_at is not None and self.slow is not None:  # slow crawl: sleep until the task is due, then go on
                    why, waited = self.slow.idle_wait(ready_at)
                    summary.waited_seconds += waited
                    if why is not None:
                        return self._slow_pause(summary, why)
                    continue
                if ready_at is not None:  # rescheduled work is waiting: wait if short, else pause (resumable)
                    wait = max(0.0, (ready_at - d.clock.now()).total_seconds())
                    if wait <= d.settings.queue_retry_wait_max_seconds and self.sleeper is not None and idle_waits < 1000:
                        idle_waits += 1
                        log.info("queue: waiting %.1fs for rescheduled tasks", wait)
                        self.sleeper.sleep(wait + 0.001)
                        summary.waited_seconds += wait
                        continue
                    summary.stopped_reason = "RETRY_WAITING"
                    summary.queue_detail = d.queue.counts_detail(d.run_id)
                    return self._stop(summary, RunStatus.PAUSED, run_finalize=False)
                summary.stopped_reason = "QUEUE_EMPTY"
                status = self.final_status()
                summary.queue_detail = d.queue.counts_detail(d.run_id)
                return self._stop(summary, status, run_finalize=run_finalize)
            summary.processed += 1
            ctx = TaskCtx(run_id=d.run_id, task_id=task.task_id, entity_id=task.entity_id, start_url=task.start_url,
                          depth=task.depth, parent_entity_id=task.parent_entity_id, attempt=task.attempts,
                          max_attempts=d.queue.max_attempts)
            log.info("Processing entity %s (task %s, depth %s)", task.entity_id, task.task_id, task.depth)
            if self.slow is not None:
                self.slow.begin_task()
            try:
                result = self.research.run(ctx)
            except KeyboardInterrupt:
                if self.slow is None:
                    raise
                # second signal: whatever the task was doing is abandoned; hand it back with its attempt (D11)
                if d.conn.in_transaction:
                    d.conn.rollback()
                d.queue.release_running(task.task_id)
                summary.processed -= 1
                return self._slow_pause(summary, "INTERRUPTED")
            except Exception as exc:  # noqa: BLE001 - one bad task never kills the run
                log.exception("task %s crashed", task.task_id)
                d.pages_repo.add_crawl_error(d.run_id, "TASK", type(exc).__name__, str(exc)[:400], entity_id=task.entity_id)
                # a rejected commit is deterministic: a retry would be rejected identically
                status = d.queue.fail(task.task_id, f"{type(exc).__name__}: {exc}"[:500],
                                      retry=not isinstance(exc, CommitRejectedError))
                summary.outcomes["FAILED" if status.value == "FAILED" else "RETRY"] += 1
                summary.failed_tasks += status.value == "FAILED"
                continue
            if self.slow is not None:
                self.slow.end_task()
                if (why := self.slow.refusal()) is not None:
                    # a request was refused (shutdown / runtime limit) during this task: the graph's result is incomplete
                    # whatever it says, so put the task back unchanged; its finished chunks come from the response cache
                    d.queue.release_running(task.task_id)
                    if self.slow.quota_scope() == "day" and (left := self.slow.limiter.cooldown_remaining()) > 0:
                        d.queue.defer_all_pending(d.run_id, d.clock.now() + timedelta(seconds=left))  # keep the daily cool-down
                    summary.processed -= 1
                    return self._slow_pause(summary, why)
            if breaker.state.value == "OPEN":  # LLM went down mid-task: release the task, pause the run
                d.queue.defer(task.task_id, d.clock.now())
                summary.processed -= 1
                summary.stopped_reason = "CIRCUIT_OPEN"
                return self._stop(summary, RunStatus.PAUSED, run_finalize=False)
            if result.outcome == "RETRY_LATER":
                # an HTTP 429 did no model work: wait without using up an attempt, but only a bounded number of times
                # per task and process, so a provider that never recovers still ends in the attempt-consuming path
                used = self._rl_deferrals.get(task.task_id, 0)
                slow_rl = self.slow is not None and result.rate_limited  # slow crawl: always attempt-neutral, no deferral cap
                free = slow_rl or (result.rate_limited and used < d.settings.queue_max_rate_limit_deferrals)
                if free:
                    self._rl_deferrals[task.task_id] = used + 1
                if slow_rl:
                    delay = self.slow.rate_limit_delay(result.retry_after_s, hit_429=self.slow.quota_scope() is not None)
                else:
                    delay = max(result.retry_after_s or 0.0, d.settings.queue_retry_base_seconds
                                * 2 ** (min(used, 4) if free else task.attempts - 1))
                cap = "" if slow_rl else f"/{d.settings.queue_max_rate_limit_deferrals}"
                reason = (f"{result.retry_reason or 'RETRY_PENDING'} (attempt {task.attempts}/{d.queue.max_attempts}"
                          + (f", rate limited, deferral {used + 1}{cap})" if free else ")"))
                wake = d.clock.now() + timedelta(seconds=delay)
                d.queue.reschedule(task.task_id, wake, reason, consume_attempt=not free)
                if slow_rl and self.slow.quota_scope() == "day":
                    # the daily quota is gone: nothing else can succeed before it refills, so wait for all pending work
                    n = d.queue.defer_all_pending(d.run_id, wake)
                    log.warning("daily quota exhausted: sleeping until %s (%d pending task(s) deferred)",
                                wake.isoformat(timespec="seconds"), n)
                log.warning("task %s rescheduled in %.0fs: %s", task.task_id, delay, reason)
                summary.outcomes["RETRY"] += 1
                summary.retries += 1
                summary.processed -= 1  # a retry does not count toward --max-tasks
                continue
            if (self.slow is not None and result.outcome == "BUDGET_STOPPED" and result.budget_scope == "run"
                    and result.errors and "SLOW_CRAWL" in result.errors[-1]):
                # session ceiling: the ceiling is per session, so the task is handed back for the next session, not skipped
                d.queue.release_running(task.task_id)
                summary.processed -= 1
                summary.stopped_reason = result.errors[-1]
                return self._stop(summary, RunStatus.BUDGET_EXHAUSTED, run_finalize=run_finalize)
            summary.outcomes[result.outcome] += 1
            self._release(task.task_id, result)
            if result.outcome == "BUDGET_STOPPED" and result.budget_scope == "run":
                summary.stopped_reason = result.errors[-1] if result.errors else "RUN_BUDGET"
                return self._stop(summary, RunStatus.BUDGET_EXHAUSTED, run_finalize=run_finalize)

    def _release(self, task_id: int, result) -> None:  # type: ignore[no-untyped-def]
        q = self.d.queue
        if result.outcome == "BUDGET_STOPPED":
            q.skip(task_id, (result.errors[-1] if result.errors else "budget")[:300], CrawlState.BUDGET_STOPPED)
        elif result.outcome == "SKIPPED":
            q.skip(task_id, (result.errors[-1] if result.errors else "skipped")[:300], CrawlState.SKIPPED)
        elif result.outcome == "PARTIAL":  # finished with gaps (e.g. verification unavailable after the retry cap)
            note = (result.errors[-1] if result.errors else "INCOMPLETE")[:200]
            prefix = q.RETRYABLE_PARTIAL_PREFIX if result.transient_incomplete else q.PARTIAL_PREFIX
            q.complete(task_id, OUTCOME_TO_STATE["PARTIAL"], note=f"{prefix}{note}")
        elif result.outcome in ("TERMINAL", "UNREACHABLE", "NO_WEBSITE"):  # never a terminal state without a reason
            q.complete(task_id, OUTCOME_TO_STATE[result.outcome], note=f"{result.outcome}:{result.reason}"[:500])
        else:
            q.complete(task_id, OUTCOME_TO_STATE.get(result.outcome, CrawlState.TERMINAL))

    def final_status(self) -> RunStatus:
        """Status when the queue is empty, derived from the DB so it survives resume.

        FAILED: the root task failed, or tasks failed and none finished. PARTIAL: any task FAILED, finished with gaps
        (``PARTIAL:`` note: verification or extraction was unavailable). COMPLETED: none of those. Review rows are not used:
        a task interrupted by the circuit breaker leaves rows that its successful re-run supersedes.
        """
        d = self.d
        detail = d.queue.counts_detail(d.run_id)
        done = d.queue.counts(d.run_id)[QueueStatus.DONE]
        root_failed = d.conn.execute("SELECT 1 FROM research_queue WHERE run_id=? AND depth=0 AND status='FAILED'",
                                     (d.run_id,)).fetchone()
        if root_failed or (detail["failed"] > 0 and done == 0):
            return RunStatus.FAILED
        if detail["failed"] > 0 or detail["partial"] > 0:
            return RunStatus.PARTIAL
        return RunStatus.COMPLETED

    def _slow_pause(self, summary: RunSummary, why: str) -> RunSummary:
        """Slow crawl stop that keeps the run resumable: MAX_RUNTIME or INTERRUPTED. Everything is already in SQLite."""
        summary.stopped_reason = why
        summary.queue_detail = self.d.queue.counts_detail(self.d.run_id)
        return self._stop(summary, RunStatus.PAUSED, run_finalize=False)

    def _stop(self, summary: RunSummary, status: RunStatus | None, *, run_finalize: bool) -> RunSummary:
        if run_finalize:
            summary.finalize = self.finalize_graph.run()
        if status is not None:
            self.runs.set_status(self.d.run_id, status)
            summary.status = status.value
        if self.slow is not None:  # final progress print, after the status is stored
            self.slow.on_stop(summary.stopped_reason)
        return summary

    def resume(self, *, max_tasks: int | None = None) -> RunSummary:
        """Close the breaker with one probe (25.4) and continue from the queue state."""
        self.d.runner.gateway.breaker.half_open()
        n = self.d.queue.requeue_retryable_partials(self.d.run_id)  # finished with a transient gap: redo the missing part
        if n:
            log.info("resume: %d task(s) with a transient provider gap requeued", n)
        return self.run(max_tasks=max_tasks)
