"""FetchService: a deterministic, polite, cached HTTP fetcher (ARCHITECTURE.md 10.1).

Pipeline per URL: canonicalize -> SSRF guard -> visited lookup -> page cache -> budget ->
robots -> throttle -> requests GET (manual redirects, each hop re-checked) -> classify ->
optional Playwright render -> store in the page cache -> crawl_pages / domain_stats /
crawl_errors rows. ``fetch`` never raises; every outcome is a structured ``FetchResult``.
Agents never reach this class: tools go through the ToolBroker (M2).
"""

from __future__ import annotations

import codecs
import ipaddress
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Final, Literal, Protocol

import requests
from bs4 import BeautifulSoup, NavigableString, Tag
from pydantic import BaseModel, ConfigDict

from corpdisc.clock import Clock, Sleeper, SystemClock
from corpdisc.config import DEFAULT_TRACKING_QUERY_PARAMS, Settings
from corpdisc.contracts.common import BudgetDecision
from corpdisc.db.repos.pages import PageRepo, PageRow, PageUpsert
from corpdisc.logging_setup import get_logger
from corpdisc.models.domain import CrawlStatus, PageType
from corpdisc.services.pagecache import PageCache
from corpdisc.services.renderer import Renderer, RendererError, RendererUnavailable
from corpdisc.services.robots import RobotsFetcher, RobotsFetchResult, RobotsService
from corpdisc.services.urls import canonicalize_url

if TYPE_CHECKING:  # pragma: no cover
    from tldextract import TLDExtract

log = get_logger("services.fetch")

ALLOWED_HTML_TYPES: Final = frozenset({"text/html", "application/xhtml+xml"})
ALLOWED_CONTENT_TYPES: Final = ALLOWED_HTML_TYPES | {"text/plain"}
RESPONSE_HEADER_ALLOWLIST: Final = (
    "content-type", "content-length", "content-language", "last-modified", "etag", "cache-control",
    "expires", "date", "retry-after", "location",
)  # fmt: skip
REDIRECT_STATUSES: Final = frozenset({301, 302, 303, 307, 308})
RETRYABLE_STATUSES: Final = frozenset({429})  # plus every 5xx, see _is_retryable_status
TRANSIENT_ERRORS: Final = frozenset({"TIMEOUT", "CONNECTION_ERROR"})
MAX_DELAY_CAP_SECONDS: Final = 600.0

_SPA_MARKER_RE = re.compile(
    r"""(?ix)(id\s*=\s*["'](?:root|app|__next|__nuxt|___gatsby)["']|data-reactroot|ng-app|ng-version|
    window\.__INITIAL_STATE__|<app-root)"""
)
_NOSCRIPT_JS_RE = re.compile(r"(?is)<noscript[^>]*>[^<]*(enable|requires?|needs?)[^<]*javascript")
_BLOCK_TAGS: Final = frozenset(
    "address article aside blockquote body br dd details div dl dt fieldset figcaption figure footer form "
    "h1 h2 h3 h4 h5 h6 header hr li main nav ol p pre section summary table tbody td tfoot th thead tr ul".split()
)
_DROP_TAGS: Final = ("script", "style", "noscript", "template", "head", "svg", "iframe")
_WS_RE = re.compile(r"[ \t\r\f\v\u00a0]+")


# ---------------------------------------------------------------- text extraction
def html_to_text(html: str) -> str:
    """Cleaned, stable text used for quote verification (stdlib html.parser, no JS execution).

    Script/style/noscript/template/head are dropped; block elements become line breaks and
    inline elements join without inserted spaces, so visible sentences stay contiguous.
    """
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup.find_all(_DROP_TAGS):
        tag.decompose()
    for tag in soup.find_all(True):
        if isinstance(tag, Tag) and tag.name in _BLOCK_TAGS:
            tag.insert_before(NavigableString("\n"))
            tag.insert_after(NavigableString("\n"))
    return _normalize_lines(soup.get_text(""))


def plain_to_text(raw: str) -> str:
    return _normalize_lines(raw)


