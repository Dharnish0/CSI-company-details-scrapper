"""Composition root: wires real services, the agent stack and the graphs from Settings (the only place that does).

This is where a real HTTP client and a real LLM transport may be constructed. Everything below it takes them by
injection, so tests substitute fakes and ordinary runs can never reach the network by accident.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Any

from corpdisc.agents.advisor import AdvisorAgent
from corpdisc.agents.broker import ToolBroker
from corpdisc.agents.dedup import DedupAgent
from corpdisc.agents.discovery import DiscoveryAgent
from corpdisc.agents.extraction import ExtractionAgent
from corpdisc.agents.gateway import CircuitBreaker, GatewayPolicy, LLMGateway
from corpdisc.agents.ports import LLMProvider
from corpdisc.agents.providers import build_provider_from_settings
from corpdisc.agents.relationship import RelationshipAgent
from corpdisc.agents.runner import AgentRunner, RunnerLimits
from corpdisc.agents.validation import ValidationAgent
from corpdisc.agents.website import WebsiteAgent
from corpdisc.clock import Clock, Sleeper, SystemClock
from corpdisc.config import Settings
from corpdisc.db.connection import connect_writer, init_db
from corpdisc.db.repos.audit import SqliteAuditSink, SqliteResponseCache
from corpdisc.db.repos.pages import PageRepo
from corpdisc.db.repos.runs import RunRepo
from corpdisc.graphs.deps import ResearchDeps
from corpdisc.graphs.support import AdvisorService, DedupService
from corpdisc.graphs.supervisor import Supervisor, init_run
from corpdisc.graphs.tools import make_page_excerpt_spec
from corpdisc.services.budget import BudgetGuard, BudgetLimits
from corpdisc.services.committer import Committer
from corpdisc.services.confidence import ConfidenceCalculator
from corpdisc.services.cycle_guard import CycleGuard
from corpdisc.services.fetch import (
    FetchConfig,
    FetchService,
    HttpClient,
    RequestsHttpClient,
    Throttle,
    make_page_text_loader,
    make_robots_fetcher,
)
from corpdisc.services.llm_http import RequestsJsonTransport
from corpdisc.services.matcher import Matcher
from corpdisc.services.pagecache import PageCache
from corpdisc.services.queue import QueueService
from corpdisc.services.quotes import QuoteVerifier
from corpdisc.services.renderer import PlaywrightRenderer
from corpdisc.services.robots import PageRepoRobotsStore, RobotsService
from corpdisc.services.search import SearchService
from corpdisc.transport import JsonTransport

REAL_PROVIDERS = frozenset({"anthropic", "openai"})


@dataclass
class Runtime:
    settings: Settings
    conn: sqlite3.Connection
    run_id: int
    deps: ResearchDeps
    supervisor: Supervisor
    provider: LLMProvider
    _closers: list[Any] = field(default_factory=list)

    def close(self) -> None:
        for c in self._closers:
            try:
                c.close()
            except Exception:  # noqa: BLE001 - best effort
                pass
        self.conn.close()


def latest_run_id(conn: sqlite3.Connection) -> int | None:
    row = conn.execute("SELECT run_id FROM runs ORDER BY run_id DESC LIMIT 1").fetchone()
    return None if row is None else int(row[0])


def uses_real_provider(settings: Settings) -> bool:
    return (settings.llm_provider or "").strip().lower() in REAL_PROVIDERS


def build_runtime(
    settings: Settings,
    *,
    run_id: int | None = None,
    http: HttpClient | None = None,
    transport: JsonTransport | None = None,
    provider: LLMProvider | None = None,
    clock: Clock | None = None,
    sleeper: Sleeper | None = None,
    search: SearchService | None = None,
) -> Runtime:
    """Wire everything. ``run_id=None`` creates a new run; pass an id to resume one. Never touches the network."""
    init_db(settings.db_path)
    conn = connect_writer(settings.db_path)
    sys_clock = SystemClock()
    clock, sleeper = clock or sys_clock, sleeper or sys_clock
    closers: list[Any] = []
    runs = RunRepo(conn, clock)
    if run_id is None:
        run_id = init_run(runs, settings)
    if provider is None:
        name = (settings.llm_provider or "").strip().lower()
        if name in REAL_PROVIDERS:
            if not settings.llm_model.strip():
                conn.close()
                raise ValueError(f"LLM_MODEL is required for provider '{name}'")
            if transport is None:
                transport = RequestsJsonTransport()
                closers.append(transport)
        provider = build_provider_from_settings(settings, transport=transport)
    model = settings.llm_model.strip() or "unspecified"

    cfg = FetchConfig.from_settings(settings)
    pages = PageRepo(conn, clock)
    cache = PageCache(settings.cache_dir)
    throttle = Throttle(clock, sleeper, cfg.request_delay)
    if http is None:
        http = RequestsHttpClient(verify_ssl=not settings.allow_insecure_ssl)
        closers.append(http)
    robots = RobotsService(fetcher=make_robots_fetcher(http, throttle, user_agent=cfg.user_agent, timeout=cfg.timeout),
                           store=PageRepoRobotsStore(pages), user_agent=cfg.user_agent, clock=clock,
                           respect_robots=cfg.respect_robots, failure_cooldown_seconds=cfg.robots_failure_cooldown)
    budget = BudgetGuard(conn, run_id=run_id, limits=BudgetLimits.from_settings(settings))
    renderer = None if settings.use_playwright == "false" else PlaywrightRenderer()  # imports playwright lazily
    fetch = FetchService(config=cfg, pages=pages, cache=cache, robots=robots, http=http, throttle=throttle, clock=clock,
                         sleeper=sleeper, budget=budget, renderer=renderer)
    if renderer is not None:
        closers.append(fetch)

    gateway = LLMGateway(provider, model=model, policy=GatewayPolicy.from_settings(settings),
                         breaker=CircuitBreaker(settings.llm_breaker_threshold), budget=budget, clock=clock, sleeper=sleeper)
    audit = SqliteAuditSink(conn)
    broker = ToolBroker([make_page_excerpt_spec(pages, cache, run_id)], audit=audit, clock=clock, budget=budget)
    runner = AgentRunner(gateway=gateway, audit=audit, cache=SqliteResponseCache(conn), budget=budget, broker=broker,
                         limits=RunnerLimits.from_settings(settings), clock=clock)
    committer = Committer.from_settings(conn, run_id=run_id, page_text=make_page_text_loader(pages, cache),
                                        settings=settings, clock=clock)
    deps = ResearchDeps(
        conn=conn, run_id=run_id, settings=settings, clock=clock, fetch=fetch, pages_repo=pages,
        queue=QueueService(conn, clock), budget=budget, cycle=CycleGuard(conn),
        matcher=Matcher.from_settings(conn, run_id, settings), committer=committer,
        calc=ConfidenceCalculator.from_settings(settings), verifier=QuoteVerifier(), runner=runner,
        agents={"discovery": DiscoveryAgent(), "extraction": ExtractionAgent(), "relationship": RelationshipAgent(),
                "website": WebsiteAgent(), "advisor": AdvisorAgent(), "dedup": DedupAgent(), "validation": ValidationAgent()},
        advisor=AdvisorService() if settings.advisor_enabled else None, dedup=DedupService(), search=search)
    return Runtime(settings, conn, run_id, deps, Supervisor(deps, runs), provider, closers)
