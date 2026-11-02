"""Composition root: wires real services, the agent stack and the graphs from Settings (the only place that does).

This is where a real HTTP client and a real LLM transport may be constructed. Everything below it takes them by
injection, so tests substitute fakes and ordinary runs can never reach the network by accident.
"""

from __future__ import annotations

import random
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from corpdisc.agents.advisor import AdvisorAgent
from corpdisc.agents.broker import ToolBroker
from corpdisc.agents.dedup import DedupAgent
from corpdisc.agents.discovery import DiscoveryAgent
from corpdisc.agents.extraction import ExtractionAgent
from corpdisc.agents.gateway import PROVIDER_DEFAULT_LIMITS, CircuitBreaker, GatewayPolicy, LLMGateway, RequestPacer
from corpdisc.agents.ports import LLMProvider
from corpdisc.agents.ratelimit import LimiterConfig, SlowCrawlLimiter, TokenBucket
from corpdisc.agents.providers import build_provider_from_settings
from corpdisc.agents.relationship import RelationshipAgent
from corpdisc.agents.runner import AgentRunner, RunnerLimits
from corpdisc.agents.validation import ValidationAgent
from corpdisc.agents.website import WebsiteAgent
from corpdisc.clock import Clock, Sleeper, SystemClock, iso_utc
from corpdisc.contracts.agents import AgentName
from corpdisc.config import PROVIDER_MODEL_FIELDS, Settings
from corpdisc.db.connection import connect_writer, init_db
from corpdisc.db.repos.audit import SqliteAuditSink, SqliteResponseCache
from corpdisc.db.repos.pages import PageRepo
from corpdisc.db.repos.runs import RunRepo
from corpdisc.graphs.deps import ResearchDeps
from corpdisc.graphs.support import AdvisorService, DedupService
from corpdisc.graphs.supervisor import Supervisor, init_run
from corpdisc.graphs.tools import make_page_excerpt_spec
from corpdisc.logging_setup import get_logger
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

log = get_logger("app")

REAL_PROVIDERS = frozenset({"anthropic", "openai", "ollama", "groq", "gemini"})


@dataclass
class Runtime:
    settings: Settings
    conn: sqlite3.Connection
    run_id: int
    deps: ResearchDeps
    supervisor: Supervisor
    provider: LLMProvider
    _closers: list[Any] = field(default_factory=list)
    limiter: SlowCrawlLimiter | None = None  # set only in slow crawl mode

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


def provider_settings_for_run(settings: Settings, *, run_provider: str | None, run_model: str | None) -> Settings:
    """Copy of ``settings`` with the per-run provider/model override applied to the matching model field."""
    provider = (run_provider or settings.llm_provider or "").strip().lower()
    update: dict[str, Any] = {"llm_provider": provider}
    if run_model:
        update[PROVIDER_MODEL_FIELDS.get(provider, "llm_model")] = run_model
    return settings.model_copy(update=update)


def make_pacer(settings: Settings, provider_name: str, clock: Clock, sleeper: Sleeper) -> RequestPacer | None:
    """Per-provider client-side pacing: explicit settings win, -1 means the provider default, 0 disables."""
    d_rpm, d_tpm = PROVIDER_DEFAULT_LIMITS.get((provider_name or "").lower(), (0, 0))
    rpm = d_rpm if settings.llm_rpm_limit < 0 else settings.llm_rpm_limit
    tpm = d_tpm if settings.llm_tpm_limit < 0 else settings.llm_tpm_limit
    if rpm <= 0 and tpm <= 0:
        return None
    return RequestPacer(rpm=rpm, tpm=tpm, clock=clock, sleeper=sleeper)


