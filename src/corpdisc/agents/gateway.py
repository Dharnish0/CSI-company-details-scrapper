"""LLMGateway and CircuitBreaker (plan 4.M2.5, ARCHITECTURE 25.1-25.4).

Transport retries happen here and only here (D6). The gateway never parses JSON and never touches
the database; usage is reported through the BudgetPort even if the output later fails validation.
"""

from __future__ import annotations

import hashlib
import random
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any

from corpdisc.agents.base import canonical_json, estimate_tokens
from corpdisc.agents.ports import (
    BudgetPort,
    Clock,
    LLMProvider,
    ProviderAuthError,
    ProviderError,
    ProviderRequest,
    ProviderResponse,
    Sleeper,
)
from corpdisc.agents.ratelimit import Refused, SlowCrawlLimiter, classify_429
from corpdisc.config import Settings
from corpdisc.contracts.agents import AgentName, FailureKind
from corpdisc.logging_setup import get_logger

log = get_logger("agents.gateway")


class BreakerState(StrEnum):
    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"


class CircuitBreaker:
    """In-memory breaker. No time-based recovery: the resume path calls ``half_open()`` (25.4)."""

    def __init__(self, threshold: int = 5) -> None:
        self.threshold = threshold
        self._state = BreakerState.CLOSED
        self.consecutive_failures = 0
        self._probe_used = False

    @property
    def state(self) -> BreakerState:
        return self._state

    def allow_request(self) -> bool:
        if self._state is BreakerState.OPEN:
            return False
        if self._state is BreakerState.HALF_OPEN:
            if self._probe_used:
                return False
            self._probe_used = True
        return True

    def record_success(self) -> None:
        self._state, self.consecutive_failures, self._probe_used = BreakerState.CLOSED, 0, False

    def record_failure(self, *, auth: bool = False) -> None:
        self.consecutive_failures += 1
        if auth or self._state is BreakerState.HALF_OPEN or self.consecutive_failures >= self.threshold:
            self._state = BreakerState.OPEN

    def half_open(self) -> None:
        if self._state is BreakerState.OPEN:
            self._state, self._probe_used = BreakerState.HALF_OPEN, False


class RequestPacer:
    """Client-side pacing for one provider: minimum spacing (requests per minute) and a sliding 60 s token window.

    ``wait`` is called before every provider attempt. ``observe`` feeds server-reported ``x-ratelimit-*`` hints so a
    near-empty bucket delays the next call until the reported reset. A limit of 0 disables that dimension.
    """

    WINDOW_S = 60.0

    def __init__(self, *, rpm: int, tpm: int, clock: Clock, sleeper: Sleeper) -> None:
        self.rpm, self.tpm = max(0, rpm), max(0, tpm)
        self._clock, self._sleeper = clock, sleeper
        self._last_start: float | None = None
        self._window: deque[tuple[float, int]] = deque()
        self._blocked_until = 0.0
        self.total_wait_s = 0.0
        self.waits = 0

    @property
    def enabled(self) -> bool:
        return self.rpm > 0 or self.tpm > 0

    def _prune(self, now: float) -> None:
        while self._window and now - self._window[0][0] >= self.WINDOW_S:
            self._window.popleft()

    def wait(self, estimated_tokens: int) -> float:
        """Sleep until a request of ``estimated_tokens`` may start; return the seconds slept."""
        slept = 0.0
        if not self.enabled:
            return slept
        for _ in range(1000):  # bounded: each pass strictly advances time
            now = self._clock.monotonic()
            delay = max(0.0, self._blocked_until - now)
            if self.rpm and self._last_start is not None:
                delay = max(delay, self._last_start + 60.0 / self.rpm - now)
            if self.tpm:
                self._prune(now)
                used = sum(tok for _, tok in self._window)
                if self._window and used + estimated_tokens > self.tpm:
                    # wait until enough of the oldest entries age out of the window
                    freed, need = 0, used + estimated_tokens - self.tpm
                    for ts, tok in self._window:
                        freed += tok
                        if freed >= need:
                            delay = max(delay, ts + self.WINDOW_S - now)
                            break
                    else:
                        delay = max(delay, self._window[-1][0] + self.WINDOW_S - now)
            if delay <= 1e-9:
                break
            self._sleeper.sleep(delay)
            slept += delay
        now = self._clock.monotonic()
        self._last_start = now
        if self.tpm:
            self._window.append((now, max(0, estimated_tokens)))
        if slept:
            self.total_wait_s += slept
            self.waits += 1
        return slept

    def observe(self, metrics: Mapping[str, Any], estimated_tokens: int) -> None:
        """Use server hints: an exhausted request or token bucket blocks until its reported reset."""
        now = self._clock.monotonic()
        rem_req, rem_tok = metrics.get("rl_remaining_requests"), metrics.get("rl_remaining_tokens")
        if rem_req is not None and rem_req <= 0 and metrics.get("rl_reset_requests_s") is not None:
            self._blocked_until = max(self._blocked_until, now + float(metrics["rl_reset_requests_s"]))
        if rem_tok is not None and rem_tok < estimated_tokens and metrics.get("rl_reset_tokens_s") is not None:
            self._blocked_until = max(self._blocked_until, now + float(metrics["rl_reset_tokens_s"]))

    def penalize(self, seconds: float) -> None:
        """After an HTTP 429, hold further requests for at least ``seconds``."""
        self._blocked_until = max(self._blocked_until, self._clock.monotonic() + max(0.0, seconds))


