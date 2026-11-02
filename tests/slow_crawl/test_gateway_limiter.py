"""Gateway + limiter: global interval across agents, cache/budget skips, 429 handling, single flight, app wiring."""
from __future__ import annotations

import random
import threading
import time
from datetime import timedelta

import pytest

from corpdisc.agents.gateway import CircuitBreaker, GatewayPolicy, LLMGateway, RequestPacer
from corpdisc.agents.providers.fake import FakeLLMProvider, HandlerProvider
from corpdisc.agents.ports import Message, ProviderRequest, ProviderTransportError
from corpdisc.agents.ratelimit import LimiterConfig, SlowCrawlLimiter
from corpdisc.app import build_runtime, make_limiter
from corpdisc.clock import iso_utc
from corpdisc.config import Settings
from corpdisc.contracts.agents import AgentName, CallStatus, FailureKind
from tests.agents.harness import build
from tests.agents.toy_agents import ToyEchoAgent
from tests.helpers.clock import FakeClock
from tests.helpers.db import T0, make_db, seed_run

OUT = '{"answer": "a", "score": 1.0}'


class ExtractionToy(ToyEchoAgent):
    name = AgentName.EXTRACTION


class WebsiteToy(ToyEchoAgent):
    name = AgentName.WEBSITE


class RelationshipToy(ToyEchoAgent):
    name = AgentName.RELATIONSHIP


def limiter_for(clock, **cfg) -> SlowCrawlLimiter:
    base = dict(interval_s=90.0, tpm_limit=0, tpd_limit=0)
    base.update(cfg)
    return SlowCrawlLimiter(LimiterConfig(**base), clock, clock, rng=random.Random(3))


def stamp(clock, starts):
    def step(_request):
        starts.append(clock.monotonic())
        return OUT
    return step


def req(text="hi", max_out=100) -> ProviderRequest:
    return ProviderRequest(model="m", system="s", messages=(Message("user", text),), response_schema={},
                           schema_name="X", temperature=0.0, max_output_tokens=max_out)


def e429(retry_after=None, detail="per minute"):
    return ProviderTransportError("groq transient error 429", retryable=True, retry_after_s=retry_after,
                                  rate_limited=True, detail=detail)


# ------------------------------------------------------------------ one limiter for every agent
def test_extraction_website_and_relationship_share_one_interval(tmp_path):
    st = build(tmp_path, [])
    starts: list[float] = []
    st.provider.push(*[stamp(st.clock, starts)] * 5)
    st.runner.gateway.limiter = limiter_for(st.clock)
    agents = [ExtractionToy(), WebsiteToy(), RelationshipToy(), ExtractionToy(), WebsiteToy()]
    for i, agent in enumerate(agents):
        resp = st.runner.invoke(agent, st.req(agent, text=f"t{i}"))
        assert resp.status is CallStatus.OK
    gaps = [b - a for a, b in zip(starts, starts[1:], strict=False)]
    assert len(starts) == 5 and all(g >= 90.0 - 1e-6 for g in gaps)
    assert sum(st.clock.sleeps) == pytest.approx(4 * 90.0)
    assert {r["agent"] for r in st.rows()} == {"extraction", "website_resolution", "relationship_verification"}


def test_first_request_is_immediate(tmp_path):
    st = build(tmp_path, [OUT])
    st.runner.gateway.limiter = limiter_for(st.clock)
    st.runner.invoke(ExtractionToy(), st.req(ExtractionToy()))
    assert st.clock.sleeps == []


def test_cache_hits_do_not_touch_the_limiter(tmp_path):
    st = build(tmp_path, [OUT])
    lim = st.runner.gateway.limiter = limiter_for(st.clock)
    a = ExtractionToy()
    assert st.runner.invoke(a, st.req(a, text="same")).status is CallStatus.OK
    before_sleeps, before_requests = list(st.clock.sleeps), lim.requests
    second = st.runner.invoke(a, st.req(a, text="same"))
    assert second.usage.cache_hit and st.provider.call_count == 1
    assert lim.requests == before_requests == 1 and st.clock.sleeps == before_sleeps
    third = st.runner.invoke(a, st.req(a, text="other"))  # a real call still waits one full interval after the first
    assert third.status is not CallStatus.OK or sum(st.clock.sleeps) == pytest.approx(90.0)


