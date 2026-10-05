from __future__ import annotations

from dataclasses import replace

import pytest

from corpdisc.contracts.common import BudgetDecision
from corpdisc.db.repos.pages import PageRepo
from corpdisc.models.domain import CrawlStatus, PageType
from corpdisc.services.fetch import (
    FetchConfig,
    FetchService,
    Throttle,
    make_page_text_loader,
    make_robots_fetcher,
)
from corpdisc.services.pagecache import PageCache
from corpdisc.services.renderer import RendererError, RendererUnavailable
from corpdisc.services.robots import PageRepoRobotsStore, RobotsService
from tests.helpers.clock import FakeClock
from tests.helpers.db import count, make_db, seed_run
from tests.helpers.fakes import FakeHttpClient, FakeRenderer, err, resp

U = "https://a.test"
BODY = "<html><body><h1>Northwind Holdings</h1><p>" + ("Our companies and portfolio brands. " * 12) + "</p></body></html>"
SHELL = '<html><body><div id="root"></div><script src="/app.js"></script></body></html>'


class Env:
    def __init__(self, tmp_path, http, cfg, renderer=None, budget=None):
        self.conn = make_db(tmp_path)
        self.run_id = seed_run(self.conn)
        self.clock = FakeClock()
        self.http = http
        self.pages = PageRepo(self.conn, self.clock)
        self.cache = PageCache(tmp_path / "cache")
        throttle = Throttle(self.clock, self.clock, cfg.request_delay)
        robots = RobotsService(
            fetcher=make_robots_fetcher(http, throttle, user_agent=cfg.user_agent, timeout=cfg.timeout),
            store=PageRepoRobotsStore(self.pages), user_agent=cfg.user_agent, clock=self.clock,
            respect_robots=cfg.respect_robots, failure_cooldown_seconds=cfg.robots_failure_cooldown,
        )
        self.robots = robots
        self.renderer = renderer
        self.svc = FetchService(config=cfg, pages=self.pages, cache=self.cache, robots=robots, http=http,
                                throttle=throttle, clock=self.clock, sleeper=self.clock, budget=budget,
                                renderer=renderer)

    def fetch(self, path="/", **kw):
        return self.svc.fetch(U + path, run_id=self.run_id, **kw)

    def page_calls(self):
        return [u for u in self.http.calls if not u.endswith("/robots.txt")]

    def errors(self):
        return [tuple(r) for r in self.conn.execute("SELECT error_type, url FROM crawl_errors ORDER BY error_id")]


CFG = FetchConfig(timeout=5, max_retries=2, request_delay=2.0, use_playwright="auto", playwright_min_text_chars=200)


@pytest.fixture
def make_env(tmp_path):
    envs = []

    def factory(routes=None, cfg=CFG, **kw):
        e = Env(tmp_path / f"env{len(envs)}", FakeHttpClient(routes or {}), cfg, **kw)
        envs.append(e)
        return e

    yield factory
    for e in envs:
        e.conn.close()


# ------------------------------------------------------------------ happy path & rows
def test_normal_fetch_result_and_rows(make_env):
    e = make_env({f"{U}/": resp(f"{U}/", 200, BODY, headers={"ETag": "x", "Set-Cookie": "secret=1"})})
    r = e.fetch("/", page_type=PageType.HOME, entity_id=None)
    assert r.ok and r.crawl_status is CrawlStatus.OK and r.status_code == 200
    assert r.canonical_url == f"{U}/" and r.final_url == f"{U}/" and r.fetch_method == "requests"
    assert r.cache_hit is False and r.content_type.startswith("text/html")
    assert "Northwind Holdings" in r.text and "<h1>" not in r.text and r.html == BODY
    assert r.response_headers.get("etag") == "x" and "set-cookie" not in r.response_headers
    assert r.retrieved_at == e.clock.now() or r.retrieved_at <= e.clock.now()
    row = e.pages.get_page(e.run_id, f"{U}/")
    assert row.crawl_status is CrawlStatus.OK and row.fetch_method == "requests" and row.http_status == 200
    assert row.page_type == "HOME" and row.robots_allowed is True and row.content_hash == r.content_hash
    assert row.text_path.startswith("pages/") and row.html_path.endswith(".html")
    assert e.svc.load_text(r.page_id) == r.text and e.svc.load_html(r.page_id) == BODY
    stats = e.pages.get_domain_stats("a.test")
    assert stats.pages_fetched >= 1 and stats.robots_txt is not None