def _normalize_lines(text: str) -> str:
    lines = [_WS_RE.sub(" ", ln).strip() for ln in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    return "\n".join(ln for ln in lines if ln)


def js_shell_reason(html: str, text: str, min_text_chars: int) -> str | None:
    """Deterministic JS-shell detection. Returns the reason, or None for a normal page."""
    text_len = len(text.strip())
    if text_len == 0:
        return "EMPTY_BODY"
    if text_len < min_text_chars:
        return "LITTLE_TEXT"
    if text_len < 4 * min_text_chars and (_SPA_MARKER_RE.search(html) or _NOSCRIPT_JS_RE.search(html)):
        return "JS_SHELL_MARKER"
    return None


def looks_like_js_shell(html: str, text: str, min_text_chars: int = 200) -> bool:
    return js_shell_reason(html, text, min_text_chars) is not None


# ---------------------------------------------------------------- SSRF guard (P11)
def is_private_host(host: str | None) -> bool:
    """True for loopback/private/link-local/reserved IP literals and internal-only host names.

    DNS resolution is not performed, so DNS rebinding is a documented limitation.
    """
    if not host:
        return True
    h = host.strip("[]").lower()
    try:
        ip = ipaddress.ip_address(h.split("%")[0])
        return not ip.is_global
    except ValueError:
        pass
    return h == "localhost" or h.endswith((".localhost", ".local", ".internal", ".localdomain"))


# ---------------------------------------------------------------- HTTP layer
@dataclass(frozen=True)
class RawResponse:
    url: str
    status_code: int | None
    headers: dict[str, str]  # lowercase keys
    body: bytes = b""
    truncated: bool = False
    error_code: str | None = None  # TIMEOUT | SSL_ERROR | CONNECTION_ERROR
    error: str | None = None


class HttpClient(Protocol):
    def get(self, url: str, *, headers: Mapping[str, str], timeout: float, max_bytes: int) -> RawResponse: ...


class RequestsHttpClient:
    """requests.Session wrapper: no redirects followed, bounded streaming read, never raises."""

    def __init__(self, *, verify_ssl: bool = True, trust_env: bool = True, clock: Clock | None = None) -> None:
        self._session = requests.Session()
        self._session.trust_env = trust_env
        self._verify = verify_ssl
        self._clock = clock or SystemClock()

    def close(self) -> None:
        self._session.close()

    def get(self, url: str, *, headers: Mapping[str, str], timeout: float, max_bytes: int) -> RawResponse:
        started = self._clock.monotonic()
        try:
            resp = self._session.get(
                url, headers=dict(headers), timeout=timeout, stream=True, allow_redirects=False, verify=self._verify
            )
        except requests.exceptions.SSLError as exc:
            return RawResponse(url, None, {}, error_code="SSL_ERROR", error=type(exc).__name__)
        except requests.exceptions.Timeout as exc:
            return RawResponse(url, None, {}, error_code="TIMEOUT", error=type(exc).__name__)
        except requests.exceptions.RequestException as exc:
            return RawResponse(url, None, {}, error_code="CONNECTION_ERROR", error=type(exc).__name__)
        except Exception as exc:  # noqa: BLE001 - untrusted network input must not crash the crawl
            return RawResponse(url, None, {}, error_code="CONNECTION_ERROR", error=type(exc).__name__)
        resp_headers = {k.lower(): v for k, v in resp.headers.items()}
        try:
            declared = resp_headers.get("content-length", "")
            if declared.isdigit() and int(declared) > max_bytes:
                return RawResponse(url, resp.status_code, resp_headers, truncated=True)
            chunks: list[bytes] = []
            size = 0
            truncated = False
            for chunk in resp.iter_content(chunk_size=16384):
                if not chunk:
                    continue
                size += len(chunk)  # decoded bytes: bounds compression bombs
                if size > max_bytes:
                    truncated = True
                    break
                chunks.append(chunk)
                if self._clock.monotonic() - started > timeout:
                    return RawResponse(url, None, {}, error_code="TIMEOUT", error="total read deadline exceeded")
            return RawResponse(url, resp.status_code, resp_headers, b"".join(chunks), truncated)
        except requests.exceptions.SSLError as exc:
            return RawResponse(url, None, {}, error_code="SSL_ERROR", error=type(exc).__name__)
        except requests.exceptions.Timeout as exc:
            return RawResponse(url, None, {}, error_code="TIMEOUT", error=type(exc).__name__)
        except Exception as exc:  # noqa: BLE001
            return RawResponse(url, None, {}, error_code="CONNECTION_ERROR", error=type(exc).__name__)
        finally:
            resp.close()


class Throttle:
    """Per-domain politeness delay: at least max(default, crawl-delay) between requests."""

    def __init__(self, clock: Clock, sleeper: Sleeper, default_delay: float,
                 max_delay: float = MAX_DELAY_CAP_SECONDS) -> None:
        self._clock = clock
        self._sleeper = sleeper
        self._default = default_delay
        self._max = max_delay
        self._last: dict[str, float] = {}

    def wait(self, domain: str, crawl_delay: float | None = None) -> float:
        required = min(max(self._default, crawl_delay or 0.0), self._max)
        last = self._last.get(domain)
        if last is None or required <= 0:
            return 0.0
        remaining = required - (self._clock.monotonic() - last)
        if remaining <= 0:
            return 0.0
        self._sleeper.sleep(remaining)
        return remaining

    def mark(self, domain: str) -> None:
        self._last[domain] = self._clock.monotonic()


def make_robots_fetcher(http: HttpClient, throttle: Throttle, *, user_agent: str, timeout: float,
                        max_redirects: int = 3) -> RobotsFetcher:
    """Fetch robots.txt through the same HTTP layer and throttle. Same-site redirects only."""

    def fetch(robots_url: str) -> RobotsFetchResult:
        current = robots_url
        for _ in range(max_redirects + 1):
            parsed = canonicalize_url(current)
            if not parsed.ok or parsed.domain is None:
                return RobotsFetchResult(None, None, "invalid robots url")
            throttle.wait(parsed.domain)
            resp = http.get(current, headers={"User-Agent": user_agent, "Accept": "text/plain,*/*;q=0.5"},
                            timeout=timeout, max_bytes=600 * 1024)
            throttle.mark(parsed.domain)
            if resp.error_code:
                return RobotsFetchResult(None, None, resp.error_code)
            status = resp.status_code
            if status in REDIRECT_STATUSES and resp.headers.get("location"):
                nxt = canonicalize_url(resp.headers["location"], current)
                if not nxt.ok or nxt.registrable_domain != parsed.registrable_domain:
                    return RobotsFetchResult(None, None, "robots redirect leaves the site")
                current = nxt.canonical or current
                continue
            if status is not None and 200 <= status < 300:
                return RobotsFetchResult(status, resp.body.decode("utf-8", errors="replace"))
            return RobotsFetchResult(status, None, f"HTTP {status}")
        return RobotsFetchResult(None, None, "too many robots redirects")

    return fetch


# ---------------------------------------------------------------- configuration and results
@dataclass(frozen=True)
class FetchConfig:
    timeout: float = 20.0
    max_retries: int = 2
    request_delay: float = 2.0
    user_agent: str = "CorpDiscoveryBot/0.1 (+contact-url-or-email)"
    respect_robots: bool = True
    allow_insecure_ssl: bool = False
    use_playwright: Literal["auto", "true", "false"] = "auto"
    max_response_bytes: int = 2_000_000
    allow_private_hosts: bool = False
    max_redirects: int = 5
    cache_ttl_seconds: float = 7 * 86400.0
    playwright_timeout: float = 30.0
    playwright_min_text_chars: int = 200
    robots_failure_cooldown: float = 600.0
    tracking_params: tuple[str, ...] = tuple(DEFAULT_TRACKING_QUERY_PARAMS)
    backoff_base: float = 1.0
    backoff_max: float = 30.0
    retry_after_cap: float = 30.0

    @classmethod
    def from_settings(cls, s: Settings) -> FetchConfig:
        return cls(
            timeout=s.request_timeout, max_retries=s.max_retries, request_delay=s.request_delay_seconds,
            user_agent=s.user_agent, respect_robots=s.respect_robots, allow_insecure_ssl=s.allow_insecure_ssl,
            use_playwright=s.use_playwright, max_response_bytes=s.max_response_bytes,
            allow_private_hosts=s.allow_private_hosts, max_redirects=s.fetch_max_redirects,
            cache_ttl_seconds=s.page_cache_ttl_seconds, playwright_timeout=s.playwright_timeout_seconds,
            playwright_min_text_chars=s.playwright_min_text_chars,
            robots_failure_cooldown=s.robots_failure_cooldown_seconds,
            tracking_params=tuple(s.tracking_query_params),
        )  # fmt: skip


class FetchResult(BaseModel):
    """Deterministic fetch outcome. Bodies live in memory only; SQLite keeps metadata."""

    model_config = ConfigDict(frozen=True)

    requested_url: str
    canonical_url: str | None = None
    final_url: str | None = None
    status_code: int | None = None
    content_type: str | None = None
    retrieved_at: datetime
    response_headers: dict[str, str] = {}
    text: str | None = None
    html: str | None = None
    fetch_method: Literal["requests", "playwright", "cache"] | None = None
    origin_method: str | None = None
    cache_hit: bool = False
    already_visited: bool = False
    crawl_status: CrawlStatus
    error_code: str | None = None
    error: str | None = None
    page_id: int | None = None
    content_hash: str | None = None
    robots_allowed: bool | None = None
    redirect_chain: list[str] = []

    @property
    def ok(self) -> bool:
        return self.crawl_status in (CrawlStatus.OK, CrawlStatus.CACHED) and self.text is not None


class PageBudget(Protocol):
    def check_page(self, domain: str) -> BudgetDecision: ...


RenderMode = Literal["auto", "always", "never"]


def _mime(content_type: str | None) -> str | None:
    if not content_type:
        return None
    return content_type.split(";", 1)[0].strip().lower() or None


def _charset(content_type: str | None) -> str:
    if content_type:
        m = re.search(r"charset\s*=\s*[\"']?([\w\-:.]+)", content_type, re.I)
        if m:
            try:
                return codecs.lookup(m.group(1)).name
            except LookupError:
                pass
    return "utf-8"


def _is_retryable_status(status: int | None) -> bool:
    return status is not None and (status in RETRYABLE_STATUSES or 500 <= status <= 599)


def _retry_after_seconds(value: str | None) -> float | None:
    if value and value.strip().isdigit():
        return float(value.strip())
    return None


@dataclass
class _Ctx:
    run_id: int
    entity_id: int | None
    parent_entity_id: int | None
    page_type: PageType | None
    requested_url: str
    canonical: str = ""
    domain: str = ""
    chain: list[str] = field(default_factory=list)


class FetchService:
    def __init__(
        self,
        *,
        config: FetchConfig,
        pages: PageRepo,
        cache: PageCache,
        robots: RobotsService,
        http: HttpClient,
        throttle: Throttle,
        clock: Clock,
        sleeper: Sleeper,
        budget: PageBudget | None = None,
        renderer: Renderer | None = None,
        extractor: TLDExtract | None = None,
    ) -> None:
        self._cfg = config
        self._pages = pages
        self._cache = cache
        self._robots = robots
        self._http = http
        self._throttle = throttle
        self._clock = clock
        self._sleeper = sleeper
        self._budget = budget
        self._renderer = renderer
        self._extractor = extractor
        self._renderer_warned = False

    def close(self) -> None:
        if self._renderer is not None:
            self._renderer.close()

    # ------------------------------------------------------------------ public API
    def fetch(
        self,
        url: str,
        *,
        run_id: int,
        entity_id: int | None = None,
        parent_entity_id: int | None = None,
        page_type: PageType | None = None,
        base_url: str | None = None,
        render: RenderMode | None = None,
        refetch_failed: bool = False,
    ) -> FetchResult:
        ctx = _Ctx(run_id, entity_id, parent_entity_id, page_type, url if isinstance(url, str) else "")
        try:
            return self._fetch(ctx, base_url, render, refetch_failed)
        except Exception as exc:  # noqa: BLE001 - fetch must never raise
            log.exception("unexpected fetch failure for %s", ctx.canonical or ctx.requested_url)
            return FetchResult(
                requested_url=ctx.requested_url, canonical_url=ctx.canonical or None,
                retrieved_at=self._clock.now(), crawl_status=CrawlStatus.HTTP_ERROR,
                error_code="INTERNAL_ERROR", error=type(exc).__name__,
            )  # fmt: skip

    def load_text(self, page_id: int) -> str | None:
        row = self._pages.get_page_by_id(page_id)
        return self._cache.read_text(row.text_path) if row and row.text_path else None

    def load_html(self, page_id: int) -> str | None:
        row = self._pages.get_page_by_id(page_id)
        return self._cache.read_html(row.html_path) if row and row.html_path else None

    # ------------------------------------------------------------------ pipeline
    def _canon(self, url: str, base: str | None):
        return canonicalize_url(url, base, tracking_params=self._cfg.tracking_params, extractor=self._extractor)

    def _fetch(self, ctx: _Ctx, base_url: str | None, render: RenderMode | None, refetch_failed: bool) -> FetchResult:
        cfg = self._cfg
        canon = self._canon(ctx.requested_url, base_url)
        if not canon.ok or canon.canonical is None or canon.domain is None:
            reason = canon.reason.value if canon.reason else "UNKNOWN"
            self._pages.add_crawl_error(ctx.run_id, "FETCH", f"URL_{reason}", f"rejected URL ({canon.detail or reason})",
                                        url=ctx.requested_url[:500], entity_id=ctx.entity_id)
            return FetchResult(requested_url=ctx.requested_url, retrieved_at=self._clock.now(),
                               crawl_status=CrawlStatus.SKIPPED_FILTER, error_code=f"URL_{reason}",
                               error=f"URL rejected: {reason}")
        ctx.canonical, ctx.domain, ctx.chain = canon.canonical, canon.domain, [canon.canonical]

        if not cfg.allow_private_hosts and is_private_host(canon.host):
            return self._fail(ctx, CrawlStatus.SKIPPED_FILTER, "PRIVATE_HOST_BLOCKED", "private or loopback host refused")

        existing = self._pages.get_page(ctx.run_id, ctx.canonical)
        if existing is not None:
            visited = self._visited_result(ctx, existing, refetch_failed)
            if visited is not None:
                return visited

        cached = self._from_cache(ctx)
        if cached is not None:
            return cached

        if self._budget is not None:
            decision = self._budget.check_page(ctx.domain)
            if not decision.allow:
                return self._fail(ctx, CrawlStatus.SKIPPED_LIMIT, f"BUDGET_{decision.reason}", decision.detail or "page budget exhausted")

        verdict = self._robots.is_allowed(ctx.canonical)
        if not verdict.allowed:
            code = "ROBOTS_DISALLOWED" if verdict.reason.value == "DISALLOWED" else "ROBOTS_UNAVAILABLE"
            return self._fail(ctx, CrawlStatus.BLOCKED_ROBOTS, code, f"robots: {verdict.reason.value}", robots_allowed=False)

        return self._network(ctx, render)

    # -- already visited / cache ---------------------------------------------------------
    def _visited_result(self, ctx: _Ctx, row: PageRow, refetch_failed: bool) -> FetchResult | None:
        if row.crawl_status in (CrawlStatus.OK, CrawlStatus.CACHED):
            text = self._cache.read_text(row.text_path) if row.text_path else None
            if text is not None:
                html = self._cache.read_html(row.html_path) if row.html_path else None
                return FetchResult(
                    requested_url=ctx.requested_url, canonical_url=row.canonical_url, final_url=row.final_url,
                    status_code=row.http_status, retrieved_at=row.crawled_at, text=text, html=html,
                    fetch_method="cache", origin_method=row.fetch_method, cache_hit=True, already_visited=True,
                    crawl_status=CrawlStatus.CACHED, page_id=row.page_id, content_hash=row.content_hash,
                    robots_allowed=row.robots_allowed,
                )  # fmt: skip
            return None  # stored text is gone or corrupt: fetch again
        if refetch_failed:
            return None
        code, _, message = (row.error or "").partition(": ")
        return FetchResult(
            requested_url=ctx.requested_url, canonical_url=row.canonical_url, final_url=row.final_url,
            status_code=row.http_status, retrieved_at=row.crawled_at, already_visited=True,
            crawl_status=row.crawl_status, error_code=code or None, error=message or row.error,
            page_id=row.page_id, robots_allowed=row.robots_allowed,
        )  # fmt: skip

    def _from_cache(self, ctx: _Ctx) -> FetchResult | None:
        ttl = self._cfg.cache_ttl_seconds
        if ttl <= 0:
            return None
        stored = self._cache.get(ctx.canonical)
        if stored is None:
            return None
        now = self._clock.now()
        if (now - stored.meta.retrieved_at).total_seconds() > ttl:
            return None
        text = self._cache.read_text(stored.text_path)
        if text is None:
            return None
        html = self._cache.read_html(stored.html_path) if stored.html_path else None
        meta = stored.meta
        page_id = self._pages.upsert_page(
            PageUpsert(
                run_id=ctx.run_id, url=ctx.requested_url, canonical_url=ctx.canonical, domain=ctx.domain,
                crawl_status=CrawlStatus.CACHED, final_url=meta.final_url, entity_id=ctx.entity_id,
                parent_entity_id=ctx.parent_entity_id, page_type=ctx.page_type.value if ctx.page_type else None,
                http_status=meta.status_code, fetch_method="cache", content_hash=meta.content_hash,
                text_path=stored.text_path, html_path=stored.html_path,
            )
        )  # fmt: skip
        return FetchResult(
            requested_url=ctx.requested_url, canonical_url=ctx.canonical, final_url=meta.final_url,
            status_code=meta.status_code, content_type=meta.content_type, retrieved_at=meta.retrieved_at,
            response_headers=meta.headers, text=text, html=html, fetch_method="cache",
            origin_method=meta.origin_method, cache_hit=True, crawl_status=CrawlStatus.CACHED, page_id=page_id,
            content_hash=meta.content_hash,
        )  # fmt: skip

    # -- failures ------------------------------------------------------------------------
    def _fail(
        self,
        ctx: _Ctx,
        status: CrawlStatus,
        code: str,
        message: str,
        *,
        http_status: int | None = None,
        final_url: str | None = None,
        robots_allowed: bool | None = None,
        fetch_method: str | None = None,
        log_error: bool = True,
        headers: Mapping[str, str] | None = None,
        content_type: str | None = None,
    ) -> FetchResult:
        page_id = self._pages.upsert_page(
            PageUpsert(
                run_id=ctx.run_id, url=ctx.requested_url, canonical_url=ctx.canonical, domain=ctx.domain,
                crawl_status=status, final_url=final_url, entity_id=ctx.entity_id,
                parent_entity_id=ctx.parent_entity_id, page_type=ctx.page_type.value if ctx.page_type else None,
                http_status=http_status, fetch_method=fetch_method, robots_allowed=robots_allowed,
                error=f"{code}: {message}",
            )
        )  # fmt: skip
        if log_error:
            self._pages.add_crawl_error(ctx.run_id, "FETCH", code, message, url=ctx.canonical, page_id=page_id,
                                        entity_id=ctx.entity_id)
        return FetchResult(
            requested_url=ctx.requested_url, canonical_url=ctx.canonical, final_url=final_url,
            status_code=http_status, content_type=content_type, retrieved_at=self._clock.now(),
            response_headers=dict(headers or {}), fetch_method="requests" if fetch_method == "requests" else None,
            crawl_status=status, error_code=code, error=message, page_id=page_id, robots_allowed=robots_allowed,
            redirect_chain=list(ctx.chain),
        )  # fmt: skip

    # -- network -------------------------------------------------------------------------
    def _backoff(self, attempt: int, resp: RawResponse) -> float:
        wait = None
        if resp.status_code in (429, 503):
            wait = _retry_after_seconds(resp.headers.get("retry-after"))
            if wait is not None:
                wait = min(wait, self._cfg.retry_after_cap)
        if wait is None:
            wait = min(self._cfg.backoff_base * (2**attempt), self._cfg.backoff_max)
        return wait

    def _request(self, ctx: _Ctx, url: str, domain: str) -> RawResponse:
        """One logical GET with retries for safe transient failures only."""
        cfg = self._cfg
        headers = {"User-Agent": cfg.user_agent, "Accept": "text/html,application/xhtml+xml,text/plain;q=0.8,*/*;q=0.1"}
        attempt = 0
        while True:
            self._throttle.wait(domain, self._robots.crawl_delay(url))
            resp = self._http.get(url, headers=headers, timeout=cfg.timeout, max_bytes=cfg.max_response_bytes)
            self._throttle.mark(domain)
            failed = resp.error_code is not None or (resp.status_code is not None and resp.status_code >= 400)
            self._pages.record_request(domain, self._clock.now(), error=failed)
            if failed:
                code = resp.error_code or f"HTTP_{resp.status_code}"
                self._pages.add_crawl_error(ctx.run_id, "FETCH", code, f"attempt {attempt + 1} for {url}: {resp.error or code}",
                                            url=url, entity_id=ctx.entity_id)
            transient = resp.error_code in TRANSIENT_ERRORS or _is_retryable_status(resp.status_code)
            if not (failed and transient) or attempt >= cfg.max_retries:
                return resp
            self._sleeper.sleep(self._backoff(attempt, resp))
            attempt += 1

    def _network(self, ctx: _Ctx, render: RenderMode | None) -> FetchResult:
        cfg = self._cfg
        current, domain, hops = ctx.canonical, ctx.domain, 0
        while True:
            resp = self._request(ctx, current, domain)
            if resp.error_code is not None:
                status = {"TIMEOUT": CrawlStatus.TIMEOUT, "SSL_ERROR": CrawlStatus.SSL_ERROR}.get(resp.error_code, CrawlStatus.HTTP_ERROR)
                return self._fail(ctx, status, resp.error_code, resp.error or resp.error_code, final_url=current,
                                  fetch_method="requests", log_error=False, robots_allowed=True)
            code = resp.status_code or 0
            if code in REDIRECT_STATUSES:
                location = resp.headers.get("location")
                if not location:
                    return self._fail(ctx, CrawlStatus.HTTP_ERROR, "REDIRECT_ERROR", f"HTTP {code} without Location",
                                      http_status=code, final_url=current, fetch_method="requests", robots_allowed=True)
                if hops >= cfg.max_redirects:
                    return self._fail(ctx, CrawlStatus.HTTP_ERROR, "TOO_MANY_REDIRECTS", f"more than {cfg.max_redirects} redirects",
                                      http_status=code, final_url=current, fetch_method="requests", robots_allowed=True)
                nxt = self._canon(location, current)
                if not nxt.ok or nxt.canonical is None or nxt.domain is None or nxt.canonical in ctx.chain:
                    return self._fail(ctx, CrawlStatus.HTTP_ERROR, "REDIRECT_ERROR", "invalid or looping redirect target",
                                      http_status=code, final_url=current, fetch_method="requests", robots_allowed=True)
                if not cfg.allow_private_hosts and is_private_host(nxt.host):
                    return self._fail(ctx, CrawlStatus.SKIPPED_FILTER, "PRIVATE_HOST_BLOCKED", "redirect to a private host refused",
                                      http_status=code, final_url=current, fetch_method="requests", robots_allowed=True)
                verdict = self._robots.is_allowed(nxt.canonical)
                if not verdict.allowed:
                    rc = "ROBOTS_DISALLOWED" if verdict.reason.value == "DISALLOWED" else "ROBOTS_UNAVAILABLE"
                    return self._fail(ctx, CrawlStatus.BLOCKED_ROBOTS, rc, f"redirect target blocked: {verdict.reason.value}",
                                      http_status=code, final_url=current, robots_allowed=False, fetch_method="requests")
                ctx.chain.append(nxt.canonical)
                current, domain, hops = nxt.canonical, nxt.domain, hops + 1
                continue
            break

        headers = {k: v for k, v in resp.headers.items() if k in RESPONSE_HEADER_ALLOWLIST}
        ctype_raw = resp.headers.get("content-type")
        common = dict(http_status=resp.status_code, final_url=current, fetch_method="requests", robots_allowed=True,
                      headers=headers, content_type=ctype_raw)
        if code >= 400 or code < 200:
            return self._fail(ctx, CrawlStatus.HTTP_ERROR, f"HTTP_{code}" if code else "HTTP_ERROR",
                              f"HTTP status {code}", log_error=False, **common)  # type: ignore[arg-type]
        if code >= 300:
            return self._fail(ctx, CrawlStatus.HTTP_ERROR, "REDIRECT_ERROR", f"unexpected HTTP {code}", **common)  # type: ignore[arg-type]
        if resp.truncated:
            return self._fail(ctx, CrawlStatus.SKIPPED_FILTER, "RESPONSE_TOO_LARGE",
                              f"body exceeds {cfg.max_response_bytes} bytes", **common)  # type: ignore[arg-type]

        mime = _mime(ctype_raw)
        if mime is None and resp.body.lstrip()[:1] == b"<":
            mime = "text/html"  # sniff a missing content type
        if mime not in ALLOWED_CONTENT_TYPES:
            return self._fail(ctx, CrawlStatus.SKIPPED_FILTER, "UNSUPPORTED_CONTENT_TYPE",
                              f"content type {mime or 'missing'} is not allowed", **common)  # type: ignore[arg-type]

        decoded = resp.body.decode(_charset(ctype_raw), errors="replace")
        is_html = mime in ALLOWED_HTML_TYPES
        html: str | None = decoded if is_html else None
        text = html_to_text(decoded) if is_html else plain_to_text(decoded)
        method = "requests"

        if is_html and html is not None:
            mode = render or {"auto": "auto", "true": "always", "false": "never"}[cfg.use_playwright]
            reason = js_shell_reason(html, text, cfg.playwright_min_text_chars) if mode == "auto" else None
            if mode == "always" or (mode == "auto" and reason):
                rendered = self._render(ctx, current, domain)
                if rendered is not None:
                    rtext = html_to_text(rendered.html)
                    if len(rtext) >= len(text):
                        html, text, method = rendered.html, rtext, "playwright"
                        current = rendered.final_url

        retrieved_at = self._clock.now()
        stored = self._cache.put(
            ctx.canonical, html=html, text=text, final_url=current, status_code=code, content_type=ctype_raw,
            headers=headers, retrieved_at=retrieved_at, origin_method=method,
        )  # fmt: skip
        page_id = self._pages.upsert_page(
            PageUpsert(
                run_id=ctx.run_id, url=ctx.requested_url, canonical_url=ctx.canonical, domain=ctx.domain,
                crawl_status=CrawlStatus.OK, final_url=current, entity_id=ctx.entity_id,
                parent_entity_id=ctx.parent_entity_id, page_type=ctx.page_type.value if ctx.page_type else None,
                http_status=code, fetch_method=method, content_hash=stored.meta.content_hash,
                text_path=stored.text_path, html_path=stored.html_path, robots_allowed=True,
            )
        )  # fmt: skip
        return FetchResult(
            requested_url=ctx.requested_url, canonical_url=ctx.canonical, final_url=current, status_code=code,
            content_type=ctype_raw, retrieved_at=retrieved_at, response_headers=headers, text=text, html=html,
            fetch_method=method, origin_method=method, cache_hit=False,  # type: ignore[arg-type]
            crawl_status=CrawlStatus.OK, page_id=page_id, content_hash=stored.meta.content_hash,
            robots_allowed=True, redirect_chain=list(ctx.chain),
        )  # fmt: skip

    def _render(self, ctx: _Ctx, url: str, domain: str):
        """Render after robots already allowed ``url``. Any failure keeps the requests result."""
        try:
            if self._renderer is None:
                raise RendererUnavailable("no renderer configured")
            self._throttle.wait(domain, self._robots.crawl_delay(url))
            try:
                return self._renderer.render(url, timeout=self._cfg.playwright_timeout, user_agent=self._cfg.user_agent,
                                             max_bytes=self._cfg.max_response_bytes)
            finally:
                self._throttle.mark(domain)
        except RendererUnavailable as exc:
            if not self._renderer_warned:
                self._renderer_warned = True
                log.warning("renderer unavailable; continuing with requests results: %s", exc)
                self._pages.add_crawl_error(ctx.run_id, "FETCH", "RENDERER_UNAVAILABLE", str(exc), url=url,
                                            entity_id=ctx.entity_id)
        except RendererError as exc:
            log.warning("render failed for %s: %s", url, exc)
            self._pages.add_crawl_error(ctx.run_id, "FETCH", "RENDERER_ERROR", str(exc), url=url, entity_id=ctx.entity_id)
        return None


def make_page_text_loader(pages: PageRepo, cache: PageCache) -> Callable[[int], str | None]:
    """Loader used by the Committer: page_id -> verified stored cleaned text (None if unavailable)."""

    def load(page_id: int) -> str | None:
        row = pages.get_page_by_id(page_id)
        if row is None or not row.text_path:
            return None
        return cache.read_text(row.text_path)

    return load