def test_budget_skipped_invocations_do_not_touch_the_limiter(tmp_path):
    st = build(tmp_path, [OUT], token_budget=1)
    lim = st.runner.gateway.limiter = limiter_for(st.clock)
    a = ExtractionToy()
    resp = st.runner.invoke(a, st.req(a))
    assert resp.status is CallStatus.SKIPPED and resp.error.kind is FailureKind.BUDGET
    assert lim.requests == 0 and st.provider.call_count == 0 and st.clock.sleeps == []


# ------------------------------------------------------------------ 429
def gateway(script, clock, *, limiter=None, **policy):
    return LLMGateway(FakeLLMProvider(script), model="m", policy=GatewayPolicy(jitter_ratio=0.0, **policy),
                      breaker=CircuitBreaker(3), budget=None, clock=clock, sleeper=clock, limiter=limiter)


def test_429_with_retry_after_waits_then_succeeds_and_widens_the_gap():
    clock = FakeClock()
    starts: list[float] = []
    lim = limiter_for(clock, interval_s=10.0)
    step = stamp(clock, starts)
    gw = gateway([e429(30), step, step], clock, limiter=lim)
    t0 = clock.monotonic()
    res = gw.complete(req(), max_requests=6)
    assert res.response is not None and res.requests_used == 1
    assert 30.0 <= starts[0] - t0 < 33.0  # waited at least Retry-After (plus under 2 s of jitter and the doubled gap)
    snap = lim.snapshot()
    assert snap["multiplier"] == 2.0 and snap["last_429_scope"] == "minute" and lim.rate_limited == 1
    gw.complete(req(), max_requests=6)  # the next request keeps the widened gap (20 s), not the configured 10 s
    assert starts[1] - starts[0] >= 20.0 - 1e-6
    assert gw.breaker.state.value == "CLOSED"


def test_minute_429_without_retry_after_waits_one_window():
    clock = FakeClock()
    lim = limiter_for(clock, interval_s=5.0)
    gw = gateway([e429(None), OUT], clock, limiter=lim)
    t0 = clock.monotonic()
    assert gw.complete(req(), max_requests=6).response is not None
    assert clock.monotonic() - t0 >= 60.0
    assert max(clock.sleeps) <= 30.0  # slept in chunks


def test_day_scope_429_returns_immediately_with_exact_retry_after():
    clock = FakeClock()
    lim = limiter_for(clock)
    detail = "Rate limit reached on tokens per day (TPD): Limit 200000, Used 199000"
    gw = gateway([e429(575.0, detail), OUT], clock, limiter=lim)
    res = gw.complete(req(), max_requests=6)
    assert res.response is None
    f = res.failure
    assert f.quota_scope == "day" and f.rate_limited and f.retryable and f.retry_after_s == 575.0
    assert gw.breaker.consecutive_failures == 0 and clock.sleeps == [] and gw._provider.call_count == 1
    assert res.requests_used == 0 and res.transport_errors == ("RATE_LIMITED",)
    assert lim.cooldown_remaining() >= 575.0  # the next acquire waits the cooldown


def test_day_scope_is_recognised_from_a_long_retry_after_alone():
    clock = FakeClock()
    gw = gateway([e429(900.0, "")], clock, limiter=limiter_for(clock))
    assert gw.complete(req(), max_requests=6).failure.quota_scope == "day"


def test_minute_scope_over_the_ceiling_is_deferred_with_scope_set():
    clock = FakeClock()
    gw = gateway([e429(100.0, "per minute")], clock, limiter=limiter_for(clock), retry_after_ceiling=60)
    res = gw.complete(req(), max_requests=6)
    assert res.failure.quota_scope == "minute" and res.failure.retry_after_s == 100.0


