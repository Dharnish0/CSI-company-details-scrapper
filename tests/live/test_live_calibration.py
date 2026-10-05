"""Live calibration against a REAL LLM provider. NOT run by default; see tests/live/conftest.py.

* ``live``      - the local fixture hierarchy (in-memory site) is researched by the real model. Safety properties are
                  hard assertions; quality numbers are printed as a calibration report for tuning prompts/thresholds.
* ``live_site`` - a small, low-limit crawl of the REAL target site. Needs CORPDISC_LIVE_SITE=1 in addition.
"""
from __future__ import annotations

import json
import os

import pytest

from corpdisc.agents.providers.anthropic import AnthropicProvider
from corpdisc.agents.providers.openai_compat import OpenAICompatProvider
from corpdisc.config import Settings
from corpdisc.contracts.agents import CallStatus
from corpdisc.services.calibration import build_report, format_report
from corpdisc.services.llm_http import RequestsJsonTransport
from tests.agents.toy_agents import ToyEchoAgent
from tests.fixtures.site import ROOT as ROOT_NAME
from tests.fixtures.world import build_world, seeded_world

PRODUCTS = {"Aurora Suite", "DataBridge Pro"}
EXPECTED_CONFIRMABLE = {"Group A", "Group B", "Company A1", "Company B1"}


def real_provider(cfg):
    cls = AnthropicProvider if cfg["provider"] == "anthropic" else OpenAICompatProvider
    return cls(api_key=cfg["api_key"], transport=RequestsJsonTransport(), base_url=cfg["base_url"], timeout=60,
               price_in_per_mtok=cfg["price_in"], price_out_per_mtok=cfg["price_out"])


def assert_safety(w, page_texts):
    """Zero-tolerance properties that must hold for ANY model."""
    names = w.names()
    assert not (PRODUCTS & names), "a product became an entity"
    z = w.entity("Zephyr Systems")
    assert z is None or z["verification_status"] != "CONFIRMED", "a partner was confirmed"
    assert w.edge("Company B1", "Group B") is None, "a cycle edge was committed"
    assert w.deps.committer.check_invariants() == []
    assert w.q("SELECT 1 FROM relationships r WHERE r.verification_status='CONFIRMED' AND NOT EXISTS (SELECT 1 FROM evidence e "
               "WHERE e.relationship_id=r.relationship_id AND e.quote_verified=1 AND e.supports='OWNERSHIP')") == []
    haystack = " ".join(page_texts).lower()
    for n in names - {ROOT_NAME}:
        assert n.lower().split(" ltd")[0] in haystack, f"{n!r} is not grounded in any fetched page"


@pytest.mark.live
def test_provider_smoke_one_structured_call(tmp_path, live_cfg):
    """The adapter, auth, structured JSON, usage accounting and audit work against the real API."""
    from tests.agents.harness import build

    s = build(tmp_path, [])
    s.runner.gateway._provider = real_provider(live_cfg)
    s.runner.gateway._model = live_cfg["model"]
    agent = ToyEchoAgent()
    r = s.runner.invoke(agent, s.req(agent, text="Reply with answer 'ok' and score 1.0."))
    assert r.status is CallStatus.OK, r.error
    assert r.usage.tokens_in > 0 and r.usage.tokens_out > 0
    assert s.rows()[0]["status"] == "OK" and live_cfg["api_key"] not in json.dumps([tuple(x) for x in s.rows()], default=str)


@pytest.mark.live
def test_fixture_hierarchy_with_a_real_model(tmp_path, live_cfg, capsys):
    w = build_world(tmp_path, llm_token_budget=live_cfg["token_budget"], extra_agents=("advisor", "dedup", "validation"))
    w.deps.runner.gateway._provider = real_provider(live_cfg)
    w.deps.runner.gateway._model = live_cfg["model"]
    w.conn.execute("UPDATE runs SET llm_token_budget=?", (live_cfg["token_budget"],))
    w.conn.commit()
    w.sup.seed_root()
    summary = w.sup.run()
    rep = build_report(w.conn, w.run_id)
    texts = [w.deps.fetch.load_text(r[0]) or "" for r in w.q("SELECT page_id FROM crawl_pages")]
    print("\n" + format_report(rep))
    with capsys.disabled():
        print(f"\nCALIBRATION {live_cfg['provider']}/{live_cfg['model']}:\n{format_report(rep)}")
    assert summary.status in ("COMPLETED", "BUDGET_EXHAUSTED")
    assert_safety(w, texts)
    # quality is reported, not asserted tightly: a weak model is a calibration finding, not a defect in the pipeline
    recall = len(EXPECTED_CONFIRMABLE & w.names("CONFIRMED")) / len(EXPECTED_CONFIRMABLE)
    print(f"recall of the expected confirmable set: {recall:.2f}")
    assert recall >= 0.5, f"model confirmed too few expected entities ({recall:.2f}); inspect the report above"


@pytest.mark.live_site
def test_small_crawl_of_the_real_site(tmp_path, live_cfg):
    """Opt-in: CORPDISC_LIVE_SITE=1. Honours START_URL/ROOT_COMPANY from the environment; tiny limits."""
    if os.environ.get("CORPDISC_LIVE_SITE") != "1":
        pytest.skip("set CORPDISC_LIVE_SITE=1 to crawl the real site")
    from corpdisc.app import build_runtime

    st = Settings(_env_file=None, db_path=tmp_path / "live.db", cache_dir=tmp_path / ".cache", log_dir=tmp_path / "logs",
                  llm_provider=live_cfg["provider"], llm_api_key=live_cfg["api_key"], llm_model=live_cfg["model"],
                  llm_base_url=live_cfg["base_url"], llm_token_budget=live_cfg["token_budget"], max_depth=2,
                  max_total_pages=int(os.environ.get("CORPDISC_LIVE_MAX_PAGES", "20")), max_links_per_entity=4,
                  llm_price_in_per_mtok=live_cfg["price_in"], llm_price_out_per_mtok=live_cfg["price_out"],
                  use_playwright="false")
    rt = build_runtime(st)
    try:
        rt.supervisor.seed_root()
        s = rt.supervisor.run(max_tasks=int(os.environ.get("CORPDISC_LIVE_MAX_TASKS", "3")))
        rep = build_report(rt.conn, rt.run_id)
        print("\n" + format_report(rep))
        assert s.failed_tasks == 0
        assert rt.deps.committer.check_invariants() == []
        assert rep["integrity"]["confirmed_relationships_without_verified_evidence"] == 0
    finally:
        rt.close()
