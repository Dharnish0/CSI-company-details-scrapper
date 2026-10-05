"""M2 agent infrastructure on a real SQLite DB: runner, gateway, breaker, broker, providers, audit/cache."""
from __future__ import annotations

import pytest

from corpdisc.agents.base import AgentContractError, canonical_json, make_correlation_id
from corpdisc.agents.gateway import BreakerState, CircuitBreaker
from corpdisc.agents.ports import ProviderAuthError, ProviderNotImplementedError, ProviderTransportError
from corpdisc.agents.ports import ToolCallRequest
from corpdisc.agents.providers import build_provider
from corpdisc.agents.providers.cassette import RecordingProvider, ReplayProvider
from corpdisc.agents.providers.fake import FakeLLMProvider, ToolCalls
from corpdisc.contracts.agents import CallStatus, FailureKind
from corpdisc.services.budget import BudgetLimits
from tests.agents.harness import build
from tests.agents.toy_agents import ToyEchoAgent, ToyToolAgent
from tests.helpers.db import count

OK = {"answer": "hi", "score": 0.5}
ECHO = ToyEchoAgent()


def test_ok_first_try_audited_and_tokens_recorded(tmp_path):
    s = build(tmp_path, [OK])
    r = s.runner.invoke(ECHO, s.req(ECHO))
    assert r.status is CallStatus.OK and r.payload.answer == "hi" and r.usage.tokens_in == 100
    (row,) = s.rows()
    assert row["status"] == "OK" and row["finished_at"] is not None and row["attempt"] == 1
    assert s.conn.execute("SELECT llm_tokens_used FROM runs").fetchone()[0] == 150
    assert count(s.conn, "llm_calls") == 0  # D1


def test_one_schema_repair_then_ok_and_no_third_ask(tmp_path):
    s = build(tmp_path, ["not json", OK])
    r = s.runner.invoke(ECHO, s.req(ECHO))
    assert r.status is CallStatus.OK and r.meta.attempt == 2
    rows = s.rows()
    assert [x["status"] for x in rows] == ["FAILED", "OK"] and rows[0]["failure_kind"] == "SCHEMA"
    assert len({x["correlation_id"] for x in rows}) == 1
    repair = s.provider.requests[1].messages[-1].content
    assert "hello" not in repair and "schema" in repair  # errors only, no input echo
    s2 = build(tmp_path / "b", ["bad", "worse", OK])
    r2 = s2.runner.invoke(ECHO, s2.req(ECHO))
    assert r2.status is CallStatus.FAILED and r2.error.kind is FailureKind.SCHEMA and s2.provider.call_count == 2


def test_cache_hit_and_invalid_never_cached(tmp_path):
    s = build(tmp_path, [OK])
    s.runner.invoke(ECHO, s.req(ECHO))
    again = s.runner.invoke(ECHO, s.req(ECHO))
    assert again.usage.cache_hit and s.provider.call_count == 1 and s.rows()[-1]["cache_hit"] == 1
    s.provider.push(OK)
    assert not s.runner.invoke(ECHO, s.req(ECHO, text="other")).usage.cache_hit  # input changes the key
    bad = build(tmp_path / "bad", ["x", "y", OK])
    bad.runner.invoke(ECHO, bad.req(ECHO))
    assert not bad.runner.invoke(ECHO, bad.req(ECHO)).usage.cache_hit  # failed outputs are never served


def test_budget_exhausted_skips_without_provider_call(tmp_path):
    s = build(tmp_path, [OK], token_budget=10)
    r = s.runner.invoke(ECHO, s.req(ECHO))
    assert r.status is CallStatus.SKIPPED and r.error.kind is FailureKind.BUDGET and s.provider.call_count == 0
    assert s.rows()[0]["status"] == "SKIPPED" and s.rows()[0]["failure_kind"] == "BUDGET"


def test_task_llm_call_cap(tmp_path):
    s = build(tmp_path, [OK, {"answer": "b", "score": 1}], limits=BudgetLimits(max_llm_calls_per_task=1))
    s.conn.execute("INSERT INTO entities(run_id,name,normalized_name,entity_type,verification_status,discovery_seq,"
                   "discovered_at) VALUES (?,?,?,?,?,?,?)", (s.run_id, "R", "r", "Parent Company", "CONFIRMED", 1, "t"))
    s.conn.execute("INSERT INTO research_queue(run_id,entity_id,depth,priority,status,enqueued_at) "
                   "VALUES (?,1,0,1,'RUNNING','t')", (s.run_id,))
    s.conn.commit()
    assert s.runner.invoke(ECHO, s.req(ECHO, task_id=1)).status is CallStatus.OK
    second = s.runner.invoke(ECHO, s.req(ECHO, text="new", task_id=1))
    assert second.status is CallStatus.SKIPPED and "TASK_LLM_CALL_LIMIT" in second.error.message


