from __future__ import annotations

import pytest
from pydantic import ValidationError

from corpdisc.contracts import postgate as pg
from corpdisc.contracts.common import Signal, SourceClass, Supports, VerifiedEvidence
from corpdisc.db.connection import connect_writer, init_db
from corpdisc.models.domain import EntityType, RelationshipType, VerificationStatus


def _entity(ref="e1", **kw):
    base = dict(local_ref=ref, name="Northwind Holdings", normalized_name="northwind holdings",
                entity_type=EntityType.PARENT_COMPANY, verification_status=VerificationStatus.CONFIRMED,
                confidence=1.0, classification_confidence=1.0, is_root=True)
    base.update(kw)
    return pg.EntityWrite(**base)


def _rel(ref="r1", parent="e1", child="e2", **kw):
    base = dict(local_ref=ref, parent=pg.EntityRef(local_ref=parent), child=pg.EntityRef(local_ref=child),
                relationship_type=RelationshipType.OWNED_BY, verification_status=VerificationStatus.REVIEW_REQUIRED,
                confidence=0.5)
    base.update(kw)
    return pg.RelationshipWrite(**base)


def test_entity_ref_needs_exactly_one():
    assert pg.EntityRef(entity_id=3).entity_id == 3
    assert pg.EntityRef(local_ref="x").local_ref == "x"
    with pytest.raises(ValidationError):
        pg.EntityRef()
    with pytest.raises(ValidationError):
        pg.EntityRef(entity_id=1, local_ref="x")
    with pytest.raises(ValidationError):
        pg.EntityRef(entity_id=0)


def test_commit_plan_roundtrip_and_frozen():
    plan = pg.CommitPlan(run_id=1, entities=(_entity(), _entity("e2", is_root=False, name="Child",
                                                                normalized_name="child")),
                         relationships=(_rel(),))
    assert pg.CommitPlan.model_validate_json(plan.model_dump_json()) == plan
    with pytest.raises(ValidationError):
        plan.run_id = 2  # type: ignore[misc]
    with pytest.raises(ValidationError):
        pg.CommitPlan(run_id=1, surprise=True)  # extra forbidden


def test_commit_plan_rejects_unknown_or_duplicate_local_refs():
    with pytest.raises(ValidationError, match="unknown entity local_ref"):
        pg.CommitPlan(run_id=1, entities=(_entity(),), relationships=(_rel(child="nope"),))
    with pytest.raises(ValidationError, match="duplicate entity"):
        pg.CommitPlan(run_id=1, entities=(_entity(), _entity()))
    with pytest.raises(ValidationError, match="duplicate relationship"):
        pg.CommitPlan(run_id=1, entities=(_entity(), _entity("e2")), relationships=(_rel(), _rel()))
    ev = VerifiedEvidence(page_id=1, url="u", quote="q" * 20, quote_verified=False,
                          source_class=SourceClass.THIRD_PARTY, supports=Supports.EXISTENCE)
    with pytest.raises(ValidationError, match="unknown relationship"):
        pg.CommitPlan(run_id=1, entities=(_entity(),),
                      evidence=(pg.EvidenceWrite(entity=pg.EntityRef(local_ref="e1"),
                                                 relationship_local_ref="zzz", evidence=ev),))
    with pytest.raises(ValidationError):
        pg.EvidenceWrite(entity=pg.EntityRef(entity_id=1), relationship_local_ref="a", relationship_id=2,
                         evidence=ev)


def test_existing_ids_need_no_local_ref_resolution():
    plan = pg.CommitPlan(run_id=1, relationships=(
        pg.RelationshipWrite(local_ref="r", parent=pg.EntityRef(entity_id=1), child=pg.EntityRef(entity_id=2),
                             relationship_type=RelationshipType.OWNED_BY,
                             verification_status=VerificationStatus.REVIEW_REQUIRED, confidence=0.5),))
    assert plan.relationships[0].parent.entity_id == 1


