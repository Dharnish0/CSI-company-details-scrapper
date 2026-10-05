"""Calibration report derived from the database (fixture run through the real adapter wire format)."""
from __future__ import annotations

import json

import pytest

from corpdisc.services.calibration import build_report, format_report
from tests.agents.test_m8_integration import BrainTransport, adapter
from tests.fixtures.site import Brain
from tests.fixtures.world import build_world, seeded_world


@pytest.fixture
def run(tmp_path):
    t = BrainTransport(Brain(), "anthropic")
    w = build_world(tmp_path, extra_agents=("advisor", "dedup", "validation"))
    w.deps.runner.gateway._provider = adapter("anthropic", t, price_in_per_mtok=3.0, price_out_per_mtok=15.0)
    w.sup.seed_root()
    w.sup.run()
    return w, t, build_report(w.conn, w.run_id)


def test_report_numbers_agree_with_the_database(run):
    w, t, rep = run
    assert rep["run"]["status"] == "COMPLETED" and rep["run"]["tokens_used"] == 180 * len(t.calls)
    assert rep["llm"]["provider_calls"] == len(t.calls) and rep["llm"]["cache_hits"] == 0
    assert sum(a["provider_calls"] for a in rep["llm"]["per_agent"].values()) == len(t.calls)
    assert rep["run"]["cost_usd"] == pytest.approx((120 * 3 + 60 * 15) / 1e6 * len(t.calls), abs=1e-5)
    assert rep["entities"]["non_root"] == len(w.names()) - 1
    assert rep["entities"]["by_status"]["CONFIRMED"] == len(w.names("CONFIRMED"))
    assert 0 < rep["entities"]["review_ratio"] < 1
    assert rep["llm"]["cost_per_entity_usd"] == pytest.approx(rep["run"]["cost_usd"] / rep["entities"]["non_root"], rel=1e-3)
    assert rep["gate_rejections"].get("G10") == 1 and rep["candidates"]["REJECTED_NOT_ENTITY"] >= 2  # cycle + products
    assert rep["integrity"]["confirmed_relationships_without_verified_evidence"] == 0
    assert set(rep["llm"]["per_agent"]) >= {"extraction", "relationship_verification"}


def test_report_counts_cache_hits_and_is_json_serialisable_and_formats(run):
    w, t, _ = run
    w.conn.execute("UPDATE research_queue SET status='PENDING' WHERE task_id=1")
    w.conn.commit()
    w.sup.run(max_tasks=1)
    rep = build_report(w.conn, w.run_id)
    assert rep["llm"]["cache_hits"] >= 1 and 0 < rep["llm"]["cache_hit_rate"] < 1
    json.dumps(rep)
    text = format_report(rep)
    assert f"Run {w.run_id}" in text and "extraction" in text and "cache" in text


def test_report_counts_budget_skips_and_unknown_run(tmp_path):
    w = seeded_world(tmp_path)
    w.conn.execute("UPDATE runs SET llm_token_budget=10")
    w.conn.commit()
    w.sup.run()
    rep = build_report(w.conn, w.run_id)
    assert rep["run"]["status"] == "BUDGET_EXHAUSTED" and rep["llm"]["provider_calls"] == 0
    assert sum(a["skipped_by_budget_or_circuit"] for a in rep["llm"]["per_agent"].values()) >= 1
    with pytest.raises(LookupError):
        build_report(w.conn, 999)