def test_robots_checked_before_any_page_request(make_env):
    e = make_env({f"{U}/robots.txt": resp(f"{U}/robots.txt", 200, "User-agent: *\nDisallow: /secret\n", ctype="text/plain"),
                  f"{U}/ok": resp(f"{U}/ok", 200, BODY)})
    assert e.fetch("/ok").ok
    assert e.http.calls[0] == f"{U}/robots.txt" and e.http.calls[1] == f"{U}/ok"


def test_robots_disallowed_is_blocked_without_requesting_the_page(make_env):
    e = make_env({f"{U}/robots.txt": resp(f"{U}/robots.txt", 200, "User-agent: *\nDisallow: /secret\n", ctype="text/plain")})
    r = e.fetch("/secret/x")
    assert r.crawl_status is CrawlStatus.BLOCKED_ROBOTS and r.error_code == "ROBOTS_DISALLOWED" and not r.ok
    assert r.robots_allowed is False and e.page_calls() == []
    row = e.pages.get_page(e.run_id, f"{U}/secret/x")
    assert row.crawl_status is CrawlStatus.BLOCKED_ROBOTS and row.robots_allowed is False
    assert ("ROBOTS_DISALLOWED", f"{U}/secret/x") in e.errors()


def test_robots_unavailable_blocks_and_does_not_hammer(make_env):
    e = make_env({f"{U}/robots.txt": resp(f"{U}/robots.txt", 503, "down", ctype="text/plain")})
    r1, r2 = e.fetch("/a"), e.fetch("/b")
    assert r1.crawl_status is r2.crawl_status is CrawlStatus.BLOCKED_ROBOTS and r1.error_code == "ROBOTS_UNAVAILABLE"
    assert e.http.count(f"{U}/robots.txt") == 1 and e.page_calls() == []


def test_respect_robots_false_skips_robots(make_env):
    e = make_env({f"{U}/x": resp(f"{U}/x", 200, BODY)}, cfg=replace(CFG, respect_robots=False))
    assert e.fetch("/x").ok and f"{U}/robots.txt" not in e.http.calls


def test_crawl_delay_is_honoured_by_throttle(make_env):
    e = make_env({f"{U}/robots.txt": resp(f"{U}/robots.txt", 200, "User-agent: *\nCrawl-delay: 7\n", ctype="text/plain"),
                  f"{U}/1": resp(f"{U}/1", 200, BODY), f"{U}/2": resp(f"{U}/2", 200, BODY)})
    e.fetch("/1")
    e.clock.sleeps.clear()
    e.fetch("/2")
    assert e.clock.sleeps and e.clock.sleeps[-1] == pytest.approx(7.0)


# ------------------------------------------------------------------ status mapping
def test_404_maps_to_http_error_without_retry(make_env):
    e = make_env({f"{U}/nope": resp(f"{U}/nope", 404, "missing", ctype="text/plain")})
    r = e.fetch("/nope")
    assert (r.crawl_status, r.status_code, r.error_code) == (CrawlStatus.HTTP_ERROR, 404, "HTTP_404")
    assert e.http.count(f"{U}/nope") == 1 and e.clock.sleeps == []
    assert e.pages.get_page(e.run_id, f"{U}/nope").error.startswith("HTTP_404")
    assert e.cache.get(f"{U}/nope") is None  # failures are never cached


def test_500_then_200_is_retried_with_backoff(make_env):
    e = make_env({f"{U}/flaky": [resp(f"{U}/flaky", 500, "e"), resp(f"{U}/flaky", 503, "e"), resp(f"{U}/flaky", 200, BODY)]})
    r = e.fetch("/flaky")
    assert r.ok and e.http.count(f"{U}/flaky") == 3
    assert [s for s in e.clock.sleeps if s >= 1][:2] == [pytest.approx(1.0), pytest.approx(2.0)]
    assert [t for t, _ in e.errors()] == ["HTTP_500", "HTTP_503"]  # every failed attempt is logged