def test_default_gateway_failure_has_no_quota_scope():
    clock = FakeClock()
    res = gateway([e429(600)], clock).complete(req(), max_requests=6)
    assert res.failure.quota_scope is None


# ------------------------------------------------------------------ refusals and slot release
def test_refused_acquire_returns_retryable_transport_failure_without_a_provider_call():
    clock = FakeClock()
    lim = limiter_for(clock)
    lim._stop_check = lambda: "SHUTDOWN"
    gw = gateway([OUT], clock, limiter=lim)
    res = gw.complete(req(), max_requests=6)
    assert res.response is None and res.failure.retryable and res.failure.kind is FailureKind.TRANSPORT
    assert "SHUTDOWN" in res.failure.message and res.requests_used == 0
    assert gw._provider.call_count == 0 and gw.breaker.consecutive_failures == 0


def test_runtime_limit_refuses_the_request():
    clock = FakeClock()
    lim = limiter_for(clock)
    lim.deadline = clock.monotonic() - 1
    res = gateway([OUT], clock, limiter=lim).complete(req(), max_requests=6)
    assert "MAX_RUNTIME" in res.failure.message and res.failure.retryable


def test_slot_is_released_after_every_kind_of_failure():
    clock = FakeClock()
    lim = limiter_for(clock, interval_s=1.0)
    def boom(_req):
        raise RuntimeError("provider bug")

    gw = gateway([ProviderTransportError("400", retryable=False), boom, e429(None, "per minute"), OUT], clock,
                 limiter=lim, rate_limit_retries=0)
    assert gw.complete(req(), max_requests=6).failure is not None
    assert gw.complete(req(), max_requests=6).failure is not None
    assert gw.complete(req(), max_requests=6).failure.rate_limited
    assert gw.complete(req(), max_requests=6).response is not None  # would block forever if a slot leaked
    assert lim.requests == 4


def test_keyboard_interrupt_frees_the_slot_and_propagates():
    clock = FakeClock()
    lim = limiter_for(clock, interval_s=1.0)

    def interrupt(_req):
        raise KeyboardInterrupt

    gw = gateway([interrupt, OUT], clock, limiter=lim)
    with pytest.raises(KeyboardInterrupt):
        gw.complete(req(), max_requests=6)
    assert gw.complete(req(), max_requests=6).response is not None


def test_usage_and_metrics_feed_the_limiter():
    from corpdisc.agents.ports import ProviderResponse, ProviderUsage
    clock = FakeClock()
    lim = limiter_for(clock, interval_s=10.0, tpm_limit=8000)
    resp = ProviderResponse(content=OUT, usage=ProviderUsage(5000, 1000, 0.0),
                            metrics={"rl_limit_tokens": 8000, "rl_remaining_tokens": 200, "rl_reset_tokens_s": 77.0})
    gw = gateway([resp, OUT], clock, limiter=lim)
    gw.complete(req(), max_requests=6)
    assert lim.snapshot()["ewma_tokens"] == 6000 and lim.snapshot()["limit_tokens"] == 8000
    t0 = clock.monotonic()
    gw.complete(req(), max_requests=6)
    assert clock.monotonic() - t0 >= 77.0 - 1e-6


# ------------------------------------------------------------------ concurrency
def test_four_threads_never_exceed_one_request_in_flight():
    inflight, peak, lock = [0], [0], threading.Lock()

    def handler(_request):
        with lock:
            inflight[0] += 1
            peak[0] = max(peak[0], inflight[0])
        time.sleep(0.02)
        with lock:
            inflight[0] -= 1
        return OUT

    clock = FakeClock()
    lim = limiter_for(clock, interval_s=0.0)
    gw = LLMGateway(HandlerProvider(handler), model="m", policy=GatewayPolicy(), breaker=CircuitBreaker(5),
                    budget=None, clock=clock, sleeper=clock, limiter=lim)
    results = []

    def worker():
        for _ in range(3):
            results.append(gw.complete(req(), max_requests=6))

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert len(results) == 12 and all(r.response is not None for r in results)
    assert peak[0] == 1 and lim.requests == 12


