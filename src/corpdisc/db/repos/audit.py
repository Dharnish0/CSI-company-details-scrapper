"""SQLite audit sink and response cache for the agent layer (plan 4.M2.3, D1, D7, P6).

The only writer of agent_invocations and agent_tool_calls. db/ never imports agents/: these classes
satisfy the agents.ports protocols structurally. llm_calls is never written (D1).
"""

from __future__ import annotations

import sqlite3

from corpdisc.clock import iso_utc
from corpdisc.contracts.agents import AgentName, CachedResponse, InvocationFinish, InvocationStart, ToolCallRecord


class SqliteAuditSink:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def start_invocation(self, rec: InvocationStart) -> int:
        """Inserted fail-closed: status FAILED, finished_at NULL until finish_invocation (P6)."""
        with self._conn:
            cur = self._conn.execute(
                "INSERT INTO agent_invocations (run_id, task_id, agent, correlation_id, schema_version, prompt_version, "
                "attempt, input_hash, request_json, status, started_at) VALUES (?,?,?,?,?,?,?,?,?,'FAILED',?)",
                (rec.run_id, rec.task_id, rec.agent.value, rec.correlation_id, rec.schema_version, rec.prompt_version,
                 rec.attempt, rec.input_hash, rec.request_json, iso_utc(rec.started_at)),
            )
        return int(cur.lastrowid)

    def finish_invocation(self, invocation_id: int, rec: InvocationFinish) -> None:
        with self._conn:
            self._conn.execute(
                "UPDATE agent_invocations SET status=?, failure_kind=?, response_json=?, cache_hit=?, tokens_in=?, "
                "tokens_out=?, cost_usd=?, latency_ms=?, finished_at=? WHERE invocation_id=?",
                (rec.status.value, rec.failure_kind.value if rec.failure_kind else None, rec.response_json,
                 int(rec.cache_hit), rec.tokens_in, rec.tokens_out, rec.cost_usd, rec.latency_ms,
                 iso_utc(rec.finished_at), invocation_id),
            )

    def record_tool_call(self, rec: ToolCallRecord) -> int:
        with self._conn:
            cur = self._conn.execute(
                "INSERT INTO agent_tool_calls (invocation_id, tool, args_json, allowed, result_summary, duration_ms, "
                "created_at) VALUES (?,?,?,?,?,?,?)",
                (rec.invocation_id, rec.tool, rec.args_json, int(rec.allowed), rec.result_summary, rec.duration_ms,
                 iso_utc(rec.created_at)),
            )
        return int(cur.lastrowid)


class SqliteResponseCache:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def lookup(self, *, agent: AgentName, schema_version: str, prompt_version: str,
               input_hash: str) -> CachedResponse | None:
        row = self._conn.execute(
            "SELECT invocation_id, response_json, tokens_in, tokens_out FROM agent_invocations "
            "WHERE agent = ? AND schema_version = ? AND prompt_version = ? AND input_hash = ? AND status = 'OK' "
            "AND cache_hit = 0 AND response_json IS NOT NULL ORDER BY invocation_id DESC LIMIT 1",
            (agent.value, schema_version, prompt_version, input_hash),
        ).fetchone()
        if row is None:
            return None
        return CachedResponse(invocation_id=row[0], response_json=row[1], tokens_in=row[2], tokens_out=row[3])