def test_only_transient_statuses_are_retried(make_env):
    for code in (403, 404, 410):
        e = make_env({f"{U}/a": resp(f"{U}/a", code, "e")})
        assert e.fetch("/a").crawl_status is CrawlStatus.HTTP_ERROR and e.http.count(f"{U}/a") == 1
    for code in (429, 500, 502, 503, 504):
        e2 = make_env({f"{U}/b": resp(f"{U}/b", code, "e")})
        r = e2.fetch("/b")
        assert r.status_code == code and e2.http.count(f"{U}/b") == 3, code  # 1 try + max_retries


def test_retry_after_is_honoured_and_capped(make_env):
    e = make_env({f"{U}/r": [resp(f"{U}/r", 429, "slow", headers={"Retry-After": "7"}), resp(f"{U}/r", 200, BODY)]})
    assert e.fetch("/r").ok
    assert any(abs(s - 7.0) < 1e-6 for s in e.clock.sleeps)
    e = make_env({f"{U}/r": [resp(f"{U}/r", 429, "slow", headers={"Retry-After": "9999"}), resp(f"{U}/r", 200, BODY)]})
    assert e.fetch("/r").ok
    assert max(e.clock.sleeps) <= CFG.retry_after_cap + 1e-6 and any(abs(s - 30.0) < 1e-6 for s in e.clock.sleeps)


def test_max_retries_zero_disables_retry(make_env):
    e = make_env({f"{U}/x": resp(f"{U}/x", 503, "e")}, cfg=replace(CFG, max_retries=0))
    e.fetch("/x")
    assert e.http.count(f"{U}/x") == 1


def test_timeout_maps_and_retries_then_gives_up(make_env):
    e = make_env({f"{U}/t": err(f"{U}/t", "TIMEOUT")})
    r = e.fetch("/t")
    assert (r.crawl_status, r.error_code, r.status_code) == (CrawlStatus.TIMEOUT, "TIMEOUT", None)
    assert e.http.count(f"{U}/t") == 3
    assert [t for t, _ in e.errors()].count("TIMEOUT") == 3  # every failed attempt is logged


def test_connection_error_maps_to_http_error(make_env):
    e = make_env({f"{U}/c": err(f"{U}/c", "CONNECTION_ERROR")})
    r = e.fetch("/c")
    assert (r.crawl_status, r.error_code) == (CrawlStatus.HTTP_ERROR, "CONNECTION_ERROR")


def test_ssl_error_maps_and_is_never_retried(make_env):
    e = make_env({f"{U}/s": err(f"{U}/s", "SSL_ERROR")})
    r = e.fetch("/s")
    assert r.crawl_status is CrawlStatus.SSL_ERROR and e.http.count(f"{U}/s") == 1


def test_unsupported_content_type_is_filtered(make_env):
    e = make_env({f"{U}/f.pdf": resp(f"{U}/f.pdf", 200, b"%PDF-1.4", ctype="application/pdf")})
    r = e.fetch("/f.pdf")
    assert (r.crawl_status, r.error_code, r.status_code) == (CrawlStatus.SKIPPED_FILTER, "UNSUPPORTED_CONTENT_TYPE", 200)
    assert e.cache.get(f"{U}/f.pdf") is None


def test_allowed_types_and_sniffing(make_env):
    e = make_env({f"{U}/x": resp(f"{U}/x", 200, BODY, ctype="application/xhtml+xml"),
                  f"{U}/t": resp(f"{U}/t", 200, "plain  text\n\n\nbody", ctype="text/plain"),
                  f"{U}/n": resp(f"{U}/n", 200, BODY, ctype=None),
                  f"{U}/b": resp(f"{U}/b", 200, b"\x00\x01binary", ctype=None)})
    assert e.fetch("/x").ok
    plain = e.fetch("/t")
    assert plain.ok and plain.text == "plain text\nbody" and plain.html is None
    assert e.fetch("/n").ok
    assert e.fetch("/b").crawl_status is CrawlStatus.SKIPPED_FILTER


def test_oversize_body_is_filtered_with_error(make_env):
    e = make_env({f"{U}/big": resp(f"{U}/big", 200, b"x" * 10, truncated=True)})
    r = e.fetch("/big")
    assert (r.crawl_status, r.error_code) == (CrawlStatus.SKIPPED_FILTER, "RESPONSE_TOO_LARGE")