# ------------------------------------------------------------------ composition root
def settings(tmp_path, **kw) -> Settings:
    return Settings(_env_file=None, db_path=tmp_path / "x.db", cache_dir=tmp_path / ".cache", log_dir=tmp_path / "l",
                    use_playwright="false", llm_provider="groq", groq_model="m", **kw)


class _NoHttp:
    def get(self, *a, **k):
        raise AssertionError("no network in this test")

    def close(self):
        pass


def test_default_runtime_has_no_limiter_and_keeps_the_request_pacer(tmp_path):
    rt = build_runtime(settings(tmp_path), provider=FakeLLMProvider(), http=_NoHttp(), transport=object())
    try:
        gw = rt.deps.runner.gateway
        assert gw.limiter is None and rt.limiter is None
        assert isinstance(gw.pacer, RequestPacer) and (gw.pacer.rpm, gw.pacer.tpm) == (30, 0)
    finally:
        rt.close()


def test_slow_runtime_builds_the_limiter_and_drops_the_pacer(tmp_path):
    clock = FakeClock()
    rt = build_runtime(settings(tmp_path, slow_crawl=True, slow_crawl_interval_seconds=45, llm_rpm_limit=600,
                                llm_tpm_limit=200000),
                       provider=FakeLLMProvider(), http=_NoHttp(), transport=object(), clock=clock, sleeper=clock,
                       stop_check=lambda: None)
    try:
        gw = rt.deps.runner.gateway
        assert gw.pacer is None and gw.limiter is rt.limiter and rt.limiter is not None
        assert rt.limiter.cfg.interval_s == 45
        assert rt.limiter.deadline == pytest.approx(clock.monotonic() + 24 * 3600)
    finally:
        rt.close()


def test_explicit_deadline_and_stop_check_reach_the_limiter(tmp_path):
    clock = FakeClock()
    rt = build_runtime(settings(tmp_path, slow_crawl=True), provider=FakeLLMProvider(), http=_NoHttp(),
                       transport=object(), clock=clock, sleeper=clock, stop_check=lambda: "SHUTDOWN", deadline=5.0)
    try:
        assert rt.limiter.deadline == 5.0 and rt.limiter.acquire(1, 1).reason == "SHUTDOWN"
    finally:
        rt.close()


def test_limiter_seed_replays_the_last_24_hours_of_recorded_tokens(tmp_path):
    conn = make_db(tmp_path)
    run = seed_run(conn)

    def row(age_h, tin, tout, cache_hit=0):
        conn.execute(
            "INSERT INTO agent_invocations(run_id, agent, correlation_id, schema_version, prompt_version, input_hash, "
            "request_json, status, cache_hit, tokens_in, tokens_out, started_at) "
            "VALUES (?, 'extraction', 'c', 'v', 'p', 'h', '{}', 'OK', ?, ?, ?, ?)",
            (run, cache_hit, tin, tout, iso_utc(T0 - timedelta(hours=age_h))))

    row(1, 80_000, 20_000)       # counted
    row(30, 150_000, 40_000)     # older than 24 h: ignored
    row(0.5, 70_000, 0, cache_hit=1)  # a cache hit spends nothing
    conn.commit()
    clock = FakeClock(T0)
    s = Settings(_env_file=None, slow_crawl=True, slow_crawl_tpd_limit=200_000)
    lim = make_limiter(s, conn, clock, clock)
    expected = 200_000 - 100_000 + 3600 * 200_000 / 86400
    assert lim.bucket.level_at(clock.monotonic()) == pytest.approx(expected, abs=2.0)
    conn.close()


def test_limiter_seed_is_skipped_when_the_daily_estimate_is_off(tmp_path):
    conn = make_db(tmp_path)
    clock = FakeClock(T0)
    lim = make_limiter(Settings(_env_file=None, slow_crawl=True, slow_crawl_tpd_limit=0), conn, clock, clock)
    assert not lim.bucket.enabled
    conn.close()
