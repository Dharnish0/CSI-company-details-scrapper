"""M7: Supervisor Advisor - narrow, advisory, thresholded in code, never able to override limits."""
from __future__ import annotations

import json

import pytest

from corpdisc.contracts.agent_io import RecursionOutput
from tests.fixtures.world import build_world


def run(tmp_path, *, decision="CRAWL", conf=0.9, settings_kw=None, garbage=False, tokens=None):
    w = build_world(tmp_path, extra_agents=("advisor",), settings_kw=settings_kw)
    w.brain.advisor_decision, w.brain.advisor_conf = decision, conf
    if garbage:
        base = w.brain
        w.provider._handler = lambda r: "nope" if r.schema_name == "RecursionOutput" else base(r)
    w.sup.seed_root()
    if tokens:
        w.conn.execute("UPDATE runs SET llm_token_budget=?, llm_tokens_used=?", tokens); w.conn.commit()
    s = w.sup.run()
    return w, s


def labs_task(w):
    return w.q("SELECT depth, status FROM research_queue WHERE entity_id=?", w.entity("A1 Labs")["entity_id"])


def test_only_grey_zone_entities_reach_the_advisor(tmp_path):
    w, _ = run(tmp_path)
    assert w.brain.calls.count("RecursionOutput") == 1  # A1 Labs (Business Unit); companies and groups are policy-decided


def test_crawl_enqueues_the_grey_zone_entity(tmp_path):
    w, s = run(tmp_path, decision="CRAWL", conf=0.9)
    assert [tuple(r) for r in labs_task(w)] == [(3, "DONE")] and s.status == "COMPLETED"


@pytest.mark.parametrize("decision, conf", [("TERMINAL", 0.95), ("CRAWL", 0.5)], ids=["terminal", "low_confidence_crawl"])
def test_terminal_or_low_confidence_crawl_is_not_enqueued(tmp_path, decision, conf):
    w, _ = run(tmp_path, decision=decision, conf=conf)
    assert labs_task(w) == [] and w.entity("A1 Labs")["crawl_state"] == "TERMINAL"


def test_advisor_failure_uses_the_fixed_default(tmp_path):
    w, _ = run(tmp_path, garbage=True)
    assert labs_task(w) == [] and w.entity("A1 Labs")["crawl_state"] == "TERMINAL"


def test_disabled_advisor_is_never_called(tmp_path):
    w, _ = run(tmp_path, settings_kw={"advisor_enabled": False})
    assert "RecursionOutput" not in w.brain.calls and labs_task(w) == []


def test_advisor_is_switched_off_from_80_percent_of_the_token_budget(tmp_path):
    w, _ = run(tmp_path, tokens=(10_000_000, 8_500_000))
    assert "RecursionOutput" not in w.brain.calls and labs_task(w) == []


def test_advisor_cannot_override_the_depth_limit(tmp_path):
    w, s = run(tmp_path, decision="CRAWL", conf=0.99, settings_kw={"max_depth": 2})
    assert s.status == "COMPLETED" and w.entity("A1 Labs")["crawl_state"] in ("BUDGET_STOPPED", "TERMINAL")
    assert not [r for r in labs_task(w) if r[1] == "DONE"]


def test_advisor_never_changes_verification_status(tmp_path):
    w1, _ = run(tmp_path / "a", decision="CRAWL")
    w2, _ = run(tmp_path / "b", decision="TERMINAL")
    assert {r[0]: r[1] for r in w1.q("SELECT name, verification_status FROM entities")} == \
           {r[0]: r[1] for r in w2.q("SELECT name, verification_status FROM entities")}


def test_advisor_input_is_a_bounded_card_with_the_grey_zone_reason(tmp_path):
    w = build_world(tmp_path, extra_agents=("advisor",))
    seen = []
    base = w.brain

    def handler(r):
        if r.schema_name == "RecursionOutput":
            seen.append(json.JSONDecoder().raw_decode(r.messages[0].content)[0])
        return base(r)

    w.provider._handler = handler
    w.sup.seed_root()
    w.sup.run()
    p = seen[0]
    assert p["grey_zone_reason"] == "TYPE_DIVISION" and p["entity"]["name"] == "A1 Labs" and p["depth"] == 3
    assert p["homepage_title"] == "A1 Labs" and "text" not in p and len(json.dumps(p)) < 2000


def test_schema_has_only_crawl_or_terminal():
    with pytest.raises(ValueError):
        RecursionOutput(decision="CONFIRM", agent_confidence=0.9, reason="x")
