"""M7: Discovery Agent - bands, deterministic fallback, and the gates that police its selections."""
from __future__ import annotations

import json

import pytest

from corpdisc.contracts.agent_io import DiscoveryOutput, PageSelection
from corpdisc.agents.discovery import DiscoveryAgent
from tests.fixtures.site import ROOT, S, Brain, page, routes
from tests.fixtures.world import build_world
from tests.helpers.fakes import FakeHttpClient, resp


def site(nav: str, extra: dict | None = None):
    r = routes()
    r["https://root.test/"] = resp("https://root.test/", 200, page(ROOT, "<p>Welcome.</p>", nav=nav))
    r["https://root.test/group"] = resp("https://root.test/group", 200, page("Our Group", f"<p>{S['ga']}</p>"))
    r["https://root.test/careers"] = resp("https://root.test/careers", 200, page("Careers", "<p>Jobs.</p>"))
    r.update(extra or {})
    return FakeHttpClient(r)


def brain_for(discovery):
    base = Brain()

    def b(request):
        if request.schema_name == "DiscoveryOutput":
            base.calls.append("DiscoveryOutput")
            return discovery(json.JSONDecoder().raw_decode(request.messages[0].content)[0])
        if request.schema_name == "ExtractionOutput":
            p, _ = json.JSONDecoder().raw_decode(request.messages[0].content)
            base.calls.append("ExtractionOutput")
            if S["ga"] not in p["page"]["text"]:
                return {"entities": [], "rejected": [], "page_has_ownership_language": False, "agent_confidence": 0.5}
            return {"entities": [{"local_ref": "e1", "name": "Group A", "kind": "COMPANY", "proposed_type": "Operating Group",
                                  "is_corporate_entity": True, "claim": {"page_id": p["page"]["page_id"], "quote": S["ga"],
                                                                         "supports": "OWNERSHIP"},
                                  "agent_confidence": 0.9, "reason": "x"}], "rejected": [],
                    "page_has_ownership_language": True, "agent_confidence": 0.9}
        return base(request)

    b.base = base
    return b


MID_NAV = "<a href='/group'>Group</a><a href='/careers'>Careers</a>"  # 'group' scores 0.5: ambiguous band


def run(tmp_path, discovery, nav=MID_NAV, **kw):
    brain = brain_for(discovery)
    w = build_world(tmp_path, http=site(nav), brain=brain, **kw)
    w.sup.seed_root()
    s = w.sup.run(max_tasks=1)
    w.brain_calls = brain.base.calls
    return w, s


def picks(p, ids=None, relevance=0.8, no_relevant=False, conf=0.9):
    links = {lk["anchor_text"]: lk["link_id"] for lk in p["links"]}
    sel = [] if no_relevant else [{"link_id": i, "page_type": "COMPANIES", "relevance": relevance, "reason": "x"}
                                  for i in (ids or [links["Group"]])]
    return {"selections": sel, "no_relevant_pages": no_relevant, "agent_confidence": conf}


def test_clear_scores_do_not_call_the_agent(tmp_path):
    w = build_world(tmp_path)
    w.sup.seed_root()
    w.sup.run(max_tasks=1)
    assert "DiscoveryOutput" not in w.brain.calls


def test_nothing_relevant_is_terminal_without_an_agent_call(tmp_path):
    w, s = run(tmp_path, lambda p: picks(p), nav="<a href='/careers'>Careers</a><a href='/contact'>Contact</a>")
    assert "DiscoveryOutput" not in w.brain_calls and s.outcomes["TERMINAL"] == 1


def test_ambiguous_band_calls_agent_and_selection_is_fetched(tmp_path):
    w, s = run(tmp_path, lambda p: picks(p))
    assert w.brain_calls.count("DiscoveryOutput") == 1 and "Group A" in w.names("CONFIRMED") | w.names("REVIEW_REQUIRED")
    assert not [u for u in w.page_requests() if "careers" in u]


def test_agent_selecting_an_excluded_page_is_stopped_by_g4(tmp_path):
    w, _ = run(tmp_path, lambda p: picks(p, ids=[next(x["link_id"] for x in p["links"] if x["anchor_text"] == "Careers")]))
    assert w.q("SELECT 1 FROM gate_rejections WHERE gate_code='G4'") or w.q("SELECT 1 FROM gate_rejections WHERE gate_code='G5'")
    assert not [u for u in w.page_requests() if "careers" in u]


def test_unknown_link_id_is_dropped_by_g1_then_fallback_runs(tmp_path):
    w, _ = run(tmp_path, lambda p: picks(p, ids=[987654]))
    assert w.q("SELECT 1 FROM gate_rejections WHERE gate_code='G1'")
    assert [u for u in w.page_requests() if u.endswith("/group")]  # deterministic fallback still fetched the best link


def test_low_relevance_selection_is_dropped_and_fallback_used(tmp_path):
    w, _ = run(tmp_path, lambda p: picks(p, relevance=0.2))
    assert [u for u in w.page_requests() if u.endswith("/group")]


def test_no_relevant_pages_needs_confidence_070_to_end_the_task(tmp_path):
    w, s = run(tmp_path / "hi", lambda p: picks(p, no_relevant=True, conf=0.9))
    assert s.outcomes["TERMINAL"] == 1 and not [u for u in w.page_requests() if u.endswith("/group")]
    w2, _ = run(tmp_path / "lo", lambda p: picks(p, no_relevant=True, conf=0.5))
    assert [u for u in w2.page_requests() if u.endswith("/group")]  # below 0.70 -> deterministic fallback


def test_agent_failure_falls_back_to_deterministic_ranking(tmp_path):
    w, _ = run(tmp_path, lambda p: "this is not json")
    assert [u for u in w.page_requests() if u.endswith("/group")]
    assert w.q("SELECT 1 FROM agent_invocations WHERE agent='discovery' AND failure_kind='SCHEMA'")


def test_token_budget_exhaustion_stops_the_run(tmp_path):
    brain = brain_for(lambda p: picks(p))
    w = build_world(tmp_path, http=site(MID_NAV), brain=brain)
    w.sup.seed_root()
    w.conn.execute("UPDATE runs SET llm_token_budget=10"); w.conn.commit()
    s = w.sup.run()
    assert s.status == "BUDGET_EXHAUSTED" and w.provider.call_count == 0


def test_output_schema_rejects_contradiction_and_agent_has_no_tools():
    with pytest.raises(ValueError):
        DiscoveryOutput(selections=[PageSelection(link_id=1, page_type="COMPANIES", relevance=0.9, reason="x")],
                        no_relevant_pages=True, agent_confidence=0.9)
    assert DiscoveryAgent.allowed_tools == {} and DiscoveryAgent.max_tool_calls == 0
