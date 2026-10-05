"""Read-only calibration report (ARCHITECTURE 28.2 M8): calls, cost, cache rate, review ratio, gate and tool use.

Everything is derived from the database, so the same report works for a fixture run, a resumed run or a live run.
It never reads page text or prompts and contains no secrets.
"""

from __future__ import annotations

import sqlite3
from typing import Any

_PROVIDER_CALL = ("cache_hit = 0 AND status <> 'SKIPPED' AND (failure_kind IS NULL OR failure_kind NOT IN ('BUDGET','CIRCUIT_OPEN'))")


def _rows(conn: sqlite3.Connection, sql: str, *params: Any) -> list[sqlite3.Row]:
    return conn.execute(sql, params).fetchall()


def _map(conn: sqlite3.Connection, sql: str, *params: Any) -> dict[str, int]:
    return {str(r[0]): int(r[1]) for r in _rows(conn, sql, *params)}


def build_report(conn: sqlite3.Connection, run_id: int) -> dict[str, Any]:
    run = _rows(conn, "SELECT * FROM runs WHERE run_id=?", run_id)
    if not run:
        raise LookupError(f"unknown run {run_id}")
    run = run[0]
    ents = _map(conn, "SELECT verification_status, COUNT(*) FROM entities WHERE run_id=? AND merged_into_id IS NULL GROUP BY 1", run_id)
    root_id = _rows(conn, "SELECT entity_id FROM entities WHERE run_id=? ORDER BY discovery_seq LIMIT 1", run_id)
    non_root = max(sum(ents.values()) - (1 if root_id else 0), 0)
    confirmed_non_root = max(ents.get("CONFIRMED", 0) - (1 if root_id else 0), 0)
    review = ents.get("REVIEW_REQUIRED", 0)

    per_agent: dict[str, dict[str, Any]] = {}
    tot_calls = tot_hits = 0
    for r in _rows(conn, f"""SELECT agent,
        SUM(CASE WHEN {_PROVIDER_CALL} THEN 1 ELSE 0 END) calls, SUM(cache_hit) hits,
        SUM(CASE WHEN failure_kind='SCHEMA' THEN 1 ELSE 0 END) schema_failures,
        SUM(CASE WHEN failure_kind='TRANSPORT' THEN 1 ELSE 0 END) transport_failures,
        SUM(CASE WHEN status='SKIPPED' THEN 1 ELSE 0 END) skipped,
        COALESCE(SUM(tokens_in),0) tin, COALESCE(SUM(tokens_out),0) tout, COALESCE(SUM(cost_usd),0) cost,
        AVG(latency_ms) latency FROM agent_invocations WHERE run_id=? GROUP BY agent""", run_id):
        calls, hits = int(r["calls"]), int(r["hits"])
        tot_calls, tot_hits = tot_calls + calls, tot_hits + hits
        per_agent[r["agent"]] = {
            "provider_calls": calls, "cache_hits": hits, "schema_failures": int(r["schema_failures"]),
            "transport_failures": int(r["transport_failures"]), "skipped_by_budget_or_circuit": int(r["skipped"]),
            "tokens_in": int(r["tin"]), "tokens_out": int(r["tout"]), "cost_usd": round(float(r["cost"]), 6),
            "avg_latency_ms": None if r["latency"] is None else round(float(r["latency"]), 1)}
    tasks = _map(conn, "SELECT status, COUNT(*) FROM research_queue WHERE run_id=? GROUP BY 1", run_id)
    n_tasks = max(sum(tasks.values()), 1)
    tokens = int(run["llm_tokens_used"] or 0)
    cost = float(run["llm_cost_usd"] or 0.0)
    tool = _rows(conn, "SELECT SUM(allowed), COUNT(*) FROM agent_tool_calls c JOIN agent_invocations i USING(invocation_id) WHERE i.run_id=?", run_id)[0]
    bad_confirmed = _rows(conn, """SELECT COUNT(*) FROM relationships r JOIN entities c ON c.entity_id=r.child_id
        WHERE c.run_id=? AND r.verification_status='CONFIRMED' AND NOT EXISTS (SELECT 1 FROM evidence e
        WHERE e.relationship_id=r.relationship_id AND e.quote_verified=1 AND e.supports='OWNERSHIP'
        AND e.source_class IN ('FIRST_PARTY_PARENT','FIRST_PARTY_CHILD','OFFICIAL_DOCUMENT'))""", run_id)[0][0]
    return {
        "run": {"run_id": run_id, "status": run["status"], "root": run["root_name"], "started_at": run["started_at"],
                "finished_at": run["finished_at"], "token_budget": run["llm_token_budget"], "tokens_used": tokens,
                "cost_usd": round(cost, 6)},
        "entities": {"by_status": ents, "non_root": non_root,
                     "review_ratio": round(review / (confirmed_non_root + review), 4) if (confirmed_non_root + review) else None,
                     "by_crawl_state": _map(conn, "SELECT crawl_state, COUNT(*) FROM entities WHERE run_id=? AND merged_into_id IS NULL GROUP BY 1", run_id),
                     "merged": _rows(conn, "SELECT COUNT(*) FROM entities WHERE run_id=? AND merged_into_id IS NOT NULL", run_id)[0][0]},
        "llm": {"per_agent": per_agent, "provider_calls": tot_calls, "cache_hits": tot_hits,
                "cache_hit_rate": round(tot_hits / (tot_hits + tot_calls), 4) if (tot_hits + tot_calls) else None,
                "calls_per_task": round(tot_calls / n_tasks, 3),
                "cost_per_entity_usd": round(cost / non_root, 6) if non_root else None,
                "tokens_per_entity": round(tokens / non_root, 1) if non_root else None,
                "tool_calls_allowed": int(tool[0] or 0), "tool_calls_total": int(tool[1] or 0)},
        "tasks": tasks,
        "pages": _map(conn, "SELECT crawl_status, COUNT(*) FROM crawl_pages WHERE run_id=? GROUP BY 1", run_id),
        "gate_rejections": _map(conn, "SELECT gate_code, COUNT(*) FROM gate_rejections WHERE run_id=? GROUP BY 1", run_id),
        "candidates": _map(conn, "SELECT COALESCE(decision,'NONE'), COUNT(*) FROM candidates WHERE run_id=? GROUP BY 1", run_id),
        "review_items": _map(conn, "SELECT reason_code, COUNT(*) FROM review_items WHERE run_id=? GROUP BY 1", run_id),
        "findings": _map(conn, "SELECT severity, COUNT(*) FROM validation_findings WHERE run_id=? GROUP BY 1", run_id),
        "errors": _map(conn, "SELECT stage || ':' || error_type, COUNT(*) FROM crawl_errors WHERE run_id=? GROUP BY 1", run_id),
        "integrity": {"confirmed_relationships_without_verified_evidence": int(bad_confirmed)},
    }