def test_redirect_followed_and_recorded(make_env):
    e = make_env({f"{U}/old": resp(f"{U}/old", 301, "", headers={"Location": "/new"}),
                  f"{U}/new": resp(f"{U}/new", 200, BODY)})
    r = e.fetch("/old")
    assert r.ok and r.canonical_url == f"{U}/old" and r.final_url == f"{U}/new"
    assert r.redirect_chain == [f"{U}/old", f"{U}/new"]


def test_cross_domain_redirect_recorded_and_robots_checked_per_hop(make_env):
    e = make_env({f"{U}/go": resp(f"{U}/go", 302, "", headers={"Location": "https://b.test/landing"}),
                  "https://b.test/robots.txt": resp("https://b.test/robots.txt", 200, "User-agent: *\nDisallow: /landing\n", ctype="text/plain")})
    r = e.fetch("/go")
    assert r.crawl_status is CrawlStatus.BLOCKED_ROBOTS and "https://b.test/landing" not in e.http.calls
    assert "https://b.test/robots.txt" in e.http.calls
    e2 = make_env({f"{U}/go": resp(f"{U}/go", 302, "", headers={"Location": "https://b.test/landing"}),
                   "https://b.test/landing": resp("https://b.test/landing", 200, BODY)})
    r2 = e2.fetch("/go")
    assert r2.ok and r2.redirect_chain == [f"{U}/go", "https://b.test/landing"]


def test_too_many_redirects_and_loops(make_env):
    routes = {f"{U}/h{i}": resp(f"{U}/h{i}", 302, "", headers={"Location": f"/h{i + 1}"}) for i in range(10)}
    e = make_env(routes)
    r = e.fetch("/h0")
    assert (r.crawl_status, r.error_code) == (CrawlStatus.HTTP_ERROR, "TOO_MANY_REDIRECTS")
    e = make_env({f"{U}/l1": resp(f"{U}/l1", 302, "", headers={"Location": "/l2"}),
                  f"{U}/l2": resp(f"{U}/l2", 302, "", headers={"Location": "/l1"})})
    assert e.fetch("/l1").error_code == "REDIRECT_ERROR"
    e = make_env({f"{U}/nol": resp(f"{U}/nol", 302, "")})
    assert e.fetch("/nol").error_code == "REDIRECT_ERROR"


def test_private_host_is_blocked_unless_allowed(make_env):
    e = make_env({})
    r = e.svc.fetch("http://127.0.0.1:9/x", run_id=e.run_id)
    assert (r.crawl_status, r.error_code) == (CrawlStatus.SKIPPED_FILTER, "PRIVATE_HOST_BLOCKED") and e.http.calls == []
    e2 = make_env({"http://127.0.0.1:9/x": resp("http://127.0.0.1:9/x", 200, BODY)}, cfg=replace(CFG, allow_private_hosts=True))
    assert e2.svc.fetch("http://127.0.0.1:9/x", run_id=e2.run_id).ok
    e3 = make_env({f"{U}/r": resp(f"{U}/r", 302, "", headers={"Location": "http://169.254.169.254/latest"})})
    assert e3.fetch("/r").error_code == "PRIVATE_HOST_BLOCKED"


def test_invalid_url_is_skipped_without_page_row(make_env):
    e = make_env({})
    for bad in ("javascript:void(0)", "mailto:a@b.test", "", "http://"):
        r = e.svc.fetch(bad, run_id=e.run_id)
        assert r.crawl_status is CrawlStatus.SKIPPED_FILTER and r.error_code.startswith("URL_") and r.canonical_url is None
    assert count(e.conn, "crawl_pages") == 0 and count(e.conn, "crawl_errors") == 4


def test_relative_url_with_base(make_env):
    e = make_env({f"{U}/about": resp(f"{U}/about", 200, BODY)})
    r = e.svc.fetch("/about#team", run_id=e.run_id, base_url=f"{U}/x/")
    assert r.ok and r.canonical_url == f"{U}/about"


