"""Wire the real M2 stack (gateway, runner, broker, SQLite audit/cache, BudgetGuard) on a tmp DB."""
from __future__ import annotations

from dataclasses import dataclass

from corpdisc.agents.base import make_correlation_id
from corpdisc.agents.broker import ToolBroker, ToolSpec
from corpdisc.agents.gateway import CircuitBreaker, GatewayPolicy, LLMGateway
from corpdisc.agents.providers.fake import FakeLLMProvider
from corpdisc.agents.runner import AgentRunner, RunnerLimits
from corpdisc.contracts.agents import AgentRequest, CallLimits, Meta
from corpdisc.db.repos.audit import SqliteAuditSink, SqliteResponseCache
from corpdisc.services.budget import BudgetGuard, BudgetLimits
from tests.agents.toy_agents import EchoIn, LookupArgs
from tests.helpers.clock import FakeClock
from tests.helpers.db import make_db, seed_run


@dataclass
class Stack:
    conn: object
    run_id: int
    provider: FakeLLMProvider
    clock: FakeClock
    breaker: CircuitBreaker
    budget: BudgetGuard
    runner: AgentRunner

    def req(self, agent, text="hello", task_id=None):
        meta = Meta(run_id=self.run_id, task_id=task_id, correlation_id=make_correlation_id(self.run_id, task_id, "t"),
                    agent=agent.name, schema_version=agent.schema_version, prompt_version=agent.prompt_version)
        return AgentRequest[EchoIn](meta=meta, limits=CallLimits(max_output_tokens=200), payload=EchoIn(text=text))

    def rows(self):
        return self.conn.execute("SELECT * FROM agent_invocations ORDER BY invocation_id").fetchall()


def build(tmp_path, script=(), *, token_budget=None, limits=None, max_requests=6, cache=True, threshold=5):
    conn = make_db(tmp_path)
    run = seed_run(conn, token_budget=token_budget)
    clock = FakeClock()
    provider = FakeLLMProvider(script)
    budget = BudgetGuard(conn, run_id=run, limits=limits or BudgetLimits())
    breaker = CircuitBreaker(threshold)
    gw = LLMGateway(provider, model="m", policy=GatewayPolicy(), breaker=breaker, budget=budget, clock=clock,
                    sleeper=clock)
    audit = SqliteAuditSink(conn)
    broker = ToolBroker([ToolSpec("lookup", "number lookup", LookupArgs, lambda a: {"value": a.n * 2})],
                        audit=audit, clock=clock)
    runner = AgentRunner(gateway=gw, audit=audit, cache=SqliteResponseCache(conn) if cache else None, budget=budget,
                         broker=broker, limits=RunnerLimits(max_provider_requests=max_requests), clock=clock)
    return Stack(conn, run, provider, clock, breaker, budget, runner)
