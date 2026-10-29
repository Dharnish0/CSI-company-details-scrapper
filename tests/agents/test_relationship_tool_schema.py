"""Runner-level regression: a tools-enabled agent (RelationshipAgent) yields valid `assessments` or a repaired schema."""
from __future__ import annotations

import json

from pydantic import BaseModel, ConfigDict

from corpdisc.agents.base import make_correlation_id
from corpdisc.agents.broker import ToolBroker, ToolSpec
from corpdisc.agents.gateway import CircuitBreaker, GatewayPolicy, LLMGateway
from corpdisc.agents.ports import ToolCallRequest
from corpdisc.agents.providers.fake import FakeLLMProvider, ToolCalls
from corpdisc.agents.relationship import RelationshipAgent
from corpdisc.agents.runner import AgentRunner, RunnerLimits
from corpdisc.contracts.agent_io import ChildProposal, EntityCard, RelationshipInput, RelationshipOutput
from corpdisc.contracts.agents import AgentRequest, CallLimits, Meta
from corpdisc.contracts.common import CandidateKind
from corpdisc.db.repos.audit import SqliteAuditSink, SqliteResponseCache
from corpdisc.services.budget import BudgetGuard, BudgetLimits
from tests.helpers.clock import FakeClock
from tests.helpers.db import make_db, seed_run


class ExcerptArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    page_id: int


def build(tmp_path, script):
    conn = make_db(tmp_path)
    run = seed_run(conn)
    clock = FakeClock()
    provider = FakeLLMProvider(script)
    budget = BudgetGuard(conn, run_id=run, limits=BudgetLimits())
    gw = LLMGateway(provider, model="m", policy=GatewayPolicy(), breaker=CircuitBreaker(5), budget=budget, clock=clock,
                    sleeper=clock)
    audit = SqliteAuditSink(conn)
    broker = ToolBroker([ToolSpec("get_page_excerpt", "excerpt", ExcerptArgs, lambda a: {"text": "x"})],
                        audit=audit, clock=clock)
    runner = AgentRunner(gateway=gw, audit=audit, cache=SqliteResponseCache(conn), budget=budget, broker=broker,
                         limits=RunnerLimits(), clock=clock)
    agent = RelationshipAgent()
    meta = Meta(run_id=run, task_id=None, correlation_id=make_correlation_id(run, None, "t"), agent=agent.name,
                schema_version=agent.schema_version, prompt_version=agent.prompt_version)
    payload = RelationshipInput(parent=EntityCard(name="Parent"),
                                children=[ChildProposal(local_ref="c1", name="Child", kind=CandidateKind.COMPANY)],
                                pages=[])
    req = AgentRequest[RelationshipInput](meta=meta, limits=CallLimits(max_output_tokens=500), payload=payload)
    return runner, agent, req, provider


GOOD = {"assessments": [{"local_ref": "c1", "support": "INSUFFICIENT", "relationship_type": "Unknown",
                         "agent_confidence": 0.1, "reason": "no evidence"}]}
WRONG_KEY = {"results": GOOD["assessments"]}
CALL = ToolCalls((ToolCallRequest("t1", "get_page_excerpt", {"page_id": 1}),))


def test_valid_assessments_pass_without_repair(tmp_path):
    runner, agent, req, provider = build(tmp_path, [json.dumps(GOOD)])
    resp = runner.invoke(agent, req)
    assert resp.status.value == "OK" and isinstance(resp.payload, RelationshipOutput)
    assert provider.call_count == 1
    assert provider.requests[0].tools  # tools offered on the first turn


def test_results_instead_of_assessments_is_repaired_tool_free(tmp_path):
    runner, agent, req, provider = build(tmp_path, [CALL, json.dumps(WRONG_KEY), json.dumps(GOOD)])
    resp = runner.invoke(agent, req)
    assert resp.status.value == "OK", resp.error
    assert resp.payload.assessments[0].local_ref == "c1"
    kinds = [bool(r.tools) for r in provider.requests]
    assert kinds == [True, True, False]  # tool turn, continuation (budget left), repair is tool-free
    assert provider.requests[2].response_schema["required"] == ["assessments"]
    assert "assessments" in str(provider.requests[2].messages[-1].content) or "results" in provider.requests[2].messages[-1].content


def test_continuation_is_tool_free_once_tool_budget_spent(tmp_path):
    two = ToolCalls((ToolCallRequest("a", "get_page_excerpt", {"page_id": 1}),
                     ToolCallRequest("b", "get_page_excerpt", {"page_id": 2})))
    runner, agent, req, provider = build(tmp_path, [two, json.dumps(GOOD)])
    resp = runner.invoke(agent, req)
    assert resp.status.value == "OK"
    assert [bool(r.tools) for r in provider.requests] == [True, False]


def test_persistent_wrong_schema_fails_as_schema_error(tmp_path):
    runner, agent, req, provider = build(tmp_path, [json.dumps(WRONG_KEY), json.dumps(WRONG_KEY)])
    resp = runner.invoke(agent, req)
    assert resp.status.value == "FAILED" and resp.error.kind.value == "SCHEMA"
    assert "assessments" in resp.error.message