# ------------------------------------------------------------------ cache / visited
def test_second_fetch_is_a_cache_hit_without_network(make_env):
    e = make_env({f"{U}/p": resp(f"{U}/p", 200, BODY)})
    first = e.fetch("/p")
    calls = list(e.http.calls)
    second = e.fetch("/p")
    assert e.http.calls == calls
    assert second.cache_hit and second.already_visited and second.fetch_method == "cache"
    assert second.crawl_status is CrawlStatus.CACHED and second.text == first.text and second.ok


def test_url_variants_share_one_cache_entry(make_env):
    e = make_env({f"{U}/p": resp(f"{U}/p", 200, BODY)})
    e.fetch("/p")
    before = e.http.count(f"{U}/p")
    for variant in ("/p/", "/p#frag", "//p", "/p?utm_source=x"):
        assert e.fetch(variant).cache_hit
    assert e.http.count(f"{U}/p") == before


def test_new_run_reuses_disk_cache_and_writes_cached_row(make_env):
    e = make_env({f"{U}/p": resp(f"{U}/p", 200, BODY)})
    e.fetch("/p")
    run2 = seed_run(e.conn, root_code="EY")
    r = e.svc.fetch(f"{U}/p", run_id=run2)
    assert r.cache_hit and r.crawl_status is CrawlStatus.CACHED and e.http.count(f"{U}/p") == 1
    row = e.pages.get_page(run2, f"{U}/p")
    assert row.crawl_status is CrawlStatus.CACHED and row.fetch_method == "cache" and row.text_path


def test_cache_ttl_expiry_and_zero_ttl_refetches(make_env):
    e = make_env({f"{U}/p": resp(f"{U}/p", 200, BODY)}, cfg=replace(CFG, cache_ttl_seconds=100))
    e.fetch("/p")
    run2 = seed_run(e.conn, root_code="EY")
    e.clock.advance(50)
    assert e.svc.fetch(f"{U}/p", run_id=run2).cache_hit
    run3 = seed_run(e.conn, root_code="EZ")
    e.clock.advance(100)
    r = e.svc.fetch(f"{U}/p", run_id=run3)
    assert not r.cache_hit and e.http.count(f"{U}/p") == 2
    e0 = make_env({f"{U}/p": resp(f"{U}/p", 200, BODY)}, cfg=replace(CFG, cache_ttl_seconds=0))
    e0.fetch("/p")
    run = seed_run(e0.conn, root_code="E0")
    assert not e0.svc.fetch(f"{U}/p", run_id=run).cache_hit


def test_corrupt_cache_file_is_a_miss_and_refetches(make_env, tmp_path):
    e = make_env({f"{U}/p": resp(f"{U}/p", 200, BODY)})
    first = e.fetch("/p")
    row = e.pages.get_page_by_id(first.page_id)
    (tmp_path / "cache" / row.text_path).write_text("tampered", encoding="utf-8")
    assert e.svc.load_text(first.page_id) is None  # verified read refuses corrupt text
    again = e.fetch("/p")
    assert again.ok and not again.cache_hit and e.http.count(f"{U}/p") == 2
    assert again.text == first.text and e.svc.load_text(again.page_id) == first.text


def test_failed_pages_are_not_refetched_unless_requested(make_env):
    e = make_env({f"{U}/f": [resp(f"{U}/f", 404, "x", ctype="text/plain"), resp(f"{U}/f", 200, BODY)]})
    assert e.fetch("/f").status_code == 404
    again = e.fetch("/f")
    assert again.already_visited and again.crawl_status is CrawlStatus.HTTP_ERROR and e.http.count(f"{U}/f") == 1
    fixed = e.fetch("/f", refetch_failed=True)
    assert fixed.ok and e.pages.get_page(e.run_id, f"{U}/f").crawl_status is CrawlStatus.OK


# ------------------------------------------------------------------ budget
class StubBudget:
    def __init__(self, allow=True):
        self.allow, self.seen = allow, []

    def check_page(self, domain):
        self.seen.append(domain)
        return BudgetDecision(allow=self.allow, reason=None if self.allow else "DOMAIN_PAGE_LIMIT", detail="limit")


