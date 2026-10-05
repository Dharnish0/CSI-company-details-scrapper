from __future__ import annotations

import pytest

from corpdisc.db.repos.pages import PageRepo
from corpdisc.services.robots import (
    PageRepoRobotsStore,
    RobotsFetchResult,
    RobotsReason,
    RobotsService,
)
from tests.helpers.clock import FakeClock
from tests.helpers.db import make_db

UA = "CorpDiscoveryBot/0.1 (+test)"
ROBOTS = "User-agent: *\nDisallow: /private\nCrawl-delay: 3\n\nUser-agent: otherbot\nDisallow: /\n"


class ScriptedFetcher:
    def __init__(self, *results: RobotsFetchResult) -> None:
        self.results = list(results)
        self.calls: list[str] = []

    def __call__(self, url: str) -> RobotsFetchResult:
        self.calls.append(url)
        return self.results.pop(0) if len(self.results) > 1 else self.results[0]


class MemoryStore:
    def __init__(self) -> None:
        self.data = {}
        self.failures = []

    def load(self, domain):
        return self.data.get(domain)

    def save(self, domain, text, at, delay):
        self.data[domain] = (text, at, delay)

    def record_failure(self, domain, at):
        self.failures.append((domain, at))


def ok(text=ROBOTS):
    return RobotsFetchResult(200, text)


def make(fetcher, *, store=None, clock=None, **kw):
    clock = clock or FakeClock()
    store = store or MemoryStore()
    return RobotsService(fetcher=fetcher, store=store, user_agent=UA, clock=clock, **kw), store, clock


def test_allowed_and_disallowed_urls():
    svc, _, _ = make(ScriptedFetcher(ok()))
    allowed = svc.is_allowed("https://a.test/companies")
    assert allowed.allowed and allowed.reason is RobotsReason.ALLOWED and allowed.domain == "a.test"
    denied = svc.is_allowed("https://a.test/private/area")
    assert not denied.allowed and denied.reason is RobotsReason.DISALLOWED


def test_crawl_delay_parsed_and_exposed():
    svc, _, _ = make(ScriptedFetcher(ok()))
    assert svc.crawl_delay("https://a.test/x") is None  # nothing loaded yet
    d = svc.is_allowed("https://a.test/x")
    assert d.crawl_delay == 3.0 and svc.crawl_delay("https://a.test/x") == 3.0


@pytest.mark.parametrize("status", [404, 410, 401, 403])
def test_missing_or_forbidden_robots_allows_everything(status):
    svc, store, _ = make(ScriptedFetcher(RobotsFetchResult(status, None, f"HTTP {status}")))
    d = svc.is_allowed("https://a.test/private")
    assert d.allowed and d.reason is RobotsReason.NO_ROBOTS
    assert store.data["a.test"][0] == ""  # cached as empty text


@pytest.mark.parametrize("result", [RobotsFetchResult(503, None, "HTTP 503"), RobotsFetchResult(500, None, "x"),
                                    RobotsFetchResult(None, None, "TIMEOUT")])
def test_temporary_failure_denies_and_is_not_cached_as_success(result):
    fetcher = ScriptedFetcher(result)
    svc, store, _ = make(fetcher)
    d = svc.is_allowed("https://a.test/anything")
    assert not d.allowed and d.reason is RobotsReason.UNAVAILABLE_DENY
    assert store.data == {} and len(store.failures) == 1
    for _ in range(5):  # never hammered within the cool-down
        assert not svc.is_allowed("https://a.test/other").allowed
    assert len(fetcher.calls) == 1


def test_failure_is_retried_only_after_cooldown():
    fetcher = ScriptedFetcher(RobotsFetchResult(503, None), ok())
    svc, _, clock = make(fetcher, failure_cooldown_seconds=600)
    assert not svc.is_allowed("https://a.test/x").allowed
    clock.advance(599)
    assert not svc.is_allowed("https://a.test/x").allowed and len(fetcher.calls) == 1
    clock.advance(2)
    assert svc.is_allowed("https://a.test/x").allowed and len(fetcher.calls) == 2


