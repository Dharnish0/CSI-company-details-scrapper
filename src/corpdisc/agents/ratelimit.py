"""Global single-flight adaptive limiter for slow crawl mode (slow-crawl plan D1-D6).

One ``SlowCrawlLimiter`` is owned by the single ``LLMGateway``, so every provider request of every agent passes through
it. It holds one slot at a time (``acquire`` ... ``release_*``), spaces request *starts* by a gap that adapts to the
observed token usage, the provider's rate-limit headers and HTTP 429 answers, and never shortens a provider-stated wait.

Pure logic: time and sleeping come from the injected ``Clock`` and ``Sleeper``; randomness from an injected
``random.Random`` (positive jitter only, so a wait is never shorter than the provider asked).
"""

from __future__ import annotations

import random
import threading
from collections import deque
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from corpdisc.clock import Clock, Sleeper
from corpdisc.config import Settings
from corpdisc.logging_setup import get_logger

log = get_logger("agents.ratelimit")

BACKOFF_FACTOR = 2.0  # gap multiplier per HTTP 429
MAX_MULT = 8.0  # cap of the backoff multiplier
SHRINK = 0.85  # multiplier factor after every SHRINK_EVERY consecutive successes
SHRINK_EVERY = 3
EWMA_ALPHA = 0.3
DEFAULT_COMPLETION_TOKENS = 1000  # projected completion size before any response was observed
TPM_WINDOW_S = 60.0
LOW_TOKENS_FLOOR = 1000  # remaining-token level below which the gap widens to the reported reset ...
LOW_TOKENS_FRACTION = 0.15  # ... or this share of the limit, whichever is larger
LOW_REQUESTS = 5
DAY_RETRY_AFTER_THRESHOLD_S = 65.0  # a Retry-After beyond one minute window means a daily quota
MINUTE_DEFAULT_WAIT_S = 60.0  # minute-scope 429 without Retry-After: wait one full window
JITTER_MAX_S = 2.0
JITTER_FRACTION = 0.05


def classify_429(detail: str, retry_after_s: float | None) -> str:
    """Return ``"day"`` or ``"minute"`` for a rate-limit answer from the provider's text, else from Retry-After."""
    text = (detail or "").lower()
    if "per day" in text or "(tpd)" in text or "(rpd)" in text:
        return "day"
    if "per minute" in text or "(tpm)" in text or "(rpm)" in text:
        return "minute"
    return "day" if retry_after_s is not None and retry_after_s > DAY_RETRY_AFTER_THRESHOLD_S else "minute"


class TokenBucket:
    """Local estimate of a tokens-per-day quota: capacity refills continuously at ``capacity / 86400`` per second.

    ``capacity <= 0`` disables it. Times are monotonic seconds supplied by the caller.
    """

    DAY_S = 86400.0

    def __init__(self, capacity: int, now: float = 0.0) -> None:
        self.capacity = max(0, int(capacity))
        self.level = float(self.capacity)
        self._t = now

    @property
    def enabled(self) -> bool:
        return self.capacity > 0

    @property
    def rate(self) -> float:
        return self.capacity / self.DAY_S

    def _advance(self, now: float) -> None:
        if now > self._t:
            self.level = min(float(self.capacity), self.level + (now - self._t) * self.rate)
            self._t = now

    def level_at(self, now: float) -> float:
        self._advance(now)
        return self.level

    def consume(self, tokens: int, now: float) -> None:
        if not self.enabled:
            return
        self._advance(now)
        self.level = max(0.0, self.level - max(0, tokens))

    def wait_for(self, tokens: int, now: float) -> float:
        """Seconds until ``tokens`` fit in the bucket (a request larger than the capacity waits for a full bucket)."""
        if not self.enabled:
            return 0.0
        self._advance(now)
        need = min(float(max(0, tokens)), float(self.capacity))
        return 0.0 if self.level >= need else (need - self.level) / self.rate

    def cap_level(self, level: float, now: float) -> None:
        """A provider 429 is authoritative: the real level is at most ``level``."""
        if self.enabled:
            self._advance(now)
            self.level = max(0.0, min(self.level, level))

    def replay(self, events: Iterable[tuple[float, int]], now: float) -> None:
        """Seed from past usage. ``events`` are ``(seconds_ago, tokens)``; the bucket is assumed full ``max(age)`` ago."""
        if not self.enabled:
            return
        ordered = sorted(((max(0.0, age), max(0, tok)) for age, tok in events), reverse=True)  # oldest first
        if not ordered:
            self.level, self._t = float(self.capacity), now
            return
        self.level, self._t = float(self.capacity), now - ordered[0][0]
        for age, tok in ordered:
            self.consume(tok, now - age)
        self._advance(now)


