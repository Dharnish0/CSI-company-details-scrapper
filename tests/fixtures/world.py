"""Assemble a complete M4 stack on a tmp SQLite DB: real services + real AgentRunner + fixture Brain."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from corpdisc.agents.broker import ToolBroker
from corpdisc.agents.discovery import DiscoveryAgent
from corpdisc.agents.extraction import ExtractionAgent
from corpdisc.agents.gateway import CircuitBreaker, GatewayPolicy, LLMGateway
from corpdisc.agents.providers.fake import HandlerProvider
from corpdisc.agents.relationship import RelationshipAgent
from corpdisc.agents.runner import AgentRunner, RunnerLimits
from corpdisc.agents.website import WebsiteAgent
from corpdisc.config import Settings
from corpdisc.db.repos.audit import SqliteAuditSink, SqliteResponseCache
from corpdisc.db.repos.pages import PageRepo
from corpdisc.db.repos.runs import RunRepo
from corpdisc.graphs.deps import ResearchDeps
from corpdisc.graphs.tools import make_page_excerpt_spec
from corpdisc.graphs.supervisor import Supervisor, init_run
from corpdisc.services.budget import BudgetGuard, BudgetLimits
from corpdisc.services.committer import Committer
from corpdisc.services.confidence import ConfidenceCalculator
from corpdisc.services.cycle_guard import CycleGuard
from corpdisc.services.fetch import FetchConfig, FetchService, Throttle, make_page_text_loader, make_robots_fetcher
from corpdisc.services.matcher import Matcher
from corpdisc.services.pagecache import PageCache
from corpdisc.services.queue import QueueService
from corpdisc.services.quotes import QuoteVerifier
from corpdisc.services.robots import PageRepoRobotsStore, RobotsService
from tests.fixtures.site import ROOT, Brain, routes
from tests.helpers.clock import FakeClock
from tests.helpers.db import make_db
from tests.helpers.fakes import FakeHttpClient


@dataclass
class World:
    conn: object
    deps: ResearchDeps
    sup: Supervisor
    http: FakeHttpClient
    brain: Brain
    provider: HandlerProvider
    clock: FakeClock
    run_id: int

    def q(self, sql: str, *params):
        return self.conn.execute(sql, params).fetchall()

    def entity(self, name: str):
        return self.conn.execute("SELECT * FROM entities WHERE run_id=? AND name=?", (self.run_id, name)).fetchone()

    def names(self, status: str | None = None) -> set[str]:
        sql = "SELECT name FROM entities WHERE run_id=?" + (" AND verification_status=?" if status else "")
        return {r[0] for r in self.conn.execute(sql, (self.run_id, status) if status else (self.run_id,))}

    def edge(self, parent: str, child: str):
        return self.conn.execute(
            "SELECT r.* FROM relationships r JOIN entities p ON p.entity_id=r.parent_id JOIN entities c ON c.entity_id=r.child_id "
            "WHERE p.name=? AND c.name=? AND p.run_id=?", (parent, child, self.run_id)).fetchone()

    def page_requests(self) -> list[str]:
        return [u for u in self.http.calls if not u.endswith("/robots.txt")]


def build_world(tmp_path: Path, *, conn=None, http=None, brain=None, settings_kw=None, breaker_threshold=5,
                llm_token_budget=None, run_id=None, token_budget=None, cache_dir=None) -> World:
    conn = conn or make_db(tmp_path)
    kw = dict(start_url="https://root.test/", root_company=ROOT, root_code="NWH", request_delay_seconds=0.0,
              max_retries=0, db_path=tmp_path / "x.db", cache_dir=cache_dir or tmp_path / ".cache",
              log_dir=tmp_path / "logs", max_links_per_entity=4)
    kw.update(settings_kw or {})
    settings = Settings(_env_file=None, **kw)
    clock = FakeClock()
    runs = RunRepo(conn, clock)
    if run_id is None:
        run_id = init_run(runs, settings)
    http = http or FakeHttpClient(routes())
    brain = brain or Brain()
    cfg = FetchConfig.from_settings(settings)
    pages = PageRepo(conn, clock)
    cache = PageCache(settings.cache_dir)
    throttle = Throttle(clock, clock, cfg.request_delay)
    robots = RobotsService(fetcher=make_robots_fetcher(http, throttle, user_agent=cfg.user_agent, timeout=cfg.timeout),
                           store=PageRepoRobotsStore(pages), user_agent=cfg.user_agent, clock=clock,
                           respect_robots=cfg.respect_robots, failure_cooldown_seconds=cfg.robots_failure_cooldown)
    budget = BudgetGuard(conn, run_id=run_id, limits=BudgetLimits.from_settings(settings))
    fetch = FetchService(config=cfg, pages=pages, cache=cache, robots=robots, http=http, throttle=throttle,
                         clock=clock, sleeper=clock, budget=budget)
    provider = HandlerProvider(brain)
    gateway = LLMGateway(provider, model="stub", policy=GatewayPolicy.from_settings(settings),
                         breaker=CircuitBreaker(breaker_threshold), budget=budget, clock=clock, sleeper=clock)
    audit = SqliteAuditSink(conn)
    runner = AgentRunner(gateway=gateway, audit=audit, cache=SqliteResponseCache(conn), budget=budget,
                         broker=ToolBroker([make_page_excerpt_spec(pages, cache, run_id)], audit=audit, clock=clock, budget=budget),
                         limits=RunnerLimits.from_settings(settings), clock=clock)
    verifier = QuoteVerifier()
    committer = Committer.from_settings(conn, run_id=run_id, page_text=make_page_text_loader(pages, cache),
                                        settings=settings, clock=clock)
    deps = ResearchDeps(
        conn=conn, run_id=run_id, settings=settings, clock=clock, fetch=fetch, pages_repo=pages,
        queue=QueueService(conn, clock), budget=budget, cycle=CycleGuard(conn),
        matcher=Matcher.from_settings(conn, run_id, settings), committer=committer,
        calc=ConfidenceCalculator.from_settings(settings), verifier=verifier, runner=runner,
        agents={"discovery": DiscoveryAgent(), "extraction": ExtractionAgent(), "relationship": RelationshipAgent(),
                "website": WebsiteAgent()})
    sup = Supervisor(deps, runs)
    return World(conn, deps, sup, http, brain, provider, clock, run_id)


def seeded_world(tmp_path: Path, **kw) -> World:
    w = build_world(tmp_path, **kw)
    w.sup.seed_root()
    return w
