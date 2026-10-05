"""ConfidenceCalculator (section 7, D13) and the deterministic matcher ladder (Part I 6.2)."""
from __future__ import annotations

import pytest

from corpdisc.models.domain import ReviewReason, SourceClass, Supports, VerificationStatus
from corpdisc.services.confidence import ConfidenceCalculator, ConfidenceFlag, EvidenceInput
from corpdisc.services.matcher import MatchCandidate, Matcher
from corpdisc.services.normalize import normalize_name
from tests.helpers.db import make_db, seed_entity, seed_relationship, seed_run, seed_website

CALC = ConfidenceCalculator()


def ev(sc=SourceClass.FIRST_PARTY_PARENT, *, page=1, verified=True, supports=Supports.OWNERSHIP, wording=True):
    return EvidenceInput(source_class=sc, supports=supports, quote_verified=verified, page_id=page,
                         ownership_wording=wording)


# ------------------------------------------------------------------ confidence
@pytest.mark.parametrize("e, base", [
    (ev(wording=True), 0.85), (ev(wording=False), 0.75), (ev(SourceClass.FIRST_PARTY_CHILD), 0.85),
    (ev(SourceClass.OFFICIAL_DOCUMENT), 0.85), (ev(SourceClass.FIRST_PARTY_OTHER), 0.50),
    (ev(SourceClass.THIRD_PARTY), 0.40), (ev(verified=False), 0.40),
])
def test_base_scores(e, base):
    assert CALC.evidence_score(e) == pytest.approx(base)


def test_confirmed_needs_verified_first_party_ownership():
    r = CALC.score([ev()], agent_confidence=1.0, classification_confidence=0.9)
    assert r.status is VerificationStatus.CONFIRMED and r.relationship_confidence == pytest.approx(0.85)
    assert r.entity_confidence == pytest.approx(0.85)
    exist = CALC.score([ev(supports=Supports.EXISTENCE)], agent_confidence=1.0, classification_confidence=0.9)
    assert exist.status is VerificationStatus.REVIEW_REQUIRED and exist.review_reason is ReviewReason.LOW_CONFIDENCE


def test_stacked_third_party_never_confirms_and_same_page_counts_once():
    many = [ev(SourceClass.THIRD_PARTY, page=i) for i in range(10)]
    r = CALC.score(many, agent_confidence=1.0, classification_confidence=1.0)
    assert r.evidence_score <= 0.40 and r.status is VerificationStatus.REVIEW_REQUIRED
    assert r.review_reason is ReviewReason.THIRD_PARTY_ONLY
    same_page = CALC.score([ev(wording=False), ev(wording=False)], agent_confidence=1.0, classification_confidence=1)
    two_pages = CALC.score([ev(wording=False, page=1), ev(wording=False, page=2)], agent_confidence=1.0,
                           classification_confidence=1)
    assert same_page.evidence_score == pytest.approx(0.75)
    assert two_pages.evidence_score == pytest.approx(1 - 0.25 * 0.25)


def test_agent_confidence_only_scales_down_and_reject_rule():
    hi = CALC.score([ev()], agent_confidence=1.0, classification_confidence=1)
    lo = CALC.score([ev()], agent_confidence=0.0, classification_confidence=1)
    assert lo.relationship_confidence == pytest.approx(0.425)
    assert lo.relationship_confidence < hi.relationship_confidence
    assert lo.status is VerificationStatus.REVIEW_REQUIRED
    unverified = CALC.score([ev(verified=False)], agent_confidence=0.5, classification_confidence=1)
    assert unverified.status is VerificationStatus.REJECTED  # 0.40 * 0.75 < 0.40
    assert CALC.score([], agent_confidence=1, classification_confidence=1).status is VerificationStatus.REJECTED


def test_flags_force_review_with_priority():
    r = CALC.score([ev()], agent_confidence=1.0, classification_confidence=1.0,
                   flags={ConfidenceFlag.NO_WEBSITE, ConfidenceFlag.CONTRADICTION, ConfidenceFlag.POSSIBLE_PRODUCT})
    assert r.status is VerificationStatus.REVIEW_REQUIRED and r.review_reason is ReviewReason.CONTRADICTION
    assert CALC.score([ev()], agent_confidence=1, classification_confidence=1,
                      flags={ConfidenceFlag.CYCLE, ConfidenceFlag.CONTRADICTION}).review_reason is ReviewReason.CYCLE


def test_threshold_boundaries_exact():
    calc = ConfidenceCalculator(confirm_threshold=0.85, review_threshold=0.40)
    assert calc.score([ev()], agent_confidence=1.0, classification_confidence=1).status is VerificationStatus.CONFIRMED
    with pytest.raises(ValueError):
        ConfidenceCalculator(confirm_threshold=0.3, review_threshold=0.5)