def test_candidates_and_rejections_only_plan_is_legal():
    plan = pg.CommitPlan(
        run_id=1,
        candidates=(pg.CandidateWrite(source_page_id=1, parent=pg.EntityRef(entity_id=1), raw_name="Thing",
                                      normalized_name="thing", decision="REJECTED_NOT_ENTITY"),),
        gate_rejections=(pg.GateRejectionWrite(gate_code="G1", item_ref="x", detail="d"),),
    )
    assert len(plan.candidates) == 1


def test_confidence_bounds_enforced():
    with pytest.raises(ValidationError):
        _entity(confidence=1.5)
    with pytest.raises(ValidationError):
        _rel(confidence=-0.1)


def test_results_and_decisions_roundtrip():
    r = pg.CommitResult(entity_ids={"e1": 1}, evidence_ids=(1, 2))
    assert pg.CommitResult.model_validate_json(r.model_dump_json()) == r
    rej = pg.CommitRejected(failed_invariant="INV1", message="m",
                            violations=(pg.InvariantViolation(code="INV1", message="m"),), duplicate_of=4)
    assert pg.CommitRejected.model_validate_json(rej.model_dump_json()) == rej
    m = pg.MatchResult(outcome="AMBIGUOUS", step=4, existing_entity_id=2,
                       signals=(Signal(name="NAME_EXACT", value=True),))
    assert pg.MatchResult.model_validate_json(m.model_dump_json()) == m
    with pytest.raises(ValidationError):
        pg.MatchResult(outcome="MERGED")
    d = pg.MatchDecision(candidate_id=1, outcome="NEW", decided_by="LADDER")
    assert d.existing_entity_id is None
    s = pg.ScoredRelationship(candidate_id=1, relationship_type=RelationshipType.UNKNOWN,
                              status=VerificationStatus.REVIEW_REQUIRED, confidence=0.5,
                              classification_confidence=0.6, confidence_breakdown={"a": 0.5})
    assert pg.ScoredRelationship.model_validate_json(s.model_dump_json()) == s


# ------------------------------------------- field names mirror the schema columns they write
def _columns(tmp_path, table):
    path = tmp_path / "s.db"
    init_db(path)
    conn = connect_writer(path)
    try:
        return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}  # fixed names
    finally:
        conn.close()


@pytest.mark.parametrize(
    ("model", "table", "not_columns", "no_model_field"),
    [
        (pg.EntityWrite, "entities", {"local_ref", "is_root"},
         {"entity_id", "run_id", "discovery_seq", "discovered_at", "primary_website_id", "level", "tree_id",
          "hierarchy_path", "merged_into_id"}),
        (pg.WebsiteWrite, "websites", {"local_ref", "entity", "is_primary"},
         {"website_id", "entity_id", "last_checked_at"}),
        (pg.RelationshipWrite, "relationships", {"local_ref", "parent", "child"},
         {"relationship_id", "parent_id", "child_id", "created_at"}),
        (pg.CandidateWrite, "candidates", {"parent", "resolved"},
         {"candidate_id", "run_id", "parent_entity_id", "resolved_entity_id"}),
        (pg.ReviewItemWrite, "review_items", {"entity", "possible_parent"},
         {"review_id", "run_id", "entity_id", "possible_parent_id", "created_at"}),
        (pg.GateRejectionWrite, "gate_rejections", set(),
         {"rejection_id", "run_id", "created_at"}),
        (pg.AliasWrite, "entity_aliases", {"entity"}, {"alias_id", "entity_id"}),
    ],
)
def test_write_models_mirror_schema_columns(tmp_path, model, table, not_columns, no_model_field):
    columns = _columns(tmp_path, table)
    fields = set(model.model_fields)
    extra = fields - columns - not_columns
    assert not extra, f"{model.__name__} has fields that are not {table} columns: {extra}"
    # every column is either carried by the model or assigned by the Committer
    uncovered = columns - fields - no_model_field
    assert not uncovered, f"{table} columns neither modelled nor committer-assigned: {uncovered}"