def make_limiter(settings: Settings, conn: sqlite3.Connection, clock: Clock, sleeper: Sleeper, *,
                 stop_check: Callable[[], str | None] | None = None, deadline: float | None = None) -> SlowCrawlLimiter:
    """Slow crawl limiter; the daily-token estimate is seeded from the last 24 h of recorded provider usage."""
    cfg = LimiterConfig.from_settings(settings)
    now_mono, now = clock.monotonic(), clock.now()
    bucket = TokenBucket(cfg.tpd_limit, now_mono)
    if bucket.enabled:
        rows = conn.execute(
            "SELECT started_at, COALESCE(tokens_in,0) + COALESCE(tokens_out,0) FROM agent_invocations "
            "WHERE cache_hit = 0 AND started_at >= ? AND COALESCE(tokens_in,0) + COALESCE(tokens_out,0) > 0",
            (iso_utc(now - timedelta(hours=24)),)).fetchall()
        bucket.replay([((now - datetime.fromisoformat(r[0])).total_seconds(), int(r[1])) for r in rows], now_mono)
    if deadline is None:
        deadline = now_mono + settings.slow_crawl_max_runtime_hours * 3600.0
    return SlowCrawlLimiter(cfg, clock, sleeper, rng=random.Random(), stop_check=stop_check, deadline=deadline, bucket=bucket)


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
    run_provider: str | None = None,  # Runtime provider override
    run_model: str | None = None,  # Runtime model override
    stop_check: Callable[[], str | None] | None = None,  # slow crawl: returns a stop reason (e.g. "SHUTDOWN") or None
    deadline: float | None = None,  # slow crawl: monotonic deadline; default = now + SLOW_CRAWL_MAX_RUNTIME_HOURS
) -> Runtime:
    """Wire everything. ``run_id=None`` creates a new run; pass an id to resume one. Never touches the network.
    
    Args:
        settings: Base settings from .env
        run_id: Run ID (None = create new)
        http: HTTP client (optional, created if not provided)
        transport: LLM transport (optional, created if not provided)
        provider: LLM provider (optional, built from settings if not provided)
        clock: Clock service (optional, SystemClock if not provided)
        sleeper: Sleeper service (optional, SystemClock if not provided)
        search: Search service (optional)
        run_provider: Override provider for this run (e.g., "groq")
        run_model: Override model for this run (e.g., "openai/gpt-oss-120b")
        stop_check: Slow crawl only. Polled while the limiter waits; a non-empty reason stops new provider requests.
        deadline: Slow crawl only. Monotonic time after which no new provider request starts.
    """
    init_db(settings.db_path)
    conn = connect_writer(settings.db_path)
    sys_clock = SystemClock()
    clock, sleeper = clock or sys_clock, sleeper or sys_clock
    closers: list[Any] = []
    runs = RunRepo(conn, clock)
    if run_id is None:
        run_id = init_run(runs, settings, run_provider=run_provider, run_model=run_model)
    
    # Determine provider/model to use (runtime override takes precedence)
    effective_provider = (run_provider or settings.llm_provider or "").strip().lower()
    model_field = PROVIDER_MODEL_FIELDS.get(effective_provider, "llm_model")
    effective_model = run_model or getattr(settings, model_field) or settings.llm_model or ""
    
    if provider is None:
        name = effective_provider
        if name in REAL_PROVIDERS or name == "gemini":
            if not effective_model.strip():
                conn.close()
                raise ValueError(f"LLM_MODEL is required for provider '{name}'")
            if transport is None:
                transport = RequestsJsonTransport()
                closers.append(transport)
        provider_settings = provider_settings_for_run(settings, run_provider=run_provider, run_model=run_model)
        provider = build_provider_from_settings(provider_settings, transport=transport)
    
    # Use effective model
    model = effective_model.strip() if effective_model else "unspecified"

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

    limiter = None
    if settings.slow_crawl:
        limiter = make_limiter(settings, conn, clock, sleeper, stop_check=stop_check, deadline=deadline)
        pacer = None  # the limiter replaces the RPM/TPM pacer (double sleeping would only add waits)
        log.info("slow crawl: LLM_RPM_LIMIT and LLM_TPM_LIMIT are ignored; one request at a time, %.0fs between starts",
                 settings.slow_crawl_interval_seconds)
    else:
        pacer = make_pacer(settings, effective_provider or getattr(provider, "name", ""), clock, sleeper)
    gateway = LLMGateway(provider, model=model, policy=GatewayPolicy.from_settings(settings),
                         breaker=CircuitBreaker(settings.llm_breaker_threshold), budget=budget, clock=clock, sleeper=sleeper,
                         pacer=pacer, limiter=limiter)
    audit = SqliteAuditSink(conn)
    broker = ToolBroker([make_page_excerpt_spec(pages, cache, run_id)], audit=audit, clock=clock, budget=budget)
    runner = AgentRunner(gateway=gateway, audit=audit, cache=SqliteResponseCache(conn), budget=budget, broker=broker,
                         limits=RunnerLimits.from_settings(settings), clock=clock,
                         reasoning_overrides={AgentName.EXTRACTION: "none"} if settings.llm_extraction_reasoning == "none" else None)
    committer = Committer.from_settings(conn, run_id=run_id, page_text=make_page_text_loader(pages, cache),
                                        settings=settings, clock=clock)
    deps = ResearchDeps(
        conn=conn, run_id=run_id, settings=settings, clock=clock, fetch=fetch, pages_repo=pages,
        queue=QueueService(conn, clock, max_attempts=settings.queue_max_attempts), budget=budget, cycle=CycleGuard(conn),
        matcher=Matcher.from_settings(conn, run_id, settings), committer=committer,
        calc=ConfidenceCalculator.from_settings(settings), verifier=QuoteVerifier(), runner=runner,
        agents={"discovery": DiscoveryAgent(), "extraction": ExtractionAgent(), "relationship": RelationshipAgent(),
                "website": WebsiteAgent(), "advisor": AdvisorAgent(), "dedup": DedupAgent(), "validation": ValidationAgent()},
        advisor=AdvisorService() if settings.advisor_enabled else None, dedup=DedupService(), search=search,
        export_path=settings.output_xlsx)
    return Runtime(settings, conn, run_id, deps, Supervisor(deps, runs, sleeper), provider, closers, limiter)
