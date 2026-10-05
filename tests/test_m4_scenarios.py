"""M4 scenarios A-H on the local fixture hierarchy. No LLM, no network, no API key."""
from __future__ import annotations

import copy
import json
from collections import Counter

import pytest

from corpdisc.agents.ports import ProviderAuthError
from corpdisc.models.domain import RunStatus
from tests.fixtures.site import ROOT, S
from tests.fixtures.world import build_world, seeded_world


@pytest.fixture
def full(tmp_path):
    w = seeded_world(tmp_path)
    return w, w.sup.run()


# ---------------------------------------------------------------- A. multi-level hierarchy
def test_a_multi_level_hierarchy_with_evidence(full):
    w, s = full
    assert s.status == "COMPLETED" and s.stopped_reason == "QUEUE_EMPTY" and s.failed_tasks == 0
    chain = [(ROOT, "Group A"), ("Group A", "Company A1"), ("Company A1", "A1 Labs"), (ROOT, "Group B"),
             ("Group B", "Company B1")]
    for parent, child in chain:
        e = w.edge(parent, child)
        assert e is not None and e["verification_status"] == "CONFIRMED" and e["is_primary"] == 1, (parent, child)
    labs = w.entity("A1 Labs")
    assert labs["entity_type"] == "Business Unit"
    # every CONFIRMED relationship carries verified first-party ownership evidence
    bad = w.q("SELECT r.relationship_id FROM relationships r WHERE r.verification_status='CONFIRMED' AND NOT EXISTS "
              "(SELECT 1 FROM evidence e WHERE e.relationship_id=r.relationship_id AND e.quote_verified=1 AND "
              "e.supports='OWNERSHIP' AND e.source_class IN ('FIRST_PARTY_PARENT','FIRST_PARTY_CHILD','OFFICIAL_DOCUMENT'))")
    assert bad == []
    assert s.finalize.violations == () and w.deps.committer.check_invariants() == []
    # child-side evidence was found deterministically on the child's own site
    assert w.q("SELECT 1 FROM evidence WHERE source_class='FIRST_PARTY_CHILD' AND quote_verified=1")


# ---------------------------------------------------------------- B. cycle
def test_b_cycle_is_rejected_by_g10_and_never_committed(full):
    w, _ = full
    assert w.edge("Company B1", "Group B") is None
    assert len(w.q("SELECT 1 FROM relationships WHERE child_id=?", w.entity("Group B")["entity_id"])) == 1
    assert [r["item_ref"] for r in w.q("SELECT item_ref FROM gate_rejections WHERE gate_code='G10'")]
    assert ("Group B", "CYCLE") in [(r[0], r[1]) for r in w.q(
        "SELECT e.name, r.reason_code FROM review_items r JOIN entities e USING(entity_id)")]
    assert w.deps.cycle.has_cycle_through([r[0] for r in w.q("SELECT entity_id FROM entities")], include_review=True) == []
    assert w.q("SELECT 1 FROM crawl_errors WHERE stage='REGISTRY'") == []  # stopped by the gate, not by the Committer


# ---------------------------------------------------------------- C. product list
def test_c_products_never_become_entities(full):
    w, _ = full
    assert not {"Aurora Suite", "DataBridge Pro"} & w.names()
    rows = {r["raw_name"]: r["decision"] for r in w.q("SELECT raw_name, decision FROM candidates")}
    assert rows["Aurora Suite"] == rows["DataBridge Pro"] == "REJECTED_NOT_ENTITY"


# ---------------------------------------------------------------- D. duplicates
def test_d_duplicates_are_resolved_deterministically(full, tmp_path):
    w, _ = full
    assert Counter(r[0] for r in w.q("SELECT name FROM entities")).most_common(1)[0][1] == 1
    assert w.entity("Company A2") is not None and w.edge("Group B", "Company A2")["verification_status"] == "REVIEW_REQUIRED"
    assert w.edge("Group B", "Company A2")["is_primary"] == 0  # the first parent keeps the primary edge
    assert w.edge("Group A", "Company A2")["verification_status"] == "CONFIRMED"
    assert ("Company A2", "MULTI_PARENT") in [(r[0], r[1]) for r in w.q(
        "SELECT e.name, r.reason_code FROM review_items r JOIN entities e USING(entity_id)")]
    assert w.q("SELECT 1 FROM entities WHERE name='A1 Analytics'") == []  # second reference to Company A1's site
    alias = w.q("SELECT a.normalized_alias FROM entity_aliases a JOIN entities e USING(entity_id) WHERE e.name='Company A1'")
    assert [r[0] for r in alias] == ["a1 analytics"]
    again = seeded_world(tmp_path / "again")
    again.sup.run()
    assert again.names() == w.names() and again.names("CONFIRMED") == w.names("CONFIRMED")  # deterministic


