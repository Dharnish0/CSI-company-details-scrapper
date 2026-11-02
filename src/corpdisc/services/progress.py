"""Read-only progress snapshot of a run (slow-crawl monitoring).

``build_progress`` only runs SELECT statements, so it is safe against a database another process is writing (WAL) and
against the protected user databases when opened with ``connect_readonly``. ``session`` is the dict returned by
``instance_lock.read_session`` (None when no crawler holds the lock). Keys read from it, all optional:
``pid``, ``state`` (STARTING/RUNNING/SLEEPING/STOPPING), ``sleeping_until``, ``heartbeat``, ``started_at``, ``deadline_at``,
``tokens_baseline``, ``cost_baseline``, ``http_429_day``, ``http_429_minute``, ``last_429_scope``.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from typing import Any, Final

from corpdisc.clock import iso_utc, parse_iso_utc
from corpdisc.config import Settings
from corpdisc.models.domain import run_status_label
from corpdisc.services.budget import BudgetGuard, BudgetLimits
from corpdisc.services.calibration import count_http_429
from corpdisc.services.instance_lock import pid_alive

OPERATING_TYPES: Final = ("Operating Group", "Business Group")
PORTFOLIO_TYPES: Final = ("Portfolio Group", "Portfolio Company")
DEPTH_CAP: Final = 64


def _one(conn: sqlite3.Connection, sql: str, *params: Any) -> Any:
    return conn.execute(sql, params).fetchone()[0]


def _ts(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return parse_iso_utc(value)
    except ValueError:
        return None


def _group(conn: sqlite3.Connection, run_id: int, types: tuple[str, ...]) -> dict[str, int]:
    marks = ",".join("?" for _ in types)
    base = f"FROM entities e WHERE e.run_id = ? AND e.merged_into_id IS NULL AND e.entity_type IN ({marks})"  # noqa: S608
    found = _one(conn, f"SELECT COUNT(*) {base}", run_id, *types)
    done = _one(conn, f"SELECT COUNT(*) {base} AND EXISTS (SELECT 1 FROM research_queue q WHERE q.run_id = e.run_id "  # noqa: S608
                      "AND q.entity_id = e.entity_id AND q.status = 'DONE')", run_id, *types)
    return {"discovered": int(found), "processed": int(done)}


def _max_depth(conn: sqlite3.Connection, run_id: int) -> int:
    """Deepest tree level reached over CONFIRMED primary edges between CONFIRMED, non-merged entities (root = level 0, the
    same convention as ``tree.compute_tree``). ``UNION`` keeps the walk finite on cycles; DEPTH_CAP bounds it."""
    root = conn.execute("SELECT entity_id FROM entities WHERE run_id = ? ORDER BY discovery_seq LIMIT 1", (run_id,)).fetchone()
    if root is None:
        return 0
    row = conn.execute(
        "WITH RECURSIVE walk(entity_id, depth) AS (SELECT ?, 0 UNION "
        "SELECT x.child_id, w.depth + 1 FROM walk w JOIN relationships x ON x.parent_id = w.entity_id "
        "JOIN entities c ON c.entity_id = x.child_id JOIN entities p ON p.entity_id = x.parent_id "
        "WHERE x.verification_status = 'CONFIRMED' AND x.is_primary = 1 AND c.run_id = ? "
        "AND c.verification_status = 'CONFIRMED' AND c.merged_into_id IS NULL "
        "AND p.verification_status = 'CONFIRMED' AND p.merged_into_id IS NULL AND w.depth < ?) "
        "SELECT MAX(depth) FROM walk", (root[0], run_id, DEPTH_CAP)).fetchone()
    return int(row[0] or 0)


def build_progress(conn: sqlite3.Connection, run_id: int, settings: Settings, *, now: datetime,
                   session: dict[str, Any] | None = None) -> dict[str, Any]:
    run = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
    if run is None:
        raise LookupError(f"unknown run {run_id}")
    now_iso = iso_utc(now)

    ents = {r[0]: int(r[1]) for r in conn.execute(
        "SELECT verification_status, COUNT(*) FROM entities WHERE run_id = ? AND merged_into_id IS NULL GROUP BY 1", (run_id,))}
    by_type = {r[0]: int(r[1]) for r in conn.execute(
        "SELECT entity_type, COUNT(*) FROM entities WHERE run_id = ? AND merged_into_id IS NULL GROUP BY 1", (run_id,))}
    entities = {"total": sum(ents.values()), "confirmed": ents.get("CONFIRMED", 0),
                "review_required": ents.get("REVIEW_REQUIRED", 0), "rejected": ents.get("REJECTED", 0), "by_type": by_type}

    st = {r[0]: int(r[1]) for r in conn.execute("SELECT status, COUNT(*) FROM research_queue WHERE run_id = ? GROUP BY 1", (run_id,))}
    waiting = int(_one(conn, "SELECT COUNT(*) FROM research_queue WHERE run_id = ? AND status = 'PENDING' "
                             "AND not_before IS NOT NULL AND not_before > ?", run_id, now_iso))
    pending_all = st.get("PENDING", 0)
    tasks = {"total": sum(st.values()), "done": st.get("DONE", 0), "pending": pending_all - waiting, "waiting_retry": waiting,
             "running": st.get("RUNNING", 0), "failed": st.get("FAILED", 0), "skipped": st.get("SKIPPED", 0),
             "done_with_gaps": int(_one(conn, "SELECT COUNT(*) FROM research_queue WHERE run_id = ? AND status = 'DONE' "
                                              "AND error LIKE 'PARTIAL:%'", run_id))}
    next_row = _one(conn, "SELECT MIN(not_before) FROM research_queue WHERE run_id = ? AND status = 'PENDING' "
                          "AND not_before > ?", run_id, now_iso)
    next_at = _ts(next_row)
    next_retry = {"at": next_row, "in_seconds": None if next_at is None else max((next_at - now).total_seconds(), 0.0)}

    inv = conn.execute(
        "SELECT COUNT(*) n, COALESCE(SUM(cache_hit), 0) hits, "
        "COALESCE(SUM(CASE WHEN status = 'OK' AND cache_hit = 0 THEN 1 ELSE 0 END), 0) ok_calls, "
        "COALESCE(SUM(CASE WHEN status = 'FAILED' THEN 1 ELSE 0 END), 0) failed_calls, "
        "COALESCE(SUM(CASE WHEN json_valid(response_json) THEN COALESCE(json_extract(response_json, '$.transport.requests'), 0) "
        "ELSE 0 END), 0) reqs FROM agent_invocations WHERE run_id = ?", (run_id,)).fetchone()
    http_429 = count_http_429(conn, run_id)
    sess = session or {}
    api = {"invocations": int(inv["n"]), "requests": int(inv["reqs"]) + http_429, "successful_calls": int(inv["ok_calls"]),
           "failed_calls": int(inv["failed_calls"]), "cache_hits": int(inv["hits"]), "http_429": http_429,
           "http_429_day": sess.get("http_429_day"), "http_429_minute": sess.get("http_429_minute"),
           "last_429_scope": sess.get("last_429_scope")}

    tokens, cost = int(run["llm_tokens_used"] or 0), float(run["llm_cost_usd"] or 0.0)
    prices_set = settings.llm_price_in_per_mtok > 0 or settings.llm_price_out_per_mtok > 0
    usage = {"tokens": tokens, "cost_usd": round(cost, 6), "prices_set": prices_set,
             "cost_note": None if prices_set else "prices unset (LLM_PRICE_IN_PER_MTOK / LLM_PRICE_OUT_PER_MTOK): cost is not estimated"}

    # ---- crawler state (session file)
    heartbeat = _ts(sess.get("heartbeat"))
    pid = sess.get("pid") if isinstance(sess.get("pid"), int) else None
    alive = None if pid is None else pid_alive(pid)
    until = _ts(sess.get("sleeping_until"))
    if session is None:
        label = "IDLE"
    elif alive is False:
        label = "STALE (crawler process is gone)"
    else:
        label = str(sess.get("state") or "RUNNING").upper()
        if label == "SLEEPING" and until is not None:
            label = f"SLEEPING until {sess['sleeping_until']}"
    state = {"state": label, "pid": pid, "pid_alive": alive, "sleeping_until": sess.get("sleeping_until"),
             "heartbeat_age_seconds": None if heartbeat is None else max((now - heartbeat).total_seconds(), 0.0)}

    # ---- remaining budgets
    slow = bool(settings.slow_crawl or (session is not None and alive is not False))
    guard = BudgetGuard(conn, run_id=run_id, limits=BudgetLimits.from_settings(settings), exclude_rate_limited_from_run_count=slow)
    rep = guard.report()
    lim = guard.limits
    budgets: dict[str, Any] = {
        "pages": {"used": rep.pages_total, "limit": lim.max_total_pages, "remaining": max(lim.max_total_pages - rep.pages_total, 0)},
        "llm_calls": {"used": rep.llm_calls_made, "limit": lim.max_llm_calls_per_run,
                      "remaining": max(lim.max_llm_calls_per_run - rep.llm_calls_made, 0)},
        "run_tokens": {"budget": rep.token_budget, "remaining": rep.tokens_remaining},
        "session_tokens": None, "session_cost": None, "runtime": None}
    if slow:
        base_t = sess.get("tokens_baseline")
        if isinstance(base_t, int):
            used = max(tokens - base_t, 0)
            budgets["session_tokens"] = {"used": used, "ceiling": settings.slow_crawl_token_ceiling,
                                         "remaining": max(settings.slow_crawl_token_ceiling - used, 0)}
        else:
            budgets["session_tokens"] = {"used": None, "ceiling": settings.slow_crawl_token_ceiling, "remaining": None}
        ceil_c = settings.slow_crawl_cost_ceiling_usd
        if ceil_c is not None:
            base_c = sess.get("cost_baseline")
            spent = max(cost - float(base_c), 0.0) if isinstance(base_c, (int, float)) else None
            budgets["session_cost"] = {"used": spent, "ceiling": ceil_c, "remaining": None if spent is None else max(ceil_c - spent, 0.0),
                                       "active": prices_set}
        deadline = _ts(sess.get("deadline_at"))
        started = _ts(sess.get("started_at"))
        if deadline is None and started is not None:
            deadline = started + timedelta(hours=settings.slow_crawl_max_runtime_hours)
        if deadline is not None and alive is not False and session is not None:
            budgets["runtime"] = {"deadline": iso_utc(deadline), "remaining_seconds": max((deadline - now).total_seconds(), 0.0)}

    return {
        "run": {"run_id": run_id, "status": run["status"], "status_label": run_status_label(run["status"]), "root": run["root_name"],
                "started_at": run["started_at"], "as_of": now_iso},
        "entities": entities,
        "groups": {"operating_groups": _group(conn, run_id, OPERATING_TYPES), "portfolio_companies": _group(conn, run_id, PORTFOLIO_TYPES)},
        "hierarchy": {"max_depth": (d := _max_depth(conn, run_id)), "levels": d + 1,
                      "max_task_depth": int(_one(conn, "SELECT COALESCE(MAX(depth), 0) FROM research_queue WHERE run_id = ?", run_id))},
        "tasks": tasks, "next_retry": next_retry, "api": api, "usage": usage, "crawler": state, "budgets": budgets,
    }


def _dur(seconds: float | None) -> str:
    if seconds is None:
        return "n/a"
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}h{m:02d}m{sec:02d}s" if h else (f"{m}m{sec:02d}s" if m else f"{sec}s")


def format_progress(p: dict[str, Any]) -> str:
    r, e, g, h, t = p["run"], p["entities"], p["groups"], p["hierarchy"], p["tasks"]
    a, u, c, b, n = p["api"], p["usage"], p["crawler"], p["budgets"], p["next_retry"]
    og, pc = g["operating_groups"], g["portfolio_companies"]
    split = ""
    if a["http_429_day"] is not None or a["http_429_minute"] is not None:
        split = f" (daily {a['http_429_day'] or 0}, per-minute {a['http_429_minute'] or 0})"
    lines = [
        f"Run {r['run_id']} [{r['status_label']}] root={r['root']}  as of {r['as_of']}",
        f"  crawler: {c['state']}" + (f"  pid={c['pid']}" if c["pid"] else "")
        + (f"  heartbeat {_dur(c['heartbeat_age_seconds'])} ago" if c["heartbeat_age_seconds"] is not None else ""),
        f"  companies discovered: {e['total']}  confirmed: {e['confirmed']}  review-required: {e['review_required']}"
        f"  rejected: {e['rejected']}",
        f"  operating groups: {og['discovered']} found, {og['processed']} processed   "
        f"portfolio companies: {pc['discovered']} found, {pc['processed']} processed",
        f"  hierarchy depth: {h['max_depth']} below the root ({h['levels']} levels); deepest task depth {h['max_task_depth']}",
        f"  tasks: {t['done']} done, {t['pending']} pending, {t['waiting_retry']} waiting for retry, {t['running']} running, "
        f"{t['failed']} failed, {t['skipped']} skipped (of {t['total']})",
        f"  API: {a['requests']} requests, {a['successful_calls']} successful calls, {a['cache_hits']} cache hits, "
        f"{a['http_429']} HTTP 429{split}",
        f"  tokens: {u['tokens']}  cost: ${u['cost_usd']}" + (f"  [{u['cost_note']}]" if u["cost_note"] else ""),
        "  next scheduled retry: " + (f"{n['at']} (in {_dur(n['in_seconds'])})" if n["at"] else "none"),
        f"  remaining pages: {b['pages']['remaining']} of {b['pages']['limit']}   "
        f"LLM calls: {b['llm_calls']['remaining']} of {b['llm_calls']['limit']}",
    ]
    rt = b["run_tokens"]
    if rt["budget"] is not None:
        lines.append(f"  run token budget remaining: {rt['remaining']} of {rt['budget']}")
    if b["session_tokens"] is not None:
        st = b["session_tokens"]
        lines.append(f"  session token ceiling: {st['ceiling']} (remaining {st['remaining'] if st['remaining'] is not None else 'n/a'})")
    if b["session_cost"] is not None:
        sc = b["session_cost"]
        lines.append(f"  session cost ceiling: ${sc['ceiling']} (remaining "
                     f"{'n/a' if sc['remaining'] is None else '$%.4f' % sc['remaining']}{'' if sc['active'] else ', inert: prices unset'})")
    if b["runtime"] is not None:
        lines.append(f"  runtime remaining: {_dur(b['runtime']['remaining_seconds'])} (until {b['runtime']['deadline']})")
    return "\n".join(lines)
