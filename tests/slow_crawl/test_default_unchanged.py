"""Characterization of the default (non-slow) crawl: values observed before any slow-crawl code existed.

This file must stay green at every slow-crawl commit.
"""
from __future__ import annotations

from corpdisc.agents.gateway import RequestPacer
from corpdisc.agents.ports import Message, ProviderRequest, ProviderTransportError
from corpdisc.app import make_pacer
from corpdisc.config import Settings
from tests.fixtures.world import seeded_world
from tests.helpers.clock import FakeClock
from tests.unit.test_gateway_ratelimit import gateway


def _req() -> ProviderRequest:
    return ProviderRequest(model="m", system="s", messages=(Message("user", "hi"),), response_schema={},
                           schema_name="X", temperature=0.0, max_output_tokens=100)


def _e429(retry_after):
    return ProviderTransportError("429", retryable=True, retry_after_s=retry_after, rate_limited=True)


def test_retry_after_45_then_success_sleeps_exactly_45():
    clock = FakeClock()
    gw = gateway([_e429(45), '{"ok": 1}'], clock)
    res = gw.complete(_req(), max_requests=6)
    assert res.response is not None and res.failure is None
    assert clock.sleeps == [45]
    assert res.requests_used == 1
    assert res.transport_errors == ("RATE_LIMITED",)
    assert gw.breaker.state.value == "CLOSED" and gw.breaker.consecutive_failures == 0
    assert gw.pacer is None and getattr(gw, "limiter", None) is None  # no slow-crawl limiter in default mode


def test_retry_after_600_reschedules_and_pauses_with_retry_waiting(tmp_path):
    w = seeded_world(tmp_path)
    state = {"n": 0}

    def m(name, out):
        if name == "RelationshipOutput":
            state["n"] += 1
            raise ProviderTransportError("429", retryable=True, rate_limited=True, retry_after_s=600)
        return out

    w.brain.mutators.append(m)
    s = w.sup.run()
    assert s.status == "PAUSED" and s.stopped_reason == "RETRY_WAITING"
    assert s.retries == 1
    assert state["n"] == 1  # Retry-After above the 120 s ceiling: no in-call retry
    assert w.clock.sleeps == []
    task = w.q("SELECT status, attempts, not_before, error FROM research_queue WHERE task_id=1")[0]
    assert task["status"] == "PENDING" and task["attempts"] == 0
    assert task["not_before"] == "2025-01-01T12:10:00.000000+00:00"
    assert task["error"] == "VERIFICATION_RETRY_PENDING (attempt 1/3, rate limited, deferral 1/6)"
    assert s.queue_detail == {"failed": 0, "partial": 0, "waiting": 1, "done_clean": 0, "retryable_partial": 0}
    assert dict(s.outcomes) == {"RETRY": 1}
    assert w.q("SELECT status FROM runs")[0][0] == "PAUSED"
    inv = w.q("SELECT status, failure_kind FROM agent_invocations WHERE failure_kind IS NOT NULL")
    assert [(r["status"], r["failure_kind"]) for r in inv] == [("FAILED", "TRANSPORT")]
    assert w.deps.runner.gateway.breaker.state.value == "CLOSED"


def test_groq_default_pacer_is_30_rpm_and_no_token_limit():
    clock = FakeClock()
    pacer = make_pacer(Settings(_env_file=None), "groq", clock, clock)
    assert isinstance(pacer, RequestPacer) and (pacer.rpm, pacer.tpm) == (30, 0)
    assert make_pacer(Settings(_env_file=None), "openai", clock, clock) is None