def test_transport_retries_backoff_then_breaker(tmp_path):
    flaky = ProviderTransportError("503", retryable=True)
    s = build(tmp_path, [flaky, flaky, OK])
    assert s.runner.invoke(ECHO, s.req(ECHO)).status is CallStatus.OK
    assert len(s.clock.sleeps) == 2 and 0.75 <= s.clock.sleeps[0] <= 1.25 and 1.5 <= s.clock.sleeps[1] <= 2.5
    dead = build(tmp_path / "d", [flaky] * 40, threshold=2)
    for i in range(2):
        r = dead.runner.invoke(ECHO, dead.req(ECHO, text=str(i)))
        assert r.error.kind is FailureKind.TRANSPORT
    assert dead.breaker.state is BreakerState.OPEN
    calls = dead.provider.call_count
    blocked = dead.runner.invoke(ECHO, dead.req(ECHO, text="z"))
    assert blocked.error.kind is FailureKind.CIRCUIT_OPEN and dead.provider.call_count == calls


def test_auth_error_opens_breaker_immediately_and_half_open_probe():
    b = CircuitBreaker(5)
    b.record_failure(auth=True)
    assert b.state is BreakerState.OPEN and not b.allow_request()
    b.half_open()
    assert b.allow_request() and not b.allow_request()  # exactly one probe
    b.record_success()
    assert b.state is BreakerState.CLOSED


def test_max_provider_requests_cap(tmp_path):
    flaky = ProviderTransportError("t", retryable=True)
    s = build(tmp_path, [flaky, flaky, "bad", flaky, "bad", OK], max_requests=3)
    r = s.runner.invoke(ECHO, s.req(ECHO))
    assert r.status is CallStatus.FAILED and s.provider.call_count <= 3


def test_tool_loop_allowed_denied_and_capped(tmp_path):
    agent = ToyToolAgent()
    calls = ToolCalls((ToolCallRequest("1", "lookup", {"n": 2}), ToolCallRequest("2", "secret", {}),
                       ToolCallRequest("3", "lookup", {"n": "x"})))
    s = build(tmp_path, [calls, ToolCalls((ToolCallRequest("4", "lookup", {"n": 1}),)), OK])
    r = s.runner.invoke(agent, s.req(agent))
    assert r.status is CallStatus.OK and r.usage.tool_calls == 4
    rows = s.conn.execute("SELECT tool, allowed, result_summary FROM agent_tool_calls ORDER BY tool_call_id").fetchall()
    summaries = [x["result_summary"].split(":")[0] + ":" + x["result_summary"].split(":")[1].strip()
                 for x in rows]
    assert summaries[0].startswith("OK") and rows[1]["allowed"] == 0 and "INVALID_ARGS" in rows[2]["result_summary"]
    assert rows[3]["result_summary"] == "DENIED:CAP_EXCEEDED"  # per-tool cap 2
    tool_msg = s.provider.requests[1].messages[-1]
    assert tool_msg.role == "tool" and '"value":4' in tool_msg.content
    assert [x["status"] for x in s.rows()] == ["PARTIAL", "PARTIAL", "OK"]


def test_contract_errors_raise_and_unexpected_errors_do_not(tmp_path):
    s = build(tmp_path, [lambda req: (_ for _ in ()).throw(ZeroDivisionError())])
    with pytest.raises(AgentContractError):
        s.runner.invoke(ToyToolAgent(), s.req(ECHO))
    r = s.runner.invoke(ECHO, s.req(ECHO))
    assert r.status is CallStatus.FAILED and r.error.kind is FailureKind.TRANSPORT


def test_record_then_replay_round_trip_and_replay_miss_fails_closed(tmp_path):
    cassette = tmp_path / "c.jsonl"
    s = build(tmp_path / "a", [])
    s.runner._gw._provider = RecordingProvider(FakeLLMProvider([OK]), cassette)
    first = s.runner.invoke(ECHO, s.req(ECHO))
    s2 = build(tmp_path / "b", [], cache=False)
    s2.runner._gw._provider = ReplayProvider(cassette)
    assert s2.runner.invoke(ECHO, s2.req(ECHO)).payload == first.payload
    miss = s2.runner.invoke(ECHO, s2.req(ECHO, text="never recorded"))
    assert miss.status is CallStatus.FAILED and "cassette miss" in miss.error.message


def test_provider_factory_refuses_real_providers(tmp_path):
    assert build_provider("fake").name == "fake"
    for name in ("openai", "anthropic", "gemini", "google"):
        with pytest.raises(ProviderNotImplementedError):
            build_provider(name)
    with pytest.raises(ValueError):
        build_provider("replay")
    with pytest.raises(ValueError):
        build_provider("")
    assert isinstance(ProviderAuthError("x"), Exception)


def test_helpers():
    assert make_correlation_id(3, None, "extract") == "3:-:extract"
    assert canonical_json({"b": 1, "a": 2}) == canonical_json({"a": 2, "b": 1}) == '{"a":2,"b":1}'