def test_budget_denial_skips_before_network(make_env):
    b = StubBudget(allow=False)
    e = make_env({f"{U}/p": resp(f"{U}/p", 200, BODY)}, budget=b)
    r = e.fetch("/p")
    assert (r.crawl_status, r.error_code) == (CrawlStatus.SKIPPED_LIMIT, "BUDGET_DOMAIN_PAGE_LIMIT") and e.http.calls == []
    assert b.seen == ["a.test"]


def test_cache_hits_do_not_consult_or_consume_budget(make_env):
    b = StubBudget(allow=True)
    e = make_env({f"{U}/p": resp(f"{U}/p", 200, BODY)}, budget=b)
    e.fetch("/p")
    e.fetch("/p")
    e.fetch("/p/")
    assert b.seen == ["a.test"]


def test_real_budget_guard_integration(make_env, tmp_path):
    from corpdisc.services.budget import BudgetGuard, BudgetLimits

    routes = {f"{U}/{i}": resp(f"{U}/{i}", 200, BODY) for i in range(5)}
    e = make_env(routes)
    guard = BudgetGuard(e.conn, run_id=e.run_id, limits=BudgetLimits(max_pages_per_domain=2))
    e.svc._budget = guard
    results = [e.fetch(f"/{i}") for i in range(4)]
    assert [r.crawl_status for r in results[:2]] == [CrawlStatus.OK, CrawlStatus.OK]
    assert all(r.crawl_status is CrawlStatus.SKIPPED_LIMIT and r.error_code == "BUDGET_DOMAIN_PAGE_LIMIT" for r in results[2:])
    assert e.pages.get_page(e.run_id, f"{U}/3").error.startswith("BUDGET_DOMAIN_PAGE_LIMIT")


# ------------------------------------------------------------------ playwright fallback (fake renderer)
RENDERED = "<html><body><h1>Northwind Holdings</h1><p>" + ("Rendered company listing content. " * 15) + "</p></body></html>"


def test_js_shell_triggers_render_in_auto_mode(make_env):
    r_ = FakeRenderer(html=RENDERED)
    e = make_env({f"{U}/spa": resp(f"{U}/spa", 200, SHELL)}, renderer=r_)
    r = e.fetch("/spa")
    assert r.ok and r.fetch_method == "playwright" and r_.calls == [f"{U}/spa"]
    assert "Rendered company listing" in r.text and r.html == RENDERED
    row = e.pages.get_page(e.run_id, f"{U}/spa")
    assert row.fetch_method == "playwright"
    assert e.cache.get(f"{U}/spa").meta.origin_method == "playwright"
    again = e.fetch("/spa")
    assert again.cache_hit and again.fetch_method == "cache" and again.origin_method == "playwright"


def test_normal_page_does_not_render_in_auto_mode(make_env):
    r_ = FakeRenderer(html=RENDERED)
    e = make_env({f"{U}/n": resp(f"{U}/n", 200, BODY)}, renderer=r_)
    assert e.fetch("/n").fetch_method == "requests" and r_.calls == []


def test_use_playwright_false_never_renders(make_env):
    r_ = FakeRenderer(html=RENDERED)
    e = make_env({f"{U}/spa": resp(f"{U}/spa", 200, SHELL)}, cfg=replace(CFG, use_playwright="false"), renderer=r_)
    res = e.fetch("/spa")
    assert res.ok and res.fetch_method == "requests" and r_.calls == []


def test_use_playwright_true_renders_every_html_page(make_env):
    r_ = FakeRenderer(html=RENDERED)
    e = make_env({f"{U}/n": resp(f"{U}/n", 200, BODY)}, cfg=replace(CFG, use_playwright="true"), renderer=r_)
    assert e.fetch("/n").fetch_method == "playwright" and len(r_.calls) == 1


def test_render_mode_argument_overrides_config(make_env):
    r_ = FakeRenderer(html=RENDERED)
    e = make_env({f"{U}/n": resp(f"{U}/n", 200, BODY)}, renderer=r_)
    assert e.fetch("/n", render="never").fetch_method == "requests"
    e2 = make_env({f"{U}/n": resp(f"{U}/n", 200, BODY)}, renderer=r_)
    assert e2.fetch("/n", render="always").fetch_method == "playwright"


