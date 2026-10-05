"""Committer: one transaction per plan, invariants INV1-INV6 (+ CONF/EVIDENCE/PLAN/DUPLICATE), rollback."""
from __future__ import annotations

import pytest

from corpdisc.contracts.common import VerifiedEvidence
from corpdisc.contracts.postgate import (
    CandidateWrite,
    CommitPlan,
    CommitRejected,
    CommitResult,
    EntityRef,
    EntityWrite,
    EvidenceWrite,
    GateRejectionWrite,
    RelationshipWrite,
    ReviewItemWrite,
    TaskWrite,
    WebsiteWrite,
)
from corpdisc.models.domain import EntityType, RelationshipType, ReviewReason, SourceClass, Supports
from corpdisc.models.domain import VerificationStatus as VS
from corpdisc.services.committer import CommitError, Committer
from corpdisc.services.quotes import QuoteVerifier
from tests.helpers.clock import FakeClock
from tests.helpers.db import count, make_db, seed_page, seed_run

TEXT = "Northwind Holdings. Our companies: Aurora Analytics is a wholly owned subsidiary of Northwind Holdings."
QUOTE = "Aurora Analytics is a wholly owned subsidiary of Northwind Holdings"
TABLES = ("entities", "websites", "relationships", "evidence", "candidates", "review_items", "research_queue",
          "gate_rejections", "entity_aliases")


@pytest.fixture
def env(tmp_path):
    conn = make_db(tmp_path)
    run = seed_run(conn)
    texts: dict[int, str] = {}
    page = seed_page(conn, run, "https://northwind.test/companies")
    texts[page] = TEXT
    committer = Committer(conn, run_id=run, page_text=texts.get, clock=FakeClock())
    return conn, run, page, texts, committer


def root(local="root"):
    return EntityWrite(local_ref=local, name="Northwind Holdings", normalized_name="northwind holdings",
                       entity_type=EntityType.PARENT_COMPANY, verification_status=VS.CONFIRMED, confidence=1.0,
                       classification_confidence=1.0, is_root=True)


def child(local="c1", name="Aurora Analytics", status=VS.CONFIRMED, etype=EntityType.COMPANY, conf=0.85):
    return EntityWrite(local_ref=local, name=name, normalized_name=name.lower(), entity_type=etype,
                       verification_status=status, confidence=conf, classification_confidence=0.9)


def evidence(page, quote=QUOTE, verified=True):
    qc = QuoteVerifier().check(quote, TEXT)
    return VerifiedEvidence(page_id=page, url="https://northwind.test/companies", quote=quote,
                            quote_verified=verified, char_start=qc.char_start if verified else None,
                            char_end=qc.char_end if verified else None,
                            source_class=SourceClass.FIRST_PARTY_PARENT, supports=Supports.OWNERSHIP)


def site(entity, local="w1", url="https://aurora.test/"):
    return WebsiteWrite(local_ref=local, entity=entity, url=url, domain="aurora.test", registrable_domain="aurora.test",
                        kind="OFFICIAL", resolution_method="LINKED_ON_PARENT", reachable=True, is_primary=True)


def rel(parent, kid, local="r1", status=VS.CONFIRMED, conf=0.85):
    return RelationshipWrite(local_ref=local, parent=parent, child=kid, relationship_type=RelationshipType.SUBSIDIARY_OF,
                             verification_status=status, confidence=conf)


def full_plan(run, page, **over):
    r, c = EntityRef(local_ref="root"), EntityRef(local_ref="c1")
    base = dict(
        run_id=run, entities=(root(), child()), websites=(site(c),), relationships=(rel(r, c),),
        evidence=(EvidenceWrite(entity=c, relationship_local_ref="r1", evidence=evidence(page)),),
        candidates=(CandidateWrite(source_page_id=page, parent=r, raw_name="Aurora Analytics",
                                   normalized_name="aurora analytics", decision="ACCEPTED", resolved=c),),
        enqueue=(TaskWrite(entity=c, start_url="https://aurora.test/", depth=1),),
        gate_rejections=(GateRejectionWrite(gate_code="G2", item_ref="e9", detail="NOT_IN_TEXT"),),
    )
    base.update(over)
    return CommitPlan(**base)


def test_happy_path_writes_everything_in_one_commit(env):
    conn, run, page, _, committer = env
    out = committer.commit(full_plan(run, page))
    assert isinstance(out, CommitResult), out
    cid = out.entity_ids["c1"]
    row = conn.execute("SELECT discovery_seq, primary_website_id FROM entities WHERE entity_id=?", (cid,)).fetchone()
    assert row["discovery_seq"] == 2 and row["primary_website_id"] == out.website_ids["w1"]
    assert len(out.task_ids) == 1 and len(out.candidate_ids) == 1 and len(out.rejection_ids) == 1
    assert count(conn, "evidence") == 1 and committer.check_invariants() == []


def assert_rejected(env, plan, code):
    conn, *_ , committer = env
    before = {t: count(conn, t) for t in TABLES}
    out = committer.commit(plan)
    assert isinstance(out, CommitRejected) and out.failed_invariant == code, out
    assert {t: count(conn, t) for t in TABLES} == before  # atomic: nothing written
    return out


def test_inv1_confirmed_without_ownership_evidence(env):
    _, run, page, _, _ = env
    assert_rejected(env, full_plan(run, page, evidence=()), "INV1")


