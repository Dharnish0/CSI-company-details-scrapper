"""BudgetGuard: deterministic budget decisions with counters derived from SQLite.

Nothing is held in memory, so limits survive a restart. Every check returns a
``BudgetDecision(allow, reason, detail)``; a limit that is exactly reached is exhausted
(``used >= limit`` denies). Limits come from Settings. The LLM/tool checks are shaped so
the M2 agent infrastructure can use this class as its budget port.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Final, Literal

from corpdisc.config import Settings
from corpdisc.contracts.common import BudgetDecision
from corpdisc.db.repos.pages import UNBUDGETED_STATUSES
from corpdisc.models.domain import CrawlStatus

DegradationLevel = Literal["NORMAL", "WARN_80", "STOP_ENQUEUE_95", "EXHAUSTED_100"]
_NO_PROVIDER_CALL: Final = ("BUDGET", "CIRCUIT_OPEN")


@dataclass(frozen=True)
class BudgetLimits:
    max_depth: int = 8
    max_pages_per_domain: int = 40
    max_total_pages: int = 2000
    max_llm_calls_per_task: int = 12
    max_llm_calls_per_run: int = 5000
    max_tool_calls_per_task: int = 20
    max_tool_calls_per_run: int = 5000

    @classmethod
    def from_settings(cls, s: Settings) -> BudgetLimits:
        return cls(
            max_depth=s.max_depth, max_pages_per_domain=s.max_pages_per_domain, max_total_pages=s.max_total_pages,
            max_llm_calls_per_task=s.max_llm_calls_per_task, max_llm_calls_per_run=s.max_llm_calls_per_run,
            max_tool_calls_per_task=s.max_tool_calls_per_task, max_tool_calls_per_run=s.max_tool_calls_per_run,
        )  # fmt: skip


@dataclass(frozen=True)
class BudgetReport:
    pages_total: int
    pages_skipped_limit: int
    llm_calls_made: int
    llm_calls_skipped: int
    tool_calls_made: int
    tokens_used: int
    token_budget: int | None
    tokens_remaining: int | None
    cost_usd: float


def _allow() -> BudgetDecision:
    return BudgetDecision(allow=True)


def _deny(reason: str, detail: str, limit: float, used: float) -> BudgetDecision:
    return BudgetDecision(allow=False, reason=reason, detail=detail, limit=limit, used=used)


class BudgetGuard:
    def __init__(self, conn: sqlite3.Connection, *, run_id: int, limits: BudgetLimits) -> None:
        self._conn = conn
        self._run_id = run_id
        self.limits = limits

    # ------------------------------------------------------------------ crawl limits
    def check_depth(self, depth: int) -> BudgetDecision:
        if depth > self.limits.max_depth:
            return _deny("DEPTH_LIMIT", f"depth {depth} exceeds MAX_DEPTH {self.limits.max_depth}",
                         self.limits.max_depth, depth)
        return _allow()

    def _pages(self, domain: str | None) -> int:
        marks = ",".join("?" for _ in UNBUDGETED_STATUSES)
        sql = f"SELECT COUNT(*) FROM crawl_pages WHERE run_id = ? AND crawl_status NOT IN ({marks})"  # noqa: S608
        params: list[object] = [self._run_id, *UNBUDGETED_STATUSES]
        if domain is not None:
            sql += " AND domain = ?"
            params.append(domain)
        return int(self._conn.execute(sql, params).fetchone()[0])

    def check_page(self, domain: str) -> BudgetDecision:
        total = self._pages(None)
        if total >= self.limits.max_total_pages:
            return _deny("TOTAL_PAGE_LIMIT", f"{total} pages fetched in this run", self.limits.max_total_pages, total)
        per_domain = self._pages(domain)
        if per_domain >= self.limits.max_pages_per_domain:
            return _deny("DOMAIN_PAGE_LIMIT", f"{per_domain} pages fetched for {domain}",
                         self.limits.max_pages_per_domain, per_domain)
        return _allow()

    # --------------------------------------------------------------------- LLM limits
    def _run_row(self) -> sqlite3.Row:
        row = self._conn.execute(
            "SELECT llm_token_budget, llm_tokens_used, llm_cost_usd FROM runs WHERE run_id = ?", (self._run_id,)
        ).fetchone()
        if row is None:
            raise LookupError(f"unknown run_id {self._run_id}")
        return row

    def llm_calls(self, task_id: int | None = None) -> int:
        """Provider-reaching calls: cache hits, SKIPPED and BUDGET/CIRCUIT_OPEN refusals do not count."""
        sql = (
            "SELECT COUNT(*) FROM agent_invocations WHERE run_id = ? AND cache_hit = 0 AND status <> 'SKIPPED' "
            "AND (failure_kind IS NULL OR failure_kind NOT IN (?, ?))"
        )
        params: list[object] = [self._run_id, *_NO_PROVIDER_CALL]
        if task_id is not None:
            sql += " AND task_id = ?"
            params.append(task_id)
        return int(self._conn.execute(sql, params).fetchone()[0])

    def tool_calls(self, task_id: int | None = None) -> int:
        sql = (
            "SELECT COUNT(*) FROM agent_tool_calls c JOIN agent_invocations i ON i.invocation_id = c.invocation_id "
            "WHERE i.run_id = ? AND c.allowed = 1"
        )
        params: list[object] = [self._run_id]
        if task_id is not None:
            sql += " AND i.task_id = ?"
            params.append(task_id)
        return int(self._conn.execute(sql, params).fetchone()[0])

    def check_llm(
        self,
        *,
        task_id: int | None,
        requested_output_tokens: int = 0,
        agent_output_cap: int | None = None,
        estimated_input_tokens: int = 0,
    ) -> BudgetDecision:
        if agent_output_cap is not None and requested_output_tokens > agent_output_cap:
            return _deny("OUTPUT_TOKENS_OVER_AGENT_CAP",
                         f"requested {requested_output_tokens} output tokens, agent cap {agent_output_cap}",
                         agent_output_cap, requested_output_tokens)
        row = self._run_row()
        budget, used = row["llm_token_budget"], row["llm_tokens_used"] or 0
        if budget is not None:
            needed = used + max(estimated_input_tokens, 0) + max(requested_output_tokens, 0)
            if used >= budget or needed > budget:
                return _deny("TOKEN_BUDGET_EXHAUSTED",
                             f"{used} of {budget} tokens used; call needs about {needed - used} more", budget, used)
        if task_id is not None:
            made = self.llm_calls(task_id)
            if made >= self.limits.max_llm_calls_per_task:
                return _deny("TASK_LLM_CALL_LIMIT", f"{made} LLM calls for task {task_id}",
                             self.limits.max_llm_calls_per_task, made)
        made_run = self.llm_calls()
        if made_run >= self.limits.max_llm_calls_per_run:
            return _deny("RUN_LLM_CALL_LIMIT", f"{made_run} LLM calls in this run",
                         self.limits.max_llm_calls_per_run, made_run)
        return _allow()

    def check_tool_call(self, *, task_id: int | None) -> BudgetDecision:
        if task_id is not None:
            made = self.tool_calls(task_id)
            if made >= self.limits.max_tool_calls_per_task:
                return _deny("TASK_TOOL_CALL_LIMIT", f"{made} tool calls for task {task_id}",
                             self.limits.max_tool_calls_per_task, made)
        made_run = self.tool_calls()
        if made_run >= self.limits.max_tool_calls_per_run:
            return _deny("RUN_TOOL_CALL_LIMIT", f"{made_run} tool calls in this run",
                         self.limits.max_tool_calls_per_run, made_run)
        return _allow()

    def record_llm_usage(self, *, tokens_in: int, tokens_out: int, cost_usd: float = 0.0) -> None:
        with self._conn:
            self._conn.execute(
                "UPDATE runs SET llm_tokens_used = llm_tokens_used + ?, llm_cost_usd = llm_cost_usd + ? "
                "WHERE run_id = ?",
                (max(tokens_in, 0) + max(tokens_out, 0), max(cost_usd, 0.0), self._run_id),
            )

    # ------------------------------------------------------------------- reporting
    def token_usage_fraction(self) -> float | None:
        row = self._run_row()
        if row["llm_token_budget"] is None:
            return None
        return (row["llm_tokens_used"] or 0) / row["llm_token_budget"]

    def degradation_level(self) -> DegradationLevel:
        frac = self.token_usage_fraction()
        if frac is None or frac < 0.80:
            return "NORMAL"
        if frac < 0.95:
            return "WARN_80"
        if frac < 1.0:
            return "STOP_ENQUEUE_95"
        return "EXHAUSTED_100"

    def report(self) -> BudgetReport:
        row = self._run_row()
        budget, used = row["llm_token_budget"], row["llm_tokens_used"] or 0
        skipped_pages = self._conn.execute(
            "SELECT COUNT(*) FROM crawl_pages WHERE run_id = ? AND crawl_status = ?",
            (self._run_id, CrawlStatus.SKIPPED_LIMIT.value),
        ).fetchone()[0]
        skipped_llm = self._conn.execute(
            "SELECT COUNT(*) FROM agent_invocations WHERE run_id = ? AND failure_kind IN (?, ?)",
            (self._run_id, *_NO_PROVIDER_CALL),
        ).fetchone()[0]
        return BudgetReport(
            pages_total=self._pages(None), pages_skipped_limit=int(skipped_pages),
            llm_calls_made=self.llm_calls(), llm_calls_skipped=int(skipped_llm), tool_calls_made=self.tool_calls(),
            tokens_used=int(used), token_budget=budget,
            tokens_remaining=None if budget is None else max(budget - used, 0), cost_usd=float(row["llm_cost_usd"] or 0),
        )  # fmt: skip