# Conservative client-side defaults per provider when LLM_RPM_LIMIT / LLM_TPM_LIMIT are -1 (provider default).
# Token limits vary by plan, so none is assumed; the server's x-ratelimit-* headers and 429 handling cover tokens.
PROVIDER_DEFAULT_LIMITS: dict[str, tuple[int, int]] = {"groq": (30, 0)}


@dataclass(frozen=True)
class GatewayPolicy:
    transport_retries: int = 3
    rate_limit_retries: int = 5
    retry_after_ceiling: float = 120.0
    backoff_base: float = 1.0
    backoff_max: float = 30.0
    jitter_ratio: float = 0.25
    temperature: float = 0.0
    absolute_max_output_tokens: int = 8192

    @classmethod
    def from_settings(cls, s: Settings) -> GatewayPolicy:
        return cls(transport_retries=s.llm_transport_retries, backoff_base=s.llm_backoff_base_seconds,
                   backoff_max=s.llm_backoff_max_seconds, temperature=s.llm_temperature,
                   rate_limit_retries=s.llm_rate_limit_retries,
                   retry_after_ceiling=s.llm_retry_after_ceiling_seconds)


@dataclass(frozen=True)
class GatewayFailure:
    kind: FailureKind  # TRANSPORT | CIRCUIT_OPEN
    message: str
    auth: bool = False
    retryable: bool = False  # transient: the same work may succeed later (429, timeout, 5xx)
    retry_after_s: float | None = None
    rate_limited: bool = False  # HTTP 429 (not an outage): no model work was done
    quota_scope: str | None = None  # slow crawl only: "minute" or "day" for a 429, else None


@dataclass(frozen=True)
class GatewayResult:
    response: ProviderResponse | None
    failure: GatewayFailure | None
    requests_used: int
    latency_ms: int
    transport_errors: tuple[str, ...] = ()