@dataclass(frozen=True)
class LimiterConfig:
    interval_s: float = 90.0
    tpm_limit: int = 8000
    tpd_limit: int = 200_000
    tpm_target_fraction: float = 0.70
    daily_retry_s: float = 900.0
    sleep_chunk_s: float = 30.0

    @classmethod
    def from_settings(cls, s: Settings) -> LimiterConfig:
        return cls(interval_s=s.slow_crawl_interval_seconds, tpm_limit=s.slow_crawl_tpm_limit,
                   tpd_limit=s.slow_crawl_tpd_limit, tpm_target_fraction=s.slow_crawl_tpm_target_fraction,
                   daily_retry_s=s.slow_crawl_daily_retry_seconds, sleep_chunk_s=s.slow_crawl_sleep_chunk_seconds)


@dataclass(frozen=True)
class Acquired:
    started_at: float  # monotonic start of the provider request
    waited_s: float
    projected_tokens: int


@dataclass(frozen=True)
class Refused:
    reason: str  # "MAX_RUNTIME", or whatever ``stop_check`` returned (e.g. "SHUTDOWN")


class SlowCrawlLimiter:
    """Single-flight limiter. ``acquire`` blocks until a request may start and keeps the slot until a ``release_*`` call."""

    def __init__(self, cfg: LimiterConfig, clock: Clock, sleeper: Sleeper, *, rng: random.Random | None = None,
                 stop_check: Callable[[], str | None] | None = None, deadline: float | None = None,
                 bucket: TokenBucket | None = None) -> None:
        self.cfg = cfg
        self._clock, self._sleeper = clock, sleeper
        self._rng = rng or random.Random(0)
        self._stop_check = stop_check or (lambda: None)
        self.deadline = deadline  # monotonic seconds; a wait that would reach it is refused
        self.bucket = bucket if bucket is not None else TokenBucket(cfg.tpd_limit, clock.monotonic())
        self._lock = threading.Lock()
        self._held = False
        self._next_start = 0.0
        self._cooldown_until = 0.0
        self._last_start: float | None = None
        self._last_projected = 0
        self._mult = 1.0
        self._streak = 0
        self._gap = cfg.interval_s
        self._ewma_total: float | None = None
        self._ewma_out: float | None = None
        self._limit_tokens: int | None = None
        self._window: deque[list[float]] = deque()  # [start, tokens] inside the last 60 s
        self.requests = 0
        self.successes = 0
        self.rate_limited = 0
        self.last_429_scope: str | None = None
        self.total_wait_s = 0.0
        self.refusals = 0  # acquire() calls refused (shutdown / runtime limit); the supervisor requeues the task they hit
        self.last_refusal: str | None = None

    # ------------------------------------------------------------------ internals
    def _mono(self) -> float:
        return self._clock.monotonic()

    def _stop_reason(self) -> str | None:
        reason = self._stop_check()
        if reason:
            return reason
        if self.deadline is not None and self._mono() >= self.deadline:
            return "MAX_RUNTIME"
        return None

    def _refuse(self, reason: str) -> Refused:
        self.refusals += 1
        self.last_refusal = reason
        return Refused(reason)

    def _free(self) -> None:
        if self._held:
            self._held = False
            self._lock.release()

    def _jitter(self, wait_s: float) -> float:
        """Positive-only jitter: at most 5 % of the wait and never more than 2 s (strictly below that bound)."""
        return min(JITTER_MAX_S, JITTER_FRACTION * max(0.0, wait_s)) * self._rng.random()

    def _window_wait(self, now: float, projected: int) -> float:
        limit = self._limit_tokens or self.cfg.tpm_limit
        while self._window and now - self._window[0][0] >= TPM_WINDOW_S:
            self._window.popleft()
        if limit <= 0 or not self._window:
            return 0.0  # an empty window never blocks: an oversized request waits at most one window, then proceeds
        used = sum(tok for _, tok in self._window)
        if used + projected <= limit:
            return 0.0
        freed, need = 0.0, used + projected - limit
        for ts, tok in self._window:
            freed += tok
            if freed >= need:
                return max(0.0, ts + TPM_WINDOW_S - now)
        return max(0.0, self._window[-1][0] + TPM_WINDOW_S - now)

    # ------------------------------------------------------------------ public
    def acquire(self, est_in: int, max_out: int) -> Acquired | Refused:
        """Block until a provider request may start. Returns ``Refused`` on shutdown or when the deadline is reached."""
        while not self._lock.acquire(timeout=0.2):
            reason = self._stop_reason()
            if reason:
                return self._refuse(reason)
        self._held = True
        try:
            reason = self._stop_reason()
            if reason:
                self._free()
                return self._refuse(reason)
            now = self._mono()
            completion = min(max(0, max_out), int(self._ewma_out) if self._ewma_out else DEFAULT_COMPLETION_TOKENS)
            projected = max(0, est_in) + completion
            target = max(now, self._next_start, self._cooldown_until, now + self.bucket.wait_for(projected, now),
                         now + self._window_wait(now, projected))
            if self.deadline is not None and target >= self.deadline:
                self._free()
                return self._refuse("MAX_RUNTIME")
            first_wait = target - now
            while True:
                rem = target - self._mono()
                if rem <= 1e-9:
                    break
                self._sleeper.sleep(min(self.cfg.sleep_chunk_s, rem))
                reason = self._stop_check()
                if reason:
                    self._free()
                    return self._refuse(reason)
            now = self._mono()
            self._last_start, self._last_projected = now, projected
            self._window.append([now, float(projected)])
            self.requests += 1
            if first_wait > 0:
                self.total_wait_s += first_wait
            return Acquired(now, max(0.0, first_wait), projected)
        except BaseException:
            self._free()
            raise

    def release_success(self, metrics: Mapping[str, Any] | None, tokens_in: int, tokens_out: int) -> None:
        """Account a successful response and schedule the next start (gap formula of plan D3)."""
        m = metrics or {}
        now = self._mono()
        total = max(0, tokens_in) + max(0, tokens_out)
        if total > 0:
            a = EWMA_ALPHA
            self._ewma_total = float(total) if self._ewma_total is None else a * total + (1 - a) * self._ewma_total
            out = float(max(0, tokens_out))
            self._ewma_out = out if self._ewma_out is None else a * out + (1 - a) * self._ewma_out
            self.bucket.consume(total, now)
            if self._window:
                self._window[-1][1] = float(total)
        if m.get("rl_limit_tokens"):
            self._limit_tokens = int(m["rl_limit_tokens"])
        self.successes += 1
        self._streak += 1
        if self._streak >= SHRINK_EVERY:
            self._mult = max(1.0, self._mult * SHRINK)
            self._streak = 0
        limit = self._limit_tokens or self.cfg.tpm_limit
        gap = self.cfg.interval_s * self._mult
        if self._ewma_total and limit > 0:
            gap = max(gap, self._ewma_total / (self.cfg.tpm_target_fraction * limit) * 60.0)
        reset_gap = 0.0
        rem_tok, rem_req = m.get("rl_remaining_tokens"), m.get("rl_remaining_requests")
        if rem_tok is not None and limit > 0 and rem_tok < max(LOW_TOKENS_FLOOR, LOW_TOKENS_FRACTION * limit):
            reset_gap = max(reset_gap, float(m.get("rl_reset_tokens_s") or 0.0))
        if rem_req is not None and rem_req <= LOW_REQUESTS:
            reset_gap = max(reset_gap, float(m.get("rl_reset_requests_s") or 0.0))
        self._gap = gap
        start = self._last_start if self._last_start is not None else now
        self._next_start = max(start + gap, now + reset_gap, self._cooldown_until)
        self._free()

    def release_429(self, scope: str, retry_after_s: float | None, metrics: Mapping[str, Any] | None = None) -> float:
        """Account a provider 429: widen the gap, start a cooldown of at least ``retry_after_s``. Returns the cooldown."""
        m = metrics or {}
        now = self._mono()
        self.rate_limited += 1
        self.last_429_scope = scope
        self._streak = 0
        self._mult = min(MAX_MULT, self._mult * BACKOFF_FACTOR)
        if retry_after_s is not None:
            wait = max(0.0, retry_after_s)
        elif scope == "day":
            wait = self.cfg.daily_retry_s
        else:
            wait = max(MINUTE_DEFAULT_WAIT_S, float(m.get("rl_reset_tokens_s") or 0.0), float(m.get("rl_reset_requests_s") or 0.0))
        wait += self._jitter(wait)
        self._cooldown_until = max(self._cooldown_until, now + wait)
        start = self._last_start if self._last_start is not None else now
        self._gap = self.cfg.interval_s * self._mult
        self._next_start = max(self._next_start, start + self._gap, self._cooldown_until)
        if scope == "day" and retry_after_s is not None and self.bucket.enabled:
            # at wake time the level must have recovered to what this request needed
            self.bucket.cap_level(self._last_projected - retry_after_s * self.bucket.rate, now)
        log.warning("provider 429 (%s quota): cooling down %.0fs; gap now %.0fs", scope, wait, self._gap)
        self._free()
        return wait

    def release_error(self) -> None:
        """Account a request that failed for another reason (5xx, timeout, bad output): keep the interval, no widening."""
        now = self._mono()
        start = self._last_start if self._last_start is not None else now
        self._next_start = max(self._next_start, start + self.cfg.interval_s * self._mult, self._cooldown_until)
        self._free()

    def cooldown_remaining(self) -> float:
        return max(0.0, self._cooldown_until - self._mono())

    def snapshot(self) -> dict[str, Any]:
        now = self._mono()
        return {
            "gap_s": self._gap, "multiplier": self._mult, "last_429_scope": self.last_429_scope,
            "cooldown_remaining_s": self.cooldown_remaining(), "next_start_in_s": max(0.0, self._next_start - now),
            "ewma_tokens": self._ewma_total, "ewma_completion_tokens": self._ewma_out,
            "limit_tokens": self._limit_tokens or self.cfg.tpm_limit, "tpd_level": self.bucket.level_at(now),
            "requests": self.requests, "successes": self.successes, "rate_limited": self.rate_limited,
            "total_wait_s": self.total_wait_s,
        }
