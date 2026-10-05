"""Per-agent input/output payload models (ARCHITECTURE Part II sections 22.3-22.10).

Agent outputs are the only place free text from an LLM enters the system, so strings are bounded and
cross-field validators encode the Corporate-Discovery rules (a product is never a corporate entity).
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from corpdisc.contracts.common import (
    CandidateKind,
    EntityType,
    EvidenceClaim,
    LinkRef,
    PageType,
    RawCandidate,
    RelationshipType,
    ReviewReason,
    Signal,
    SourceClass,
    Strict,
    VerificationStatus,
)

NON_ENTITY_KINDS = frozenset({
    CandidateKind.PRODUCT, CandidateKind.SERVICE, CandidateKind.INDUSTRY, CandidateKind.SOLUTION,
    CandidateKind.TECHNOLOGY, CandidateKind.FEATURE, CandidateKind.LOCATION,
})  # fmt: skip
Conf = float


class EntityCard(Strict):
    """Read-only summary built by the runtime from the DB. Never contains raw HTML."""

    entity_id: int | None = None
    name: str
    entity_type: EntityType | None = None
    aliases: list[str] = Field(default_factory=list, max_length=10)
    country: str | None = None
    industry: str | None = None
    description: str | None = Field(default=None, max_length=300)
    domains: list[str] = Field(default_factory=list, max_length=10)
    parent_path: list[str] = Field(default_factory=list, max_length=12)
    verification_status: VerificationStatus | None = None


class PageExcerpt(Strict):
    page_id: int
    url: str
    page_type: PageType
    source_class: SourceClass  # assigned by the runtime, never by the agent
    text: str
    char_offset: int = 0
    is_truncated: bool = False


# ---------------------------------------------------------------- 22.4 Supervisor Advisor
class RecursionInput(Strict):
    entity: EntityCard
    depth: int
    website_kind: Literal["OFFICIAL", "SECTION_OF_PARENT", "UNKNOWN"]
    grey_zone_reason: Literal["TYPE_BRAND", "TYPE_DIVISION", "TYPE_UNKNOWN", "SHARED_DOMAIN", "LOW_RELATIONSHIP_CONF"]
    homepage_title: str | None = None
    homepage_headings: list[str] = Field(default_factory=list, max_length=15)


class RecursionOutput(Strict):
    decision: Literal["CRAWL", "TERMINAL"]
    agent_confidence: float = Field(ge=0, le=1)
    reason: str = Field(max_length=200)


# ---------------------------------------------------------------- 22.5 Discovery
class DiscoveryInput(Strict):
    entity: EntityCard
    site_domain: str
    page_title: str | None = None
    page_headings: list[str] = Field(default_factory=list, max_length=20)
    links: list[LinkRef] = Field(max_length=60)
    max_selections: int = Field(ge=1)


class PageSelection(Strict):
    link_id: int
    page_type: PageType
    relevance: float = Field(ge=0, le=1)
    reason: str = Field(max_length=160)


class DiscoveryOutput(Strict):
    selections: list[PageSelection]
    no_relevant_pages: bool
    agent_confidence: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def _consistent(self) -> DiscoveryOutput:
        if self.no_relevant_pages and self.selections:
            raise ValueError("no_relevant_pages conflicts with selections")
        return self


# ---------------------------------------------------------------- 22.6 Extraction
class ExtractionInput(Strict):
    parent: EntityCard
    page: PageExcerpt
    chunk_index: int
    chunk_count: int
    dom_candidates: list[RawCandidate]
    links: list[LinkRef]
    allowed_entity_types: list[EntityType]


class ExtractedEntity(Strict):
    local_ref: str
    raw_candidate_id: int | None = None
    name: str = Field(max_length=200)
    link_id: int | None = None
    kind: CandidateKind
    proposed_type: EntityType | None = None
    is_corporate_entity: bool
    relationship_hint: RelationshipType | None = None
    description: str | None = Field(default=None, max_length=200)
    country: str | None = None
    industry: str | None = None
    claim: EvidenceClaim
    agent_confidence: float = Field(ge=0, le=1)
    reason: str = Field(max_length=200)


class RejectedCandidate(Strict):
    raw_candidate_id: int | None = None
    name: str = Field(max_length=200)
    kind: CandidateKind
    reason: str = Field(max_length=200)


class ExtractionOutput(Strict):
    entities: list[ExtractedEntity]
    rejected: list[RejectedCandidate]
    page_has_ownership_language: bool
    agent_confidence: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def _non_entities_are_not_entities(self) -> ExtractionOutput:
        seen: set[str] = set()
        for e in self.entities:
            if e.local_ref in seen:
                raise ValueError(f"duplicate local_ref {e.local_ref}")
            seen.add(e.local_ref)
            if e.is_corporate_entity and e.kind in NON_ENTITY_KINDS:
                raise ValueError(f"{e.local_ref}: kind {e.kind} cannot be a corporate entity")
        return self


# ---------------------------------------------------------------- 22.7 Website Resolution
class SearchHit(Strict):
    hit_id: int
    url: str
    domain: str
    title: str = Field(max_length=200)
    snippet: str = Field(max_length=400)
    rank: int


class WebsiteInput(Strict):
    entity: EntityCard
    parent: EntityCard
    source_links: list[LinkRef] = Field(default_factory=list)
    search_hits: list[SearchHit] = Field(default_factory=list)
    acquisition_hint: str | None = Field(default=None, max_length=300)


class WebsiteOutput(Strict):
    decision: Literal["SELECTED", "AMBIGUOUS", "NONE_FOUND"]
    selected_hit_id: int | None = None
    selected_link_id: int | None = None
    alternate_hit_ids: list[int] = Field(default_factory=list)
    basis: list[Literal["LINKED_ON_PARENT", "NAME_MATCH", "PARENT_MENTION", "COUNTRY_MATCH", "INDUSTRY_MATCH",
                        "ACQUISITION_ANNOUNCEMENT"]] = Field(default_factory=list)
    claim: EvidenceClaim | None = None
    agent_confidence: float = Field(ge=0, le=1)
    reason: str = Field(max_length=200)

    @model_validator(mode="after")
    def _selected_needs_id(self) -> WebsiteOutput:
        if self.decision == "SELECTED" and (self.selected_hit_id is None) == (self.selected_link_id is None):
            raise ValueError("SELECTED needs exactly one of selected_hit_id or selected_link_id")
        return self


# ---------------------------------------------------------------- 22.8 Relationship Verification
class ChildProposal(Strict):
    local_ref: str
    name: str
    proposed_type: EntityType | None = None
    kind: CandidateKind
    website: str | None = None
    relationship_hint: RelationshipType | None = None


class RelationshipInput(Strict):
    parent: EntityCard
    children: list[ChildProposal] = Field(max_length=15)
    pages: list[PageExcerpt]


class RelationshipAssessment(Strict):
    local_ref: str
    support: Literal["SUPPORTED", "NOT_SUPPORTED", "INSUFFICIENT"]
    relationship_type: RelationshipType
    final_entity_type: EntityType | None = None
    claims: list[EvidenceClaim] = Field(default_factory=list)
    contradictions: list[EvidenceClaim] = Field(default_factory=list)
    agent_confidence: float = Field(ge=0, le=1)
    reason: str = Field(max_length=240)

    @model_validator(mode="after")
    def _rules(self) -> RelationshipAssessment:
        if self.support == "SUPPORTED" and (not self.claims or self.relationship_type == RelationshipType.UNKNOWN):
            raise ValueError("SUPPORTED needs a claim and a concrete relationship type")
        if self.support == "INSUFFICIENT" and self.relationship_type != RelationshipType.UNKNOWN:
            raise ValueError("INSUFFICIENT must use relationship_type Unknown")
        return self


class RelationshipOutput(Strict):
    assessments: list[RelationshipAssessment]


# ---------------------------------------------------------------- 22.9 Deduplication
class DuplicatePair(Strict):
    pair_id: int
    a: EntityCard
    b: EntityCard
    signals: list[Signal]
    evidence_a: list[str] = Field(default_factory=list, max_length=3)
    evidence_b: list[str] = Field(default_factory=list, max_length=3)


class DuplicateInput(Strict):
    pairs: list[DuplicatePair] = Field(max_length=20)


class DuplicateVerdict(Strict):
    pair_id: int
    verdict: Literal["SAME", "DIFFERENT", "UNSURE"]
    matched_signals: list[str] = Field(default_factory=list)
    conflicting_signals: list[str] = Field(default_factory=list)
    preferred_name: Literal["A", "B"] | None = None
    agent_confidence: float = Field(ge=0, le=1)
    reason: str = Field(max_length=200)


class DuplicateOutput(Strict):
    verdicts: list[DuplicateVerdict]


# ---------------------------------------------------------------- 22.10 Validation
class EvidenceSummary(Strict):
    quote: str = Field(max_length=300)
    source_class: SourceClass
    url: str


class ValidationCase(Strict):
    case_id: int
    check_name: Literal["MISCLASSIFIED", "LOW_CONFIDENCE_PATH", "MULTI_PARENT", "IMPLAUSIBLE_CHAIN", "BROKEN_SITE"]
    entity: EntityCard
    path: list[str]
    path_confidence: float
    children_count: int
    evidence: list[EvidenceSummary] = Field(default_factory=list, max_length=4)
    deterministic_findings: list[str] = Field(default_factory=list, max_length=5)


class ValidationInput(Strict):
    cases: list[ValidationCase] = Field(max_length=15)


class ValidationJudgement(Strict):
    case_id: int
    verdict: Literal["KEEP", "DEMOTE", "NEEDS_HUMAN"]
    suggested_entity_type: EntityType | None = None
    reason_code: ReviewReason | None = None
    reason: str = Field(max_length=240)
    recommended_manual_action: str = Field(max_length=240)
    agent_confidence: float = Field(ge=0, le=1)


class ValidationOutput(Strict):
    judgements: list[ValidationJudgement]