def test_inv2_forged_verified_evidence_is_caught(env):
    _, run, page, _, _ = env
    c = EntityRef(local_ref="c1")
    forged = VerifiedEvidence(page_id=page, url="u", quote="Aurora Analytics is owned by Someone Else Entirely",
                              quote_verified=True, char_start=0, char_end=10,
                              source_class=SourceClass.FIRST_PARTY_PARENT, supports=Supports.OWNERSHIP)
    out = assert_rejected(env, full_plan(run, page, evidence=(EvidenceWrite(entity=c, relationship_local_ref="r1",
                                                                          evidence=forged),)), "INV2")
    # INV1 trusts the flag; INV2 re-verifies against stored page text and catches the forgery.
    assert {v.code for v in out.violations} == {"INV2"}


def test_inv4_second_confirmed_primary_parent_and_inv3_cycle(env):
    conn, run, page, _, committer = env
    first = committer.commit(full_plan(run, page))
    assert isinstance(first, CommitResult)
    rid, cid = first.entity_ids["root"], first.entity_ids["c1"]
    g = EntityRef(local_ref="g")
    second = CommitPlan(run_id=run, entities=(child("g", "Brightline Group", etype=EntityType.OPERATING_GROUP),),
                        websites=(site(g, url="https://brightline.test/"),),
                        relationships=(rel(EntityRef(entity_id=rid), g, "rg"), rel(g, EntityRef(entity_id=cid), "gc")),
                        evidence=(EvidenceWrite(entity=g, relationship_local_ref="rg", evidence=evidence(page)),
                                  EvidenceWrite(entity=EntityRef(entity_id=cid), relationship_local_ref="gc",
                                                evidence=evidence(page))))
    assert_rejected(env, second, "INV4")
    cycle = CommitPlan(run_id=run, relationships=(
        RelationshipWrite(local_ref="x", parent=EntityRef(entity_id=cid), child=EntityRef(entity_id=rid),
                          relationship_type=RelationshipType.OWNED_BY, verification_status=VS.CONFIRMED,
                          confidence=0.9, is_primary=False),),
        evidence=(EvidenceWrite(entity=EntityRef(entity_id=rid), relationship_local_ref="x", evidence=evidence(page)),))
    out = assert_rejected(env, cycle, "INV3")
    assert out.violations[0].code == "INV3"


def test_inv5_unknown_type_and_inv_conf(env):
    _, run, page, _, _ = env
    assert_rejected(env, full_plan(run, page, entities=(root(), child(etype=EntityType.UNKNOWN))), "INV5")
    c, r = EntityRef(local_ref="c1"), EntityRef(local_ref="root")
    out = assert_rejected(env, full_plan(run, page, relationships=(rel(r, c, conf=0.5),)), "INV_CONF")
    assert out.violations[0].code == "INV_CONF"


def test_inv6_website_requirement_toggle(env):
    conn, run, page, texts, _ = env
    assert_rejected(env, full_plan(run, page, websites=()), "INV6")
    relaxed = Committer(conn, run_id=run, page_text=texts.get, clock=FakeClock(), require_website_for_confirmed=False)
    assert isinstance(relaxed.commit(full_plan(run, page, websites=())), CommitResult)


def test_plan_level_rules(env):
    conn, run, page, _, committer = env
    assert committer.commit(full_plan(run + 1, page)).failed_invariant == "INV_PLAN"
    assert_rejected(env, full_plan(run, page, entities=(child(), root())), "INV_PLAN")  # root must come first
    bad_root = root().model_copy(update={"entity_type": EntityType.COMPANY})
    assert_rejected(env, CommitPlan(run_id=run, entities=(bad_root,)), "INV_PLAN")
    assert isinstance(committer.commit(CommitPlan(run_id=run, entities=(root(),))), CommitResult)
    assert_rejected(env, CommitPlan(run_id=run, entities=(root("again"),)), "INV_PLAN")  # only one root
    with pytest.raises(CommitError):
        committer.commit_or_raise(CommitPlan(run_id=run, entities=(root("third"),)))


def test_duplicate_under_same_parent_is_rejected_with_pointer(env):
    _, run, page, _, committer = env
    first = committer.commit(full_plan(run, page))
    rid = first.entity_ids["root"]
    d = EntityRef(local_ref="dup")
    plan = CommitPlan(run_id=run, entities=(child("dup", status=VS.REVIEW_REQUIRED, conf=0.5),),
                      relationships=(rel(EntityRef(entity_id=rid), d, "rd", status=VS.REVIEW_REQUIRED, conf=0.5),))
    out = assert_rejected(env, plan, "INV_DUPLICATE")
    assert out.duplicate_of == first.entity_ids["c1"]


def test_candidates_only_plan_is_idempotent_and_review_items(env):
    conn, run, page, _, committer = env
    first = committer.commit(CommitPlan(run_id=run, entities=(root(),)))
    r = EntityRef(entity_id=first.entity_ids["root"])
    cand = CandidateWrite(source_page_id=page, parent=r, raw_name="Widget Pro", normalized_name="widget pro",
                          candidate_kind="PRODUCT", decision="REJECTED_NOT_ENTITY", reason="product")
    plan = CommitPlan(run_id=run, candidates=(cand,), review_items=(ReviewItemWrite(
        entity=r, reason_code=ReviewReason.POSSIBLE_PRODUCT, reason_text="t", recommended_action="a"),))
    a, b = committer.commit(plan), committer.commit(plan)
    assert a.candidate_ids == b.candidate_ids and count(conn, "candidates") == 1 and count(conn, "review_items") == 2


def test_whole_run_check_catches_text_changed_after_commit(env):
    _, run, page, texts, committer = env
    assert isinstance(committer.commit(full_plan(run, page)), CommitResult)
    texts[page] = "the page now says something else entirely"
    assert {v.code for v in committer.check_invariants()} == {"INV2"}
