from __future__ import annotations

import sqlite3
from datetime import timedelta

import pytest

from corpdisc.db.repos.pages import PageRepo, PageUpsert
from corpdisc.models.domain import CrawlStatus
from tests.helpers.clock import FakeClock
from tests.helpers.db import count, make_db, seed_run


@pytest.fixture
def env(tmp_path):
    conn = make_db(tmp_path)
    run_id = seed_run(conn)
    clock = FakeClock()
    yield PageRepo(conn, clock), conn, run_id, clock
    conn.close()


def _up(run_id, url="https://a.test/x", status=CrawlStatus.OK, **kw):
    return PageUpsert(run_id=run_id, url=url, canonical_url=url, domain="a.test", crawl_status=status, **kw)


def test_upsert_is_idempotent_on_run_and_canonical_url(env):
    repo, conn, run_id, _ = env
    first = repo.upsert_page(_up(run_id, http_status=200, fetch_method="requests"))
    second = repo.upsert_page(_up(run_id, http_status=500, status=CrawlStatus.HTTP_ERROR, error="HTTP_500: x"))
    assert first == second and count(conn, "crawl_pages") == 1
    row = repo.get_page(run_id, "https://a.test/x")
    assert row.http_status == 500 and row.crawl_status is CrawlStatus.HTTP_ERROR and row.error == "HTTP_500: x"
    assert repo.get_page_by_id(first).canonical_url == "https://a.test/x"
    assert repo.get_page(run_id, "https://a.test/missing") is None


def test_robots_allowed_round_trips_as_bool(env):
    repo, _, run_id, _ = env
    pid = repo.upsert_page(_up(run_id, robots_allowed=False, status=CrawlStatus.BLOCKED_ROBOTS))
    assert repo.get_page_by_id(pid).robots_allowed is False


def test_budget_counts_exclude_blocked_skipped_and_cached(env):
    repo, _, run_id, _ = env
    for i, st in enumerate([CrawlStatus.OK, CrawlStatus.HTTP_ERROR, CrawlStatus.TIMEOUT, CrawlStatus.BLOCKED_ROBOTS,
                            CrawlStatus.SKIPPED_LIMIT, CrawlStatus.SKIPPED_FILTER, CrawlStatus.CACHED]):
        repo.upsert_page(_up(run_id, url=f"https://a.test/{i}", status=st))
    repo.upsert_page(PageUpsert(run_id=run_id, url="https://b.test/", canonical_url="https://b.test/",
                                domain="b.test", crawl_status=CrawlStatus.OK))
    assert repo.count_budgeted_pages(run_id) == 4
    assert repo.count_budgeted_pages(run_id, "a.test") == 3
    assert repo.count_budgeted_pages(run_id, "b.test") == 1


def test_domain_stats_robots_and_requests(env):
    repo, _, _, clock = env
    assert repo.get_domain_stats("a.test") is None
    repo.record_request("a.test", clock.now(), error=False)
    repo.record_request("a.test", clock.now(), error=True)
    repo.save_robots("a.test", "User-agent: *\nDisallow: /x", clock.now(), 3.0)
    row = repo.get_domain_stats("a.test")
    assert (row.pages_fetched, row.error_count, row.crawl_delay_s) == (1, 1, 3.0)
    assert row.robots_txt.startswith("User-agent") and row.robots_fetched_at == clock.now()
    clock.advance(10)
    repo.save_robots("a.test", "", clock.now(), None)
    row = repo.get_domain_stats("a.test")
    assert row.robots_txt == "" and row.crawl_delay_s is None and row.pages_fetched == 1


def test_counts_and_errors(env):
    repo, conn, run_id, _ = env
    pid = repo.upsert_page(_up(run_id))
    repo.set_counts(pid, pages_found=4, entities_found=2)
    row = repo.get_page_by_id(pid)
    assert (row.pages_found, row.entities_found) == (4, 2)
    repo.add_crawl_error(run_id, "FETCH", "TIMEOUT", "m" * 900, url="https://a.test/x", page_id=pid)
    stored = conn.execute("SELECT message, stage FROM crawl_errors").fetchone()
    assert len(stored["message"]) == 500 and stored["stage"] == "FETCH"


def test_foreign_key_violations_surface(env):
    repo, _, _, _ = env
    with pytest.raises(sqlite3.IntegrityError):
        repo.upsert_page(_up(999))
    with pytest.raises(sqlite3.IntegrityError):
        repo.add_crawl_error(999, "FETCH", "X", "m")


def test_long_error_truncated_and_timestamps_utc(env):
    repo, _, run_id, clock = env
    pid = repo.upsert_page(_up(run_id, error="e" * 900))
    row = repo.get_page_by_id(pid)
    assert len(row.error) == 500 and row.crawled_at == clock.now()
    assert clock.now() + timedelta(0) == row.crawled_at