def test_render_never_happens_when_robots_blocks(make_env):
    r_ = FakeRenderer(html=RENDERED)
    e = make_env({f"{U}/robots.txt": resp(f"{U}/robots.txt", 200, "User-agent: *\nDisallow: /spa\n", ctype="text/plain"),
                  f"{U}/spa": resp(f"{U}/spa", 200, SHELL)}, renderer=r_)
    assert e.fetch("/spa").crawl_status is CrawlStatus.BLOCKED_ROBOTS and r_.calls == []


def test_renderer_failure_keeps_requests_result_and_logs_once(make_env):
    for exc, code in ((RendererUnavailable("no chromium"), "RENDERER_UNAVAILABLE"), (RendererError("boom"), "RENDERER_ERROR")):
        r_ = FakeRenderer(error=exc)
        e = make_env({f"{U}/s1": resp(f"{U}/s1", 200, SHELL), f"{U}/s2": resp(f"{U}/s2", 200, SHELL)}, renderer=r_)
        a, b = e.fetch("/s1"), e.fetch("/s2")
        assert a.ok and a.fetch_method == "requests" and b.ok
        n = [t for t, _ in e.errors()].count(code)
        assert n == (1 if code == "RENDERER_UNAVAILABLE" else 2)


def test_no_renderer_configured_is_treated_as_unavailable(make_env):
    e = make_env({f"{U}/spa": resp(f"{U}/spa", 200, SHELL)})
    r = e.fetch("/spa")
    assert r.ok and r.fetch_method == "requests"
    assert [t for t, _ in e.errors()] == ["RENDERER_UNAVAILABLE"]


def test_renderer_result_with_less_text_is_ignored(make_env):
    r_ = FakeRenderer(html="<p>x</p>")
    e = make_env({f"{U}/short": resp(f"{U}/short", 200, "<html><body><p>" + "w " * 60 + "</p></body></html>")}, renderer=r_)
    res = e.fetch("/short")
    assert res.fetch_method == "requests" and len(r_.calls) == 1


# ------------------------------------------------------------------ storage guarantees
def test_no_page_body_is_stored_in_sqlite(make_env):
    big = "<html><body>" + ("<p>Northwind portfolio company listing paragraph.</p>" * 200) + "</body></html>"
    e = make_env({f"{U}/big": resp(f"{U}/big", 200, big)})
    assert e.fetch("/big").ok
    tables = [r[0] for r in e.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    for t in tables:
        for row in e.conn.execute(f"SELECT * FROM {t}"):  # fixed table names from sqlite_master
            for v in tuple(row):
                assert not (isinstance(v, str) and len(v) > 2000), (t, len(v))
    assert e.conn.execute("SELECT robots_txt FROM domain_stats WHERE domain='a.test'").fetchone()[0] is not None


def test_make_page_text_loader_reads_verified_text(make_env):
    e = make_env({f"{U}/p": resp(f"{U}/p", 200, BODY)})
    r = e.fetch("/p")
    load = make_page_text_loader(e.pages, e.cache)
    assert load(r.page_id) == r.text
    assert load(99999) is None


def test_fetch_never_raises_on_internal_failure(make_env):
    e = make_env({f"{U}/p": resp(f"{U}/p", 200, BODY)})

    def boom(*a, **k):
        raise RuntimeError("db exploded")

    e.pages.upsert_page = boom  # type: ignore[method-assign]
    r = e.fetch("/p")
    assert r.crawl_status is CrawlStatus.HTTP_ERROR and r.error_code == "INTERNAL_ERROR" and not r.ok


def test_user_agent_header_is_sent(make_env):
    seen = {}

    class Spy(FakeHttpClient):
        def get(self, url, *, headers, timeout, max_bytes):
            seen[url] = dict(headers)
            return super().get(url, headers=headers, timeout=timeout, max_bytes=max_bytes)

    import corpdisc.services.fetch as fetch_mod  # noqa: F401

    e = make_env({f"{U}/p": resp(f"{U}/p", 200, BODY)}, cfg=replace(CFG, user_agent="TestBot/9 (+x)"))
    e.svc._http = Spy({f"{U}/p": resp(f"{U}/p", 200, BODY)})
    e.fetch("/p")
    assert seen[f"{U}/p"]["User-Agent"] == "TestBot/9 (+x)"
