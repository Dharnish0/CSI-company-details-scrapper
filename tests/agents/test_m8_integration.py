"""M8: the full pipeline through the REAL provider adapters, with an HTTP-level fake standing in for the LLM API."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from corpdisc.agents.broker import ToolBroker, ToolSpec
from corpdisc.agents.gateway import BreakerState
from corpdisc.agents.ports import ToolCallRequest
from corpdisc.agents.providers.anthropic import AnthropicProvider
from corpdisc.agents.providers.http_common import JSON_ONLY
from corpdisc.agents.providers.openai_compat import OpenAICompatProvider
from corpdisc.contracts.agents import CallStatus, FailureKind, InvocationStart
from corpdisc.db.repos.audit import SqliteAuditSink
from corpdisc.graphs.tools import PageExcerptArgs
from corpdisc.services.budget import BudgetGuard, BudgetLimits
from corpdisc.transport import HttpReply, TransportFailure
from tests.agents.harness import build
from tests.agents.toy_agents import ToyEchoAgent
from tests.fixtures.site import ROOT, Brain
from tests.fixtures.world import build_world, seeded_world
from tests.helpers.clock import FakeClock
from tests.helpers.db import make_db, seed_run

KEY = "sk-test-SECRETSECRET123456"
ECHO = ToyEchoAgent()


class BrainTransport:
    """Speaks the Anthropic or OpenAI wire format; answers using the fixture Brain. ``mode`` can simulate outages."""

    def __init__(self, brain: Brain, flavor: str) -> None:
        self.brain, self.flavor, self.calls, self.mode = brain, flavor, [], "ok"

    def post(self, url, *, headers, body, timeout):
        b = json.loads(body)
        self.calls.append({"url": url, "headers": dict(headers), "timeout": timeout})
        if self.mode == "auth":
            return HttpReply(401, {}, json.dumps({"error": {"message": "invalid x-api-key"}}))
        if self.mode == "timeout":
            raise TransportFailure("TIMEOUT")
        if self.flavor == "anthropic":
            name = json.loads(b["system"].split(JSON_ONLY, 1)[1])["title"]
            user = b["messages"][0]["content"]
        else:
            name = b["response_format"]["json_schema"]["name"]
            user = next(m for m in b["messages"] if m["role"] == "user")["content"]
        out = self.brain(SimpleNamespace(schema_name=name, messages=[SimpleNamespace(content=user)]))
        text = out if isinstance(out, str) else json.dumps(out)
        if self.flavor == "anthropic":
            data = {"model": "claude-x", "content": [{"type": "text", "text": text}], "usage": {"input_tokens": 120, "output_tokens": 60}}
        else:
            data = {"model": "gpt-x", "choices": [{"message": {"role": "assistant", "content": text}}],
                    "usage": {"prompt_tokens": 120, "completion_tokens": 60}}
        return HttpReply(200, {}, json.dumps(data))


def adapter(flavor: str, transport: BrainTransport, **prices):
    cls = AnthropicProvider if flavor == "anthropic" else OpenAICompatProvider
    return cls(api_key=KEY, transport=transport, **prices)


def snapshot(w):
    return (w.names(), w.names("CONFIRMED"), {tuple(r) for r in w.q(
        "SELECT p.name, c.name, r.verification_status FROM relationships r JOIN entities p ON p.entity_id=r.parent_id "
        "JOIN entities c ON c.entity_id=r.child_id")})


@pytest.fixture
def baseline(tmp_path):
    w = seeded_world(tmp_path / "base")
    w.sup.run()
    return snapshot(w)


@pytest.mark.parametrize("flavor", ["anthropic", "openai"])
def test_full_hierarchy_through_the_real_adapter_matches_the_baseline(tmp_path, baseline, flavor):
    t = BrainTransport(Brain(), flavor)
    w = build_world(tmp_path / flavor)
    w.deps.runner.gateway._provider = adapter(flavor, t, price_in_per_mtok=3.0, price_out_per_mtok=15.0)
    w.sup.seed_root()
    s = w.sup.run()
    assert s.status == "COMPLETED" and snapshot(w) == baseline  # same entities, same confirmed set, same edges
    assert t.calls and all(KEY in str(c["headers"].values()) for c in t.calls)
    run = w.conn.execute("SELECT llm_tokens_used, llm_cost_usd FROM runs").fetchone()
    assert run[0] == 180 * len(t.calls) and run[1] == pytest.approx((120 * 3 + 60 * 15) / 1e6 * len(t.calls))
    blob = " ".join(str(v) for row in w.conn.execute("SELECT * FROM agent_invocations") for v in tuple(row))
    assert KEY not in blob and w.q("SELECT 1 FROM agent_invocations WHERE status='OK'")


def test_every_provider_attempt_is_audited_and_repeated_work_is_served_from_cache(tmp_path):
    t = BrainTransport(Brain(), "anthropic")
    w = build_world(tmp_path)
    w.deps.runner.gateway._provider = adapter("anthropic", t)
    w.sup.seed_root()
    w.sup.run()
    rows = w.q("SELECT status, cache_hit, finished_at, input_hash, correlation_id FROM agent_invocations")
    assert rows and all(r["finished_at"] is not None and r["input_hash"] and r["correlation_id"] for r in rows)  # no dangling rows
    assert sum(1 for r in rows if r["cache_hit"] == 0 and r["status"] == "OK") == len(t.calls)  # 1 row per provider call
    before = len(t.calls)
    w.conn.execute("UPDATE research_queue SET status='PENDING' WHERE task_id=1")  # re-run the root task
    w.conn.commit()
    w.sup.run(max_tasks=1)
    assert len(t.calls) == before  # identical inputs: zero new provider calls
    assert w.q("SELECT 1 FROM agent_invocations WHERE cache_hit=1 AND agent='extraction'")


# ------------------------------------------------------------------ timeouts, retries, breaker, resume
def wire(tmp_path, transport, **kw):
    s = build(tmp_path, [], **kw)
    s.runner.gateway._provider = adapter("anthropic", transport)
    return s


def test_timeouts_are_retried_with_backoff_then_the_breaker_opens(tmp_path):
    t = BrainTransport(Brain(), "anthropic")
    t.mode = "timeout"
    s = wire(tmp_path, t, threshold=5)
    for i in range(5):
        r = s.runner.invoke(ECHO, s.req(ECHO, text=str(i)))
        assert r.status is CallStatus.FAILED and r.error.kind is FailureKind.TRANSPORT
    assert len(t.calls) == 5 * 4  # 1 attempt + 3 transport retries each, never more
    assert len(s.clock.sleeps) == 5 * 3 and all(0 < x <= 30 for x in s.clock.sleeps)
    assert s.breaker.state is BreakerState.OPEN
    blocked = s.runner.invoke(ECHO, s.req(ECHO, text="late"))
    assert blocked.error.kind is FailureKind.CIRCUIT_OPEN and len(t.calls) == 20  # no traffic once open


def test_provider_request_cap_bounds_one_invocation(tmp_path):
    t = BrainTransport(Brain(), "anthropic")
    t.mode = "timeout"
    s = wire(tmp_path, t, max_requests=2)
    s.runner.invoke(ECHO, s.req(ECHO))
    assert len(t.calls) <= 2


def test_supervisor_pauses_on_auth_failure_and_resumes_after_the_key_is_fixed(tmp_path, baseline):
    t = BrainTransport(Brain(), "openai")
    t.mode = "auth"
    w = build_world(tmp_path)
    w.deps.runner.gateway._provider = adapter("openai", t)
    w.sup.seed_root()
    s = w.sup.run()
    assert s.status == "PAUSED" and s.stopped_reason == "CIRCUIT_OPEN" and len(t.calls) == 1  # one 401 and then silence
    assert w.q("SELECT status FROM research_queue WHERE task_id=1")[0][0] == "PENDING"  # released, not failed
    t.mode = "ok"
    final = w.sup.resume()
    assert final.status == "COMPLETED" and snapshot(w) == baseline


# ------------------------------------------------------------------ tool budget through the real broker
def test_tool_calls_are_limited_by_the_run_budget(tmp_path):
    conn = make_db(tmp_path)
    run = seed_run(conn)
    clock = FakeClock()
    audit = SqliteAuditSink(conn)
    budget = BudgetGuard(conn, run_id=run, limits=BudgetLimits(max_tool_calls_per_run=1))
    spec = ToolSpec("get_page_excerpt", "read", PageExcerptArgs, lambda a: {"ok": True})
    broker = ToolBroker([spec], audit=audit, clock=clock, budget=budget)
    inv = audit.start_invocation(InvocationStart(run_id=run, task_id=None, agent=ECHO.name, correlation_id="c", schema_version="1",
                                                 prompt_version="1", attempt=1, input_hash="h", request_json="{}", started_at=clock.now()))
    session = broker.new_session(SimpleNamespace(name=ECHO.name, allowed_tools={"get_page_excerpt": 5}, max_tool_calls=5))
    first = broker.call(session, inv, ToolCallRequest("1", "get_page_excerpt", {"page_id": 1}))
    second = broker.call(session, inv, ToolCallRequest("2", "get_page_excerpt", {"page_id": 1}))
    assert first.ok and not second.ok
    rows = conn.execute("SELECT allowed, result_summary FROM agent_tool_calls ORDER BY tool_call_id").fetchall()
    assert [r[0] for r in rows] == [1, 0] and rows[1][1].startswith("DENIED:BUDGET")
