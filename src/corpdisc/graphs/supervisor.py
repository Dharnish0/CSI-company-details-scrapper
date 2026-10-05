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

from corpdisc.clock import Clock
from corpdisc.config import Settings
from corpdisc.contracts.postgate import CommitPlan, CommitRejected, EntityWrite, TaskWrite, WebsiteWrite, EntityRef
from corpdisc.db.repos.runs import RunRepo
from corpdisc.graphs.deps import ResearchDeps
from corpdisc.graphs.finalize import FinalizeGraph, FinalizeResult
from corpdisc.graphs.research import ResearchGraph, TaskCtx
from corpdisc.logging_setup import get_logger
from corpdisc.models.domain import CrawlState, EntityType, OperatingStatus, RunStatus, VerificationStatus, WebsiteKind
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
    finalize: FinalizeResult | None = None


def init_run(repo: RunRepo, settings: Settings) -> int:
    """Create the ``runs`` row from settings (retargeting = changing START_URL / ROOT_COMPANY / ROOT_CODE)."""
    cfg = {k: v for k, v in settings.model_dump(mode="json").items() if "key" not in k}
    return repo.create_run(root_name=settings.root_company, root_url=settings.start_url, root_code=settings.root_code,
                           config_json=json.dumps(cfg, sort_keys=True, default=str),
                           llm_token_budget=settings.llm_token_budget)


class Supervisor:
    def __init__(self, deps: ResearchDeps, runs: RunRepo) -> None:
        self.d = deps
        self.runs = runs
        self.research = ResearchGraph(deps)
        self.finalize_graph = FinalizeGraph(deps)

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
        while True:
            if max_tasks is not None and summary.processed >= max_tasks:
                summary.stopped_reason = "MAX_TASKS"
                return self._stop(summary, None, run_finalize=False)
            if breaker.state.value == "OPEN":
                summary.stopped_reason = "CIRCUIT_OPEN"
                return self._stop(summary, RunStatus.PAUSED, run_finalize=False)
            if (why := self._run_budget_stop()) is not None:
                summary.stopped_reason = why
                return self._stop(summary, RunStatus.BUDGET_EXHAUSTED, run_finalize=run_finalize)
            task = d.queue.claim_next(d.run_id)
            if task is None:
                summary.stopped_reason = "QUEUE_EMPTY"
                return self._stop(summary, RunStatus.COMPLETED, run_finalize=run_finalize)
            summary.processed += 1
            ctx = TaskCtx(run_id=d.run_id, task_id=task.task_id, entity_id=task.entity_id, start_url=task.start_url,
                          depth=task.depth, parent_entity_id=task.parent_entity_id)
            log.info("Processing entity %s (task %s, depth %s)", task.entity_id, task.task_id, task.depth)
            try:
                result = self.research.run(ctx)
            except Exception as exc:  # noqa: BLE001 - one bad task never kills the run
                log.exception("task %s crashed", task.task_id)
                d.pages_repo.add_crawl_error(d.run_id, "TASK", type(exc).__name__, str(exc)[:400], entity_id=task.entity_id)
                status = d.queue.fail(task.task_id, f"{type(exc).__name__}: {exc}"[:500])
                summary.outcomes["FAILED" if status.value == "FAILED" else "RETRY"] += 1
                summary.failed_tasks += status.value == "FAILED"
                continue
            if breaker.state.value == "OPEN":  # LLM went down mid-task: release the task, pause the run
                d.queue.defer(task.task_id, d.clock.now())
                summary.processed -= 1
                summary.stopped_reason = "CIRCUIT_OPEN"
                return self._stop(summary, RunStatus.PAUSED, run_finalize=False)
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
        else:
            q.complete(task_id, OUTCOME_TO_STATE.get(result.outcome, CrawlState.TERMINAL))

    def _stop(self, summary: RunSummary, status: RunStatus | None, *, run_finalize: bool) -> RunSummary:
        if run_finalize:
            summary.finalize = self.finalize_graph.run()
        if status is not None:
            self.runs.set_status(self.d.run_id, status)
            summary.status = status.value
        return summary

    def resume(self, *, max_tasks: int | None = None) -> RunSummary:
        """Close the breaker with one probe (25.4) and continue from the queue state."""
        self.d.runner.gateway.breaker.half_open()
        return self.run(max_tasks=max_tasks)
