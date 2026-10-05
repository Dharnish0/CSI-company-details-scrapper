"""LLMGateway and CircuitBreaker (plan 4.M2.5, ARCHITECTURE 25.1-25.4).

Transport retries happen here and only here (D6). The gateway never parses JSON and never touches
the database; usage is reported through the BudgetPort even if the output later fails validation.
"""

from __future__ import annotations

import hashlib
import random
from collections.abc import Mapping
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any

from corpdisc.agents.base import canonical_json
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


@dataclass(frozen=True)
class GatewayPolicy:
    transport_retries: int = 3
    backoff_base: float = 1.0
    backoff_max: float = 30.0
    jitter_ratio: float = 0.25
    temperature: float = 0.0
    absolute_max_output_tokens: int = 8192

    @classmethod
    def from_settings(cls, s: Settings) -> GatewayPolicy:
        return cls(transport_retries=s.llm_transport_retries, backoff_base=s.llm_backoff_base_seconds,
                   backoff_max=s.llm_backoff_max_seconds, temperature=s.llm_temperature)


@dataclass(frozen=True)
class GatewayFailure:
    kind: FailureKind  # TRANSPORT | CIRCUIT_OPEN
    message: str
    auth: bool = False


@dataclass(frozen=True)
class GatewayResult:
    response: ProviderResponse | None
    failure: GatewayFailure | None
    requests_used: int
    latency_ms: int
    transport_errors: tuple[str, ...] = ()


class LLMGateway:
    def __init__(self, provider: LLMProvider, *, model: str, policy: GatewayPolicy, breaker: CircuitBreaker,
                 budget: BudgetPort | None, clock: Clock, sleeper: Sleeper, rng: random.Random | None = None) -> None:
        self._provider = provider
        self._model = model
        self.policy = policy
        self.breaker = breaker
        self._budget = budget
        self._clock = clock
        self._sleeper = sleeper
        self._rng = rng or random.Random(0)

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
            return min(max(retry_after, 0.0), self.policy.backoff_max)
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
        used, errors = 0, []
        while True:
            used += 1
            try:
                resp = self._provider.complete(request)
            except ProviderAuthError as exc:
                self.breaker.record_failure(auth=True)
                log.error("LLM provider auth failure (%s); breaker open", type(exc).__name__)
                return GatewayResult(None, GatewayFailure(FailureKind.TRANSPORT, "provider auth failure", auth=True),
                                     used, ms(), (*errors, "AUTH"))
            except ProviderError as exc:
                errors.append(type(exc).__name__)
                if exc.retryable and used <= self.policy.transport_retries and used < max_requests:
                    self._sleeper.sleep(self._backoff(used - 1, exc.retry_after_s))
                    continue
                self.breaker.record_failure()
                return GatewayResult(None, GatewayFailure(FailureKind.TRANSPORT, str(exc)[:300]), used, ms(),
                                     tuple(errors))
            except Exception as exc:  # noqa: BLE001 - a provider bug must not escape
                errors.append(type(exc).__name__)
                self.breaker.record_failure()
                return GatewayResult(None, GatewayFailure(FailureKind.TRANSPORT, f"provider error {type(exc).__name__}"),
                                     used, ms(), tuple(errors))
            self.breaker.record_success()
            if self._budget is not None:
                self._budget.record_llm_usage(tokens_in=resp.usage.tokens_in, tokens_out=resp.usage.tokens_out,
                                              cost_usd=resp.usage.cost_usd)
            return GatewayResult(resp, None, used, ms(), tuple(errors))