def test_decision_cached_robots_fetched_once_per_domain():
    fetcher = ScriptedFetcher(ok())
    svc, _, _ = make(fetcher)
    first = svc.is_allowed("https://a.test/a")
    second = svc.is_allowed("https://a.test/b")
    svc.is_allowed("https://a.test/private")
    assert len(fetcher.calls) == 1 and fetcher.calls[0] == "https://a.test/robots.txt"
    assert not first.from_cache and second.from_cache
    svc.is_allowed("https://b.test/a")
    assert len(fetcher.calls) == 2  # independent per domain


def test_persisted_robots_reused_within_ttl_and_refetched_after():
    store, clock = MemoryStore(), FakeClock()
    f1 = ScriptedFetcher(ok())
    make(f1, store=store, clock=clock)[0].is_allowed("https://a.test/x")
    f2 = ScriptedFetcher(ok("User-agent: *\nDisallow: /x\n"))
    svc2, _, _ = make(f2, store=store, clock=clock, ttl_seconds=3600)
    d = svc2.is_allowed("https://a.test/x")
    assert d.allowed and d.from_cache and f2.calls == []  # stored text (which allows /x) was reused
    clock.advance(3601)
    svc3, _, _ = make(f2, store=store, clock=clock, ttl_seconds=3600)
    assert not svc3.is_allowed("https://a.test/x").allowed and len(f2.calls) == 1


def test_robots_txt_itself_is_always_allowed():
    fetcher = ScriptedFetcher(ok("User-agent: *\nDisallow: /\n"))
    svc, _, _ = make(fetcher)
    assert svc.is_allowed("https://a.test/robots.txt").allowed
    assert not svc.is_allowed("https://a.test/page").allowed


def test_respect_robots_false_is_explicit_and_recorded():
    fetcher = ScriptedFetcher(ok("User-agent: *\nDisallow: /\n"))
    svc, _, _ = make(fetcher, respect_robots=False)
    d = svc.is_allowed("https://a.test/page")
    assert d.allowed and d.reason is RobotsReason.DISABLED and fetcher.calls == []
    assert svc.decisions[-1] == d


def test_decisions_audit_ring_and_invalid_url():
    svc, _, _ = make(ScriptedFetcher(ok()))
    svc.is_allowed("https://a.test/a")
    svc.is_allowed("https://a.test/private")
    bad = svc.is_allowed("mailto:x@y.test")
    assert not bad.allowed and bad.reason is RobotsReason.INVALID_URL
    assert [d.reason for d in svc.decisions] == [RobotsReason.ALLOWED, RobotsReason.DISALLOWED, RobotsReason.INVALID_URL]


def test_fetcher_exception_fails_closed():
    def boom(url):
        raise RuntimeError("network down")

    svc, _, _ = make(boom)
    assert not svc.is_allowed("https://a.test/x").allowed


def test_no_override_api_exists():
    import inspect

    params = inspect.signature(RobotsService.is_allowed).parameters
    assert list(params) == ["self", "url"]
    assert not any("override" in n or "force" in n or "ignore" in n for n in dir(RobotsService))


def test_page_repo_store_persists_robots_in_domain_stats(tmp_path):
    conn = make_db(tmp_path)
    clock = FakeClock()
    repo = PageRepo(conn, clock)
    svc = RobotsService(fetcher=ScriptedFetcher(ok()), store=PageRepoRobotsStore(repo), user_agent=UA, clock=clock)
    svc.is_allowed("https://a.test/x")
    row = repo.get_domain_stats("a.test")
    assert row.robots_txt == ROBOTS and row.crawl_delay_s == 3.0 and row.robots_fetched_at == clock.now()
    # new process: reuses the stored text, no fetch
    f2 = ScriptedFetcher(RobotsFetchResult(500, None))
    svc2 = RobotsService(fetcher=f2, store=PageRepoRobotsStore(repo), user_agent=UA, clock=clock)
    assert not svc2.is_allowed("https://a.test/private").allowed and f2.calls == []
    # failures bump error_count
    svc3 = RobotsService(fetcher=f2, store=PageRepoRobotsStore(repo), user_agent=UA, clock=clock)
    svc3.is_allowed("https://c.test/x")
    assert repo.get_domain_stats("c.test").error_count == 1
    conn.close()
