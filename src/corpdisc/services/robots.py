"""robots.txt handling (ARCHITECTURE.md 10.1).

Deterministic policy; nothing here can be overridden by an agent or LLM (there is no
override parameter, only the explicit ``respect_robots`` setting given at construction).

Safe behaviour:
* 2xx: parse and obey (Disallow, Crawl-delay).
* 4xx (missing, 401, 403): treated as "no restrictions", cached as empty text.
* 5xx / timeout / network error: deny every URL on that domain, record it, and retry only
  after ``failure_cooldown_seconds`` (never hammered).
Robots are fetched once per domain per process and persisted in domain_stats with a TTL.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Final, Protocol
from urllib.robotparser import RobotFileParser

from corpdisc.clock import Clock
from corpdisc.db.repos.pages import PageRepo
from corpdisc.logging_setup import get_logger
from corpdisc.services.urls import canonicalize_url

log = get_logger("services.robots")

MAX_ROBOTS_BYTES: Final = 500 * 1024
DECISION_LOG_SIZE: Final = 1000


class RobotsReason(StrEnum):
    ALLOWED = "ALLOWED"
    DISALLOWED = "DISALLOWED"
    NO_ROBOTS = "NO_ROBOTS"
    UNAVAILABLE_DENY = "UNAVAILABLE_DENY"
    DISABLED = "DISABLED"
    INVALID_URL = "INVALID_URL"


@dataclass(frozen=True)
class RobotsDecision:
    url: str
    domain: str
    allowed: bool
    reason: RobotsReason
    crawl_delay: float | None
    from_cache: bool
    robots_status: int | None
    checked_at: datetime


@dataclass(frozen=True)
class RobotsFetchResult:
    status_code: int | None
    text: str | None
    error: str | None = None


RobotsFetcher = Callable[[str], RobotsFetchResult]


class RobotsStore(Protocol):
    def load(self, domain: str) -> tuple[str, datetime, float | None] | None: ...
    def save(self, domain: str, robots_txt: str, fetched_at: datetime, crawl_delay: float | None) -> None: ...
    def record_failure(self, domain: str, at: datetime) -> None: ...


class PageRepoRobotsStore:
    """Adapts PageRepo (domain_stats) to the RobotsStore protocol."""

    def __init__(self, repo: PageRepo) -> None:
        self._repo = repo

    def load(self, domain: str) -> tuple[str, datetime, float | None] | None:
        row = self._repo.get_domain_stats(domain)
        if row is None or row.robots_txt is None or row.robots_fetched_at is None:
            return None
        return row.robots_txt, row.robots_fetched_at, row.crawl_delay_s

    def save(self, domain: str, robots_txt: str, fetched_at: datetime, crawl_delay: float | None) -> None:
        self._repo.save_robots(domain, robots_txt, fetched_at, crawl_delay)

    def record_failure(self, domain: str, at: datetime) -> None:
        self._repo.record_request(domain, at, error=True)


@dataclass
class _Parsed:
    parser: RobotFileParser
    crawl_delay: float | None
    empty: bool
    status: int | None


def _parse(text: str, user_agent: str) -> _Parsed:
    parser = RobotFileParser()
    parser.parse(text[:MAX_ROBOTS_BYTES].splitlines())
    parser.modified()  # mark as loaded so an empty file means "allow all"
    delay = parser.crawl_delay(user_agent)
    return _Parsed(parser, float(delay) if delay is not None else None, not text.strip(), None)


class RobotsService:
    def __init__(
        self,
        *,
        fetcher: RobotsFetcher,
        store: RobotsStore,
        user_agent: str,
        clock: Clock,
        respect_robots: bool = True,
        ttl_seconds: float = 86_400.0,
        failure_cooldown_seconds: float = 600.0,
    ) -> None:
        self._fetcher = fetcher
        self._store = store
        self._ua = user_agent
        self._clock = clock
        self._respect = respect_robots
        self._ttl = timedelta(seconds=ttl_seconds)
        self._cooldown = timedelta(seconds=failure_cooldown_seconds)
        self._domains: dict[str, _Parsed] = {}
        self._failures: dict[str, tuple[datetime, int | None]] = {}
        self._decisions: deque[RobotsDecision] = deque(maxlen=DECISION_LOG_SIZE)

    @property
    def decisions(self) -> tuple[RobotsDecision, ...]:
        return tuple(self._decisions)

    # ------------------------------------------------------------------ loading
    def _load(self, scheme: str, domain: str) -> tuple[_Parsed | None, bool, int | None]:
        """Returns (parsed or None when unavailable, from_cache, failure_status)."""
        now = self._clock.now()
        cached = self._domains.get(domain)
        if cached is not None:
            return cached, True, None
        failure = self._failures.get(domain)
        if failure is not None:
            if now - failure[0] < self._cooldown:
                return None, True, failure[1]
            del self._failures[domain]  # cool-down over: one fresh attempt is allowed
        stored = self._store.load(domain)
        if stored is not None and now - stored[1] <= self._ttl:
            parsed = _parse(stored[0], self._ua)
            self._domains[domain] = parsed
            return parsed, True, None

        result = self._fetcher(f"{scheme}://{domain}/robots.txt")
        status = result.status_code
        if status is not None and 200 <= status < 300 and result.text is not None:
            text = result.text
        elif status is not None and 400 <= status < 500:
            text = ""  # missing / forbidden robots.txt means no restrictions
        else:
            self._failures[domain] = (now, status)
            self._store.record_failure(domain, now)
            log.warning("robots.txt unavailable for %s (status=%s error=%s): denying this domain for now",
                        domain, status, result.error)
            return None, False, status
        parsed = _parse(text, self._ua)
        parsed.status = status
        self._domains[domain] = parsed
        self._store.save(domain, text, now, parsed.crawl_delay)
        return parsed, False, status

    # ------------------------------------------------------------------ queries
    def is_allowed(self, url: str) -> RobotsDecision:
        """Never raises. There is deliberately no way to override the result."""
        now = self._clock.now()
        res = canonicalize_url(url)
        if not res.ok or res.domain is None or res.scheme is None:
            return self._record(RobotsDecision(url, "", False, RobotsReason.INVALID_URL, None, False, None, now))
        domain, canonical = res.domain, res.canonical or url
        if not self._respect:
            return self._record(RobotsDecision(canonical, domain, True, RobotsReason.DISABLED, None, False, None, now))
        if canonical.endswith("/robots.txt") and canonical.count("/") == 3:
            return self._record(RobotsDecision(canonical, domain, True, RobotsReason.ALLOWED, None, True, None, now))
        try:
            parsed, from_cache, status = self._load(res.scheme, domain)
        except Exception as exc:  # a broken fetcher must fail closed
            log.warning("robots check failed for %s: %s", domain, type(exc).__name__)
            self._failures[domain] = (now, None)
            parsed, from_cache, status = None, False, None
        if parsed is None:
            return self._record(RobotsDecision(canonical, domain, False, RobotsReason.UNAVAILABLE_DENY, None,
                                               from_cache, status, now))
        allowed = parsed.parser.can_fetch(self._ua, canonical)
        if not allowed:
            reason = RobotsReason.DISALLOWED
        else:
            reason = RobotsReason.NO_ROBOTS if parsed.empty else RobotsReason.ALLOWED
        return self._record(RobotsDecision(canonical, domain, allowed, reason, parsed.crawl_delay, from_cache,
                                           parsed.status, now))

    def crawl_delay(self, url: str) -> float | None:
        res = canonicalize_url(url)
        if not res.ok or res.domain is None:
            return None
        parsed = self._domains.get(res.domain)
        return parsed.crawl_delay if parsed else None

    def _record(self, decision: RobotsDecision) -> RobotsDecision:
        self._decisions.append(decision)
        log.debug("robots %s %s (%s)", "allow" if decision.allowed else "deny", decision.url, decision.reason.value)
        return decision
