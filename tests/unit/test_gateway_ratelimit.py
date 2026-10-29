"""Gateway rate-limit handling: Retry-After, pacing, retry ceiling, breaker semantics (FakeClock, no network)."""
from __future__ import annotations

import pytest

from corpdisc.agents.gateway import CircuitBreaker, GatewayPolicy, LLMGateway, RequestPacer
from corpdisc.agents.ports import (
    Message,
    ProviderAuthError,
    ProviderRequest,
    ProviderResponse,
    ProviderTransportError,
)
from corpdisc.agents.providers.fake import FakeLLMProvider
from corpdisc.contracts.agents import FailureKind
from tests.helpers.clock import FakeClock


def req(text: str = "hi", max_out: int = 100) -> ProviderRequest:
    return ProviderRequest(model="m", system="s", messages=(Message("user", text),), response_schema={},
                           schema_name="X", temperature=0.0, max_output_tokens=max_out)


def e429(retry_after: float | None = None) -> ProviderTransportError:
    return ProviderTransportError("429", retryable=True, retry_after_s=retry_after, rate_limited=True)


def gateway(script, clock, *, pacer=None, breaker=None, **policy):
    return LLMGateway(FakeLLMProvider(script), model="m", policy=GatewayPolicy(jitter_ratio=0.0, **policy),
                      breaker=breaker or CircuitBreaker(3), budget=None, clock=clock, sleeper=clock, pacer=pacer)


def test_retry_after_beyond_30s_is_slept_in_full():
    clock = FakeClock()
    gw = gateway([e429(45), '{"ok": 1}'], clock)
    res = gw.complete(req(), max_requests=6)
    assert res.response is not None
    assert clock.sleeps == [45]  # not clamped to backoff_max (30)
    assert res.requests_used == 1  # the 429 consumed no model work


def test_retry_after_above_ceiling_returns_retryable_failure_without_sleeping():
    clock = FakeClock()
    gw = gateway([e429(600)], clock, retry_after_ceiling=120)
    res = gw.complete(req(), max_requests=6)
    assert res.response is None and res.failure.retryable and res.failure.retry_after_s == 600
    assert clock.sleeps == []
    assert gw.breaker.consecutive_failures == 0


def test_exhausted_429_is_retryable_later_and_does_not_open_breaker():
    clock = FakeClock()
    gw = gateway([e429(), e429(), e429(), e429()], clock, rate_limit_retries=2, backoff_base=1.0, backoff_max=30.0)
    res = gw.complete(req(), max_requests=6)
    assert res.failure is not None and res.failure.retryable
    assert clock.sleeps == [1.0, 2.0]  # exponential backoff when no Retry-After
    assert gw.breaker.state.value == "CLOSED" and gw.breaker.consecutive_failures == 0


def test_429_then_success_recovers_with_retry_after_each_time():
    clock = FakeClock()
    gw = gateway([e429(7), e429(3), '{"ok": 1}'], clock)
    assert gw.complete(req(), max_requests=6).response is not None
    assert clock.sleeps == [7, 3]


def test_401_still_opens_breaker_and_is_not_retryable():
    clock = FakeClock()
    gw = gateway([ProviderAuthError("401")], clock)
    res = gw.complete(req(), max_requests=6)
    assert res.failure.auth and not res.failure.retryable
    assert gw.breaker.state.value == "OPEN"


def test_5xx_exhaustion_is_retryable_failure_and_counts_toward_breaker():
    clock = FakeClock()
    err = ProviderTransportError("503", retryable=True)
    gw = gateway([err] * 5, clock, transport_retries=2)
    res = gw.complete(req(), max_requests=6)
    assert res.failure.retryable and res.requests_used == 3
    assert gw.breaker.consecutive_failures == 1


def test_non_retryable_transport_error_is_not_retryable():
    gw = gateway([ProviderTransportError("400", retryable=False)], FakeClock())
    assert not gw.complete(req(), max_requests=6).failure.retryable


def test_pacer_spaces_calls_by_60_over_rpm():
    clock = FakeClock()
    pacer = RequestPacer(rpm=30, tpm=0, clock=clock, sleeper=clock)
    gw = gateway(['{"a":1}', '{"a":2}', '{"a":3}'], clock, pacer=pacer)
    for _ in range(3):
        assert gw.complete(req(), max_requests=6).response is not None
    assert clock.sleeps == [2.0, 2.0]  # first call free, then 60/30 s between request starts


def test_pacer_tpm_window_delays_large_request():
    clock = FakeClock()
    pacer = RequestPacer(rpm=0, tpm=1000, clock=clock, sleeper=clock)
    assert pacer.wait(800) == 0
    clock.advance(10)
    waited = pacer.wait(400)  # 800 + 400 > 1000: wait for the first entry to age out of the 60 s window
    assert waited == pytest.approx(50.0)
    assert pacer.wait(100) == 0  # now window holds only 400


def test_pacer_oversized_request_waits_for_empty_window_then_proceeds():
    clock = FakeClock()
    pacer = RequestPacer(rpm=0, tpm=100, clock=clock, sleeper=clock)
    pacer.wait(50)
    assert pacer.wait(500) == pytest.approx(60.0)  # bigger than the whole budget: never blocks forever


def test_pacer_honours_server_reset_hints_and_429_penalty():
    clock = FakeClock()
    pacer = RequestPacer(rpm=1000, tpm=0, clock=clock, sleeper=clock)
    pacer.observe({"rl_remaining_tokens": 10, "rl_reset_tokens_s": 4.0}, estimated_tokens=500)
    assert pacer.wait(500) == pytest.approx(4.0)
    pacer.penalize(9)
    assert pacer.wait(1) == pytest.approx(9.0)


def test_disabled_pacer_never_sleeps():
    clock = FakeClock()
    pacer = RequestPacer(rpm=0, tpm=0, clock=clock, sleeper=clock)
    assert pacer.wait(10_000) == 0 and clock.sleeps == []


def test_pacer_observes_success_metrics_from_provider():
    clock = FakeClock()
    pacer = RequestPacer(rpm=1000, tpm=0, clock=clock, sleeper=clock)
    resp = ProviderResponse(content="{}", metrics={"rl_remaining_requests": 0, "rl_reset_requests_s": 12.0})
    gw = gateway([resp, '{"a":1}'], clock, pacer=pacer)
    gw.complete(req(), max_requests=6)
    gw.complete(req(), max_requests=6)
    assert clock.sleeps[0] == pytest.approx(12.0)


def test_failure_kind_is_transport_for_rate_limit():
    res = gateway([e429(500)], FakeClock()).complete(req(), max_requests=6)
    assert res.failure.kind is FailureKind.TRANSPORT
