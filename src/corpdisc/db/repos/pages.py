"""Repository for the fetch-stage tables: crawl_pages, domain_stats, crawl_errors.

This is the only module that writes those tables (ARCHITECTURE.md 26.2, decision D18).
Every statement is parameterized. Page bodies never go in SQLite: only metadata and the
relative paths of the on-disk cache files.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from corpdisc.clock import Clock, iso_utc, parse_iso_utc
from corpdisc.models.domain import CrawlStatus

# Statuses that never reached (or deliberately skipped) the network, so they do not use page budget.
UNBUDGETED_STATUSES: Final[tuple[str, ...]] = (
    CrawlStatus.BLOCKED_ROBOTS.value,
    CrawlStatus.SKIPPED_LIMIT.value,
    CrawlStatus.SKIPPED_FILTER.value,
    CrawlStatus.CACHED.value,
)
MAX_ERROR_CHARS: Final = 500


@dataclass(frozen=True)
class PageRow:
    page_id: int
    run_id: int
    url: str
    canonical_url: str
    final_url: str | None
    domain: str
    entity_id: int | None
    parent_entity_id: int | None
    page_type: str | None
    http_status: int | None
    fetch_method: str | None
    content_hash: str | None
    text_path: str | None
    html_path: str | None
    robots_allowed: bool | None
    pages_found: int
    entities_found: int
    crawl_status: CrawlStatus
    error: str | None
    crawled_at: datetime


@dataclass(frozen=True)
class PageUpsert:
    run_id: int
    url: str
    canonical_url: str
    domain: str
    crawl_status: CrawlStatus
    final_url: str | None = None
    entity_id: int | None = None
    parent_entity_id: int | None = None
    page_type: str | None = None
    http_status: int | None = None
    fetch_method: str | None = None
    content_hash: str | None = None
    text_path: str | None = None
    html_path: str | None = None
    robots_allowed: bool | None = None
    error: str | None = None


@dataclass(frozen=True)
class DomainStatsRow:
    domain: str
    robots_txt: str | None
    robots_fetched_at: datetime | None
    crawl_delay_s: float | None
    pages_fetched: int
    error_count: int
    last_request_at: datetime | None


_PAGE_COLS: Final = (
    "page_id, run_id, url, canonical_url, final_url, domain, entity_id, parent_entity_id, page_type, "
    "http_status, fetch_method, content_hash, text_path, html_path, robots_allowed, pages_found, "
    "entities_found, crawl_status, error, crawled_at"
)


def _page_from_row(r: sqlite3.Row) -> PageRow:
    return PageRow(
        page_id=r["page_id"], run_id=r["run_id"], url=r["url"], canonical_url=r["canonical_url"],
        final_url=r["final_url"], domain=r["domain"], entity_id=r["entity_id"],
        parent_entity_id=r["parent_entity_id"], page_type=r["page_type"], http_status=r["http_status"],
        fetch_method=r["fetch_method"], content_hash=r["content_hash"], text_path=r["text_path"],
        html_path=r["html_path"],
        robots_allowed=None if r["robots_allowed"] is None else bool(r["robots_allowed"]),
        pages_found=r["pages_found"] or 0, entities_found=r["entities_found"] or 0,
        crawl_status=CrawlStatus(r["crawl_status"]), error=r["error"],
        crawled_at=parse_iso_utc(r["crawled_at"]),
    )  # fmt: skip


class PageRepo:
    def __init__(self, conn: sqlite3.Connection, clock: Clock) -> None:
        self._conn = conn
        self._clock = clock

    # ------------------------------------------------------------------ pages
    def get_page(self, run_id: int, canonical_url: str) -> PageRow | None:
        row = self._conn.execute(
            f"SELECT {_PAGE_COLS} FROM crawl_pages WHERE run_id = ? AND canonical_url = ?",  # noqa: S608
            (run_id, canonical_url),
        ).fetchone()
        return _page_from_row(row) if row else None

    def get_page_by_id(self, page_id: int) -> PageRow | None:
        row = self._conn.execute(
            f"SELECT {_PAGE_COLS} FROM crawl_pages WHERE page_id = ?",  # noqa: S608
            (page_id,),
        ).fetchone()
        return _page_from_row(row) if row else None

    def upsert_page(self, row: PageUpsert) -> int:
        """Insert or update the row for (run_id, canonical_url); returns its page_id."""
        error = row.error[:MAX_ERROR_CHARS] if row.error else None
        with self._conn:
            self._conn.execute(
                "INSERT INTO crawl_pages (run_id, url, canonical_url, final_url, domain, entity_id, "
                "parent_entity_id, page_type, http_status, fetch_method, content_hash, text_path, html_path, "
                "robots_allowed, crawl_status, error, crawled_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(run_id, canonical_url) DO UPDATE SET "
                "url=excluded.url, final_url=excluded.final_url, domain=excluded.domain, "
                "entity_id=excluded.entity_id, parent_entity_id=excluded.parent_entity_id, "
                "page_type=excluded.page_type, http_status=excluded.http_status, "
                "fetch_method=excluded.fetch_method, content_hash=excluded.content_hash, "
                "text_path=excluded.text_path, html_path=excluded.html_path, "
                "robots_allowed=excluded.robots_allowed, crawl_status=excluded.crawl_status, "
                "error=excluded.error, crawled_at=excluded.crawled_at",
                (
                    row.run_id, row.url, row.canonical_url, row.final_url, row.domain, row.entity_id,
                    row.parent_entity_id, row.page_type, row.http_status, row.fetch_method,
                    row.content_hash, row.text_path, row.html_path,
                    None if row.robots_allowed is None else int(row.robots_allowed),
                    row.crawl_status.value, error, iso_utc(self._clock.now()),
                ),
            )  # fmt: skip
        found = self.get_page(row.run_id, row.canonical_url)
        assert found is not None
        return found.page_id

    def set_counts(self, page_id: int, *, pages_found: int | None = None, entities_found: int | None = None) -> None:
        with self._conn:
            if pages_found is not None:
                self._conn.execute("UPDATE crawl_pages SET pages_found = ? WHERE page_id = ?", (pages_found, page_id))
            if entities_found is not None:
                self._conn.execute(
                    "UPDATE crawl_pages SET entities_found = ? WHERE page_id = ?", (entities_found, page_id)
                )

    def count_budgeted_pages(self, run_id: int, domain: str | None = None) -> int:
        marks = ",".join("?" for _ in UNBUDGETED_STATUSES)
        sql = f"SELECT COUNT(*) FROM crawl_pages WHERE run_id = ? AND crawl_status NOT IN ({marks})"  # noqa: S608
        params: list[object] = [run_id, *UNBUDGETED_STATUSES]
        if domain is not None:
            sql += " AND domain = ?"
            params.append(domain)
        return int(self._conn.execute(sql, params).fetchone()[0])

    # --------------------------------------------------------------- domains
    def get_domain_stats(self, domain: str) -> DomainStatsRow | None:
        r = self._conn.execute(
            "SELECT domain, robots_txt, robots_fetched_at, crawl_delay_s, pages_fetched, error_count, "
            "last_request_at FROM domain_stats WHERE domain = ?",
            (domain,),
        ).fetchone()
        if r is None:
            return None
        return DomainStatsRow(
            domain=r["domain"], robots_txt=r["robots_txt"],
            robots_fetched_at=parse_iso_utc(r["robots_fetched_at"]) if r["robots_fetched_at"] else None,
            crawl_delay_s=r["crawl_delay_s"], pages_fetched=r["pages_fetched"], error_count=r["error_count"],
            last_request_at=parse_iso_utc(r["last_request_at"]) if r["last_request_at"] else None,
        )  # fmt: skip

    def save_robots(self, domain: str, robots_txt: str, fetched_at: datetime, crawl_delay_s: float | None) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT INTO domain_stats (domain, robots_txt, robots_fetched_at, crawl_delay_s) VALUES (?,?,?,?) "
                "ON CONFLICT(domain) DO UPDATE SET robots_txt=excluded.robots_txt, "
                "robots_fetched_at=excluded.robots_fetched_at, crawl_delay_s=excluded.crawl_delay_s",
                (domain, robots_txt, iso_utc(fetched_at), crawl_delay_s),
            )

    def record_request(self, domain: str, at: datetime, *, error: bool) -> None:
        """Count one network request to ``domain`` (pages_fetched on success, error_count on failure)."""
        ok, bad = (0, 1) if error else (1, 0)
        with self._conn:
            self._conn.execute(
                "INSERT INTO domain_stats (domain, pages_fetched, error_count, last_request_at) VALUES (?,?,?,?) "
                "ON CONFLICT(domain) DO UPDATE SET pages_fetched = pages_fetched + excluded.pages_fetched, "
                "error_count = error_count + excluded.error_count, last_request_at = excluded.last_request_at",
                (domain, ok, bad, iso_utc(at)),
            )

    # ---------------------------------------------------------------- errors
    def add_crawl_error(
        self,
        run_id: int,
        stage: str,
        error_type: str,
        message: str,
        *,
        url: str | None = None,
        page_id: int | None = None,
        entity_id: int | None = None,
    ) -> int:
        with self._conn:
            cur = self._conn.execute(
                "INSERT INTO crawl_errors (run_id, page_id, entity_id, url, stage, error_type, message, occurred_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (run_id, page_id, entity_id, url, stage, error_type, message[:MAX_ERROR_CHARS] or "-",
                 iso_utc(self._clock.now())),
            )  # fmt: skip
        return int(cur.lastrowid)