# ---------------------------------------------------------------- E. dead website
def test_e_dead_site_is_review_not_confirmed_and_run_survives(full):
    w, s = full
    d = w.entity("Dormant Co")
    assert d["verification_status"] == "REVIEW_REQUIRED" and d["crawl_state"] == "UNREACHABLE"
    assert d["primary_website_id"] is None
    assert ("Dormant Co", "BROKEN_SITE") in [(r[0], r[1]) for r in w.q(
        "SELECT e.name, r.reason_code FROM review_items r JOIN entities e USING(entity_id)")]
    assert w.q("SELECT 1 FROM gate_rejections WHERE gate_code='G6'")
    assert s.status == "COMPLETED" and w.q("SELECT 1 FROM research_queue WHERE status IN ('PENDING','RUNNING','FAILED')") == []
    assert w.q("SELECT 1 FROM websites WHERE reachable=0")  # recorded, not primary


def test_negative_relationship_partner_is_never_confirmed(full):
    w, _ = full
    z = w.entity("Zephyr Systems")
    assert z["verification_status"] == "REVIEW_REQUIRED"
    assert w.edge("Group A", "Zephyr Systems")["relationship_type"] == "Unknown"
    assert w.edge("Group A", "Zephyr Systems")["verification_status"] != "CONFIRMED"


def test_robots_and_scope_careers_pages_never_requested(full):
    w, _ = full
    assert not [u for u in w.page_requests() if "careers" in u or "privacy" in u]
    assert len(w.page_requests()) == len(set(w.page_requests()))  # each page fetched once (cache/visited set)


# ---------------------------------------------------------------- F. budgets
def test_f_total_page_limit_stops_the_run(tmp_path):
    w = seeded_world(tmp_path, settings_kw={"max_total_pages": 4})
    s = w.sup.run()
    assert s.status == RunStatus.BUDGET_EXHAUSTED.value and s.stopped_reason in ("TOTAL_PAGE_LIMIT",) or "TOTAL_PAGE" in s.stopped_reason
    budgeted = w.q("SELECT COUNT(*) FROM crawl_pages WHERE crawl_status NOT IN ('BLOCKED_ROBOTS','SKIPPED_LIMIT','SKIPPED_FILTER')")
    assert budgeted[0][0] <= 4
    assert w.q("SELECT 1 FROM research_queue WHERE status='PENDING'")  # unfinished work stays resumable
    n = len(w.page_requests())
    w.sup.run()  # still over budget: nothing new may be fetched
    assert len(w.page_requests()) == n


def test_f_depth_limit_marks_tasks_budget_stopped_but_run_continues(tmp_path):
    w = seeded_world(tmp_path, settings_kw={"max_depth": 1})
    s = w.sup.run()
    assert s.status == "COMPLETED" and s.outcomes["BUDGET_STOPPED"] == 3  # A1, A2, B1 sit at depth 2
    assert w.entity("Company A1")["crawl_state"] == "BUDGET_STOPPED" and w.entity("A1 Labs") is None


def test_f_token_budget_stops_before_any_agent_call(tmp_path):
    w = seeded_world(tmp_path)
    w.conn.execute("UPDATE runs SET llm_token_budget=50 WHERE run_id=?", (w.run_id,))
    w.conn.commit()
    s = w.sup.run()
    assert s.status == "BUDGET_EXHAUSTED" and w.provider.call_count == 0
    assert w.q("SELECT 1 FROM agent_invocations WHERE status='SKIPPED' AND failure_kind='BUDGET'")
    assert w.q("SELECT 1 FROM entities WHERE name='Group A'") == []


# ---------------------------------------------------------------- G. resume
def test_g_resume_after_interruption_does_not_repeat_work(tmp_path):
    base = seeded_world(tmp_path / "base")
    base.sup.run()
    w = seeded_world(tmp_path / "r")
    first = w.sup.run(max_tasks=2)
    assert first.stopped_reason == "MAX_TASKS" and first.processed == 2
    done = {r[0] for r in w.q("SELECT task_id FROM research_queue WHERE status='DONE'")}
    stuck = w.deps.queue.claim_next(w.run_id)  # simulate a crash mid-task: left RUNNING
    assert stuck is not None
    w2 = build_world(tmp_path / "r", conn=w.conn, http=w.http, run_id=w.run_id, cache_dir=tmp_path / "r" / ".cache")
    assert w2.sup.seed_root() is None  # idempotent
    final = w2.sup.resume()
    assert final.status == "COMPLETED"
    assert w2.names() == base.names() and w2.names("CONFIRMED") == base.names("CONFIRMED")
    assert {r[0] for r in w2.q("SELECT task_id FROM research_queue WHERE status='DONE'")} >= done
    assert len(w.page_requests()) == len(set(w.page_requests()))  # nothing fetched twice across the interruption
    assert w.provider.call_count + w2.provider.call_count == base.provider.call_count  # nothing re-asked
    assert w2.q("SELECT 1 FROM research_queue WHERE status IN ('RUNNING','PENDING','FAILED')") == []


def test_circuit_breaker_pauses_run_and_resume_continues(tmp_path):
    w = seeded_world(tmp_path)

    def down(schema, out):
        raise ProviderAuthError("401")

    w.brain.mutators.append(down)
    s = w.sup.run()
    assert s.status == "PAUSED" and s.stopped_reason == "CIRCUIT_OPEN"
    assert w.q("SELECT status FROM research_queue WHERE task_id=1")[0][0] == "PENDING"  # released, not failed
    w.brain.mutators.clear()
    assert w.sup.resume().status == "COMPLETED" and "Group A" in w.names("CONFIRMED")


