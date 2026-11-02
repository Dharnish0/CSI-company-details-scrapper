"""BudgetGuard session ceilings (slow crawl): token/cost denial, baseline subtraction, run-count exclusion."""
from __future__ import annotations

import json

from corpdisc.clock import iso_utc
from corpdisc.services.budget import BudgetGuard, BudgetLimits, SessionCeiling
from tests.helpers.db import T0, make_db, seed_run


def _guard(tmp_path, *, session=None, exclude=False, limits=None, token_budget=None):
    conn = make_db(tmp_path)
    run = seed_run(conn, token_budget=token_budget)
    g = BudgetGuard(conn, run_id=run, limits=limits or BudgetLimits(), session=session,
                    exclude_rate_limited_from_run_count=exclude)
    return conn, run, g


def _spend(g, tokens, cost=0.0):
    g.record_llm_usage(tokens_in=tokens, tokens_out=0, cost_usd=cost)


def _invocation(conn, run, *, status="OK", kind=None, body=None, task_id=None):
    conn.execute(
        "INSERT INTO agent_invocations(run_id, task_id, agent, correlation_id, schema_version, prompt_version, input_hash, "
        "request_json, response_json, status, failure_kind, cache_hit, started_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,0,?)",
        (run, task_id, "extraction", "c", "1", "1", f"h{conn.total_changes}", "{}", None if body is None else json.dumps(body),
         status, kind, iso_utc(T0)))
    conn.commit()


def test_default_guard_check_llm_ignores_spend(tmp_path):
    conn, run, g = _guard(tmp_path)
    _spend(g, 10_000_000)
    assert g.check_llm(task_id=None, requested_output_tokens=3000, estimated_input_tokens=5000).allow
    assert g.session_usage()["active"] is False


def test_token_ceiling_denial_reasons_contain_token_budget(tmp_path):
    s = SessionCeiling(baseline_tokens=0, token_ceiling=1000)
    conn, run, g = _guard(tmp_path, session=s)
    assert g.check_llm(task_id=None, requested_output_tokens=100, estimated_input_tokens=100).allow
    _spend(g, 700)
    d = g.check_llm(task_id=None, requested_output_tokens=200, estimated_input_tokens=200)  # 700 + 400 > 1000
    assert not d.allow and d.reason == "TOKEN_BUDGET_SLOW_CRAWL_CEILING" and "TOKEN_BUDGET" in d.reason
    assert d.limit == 1000 and d.used == 700
    assert g.check_llm(task_id=None, requested_output_tokens=100, estimated_input_tokens=100).allow  # 700 + 200 <= 1000
    _spend(g, 300)
    d = g.check_llm(task_id=None)
    assert not d.allow and d.reason == "TOKEN_BUDGET_SLOW_CRAWL_CEILING"  # used >= ceiling


def test_baseline_is_subtracted(tmp_path):
    s = SessionCeiling(baseline_tokens=210_224, token_ceiling=250_000)
    conn, run, g = _guard(tmp_path, session=s)
    _spend(g, 210_224)
    assert g.check_llm(task_id=None, requested_output_tokens=1000, estimated_input_tokens=1000).allow
    u = g.session_usage()
    assert u["tokens"] == 0 and u["tokens_remaining"] == 250_000 and u["active"] is True
    _spend(g, 249_000)
    assert not g.check_llm(task_id=None, requested_output_tokens=1000, estimated_input_tokens=1000).allow
    assert g.session_usage()["tokens_remaining"] == 1000


def test_run_level_token_budget_still_checked_first(tmp_path):
    s = SessionCeiling(baseline_tokens=0, token_ceiling=10**9)
    conn, run, g = _guard(tmp_path, session=s, token_budget=500)
    _spend(g, 500)
    assert g.check_llm(task_id=None).reason == "TOKEN_BUDGET_EXHAUSTED"


def test_cost_ceiling_with_prices(tmp_path):
    s = SessionCeiling(baseline_tokens=0, token_ceiling=10**9, baseline_cost=1.0, cost_ceiling_usd=0.50,
                       price_in_per_mtok=1.0, price_out_per_mtok=2.0)
    conn, run, g = _guard(tmp_path, session=s)
    _spend(g, 0, cost=1.0)  # equals baseline: session cost 0
    assert g.check_llm(task_id=None, estimated_input_tokens=1000, requested_output_tokens=1000).allow
    _spend(g, 0, cost=0.4)
    assert g.check_llm(task_id=None, estimated_input_tokens=1000, requested_output_tokens=1000).allow  # + 0.003
    d = g.check_llm(task_id=None, estimated_input_tokens=50_000, requested_output_tokens=50_000)  # + 0.15
    assert not d.allow and d.reason == "TOKEN_BUDGET_SLOW_CRAWL_COST_CEILING"
    _spend(g, 0, cost=0.1)
    assert g.check_llm(task_id=None).reason == "TOKEN_BUDGET_SLOW_CRAWL_COST_CEILING"
    u = g.session_usage()
    assert round(u["cost_usd"], 6) == 0.5 and u["cost_remaining_usd"] == 0 and u["cost_check_active"] is True


def test_cost_ceiling_inert_without_prices(tmp_path):
    s = SessionCeiling(baseline_tokens=0, token_ceiling=10**9, cost_ceiling_usd=0.01)
    assert s.cost_check_active is False
    conn, run, g = _guard(tmp_path, session=s)
    _spend(g, 1000, cost=5.0)
    assert g.check_llm(task_id=None, estimated_input_tokens=10, requested_output_tokens=10).allow


def test_run_count_exclusion_only_when_flag_set(tmp_path):
    limits = BudgetLimits(max_llm_calls_per_run=3)
    for exclude in (False, True):
        conn, run, g = _guard(tmp_path / f"x{exclude}", exclude=exclude, limits=limits)
        _invocation(conn, run, status="OK")
        for _ in range(3):  # 429-only failures: no request reached the model
            _invocation(conn, run, status="FAILED", kind="TRANSPORT", body={"transport": {"requests": 0}})
        _invocation(conn, run, status="FAILED", kind="TRANSPORT", body={"transport": {"requests": 2}})
        assert g.llm_calls() == (2 if exclude else 5)
        assert g.check_llm(task_id=None).allow is exclude  # default mode: 5 >= 2 -> denied
        if not exclude:
            assert g.check_llm(task_id=None).reason == "RUN_LLM_CALL_LIMIT"