def format_report(rep: dict[str, Any]) -> str:
    r, e, l = rep["run"], rep["entities"], rep["llm"]
    lines = [
        f"Run {r['run_id']} [{r['status']}] root={r['root']}",
        f"  tokens used: {r['tokens_used']} / {r['token_budget'] or 'unlimited'}   cost: ${r['cost_usd']}",
        f"  entities: {e['by_status']}  review ratio: {e['review_ratio']}  merged: {e['merged']}",
        f"  LLM: {l['provider_calls']} provider calls, {l['cache_hits']} cache hits (rate {l['cache_hit_rate']}), "
        f"{l['calls_per_task']} calls/task, ${l['cost_per_entity_usd']} per entity, {l['tokens_per_entity']} tokens per entity",
        f"  tasks: {rep['tasks']}   pages: {rep['pages']}",
        f"  gate rejections: {rep['gate_rejections']}   candidates: {rep['candidates']}",
        f"  review items: {rep['review_items']}   findings: {rep['findings']}   errors: {rep['errors']}",
        f"  integrity: {rep['integrity']}",
    ]
    for agent, a in sorted(l["per_agent"].items()):
        lines.append(f"    {agent:<26} calls={a['provider_calls']:<4} cache={a['cache_hits']:<4} schema_fail={a['schema_failures']:<3} "
                     f"transport_fail={a['transport_failures']:<3} skipped={a['skipped_by_budget_or_circuit']:<3} "
                     f"tok={a['tokens_in']}/{a['tokens_out']} ${a['cost_usd']}")
    return "\n".join(lines)