class LLMGateway:
    def __init__(self, provider: LLMProvider, *, model: str, policy: GatewayPolicy, breaker: CircuitBreaker,
                 budget: BudgetPort | None, clock: Clock, sleeper: Sleeper, rng: random.Random | None = None,
                 pacer: RequestPacer | None = None, limiter: SlowCrawlLimiter | None = None) -> None:
        self._provider = provider
        self._model = model
        self.policy = policy
        self.breaker = breaker
        self._budget = budget
        self._clock = clock
        self._sleeper = sleeper
        self._rng = rng or random.Random(0)
        self.pacer = pacer
        self.limiter = limiter  # slow crawl: global single-flight limiter; None = default behaviour

    @property
    def provider_name(self) -> str:
        return self._provider.name

    @property
    def model(self) -> str:
        return self._model

    def cache_key(self, *, agent: AgentName, schema_version: str, prompt_version: str, max_output_tokens: int,
                  input_payload: Mapping[str, Any], model: str | None = None) -> str:
        doc = {"agent": agent.value, "provider": self.provider_name, "model": model or self._model,
               "schema_version": schema_version, "prompt_version": prompt_version,
               "temperature": self.policy.temperature, "max_output_tokens": max_output_tokens,
               "input": input_payload}
        return hashlib.sha256(canonical_json(doc).encode("utf-8")).hexdigest()

    def _backoff(self, n: int, retry_after: float | None) -> float:
        if retry_after is not None:
            # A server-supplied Retry-After is honoured in full (the ceiling is enforced by the caller).
            return max(retry_after, 0.0)
        base = min(self.policy.backoff_max, self.policy.backoff_base * (2 ** n))
        jitter = base * self.policy.jitter_ratio
        return max(0.0, base + self._rng.uniform(-jitter, jitter))

    def complete(self, request: ProviderRequest, *, max_requests: int) -> GatewayResult:
        """Never raises."""
        start = self._clock.monotonic()

        def ms() -> int:
            return int((self._clock.monotonic() - start) * 1000)

        if not self.breaker.allow_request():
            return GatewayResult(None, GatewayFailure(FailureKind.CIRCUIT_OPEN, "circuit breaker open"), 0, 0)
        request = replace(request, max_output_tokens=min(request.max_output_tokens,
                                                         self.policy.absolute_max_output_tokens))
        used, rl_used, errors = 0, 0, []
        while True:
            used += 1
            est = estimate_tokens(request.system + "".join(m.content for m in request.messages)) + request.max_output_tokens
            if self.pacer is not None:
                self.pacer.wait(est)
            scope: str | None = None
            if self.limiter is not None:
                acquired = self.limiter.acquire(est - request.max_output_tokens, request.max_output_tokens)
                if isinstance(acquired, Refused):  # shutdown or runtime limit: nothing was sent, the breaker is untouched
                    return GatewayResult(None, GatewayFailure(FailureKind.TRANSPORT, f"slow crawl: request not started "
                                                              f"({acquired.reason})", retryable=True),
                                         max(0, used - 1 - rl_used), ms(), tuple(errors))
            try:
                resp = self._provider.complete(request)
            except ProviderAuthError as exc:
                if self.limiter is not None:
                    self.limiter.release_error()
                self.breaker.record_failure(auth=True)
                log.error("LLM provider auth failure (%s); breaker open", type(exc).__name__)
                return GatewayResult(None, GatewayFailure(FailureKind.TRANSPORT, "provider auth failure", auth=True),
                                     used - rl_used, ms(), (*errors, "AUTH"))
            except ProviderError as exc:
                errors.append("RATE_LIMITED" if exc.rate_limited else type(exc).__name__)
                if exc.rate_limited:
                    # 429: no model work was done and the provider is healthy, so it neither opens the breaker nor
                    # consumes the invocation's request budget. Honour Retry-After up to the configured ceiling.
                    rl_used += 1
                    if self.limiter is not None:
                        # slow crawl: the limiter owns the wait (cooldown >= Retry-After); a daily quota is not retried here
                        scope = classify_429(exc.detail, exc.retry_after_s)
                        self.limiter.release_429(scope, exc.retry_after_s, exc.rl_metrics)
                        if scope == "day":
                            log.warning("rate limited (429): daily quota; deferring the work")
                            return GatewayResult(None, GatewayFailure(FailureKind.TRANSPORT, str(exc)[:300], retryable=True,
                                                                      retry_after_s=exc.retry_after_s, rate_limited=True,
                                                                      quota_scope=scope),
                                                 used - rl_used, ms(), tuple(errors))
                    wait_s = self._backoff(rl_used - 1, exc.retry_after_s)
                    if self.pacer is not None:
                        self.pacer.penalize(wait_s)
                    if exc.retry_after_s is not None and exc.retry_after_s > self.policy.retry_after_ceiling:
                        log.warning("rate limited: Retry-After %.0fs exceeds the %.0fs ceiling; deferring the work",
                                    exc.retry_after_s, self.policy.retry_after_ceiling)
                        return GatewayResult(None, GatewayFailure(FailureKind.TRANSPORT, str(exc)[:300], retryable=True,
                                                                  retry_after_s=exc.retry_after_s, rate_limited=True,
                                                                  quota_scope=scope),
                                             used - rl_used, ms(), tuple(errors))
                    if rl_used <= self.policy.rate_limit_retries:
                        log.info("rate limited (429): sleeping %.1fs before retry %d/%d", wait_s, rl_used,
                                 self.policy.rate_limit_retries)
                        if self.limiter is None:
                            self._sleeper.sleep(wait_s)
                        continue  # slow crawl: the next acquire() waits out the limiter cooldown
                    return GatewayResult(None, GatewayFailure(FailureKind.TRANSPORT, f"rate limited: {str(exc)[:250]}",
                                                              retryable=True, retry_after_s=exc.retry_after_s,
                                                              rate_limited=True, quota_scope=scope),
                                         used - rl_used, ms(), tuple(errors))
                if self.limiter is not None:
                    self.limiter.release_error()
                if exc.retryable and used - rl_used <= self.policy.transport_retries and used - rl_used < max_requests:
                    self._sleeper.sleep(min(self._backoff(used - rl_used - 1, exc.retry_after_s), self.policy.retry_after_ceiling))
                    continue
                self.breaker.record_failure()
                return GatewayResult(None, GatewayFailure(FailureKind.TRANSPORT, str(exc)[:300], retryable=exc.retryable,
                                                          retry_after_s=exc.retry_after_s),
                                     used - rl_used, ms(), tuple(errors))
            except Exception as exc:  # noqa: BLE001 - a provider bug must not escape
                if self.limiter is not None:
                    self.limiter.release_error()
                errors.append(type(exc).__name__)
                self.breaker.record_failure()
                return GatewayResult(None, GatewayFailure(FailureKind.TRANSPORT, f"provider error {type(exc).__name__}"),
                                     used - rl_used, ms(), tuple(errors))
            except BaseException:  # KeyboardInterrupt etc.: free the slot, then let it propagate
                if self.limiter is not None:
                    self.limiter.release_error()
                raise
            if self.limiter is not None:
                self.limiter.release_success(resp.metrics, resp.usage.tokens_in, resp.usage.tokens_out)
            self.breaker.record_success()
            if self.pacer is not None:
                self.pacer.observe(resp.metrics, est)
            if self._budget is not None:
                self._budget.record_llm_usage(tokens_in=resp.usage.tokens_in, tokens_out=resp.usage.tokens_out,
                                              cost_usd=resp.usage.cost_usd)
            return GatewayResult(resp, None, used - rl_used, ms(), tuple(errors))