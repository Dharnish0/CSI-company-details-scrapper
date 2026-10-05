"""M7: Deduplication Agent and Validation Agent (+ merge executor, demotion cascade, deterministic-first ordering)."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from corpdisc.contracts.agent_io import ValidationJudgement
from corpdisc.contracts.common import SourceClass, Supports
from corpdisc.contracts.postgate import CommitPlan, CommitRejected, EntityRef, EntityWrite, EvidenceWrite, MergeWrite
from corpdisc.contracts.postgate import RelationshipWrite, WebsiteWrite
from corpdisc.models.domain import EntityType, RelationshipType
from corpdisc.models.domain import VerificationStatus as VS
from corpdisc.services.validation_engine import ValidationEngine
from tests.fixtures.site import ROOT, S
from tests.fixtures.world import build_world
from tests.helpers.db import seed_entity, seed_relationship

QUOTE = S["ga"]


def world(tmp_path, agents=("dedup", "validation"), **kw):
    w = build_world(tmp_path, extra_agents=agents, **kw)
    w.sup.seed_root()
    return w


def root_id(w):
    return w.entity(ROOT)["entity_id"]


def add(w, name, parent, *, etype=EntityType.COMPANY, status=VS.CONFIRMED, domain=None):
    """Commit a fully-evidenced entity through the Committer (what a real task would produce)."""
    r = w.deps.fetch.fetch("https://root.test/companies", run_id=w.run_id)
    ev = w.deps.verifier.verify_quote(page_id=r.page_id, quote=QUOTE, supports=Supports.OWNERSHIP, page_text=r.text,
                                      url=r.canonical_url, source_class=SourceClass.FIRST_PARTY_PARENT)
    slug = (domain or name.lower().replace(" ", "")) + ".test"
    c, p = EntityRef(local_ref="n"), EntityRef(entity_id=parent)
    conf = 0.9 if status is VS.CONFIRMED else 0.5
    res = w.deps.committer.commit(CommitPlan(
        run_id=w.run_id,
        entities=(EntityWrite(local_ref="n", name=name, normalized_name=name.lower(), entity_type=etype,
                              verification_status=status, confidence=conf, classification_confidence=0.9),),
        websites=(WebsiteWrite(local_ref="w", entity=c, url=f"https://{slug}/", domain=slug, registrable_domain=slug,
                               kind="OFFICIAL", resolution_method="TEST", reachable=True, is_primary=True),),
        relationships=(RelationshipWrite(local_ref="r", parent=p, child=c, relationship_type=RelationshipType.SUBSIDIARY_OF,
                                         verification_status=status, confidence=0.85 if status is VS.CONFIRMED else 0.5),),
        evidence=(EvidenceWrite(entity=c, relationship_local_ref="r", evidence=ev),)))
    assert not isinstance(res, CommitRejected), res
    return res.entity_ids["n"]


def status(w, name):
    return w.entity(name)["verification_status"]


def merged(w):
    return {r["name"] for r in w.q("SELECT name FROM entities WHERE merged_into_id IS NOT NULL")}


def reviews(w, code):
    return [r[0] for r in w.q("SELECT e.name FROM review_items r JOIN entities e USING(entity_id) WHERE reason_code=?", code)]


# =============================================================== merge executor
def test_merge_repoints_everything_and_demotes_a_second_confirmed_primary_parent(tmp_path):
    w = world(tmp_path, agents=())
    ga = add(w, "Group A", root_id(w), etype=EntityType.OPERATING_GROUP)
    gb = add(w, "Group B", root_id(w), etype=EntityType.OPERATING_GROUP)
    keep, drop = add(w, "Echo Systems", ga, domain="echo1"), add(w, "Echo Systems Ltd", gb, domain="echo2")
    res = w.deps.committer.commit(CommitPlan(run_id=w.run_id, merges=(MergeWrite(keep_id=keep, drop_id=drop, reason="t"),)))
    assert not isinstance(res, CommitRejected), res
    assert w.entity("Echo Systems Ltd")["merged_into_id"] == keep
    assert {tuple(r) for r in w.q("SELECT parent_id, verification_status, is_primary FROM relationships WHERE child_id=?", keep)} \
        == {(ga, "CONFIRMED", 1), (gb, "REVIEW_REQUIRED", 0)}
    assert w.q("SELECT 1 FROM entity_aliases WHERE entity_id=? AND alias='Echo Systems Ltd'", keep)
    assert w.q("SELECT COUNT(*) FROM evidence WHERE entity_id=?", keep)[0][0] == 2 and w.q("SELECT 1 FROM evidence WHERE entity_id=?", drop) == []
    assert "Echo Systems" in reviews(w, "MULTI_PARENT")
    assert w.deps.committer.check_invariants() == []


def test_invalid_merges_are_rejected_atomically(tmp_path):
    w = world(tmp_path, agents=())
    a = add(w, "Alpha Co", root_id(w))
    before = w.q("SELECT COUNT(*) FROM relationships")[0][0]
    for keep, drop in ((a, a), (a, 99999)):
        out = w.deps.committer.commit(CommitPlan(run_id=w.run_id, merges=(MergeWrite(keep_id=keep, drop_id=drop, reason="x"),)))
        assert isinstance(out, CommitRejected) and out.failed_invariant == "INV_PLAN"
    assert w.q("SELECT COUNT(*) FROM relationships")[0][0] == before


# =============================================================== Deduplication Agent
def two_echoes(w):
    g1 = seed_entity(w.conn, w.run_id, "Group One", status="REVIEW_REQUIRED", entity_type="Operating Group")
    g2 = seed_entity(w.conn, w.run_id, "Group Two", status="REVIEW_REQUIRED", entity_type="Operating Group")
    seed_relationship(w.conn, root_id(w), g1, status="REVIEW_REQUIRED")
    seed_relationship(w.conn, root_id(w), g2, status="REVIEW_REQUIRED")
    a = seed_entity(w.conn, w.run_id, "Echo Systems", normalized="echo systems", status="REVIEW_REQUIRED")
    b = seed_entity(w.conn, w.run_id, "Echo Systems", normalized="echo systems", status="REVIEW_REQUIRED")
    seed_relationship(w.conn, g1, a, status="REVIEW_REQUIRED")
    seed_relationship(w.conn, g2, b, status="REVIEW_REQUIRED")
    return a, b


def test_ladder_decides_before_any_llm_same_name_same_parent_merges_without_the_agent(tmp_path):
    w = world(tmp_path)
    g = seed_entity(w.conn, w.run_id, "Group One", status="REVIEW_REQUIRED")
    seed_relationship(w.conn, root_id(w), g, status="REVIEW_REQUIRED")
    a = seed_entity(w.conn, w.run_id, "Foxtrot Labs", normalized="foxtrot labs", status="REVIEW_REQUIRED")
    b = seed_entity(w.conn, w.run_id, "Foxtrot Labs Ltd", normalized="foxtrot labs", status="REVIEW_REQUIRED")
    seed_relationship(w.conn, g, a, status="REVIEW_REQUIRED")
    seed_relationship(w.conn, g, b, status="REVIEW_REQUIRED")
    f = w.sup.finalize_graph.run()
    assert f.merges == 1 and merged(w) == {"Foxtrot Labs Ltd"} and "DuplicateOutput" not in w.brain.calls


@pytest.mark.parametrize("verdict, merges, review", [
    (("SAME", 0.90, ["NAME_NORMALIZED"], []), 1, False),
    (("SAME", 0.80, ["NAME_NORMALIZED"], []), 0, True),          # below DEDUP_MERGE_CONF
    (("SAME", 0.95, ["NAME_NORMALIZED"], ["PARENT_DIFF"]), 0, True),  # a conflicting signal blocks the merge
    (("SAME", 0.95, ["DOMAIN_SAME"], []), 0, True),               # cites a signal that was never supplied
    (("DIFFERENT", 0.80, [], []), 0, False),                      # confident DIFFERENT: separate, no review item
    (("DIFFERENT", 0.50, [], []), 0, True),
    (("UNSURE", 0.90, [], []), 0, True),
])
def test_dedup_thresholds(tmp_path, verdict, merges, review):
    w = world(tmp_path)
    two_echoes(w)
    w.brain.dedup_verdict = lambda pair: verdict
    f = w.sup.finalize_graph.run()
    assert f.merges == merges and len(merged(w)) == merges
    assert bool(reviews(w, "AMBIGUOUS_NAME")) is review
    assert w.brain.calls.count("DuplicateOutput") == 1


def test_dedup_agent_failure_keeps_entities_separate_with_review(tmp_path):
    w = world(tmp_path)
    two_echoes(w)
    base = w.brain
    w.provider._handler = lambda r: "nope" if r.schema_name == "DuplicateOutput" else base(r)
    assert w.sup.finalize_graph.run().merges == 0 and reviews(w, "AMBIGUOUS_NAME")


def test_without_the_agent_ambiguous_pairs_stay_separate_with_review(tmp_path):
    w = world(tmp_path, agents=())
    two_echoes(w)
    assert w.sup.finalize_graph.run().merges == 0 and reviews(w, "AMBIGUOUS_NAME")


# =============================================================== Validation Agent
def spy_order(monkeypatch, w):
    order: list[str] = []
    real = ValidationEngine.run

    def run(self):
        order.append("engine")
        return real(self)

    monkeypatch.setattr(ValidationEngine, "run", run)
    inner = w.brain

    def handler(req):
        if req.schema_name == "ValidationOutput":
            order.append("agent")
        return inner(req)

    w.provider._handler = handler
    return order


def suite_world(tmp_path):
    w = world(tmp_path)
    suite = add(w, "Insight Suite", root_id(w))
    add(w, "Insight Child", suite)
    return w


def test_deterministic_checks_run_before_semantic_validation(tmp_path, monkeypatch):
    w = suite_world(tmp_path)
    order = spy_order(monkeypatch, w)
    w.sup.finalize_graph.run()
    assert order.index("engine") < order.index("agent")


def test_demote_is_applied_with_cascade_and_never_promotes(tmp_path):
    w = suite_world(tmp_path)
    w.brain.validation_verdict = lambda case: ("DEMOTE", 0.8, "POSSIBLE_PRODUCT") if case["entity"]["name"] == "Insight Suite" else ("KEEP", 0.9, None)
    f = w.sup.finalize_graph.run()
    assert (f.demotions, f.cascade_demotions) == (1, 1) and f.semantic_review and f.violations == ()
    assert status(w, "Insight Suite") == "REVIEW_REQUIRED" and status(w, "Insight Child") == "REVIEW_REQUIRED"
    assert w.edge(ROOT, "Insight Suite")["verification_status"] == "REVIEW_REQUIRED"
    assert reviews(w, "POSSIBLE_PRODUCT") == ["Insight Suite"] and reviews(w, "ORPHAN") == ["Insight Child"]
    again = w.sup.finalize_graph.run()  # idempotent: nothing is promoted back or demoted twice
    assert status(w, "Insight Suite") == "REVIEW_REQUIRED" and again.demotions == 0


@pytest.mark.parametrize("verdict, conf, demoted", [("DEMOTE", 0.3, False), ("KEEP", 0.99, False), ("NEEDS_HUMAN", 0.6, True)])
def test_validation_confidence_floor_and_keep(tmp_path, verdict, conf, demoted):
    w = suite_world(tmp_path)
    w.brain.validation_verdict = lambda case: (verdict, conf, None)
    w.sup.finalize_graph.run()
    assert (status(w, "Insight Suite") == "REVIEW_REQUIRED") is demoted


def test_keep_cannot_override_a_deterministic_error_and_error_suspects_are_not_sent(tmp_path):
    w = world(tmp_path)
    bad = seed_entity(w.conn, w.run_id, "Broken Suite", status="CONFIRMED")  # CONFIRMED with no parent, evidence or website
    seed_relationship(w.conn, root_id(w), bad, status="CONFIRMED")
    w.brain.validation_verdict = lambda case: ("KEEP", 0.99, None)
    f = w.sup.finalize_graph.run()
    assert "ValidationOutput" not in w.brain.calls  # the engine already decided
    assert f.outcome == "INVARIANT_VIOLATIONS" and any("INV1" in v for v in f.violations)


def test_orphan_is_demoted_deterministically_without_any_agent(tmp_path):
    w = world(tmp_path, agents=())
    rev = add(w, "Parent Review", root_id(w), status=VS.REVIEW_REQUIRED)
    add(w, "Hanging Child", rev)  # CONFIRMED edge under a REVIEW parent
    f = w.sup.finalize_graph.run()
    assert status(w, "Hanging Child") == "REVIEW_REQUIRED" and f.violations == () and reviews(w, "ORPHAN") == ["Hanging Child"]


def test_agent_failure_lists_suspects_as_warnings_and_does_not_block(tmp_path):
    w = suite_world(tmp_path)
    base = w.brain
    w.provider._handler = lambda r: "garbage" if r.schema_name == "ValidationOutput" else base(r)
    f = w.sup.finalize_graph.run()
    assert not f.semantic_review and f.demotions == 0 and status(w, "Insight Suite") == "CONFIRMED"
    assert w.q("SELECT 1 FROM validation_findings WHERE check_name='NOT_SEMANTICALLY_REVIEWED' AND severity='WARN'")
    assert f.outcome in ("FINDINGS", "CLEAN") and f.violations == ()


def test_unknown_case_ids_are_dropped(tmp_path):
    w = suite_world(tmp_path)
    base = w.brain

    def handler(r):
        out = base(r)
        if r.schema_name == "ValidationOutput":
            import json
            d = json.loads(out)
            d["judgements"].append({"case_id": 4242, "verdict": "DEMOTE", "agent_confidence": 0.99, "reason": "x",
                                    "recommended_manual_action": "x"})
            return json.dumps(d)
        return out

    w.provider._handler = handler
    assert w.sup.finalize_graph.run().demotions == 0


def test_from_80_percent_budget_only_the_five_weakest_suspects_are_reviewed(tmp_path):
    w = world(tmp_path)
    for i in range(7):
        add(w, f"Widget Suite {i}", root_id(w))
    w.conn.execute("UPDATE runs SET llm_token_budget=1000000, llm_tokens_used=850000"); w.conn.commit()
    seen = []
    base = w.brain

    def handler(r):
        if r.schema_name == "ValidationOutput":
            import json
            seen.append(len(json.JSONDecoder().raw_decode(r.messages[0].content)[0]["cases"]))
        return base(r)

    w.provider._handler = handler
    w.sup.finalize_graph.run()
    assert seen == [5]


def test_schema_gives_the_validator_no_way_to_raise_status():
    assert set(ValidationJudgement.model_fields) >= {"verdict"} and "verification_status" not in ValidationJudgement.model_fields
    with pytest.raises(ValidationError):
        ValidationJudgement(case_id=1, verdict="PROMOTE", agent_confidence=0.9, reason="x", recommended_manual_action="x")
