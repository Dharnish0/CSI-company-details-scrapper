"""Dependencies handed to graph nodes. Graphs coordinate services (deterministic side) and the AgentRunner."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Any, Protocol

from corpdisc.agents.runner import AgentRunner
from corpdisc.clock import Clock
from corpdisc.config import Settings
from corpdisc.db.repos.pages import PageRepo
from corpdisc.services.budget import BudgetGuard
from corpdisc.services.committer import Committer
from corpdisc.services.confidence import ConfidenceCalculator
from corpdisc.services.cycle_guard import CycleGuard
from corpdisc.services.fetch import FetchService
from corpdisc.services.matcher import Matcher
from corpdisc.services.queue import QueueService
from corpdisc.services.quotes import QuoteVerifier
from corpdisc.services.search import SearchService


class AdvisorPort(Protocol):
    """Advisory only (M7): may answer the grey-zone question; the runtime applies the policy."""

    def advise(self, deps: ResearchDeps, entity_id: int, depth: int, reason: str) -> str | None: ...  # CRAWL|TERMINAL


class DedupPort(Protocol):
    """Optional Deduplication Agent (M7), consulted only for pairs the deterministic ladder left AMBIGUOUS."""

    def judge(self, deps: ResearchDeps, name: str, parent_id: int, ambiguous_ids: list[int]) -> str: ...  # SAME|DIFFERENT|UNSURE


@dataclass
class ResearchDeps:
    conn: sqlite3.Connection
    run_id: int
    settings: Settings
    clock: Clock
    fetch: FetchService
    pages_repo: PageRepo
    queue: QueueService
    budget: BudgetGuard
    cycle: CycleGuard
    matcher: Matcher
    committer: Committer
    calc: ConfidenceCalculator
    verifier: QuoteVerifier
    runner: AgentRunner
    agents: dict[str, Any]  # discovery, extraction, relationship, website (BaseAgent instances)
    advisor: AdvisorPort | None = None
    search: SearchService | None = None
    dedup: DedupPort | None = None
    scratch: dict[int, Any] = field(default_factory=dict)  # per-task bulky data, never in graph state