# ------------------------------------------------------------------ matcher
@pytest.fixture
def reg(tmp_path):
    conn = make_db(tmp_path)
    run = seed_run(conn)
    root = seed_entity(conn, run, "Northwind Holdings", normalized="northwind holdings", entity_type="Parent Company")
    group = seed_entity(conn, run, "Aurora Group", normalized="aurora group", entity_type="Operating Group")
    seed_relationship(conn, root, group)
    seed_website(conn, root, "https://northwind.test/", "northwind.test")
    return conn, run, root, group


def cand(name, parent, domain=None, country=None, aliases=()):
    return MatchCandidate(name=name, normalized_name=normalize_name(name), parent_entity_id=parent,
                          registrable_domain=domain, country=country, aliases=tuple(aliases))


def test_step1_dedicated_domain_matches_but_shared_or_parent_domain_does_not(reg):
    conn, run, root, group = reg
    child = seed_entity(conn, run, "Brightline Logistics", normalized="brightline logistics")
    seed_website(conn, child, "https://brightline.test/", "brightline.test")
    m = Matcher(conn, run_id=run)
    hit = m.find_match(cand("Brightline Freight", group, "brightline.test"))
    assert (hit.outcome, hit.step, hit.existing_entity_id) == ("MATCH", 1, child)
    assert m.find_match(cand("Unrelated Name", group, "northwind.test")).outcome == "NO_MATCH"
    assert m.find_match(cand("Unrelated Name", group, "linkedin.com")).outcome == "NO_MATCH"


def test_step2_same_name_same_parent(reg):
    conn, run, root, group = reg
    child = seed_entity(conn, run, "Cobalt Health Ltd", normalized="cobalt health")
    seed_relationship(conn, group, child, status="REVIEW_REQUIRED")
    hit = Matcher(conn, run_id=run).find_match(cand("Cobalt Health Limited", group))
    assert (hit.outcome, hit.step, hit.existing_entity_id) == ("MATCH", 2, child)


def test_step3_alias_with_country(reg):
    conn, run, root, group = reg
    child = seed_entity(conn, run, "Delta Freight", normalized="delta freight", country="Germany")
    conn.execute("INSERT INTO entity_aliases(entity_id, alias, normalized_alias) VALUES (?,?,?)",
                 (child, "DF Logistik", "df logistik"))
    conn.commit()
    hit = Matcher(conn, run_id=run).find_match(cand("DF Logistik", group, country="germany"))
    assert (hit.outcome, hit.step) == ("MATCH", 3)
    assert Matcher(conn, run_id=run).find_match(cand("DF Logistik", group, country="France")).outcome != "MATCH"


def test_step4_same_name_other_parent_is_ambiguous_with_signals(reg):
    conn, run, root, group = reg
    other = seed_entity(conn, run, "Echo Systems", normalized="echo systems")
    seed_relationship(conn, root, other)
    hit = Matcher(conn, run_id=run).find_match(cand("Echo Systems", group))
    assert (hit.outcome, hit.step, hit.ambiguous_entity_ids) == ("AMBIGUOUS", 4, (other,))
    names = {s.name for s in hit.signals}
    assert {"NAME_EXACT", "NAME_NORMALIZED", "PARENT_DIFF"} <= names


def test_step5_fuzzy_threshold_and_merged_ignored(reg):
    conn, run, root, group = reg
    seed_entity(conn, run, "Foxtrot Laboratories", normalized="foxtrot laboratories")
    m = Matcher(conn, run_id=run, fuzzy_threshold=0.92)
    assert m.find_match(cand("Foxtrot Laboratorie", group)).step == 5
    assert m.find_match(cand("Foxtrot Labs", group)).outcome == "NO_MATCH"
    merged = seed_entity(conn, run, "Golf Robotics", normalized="golf robotics", merged_into=group)
    assert merged and Matcher(conn, run_id=run).find_match(cand("Golf Robotics", root)).outcome == "NO_MATCH"


def test_two_decisive_matches_are_ambiguous(reg):
    conn, run, root, group = reg
    a = seed_entity(conn, run, "Hotel Media", normalized="hotel media")
    b = seed_entity(conn, run, "Hotel Media", normalized="hotel media")
    seed_relationship(conn, group, a, status="REVIEW_REQUIRED")
    seed_relationship(conn, group, b, status="REVIEW_REQUIRED")
    hit = Matcher(conn, run_id=run).find_match(cand("Hotel Media", group))
    assert hit.outcome == "AMBIGUOUS" and hit.ambiguous_entity_ids == (a, b)
