"""Post-gate (deterministic side) models: the only inputs the Committer accepts.

Field names mirror the schema columns they write. New entities, websites and
relationships inside one plan are cross-referenced by ``local_ref``; existing rows
by their integer id. ARCHITECTURE.md 22.11 and 26.6.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, model_validator

from corpdisc.contracts.common import (
    CandidateKind,
    CrawlState,
    EntityType,
    OperatingStatus,
    RelationshipType,
    ReviewReason,
    Signal,
    Strict,
    VerificationStatus,
    VerifiedEvidence,
    WebsiteKind,
)

Conf = Annotated[float, Field(ge=0, le=1)]


class EntityRef(Strict):
    """Reference to an existing entity (entity_id) or one created in the same plan (local_ref)."""

    entity_id: int | None = Field(default=None, ge=1)
    local_ref: str | None = Field(default=None, min_length=1, max_length=100)

    @model_validator(mode="after")
    def _exactly_one(self) -> EntityRef:
        if (self.entity_id is None) == (self.local_ref is None):
            raise ValueError("set exactly one of entity_id or local_ref")
        return self


class EntityWrite(Strict):
    """A new entity. discovery_seq and discovered_at are assigned by the Committer."""

    local_ref: str = Field(min_length=1, max_length=100)
    name: str = Field(min_length=1, max_length=300)
    normalized_name: str = Field(min_length=1, max_length=300)
    entity_type: EntityType
    description: str | None = None
    country: str | None = None
    industry: str | None = None
    operating_status: OperatingStatus = OperatingStatus.UNKNOWN
    verification_status: VerificationStatus
    classification_confidence: Conf | None = None
    confidence: Conf | None = None
    crawl_state: CrawlState = CrawlState.PENDING
    discovery_method: str | None = None
    sibling_order: int | None = Field(default=None, ge=0)
    is_root: bool = False


class EntityUpdate(Strict):
    """Change status/confidence/type of an existing entity (revalidated by the invariants)."""

    entity_id: int = Field(ge=1)
    verification_status: VerificationStatus | None = None
    confidence: Conf | None = None
    entity_type: EntityType | None = None
    crawl_state: CrawlState | None = None


class AliasWrite(Strict):
    entity: EntityRef
    alias: str = Field(min_length=1, max_length=300)
    normalized_alias: str = Field(min_length=1, max_length=300)
    source_page_id: int | None = Field(default=None, ge=1)


class WebsiteWrite(Strict):
    local_ref: str = Field(min_length=1, max_length=100)
    entity: EntityRef
    url: str = Field(min_length=1)
    domain: str = Field(min_length=1)
    registrable_domain: str = Field(min_length=1)
    kind: WebsiteKind
    resolution_method: str = Field(min_length=1)
    reachable: bool | None = None
    last_http_status: int | None = Field(default=None, ge=100, le=599)
    is_primary: bool = False  # becomes entities.primary_website_id


class RelationshipWrite(Strict):
    local_ref: str = Field(min_length=1, max_length=100)
    parent: EntityRef
    child: EntityRef
    relationship_type: RelationshipType
    verification_status: VerificationStatus
    confidence: Conf
    is_primary: bool = True
    source_page_id: int | None = Field(default=None, ge=1)
    discovery_method: str | None = None


class EvidenceWrite(Strict):
    """One evidence row. ``relationship_local_ref`` and ``relationship_id`` are mutually exclusive."""

    entity: EntityRef
    relationship_local_ref: str | None = None
    relationship_id: int | None = Field(default=None, ge=1)
    evidence: VerifiedEvidence

    @model_validator(mode="after")
    def _one_relationship_ref(self) -> EvidenceWrite:
        if self.relationship_local_ref is not None and self.relationship_id is not None:
            raise ValueError("set at most one of relationship_local_ref / relationship_id")
        return self


class CandidateWrite(Strict):
    source_page_id: int = Field(ge=1)
    parent: EntityRef
    raw_name: str = Field(min_length=1, max_length=300)
    normalized_name: str = Field(min_length=1, max_length=300)
    raw_link_url: str | None = None
    context_snippet: str | None = Field(default=None, max_length=400)
    dom_hint: str | None = None
    candidate_kind: CandidateKind | None = None
    proposed_type: EntityType | None = None
    decision: Literal["ACCEPTED", "REJECTED_NOT_ENTITY", "REVIEW", "DUPLICATE"] | None = None
    resolved: EntityRef | None = None
    reason: str | None = Field(default=None, max_length=500)
    confidence: Conf | None = None
    extraction_invocation_id: int | None = Field(default=None, ge=1)
    gate_code: str | None = None


class ReviewItemWrite(Strict):
    entity: EntityRef
    possible_parent: EntityRef | None = None
    reason_code: ReviewReason
    reason_text: str = Field(min_length=1, max_length=500)
    recommended_action: str = Field(min_length=1, max_length=500)
    confidence: Conf | None = None


class TaskWrite(Strict):
    """Enqueue a research task for an entity (idempotent on UNIQUE(run_id, entity_id))."""

    entity: EntityRef
    start_url: str | None = None
    depth: int = Field(ge=0)


class GateRejectionWrite(Strict):
    gate_code: str = Field(min_length=1, max_length=20)
    item_ref: str = Field(min_length=1, max_length=300)
    detail: str = Field(min_length=1, max_length=500)
    invocation_id: int | None = Field(default=None, ge=1)


class FindingWrite(Strict):
    """A validation_findings row (written only through the Committer)."""

    check_name: str = Field(min_length=1, max_length=60)
    severity: Literal["INFO", "WARN", "ERROR"]
    entity_id: int | None = Field(default=None, ge=1)
    relationship_id: int | None = Field(default=None, ge=1)
    details: str = Field(min_length=1, max_length=500)
    action_taken: str | None = Field(default=None, max_length=200)


class CommitPlan(Strict):
    """One transaction. Built only by the runtime; never from raw agent output."""

    run_id: int = Field(ge=1)
    task_id: int | None = Field(default=None, ge=1)
    candidates: tuple[CandidateWrite, ...] = ()
    entities: tuple[EntityWrite, ...] = ()
    entity_updates: tuple[EntityUpdate, ...] = ()
    aliases: tuple[AliasWrite, ...] = ()
    websites: tuple[WebsiteWrite, ...] = ()
    relationships: tuple[RelationshipWrite, ...] = ()
    evidence: tuple[EvidenceWrite, ...] = ()
    review_items: tuple[ReviewItemWrite, ...] = ()
    enqueue: tuple[TaskWrite, ...] = ()
    gate_rejections: tuple[GateRejectionWrite, ...] = ()
    findings: tuple[FindingWrite, ...] = ()

    @model_validator(mode="after")
    def _refs_resolve(self) -> CommitPlan:
        def unique(label: str, refs: list[str]) -> set[str]:
            if len(set(refs)) != len(refs):
                raise ValueError(f"duplicate {label} local_ref in plan")
            return set(refs)

        entity_refs = unique("entity", [e.local_ref for e in self.entities])
        unique("website", [w.local_ref for w in self.websites])
        rel_refs = unique("relationship", [r.local_ref for r in self.relationships])

        def check(ref: EntityRef | None) -> None:
            if ref is not None and ref.local_ref is not None and ref.local_ref not in entity_refs:
                raise ValueError(f"unknown entity local_ref {ref.local_ref!r}")

        for a in self.aliases:
            check(a.entity)
        for w in self.websites:
            check(w.entity)
        for r in self.relationships:
            check(r.parent)
            check(r.child)
        for ev in self.evidence:
            check(ev.entity)
            if ev.relationship_local_ref is not None and ev.relationship_local_ref not in rel_refs:
                raise ValueError(f"unknown relationship local_ref {ev.relationship_local_ref!r}")
        for c in self.candidates:
            check(c.parent)
            check(c.resolved)
        for ri in self.review_items:
            check(ri.entity)
            check(ri.possible_parent)
        for t in self.enqueue:
            check(t.entity)
        return self


# ------------------------------------------------------------------ results
class InvariantViolation(Strict):
    code: str  # INV1..INV6, INV_CONF, INV_EVIDENCE, INV_PLAN, INV_DUPLICATE, INV_FIELDS, INV_CYCLE
    message: str
    entity_id: int | None = None


class CommitResult(Strict):
    """Created ids, keyed by local_ref where the plan used one."""

    entity_ids: dict[str, int] = Field(default_factory=dict)
    website_ids: dict[str, int] = Field(default_factory=dict)
    relationship_ids: dict[str, int] = Field(default_factory=dict)
    evidence_ids: tuple[int, ...] = ()
    candidate_ids: tuple[int, ...] = ()
    review_ids: tuple[int, ...] = ()
    task_ids: tuple[int, ...] = ()
    rejection_ids: tuple[int, ...] = ()


class CommitRejected(Strict):
    """The plan was not applied; the database is unchanged."""

    failed_invariant: str
    message: str
    violations: tuple[InvariantViolation, ...] = ()
    duplicate_of: int | None = None  # existing entity id when rejected as a duplicate


# ------------------------------------------------------------ decision models
class MatchResult(Strict):
    """Outcome of the deterministic matcher ladder. The matcher never merges or writes."""

    outcome: Literal["MATCH", "AMBIGUOUS", "NO_MATCH"]
    existing_entity_id: int | None = None
    step: int | None = Field(default=None, ge=1, le=6)
    signals: tuple[Signal, ...] = ()
    ambiguous_entity_ids: tuple[int, ...] = ()
    reason: str = ""


class MatchDecision(Strict):
    candidate_id: int
    outcome: Literal["NEW", "MERGE_INTO", "SEPARATE_WITH_REVIEW"]
    existing_entity_id: int | None = None
    decided_by: Literal["LADDER", "DEDUP_AGENT"]
    signals: tuple[Signal, ...] = ()


class ScoredRelationship(Strict):
    candidate_id: int
    relationship_type: RelationshipType
    status: VerificationStatus
    confidence: Conf
    classification_confidence: Conf
    evidence: tuple[VerifiedEvidence, ...] = ()
    review_reason: ReviewReason | None = None
    confidence_breakdown: dict[str, float] = Field(default_factory=dict)