def test_one_crashing_task_does_not_kill_the_run(tmp_path):
    w = seeded_world(tmp_path)
    real = w.deps.fetch.fetch

    def flaky(url, **kw):
        if url.startswith("https://b1.test/companies"):
            raise RuntimeError("boom")
        return real(url, **kw)

    w.deps.fetch.fetch = flaky
    s = w.sup.run()
    assert s.status == "COMPLETED" and s.failed_tasks == 1
    row = w.q("SELECT status, attempts FROM research_queue WHERE entity_id=?", w.entity("Company B1")["entity_id"])[0]
    assert tuple(row) == ("FAILED", 3)
    assert {"Company A1", "A1 Labs", "Group B"} <= w.names("CONFIRMED") and w.q("SELECT 1 FROM crawl_errors WHERE stage='TASK'")


# ---------------------------------------------------------------- H. gates reject invalid agent proposals
def _first_entity_mutator(fn):
    def mutate(schema, out):
        if schema != "ExtractionOutput" or not out["entities"]:
            return out
        out = copy.deepcopy(out)
        fn(out)
        return out
    return mutate


def run_with(tmp_path, mutator, name="Group A"):
    brain_w = seeded_world(tmp_path)
    brain_w.brain.mutators.append(mutator)
    return brain_w, brain_w.sup.run()


def test_h_hallucinated_entity_name_is_rejected_by_g2(tmp_path):
    def inject(out):
        out["entities"].append({**out["entities"][0], "local_ref": "ex", "name": "Phantom Dynamics Corp",
                                "raw_candidate_id": None, "link_id": None})
    w, _ = run_with(tmp_path, _first_entity_mutator(inject))
    assert "Phantom Dynamics Corp" not in w.names()
    assert any(r["item_ref"] == "Phantom Dynamics Corp" for r in w.q("SELECT item_ref FROM gate_rejections WHERE gate_code='G2'"))
    assert w.q("SELECT decision FROM candidates WHERE raw_name='Phantom Dynamics Corp'")[0][0] == "REJECTED_NOT_ENTITY"
    assert "Group A" in w.names("CONFIRMED")  # real entities on the same page are unaffected


def test_h_altered_quote_is_rejected_by_g3(tmp_path):
    def alter(out):
        out["entities"][0]["claim"]["quote"] = "Group A was acquired by Northwind Holdings in a long ago deal."
    w, _ = run_with(tmp_path, _first_entity_mutator(alter))
    assert w.entity("Group A") is None and w.q("SELECT 1 FROM gate_rejections WHERE gate_code='G3'")


def test_h_product_marked_as_company_fails_schema_and_creates_nothing(tmp_path):
    def bad(out):
        out["entities"][0].update(kind="PRODUCT", is_corporate_entity=True)
    w, s = run_with(tmp_path, _first_entity_mutator(bad))
    assert w.entity("Group A") is None
    assert w.q("SELECT 1 FROM agent_invocations WHERE failure_kind='SCHEMA'")
    assert w.q("SELECT 1 FROM review_items WHERE reason_code='EXTRACTION_FAILED'") or s.outcomes["PARTIAL"]


def test_h_unknown_link_id_is_rejected_by_g1(tmp_path):
    def bad(out):
        out["entities"][0]["link_id"] = 99999
    w, _ = run_with(tmp_path, _first_entity_mutator(bad))
    assert w.entity("Group A") is None and w.q("SELECT 1 FROM gate_rejections WHERE gate_code='G1'")


def test_h_fabricated_relationship_quote_can_never_confirm(tmp_path):
    def forge(schema, out):
        if schema != "RelationshipOutput":
            return out
        out = copy.deepcopy(out)
        for a in out["assessments"]:
            for c in a.get("claims", []):
                c["quote"] = "Northwind Holdings has fully owned this company since the year nineteen ninety."
        return out
    w, _ = run_with(tmp_path, forge)
    assert w.names("CONFIRMED") <= {ROOT}
    assert w.q("SELECT 1 FROM evidence WHERE quote_verified=1 AND quote LIKE '%nineteen ninety%'") == []


def test_h_relationship_agent_down_means_nothing_confirmed(tmp_path):
    def down(schema, out):
        if schema == "RelationshipOutput":
            return "not json at all"
        return out
    w, _ = run_with(tmp_path, down)
    assert w.names("CONFIRMED") == {ROOT}
    assert w.q("SELECT 1 FROM review_items WHERE reason_code='VERIFICATION_UNAVAILABLE'") or \
        w.q("SELECT 1 FROM entities WHERE verification_status='REVIEW_REQUIRED'")


def test_agents_never_touch_database_or_network_and_no_api_key_needed(tmp_path, monkeypatch):
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    w = seeded_world(tmp_path)
    w.sup.run()
    assert w.provider.call_count > 0 and w.deps.settings.llm_api_key is None
    assert w.q("SELECT COUNT(*) FROM llm_calls")[0][0] == 0
