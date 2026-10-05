"""ConfidenceCalculator: code computes confidence, the LLM only scales it down (ARCHITECTURE section 7, D13).

evidence_score          = combine over pages of (max base score within a page), 1 - prod(1 - s), cap 0.99
relationship_confidence = evidence_score * (0.5 + 0.5 * agent_confidence)
entity_confidence       = min(classification_confidence, relationship_confidence)

Status: REJECTED iff relationship_confidence < review_threshold (or no evidence); CONFIRMED iff it reaches
confirm_threshold, at least one verified OWNERSHIP claim comes from a confirming source class, and no flag
is raised; otherwise REVIEW_REQUIRED with a reason.
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from corpdisc.config import Settings
from corpdisc.contracts.common import CONFIRMING_SOURCE_CLASSES, Strict
from corpdisc.models.domain import PageType, ReviewReason, SourceClass, Supports, VerificationStatus


class ConfidenceFlag(StrEnum):
    CYCLE = "CYCLE"
    CONTRADICTION = "CONTRADICTION"
    MULTI_PARENT = "MULTI_PARENT"
    AMBIGUOUS_NAME = "AMBIGUOUS_NAME"
    POSSIBLE_PRODUCT = "POSSIBLE_PRODUCT"
    NO_WEBSITE = "NO_WEBSITE"


# Fixed priority when several flags are raised (highest first).
FLAG_PRIORITY: Final = (
    ConfidenceFlag.CYCLE, ConfidenceFlag.CONTRADICTION, ConfidenceFlag.MULTI_PARENT,
    ConfidenceFlag.AMBIGUOUS_NAME, ConfidenceFlag.POSSIBLE_PRODUCT, ConfidenceFlag.NO_WEBSITE,
)  # fmt: skip


class EvidenceInput(Strict):
    source_class: SourceClass
    supports: Supports
    quote_verified: bool
    page_id: int
    page_type: PageType | None = None
    ownership_wording: bool = False


@dataclass(frozen=True)
class ConfidenceResult:
    status: VerificationStatus
    review_reason: ReviewReason | None
    evidence_score: float
    relationship_confidence: float
    entity_confidence: float
    breakdown: dict[str, float]


def _clamp(x: float) -> float:
    return max(0.0, min(1.0, float(x)))


class ConfidenceCalculator:
    def __init__(
        self,
        *,
        confirm_threshold: float = 0.75,
        review_threshold: float = 0.40,
        unverified_cap: float = 0.40,
        third_party_cap: float = 0.40,
        max_score: float = 0.99,
        first_party_other: float = 0.50,
    ) -> None:
        if not 0 <= review_threshold <= confirm_threshold <= 1:
            raise ValueError("require 0 <= review_threshold <= confirm_threshold <= 1")
        self.confirm_threshold = confirm_threshold
        self.review_threshold = review_threshold
        self.unverified_cap = unverified_cap
        self.third_party_cap = third_party_cap
        self.max_score = max_score
        self.first_party_other = first_party_other

    @classmethod
    def from_settings(cls, s: Settings) -> ConfidenceCalculator:
        return cls(confirm_threshold=s.confirm_threshold, review_threshold=s.review_threshold)

    # ------------------------------------------------------------------ evidence
    def evidence_score(self, ev: EvidenceInput) -> float:
        sc = ev.source_class
        if sc is SourceClass.FIRST_PARTY_PARENT:
            base = 0.85 if ev.ownership_wording else 0.75
        elif sc is SourceClass.FIRST_PARTY_CHILD:
            base = 0.85 if ev.supports is Supports.OWNERSHIP else self.first_party_other
        elif sc is SourceClass.OFFICIAL_DOCUMENT:
            base = 0.85
        elif sc is SourceClass.FIRST_PARTY_OTHER:
            base = self.first_party_other
        else:
            base = self.third_party_cap
        if not ev.quote_verified:
            base = min(base, self.unverified_cap)
        return round(base, 6)

    def score(
        self,
        evidence: Sequence[EvidenceInput],
        *,
        agent_confidence: float,
        classification_confidence: float,
        flags: Collection[ConfidenceFlag] = (),
    ) -> ConfidenceResult:
        agent_confidence = _clamp(agent_confidence)
        classification_confidence = _clamp(classification_confidence)
        breakdown: dict[str, float] = {}
        if not evidence:
            return ConfidenceResult(VerificationStatus.REJECTED, None, 0.0, 0.0, 0.0, {"combined": 0.0})
        per_page: dict[int, float] = {}
        for i, ev in enumerate(evidence):
            s = self.evidence_score(ev)
            breakdown[f"evidence_{i}"] = s
            per_page[ev.page_id] = max(per_page.get(ev.page_id, 0.0), s)  # same page counts once
        remaining = 1.0
        for s in per_page.values():
            remaining *= 1.0 - s
        combined = min(1.0 - remaining, self.max_score)
        strong = [
            ev for ev in evidence
            if ev.quote_verified and ev.supports is Supports.OWNERSHIP and ev.source_class in CONFIRMING_SOURCE_CLASSES
        ]  # fmt: skip
        if not any(ev.quote_verified and ev.source_class in CONFIRMING_SOURCE_CLASSES for ev in evidence):
            combined = min(combined, self.third_party_cap)  # stacked weak evidence never reaches confirm
            breakdown["cap_no_first_party"] = self.third_party_cap
        llm_factor = 0.5 + 0.5 * agent_confidence
        rel = round(combined * llm_factor, 6)
        ent = round(min(classification_confidence, rel), 6)
        breakdown.update(combined=round(combined, 6), llm_factor=round(llm_factor, 6))

        raised = [f for f in FLAG_PRIORITY if f in set(flags)]
        if rel < self.review_threshold:
            return ConfidenceResult(VerificationStatus.REJECTED, None, round(combined, 6), rel, ent, breakdown)
        if raised:
            reason = ReviewReason(raised[0].value)
            return ConfidenceResult(VerificationStatus.REVIEW_REQUIRED, reason, round(combined, 6), rel, ent, breakdown)
        if rel >= self.confirm_threshold and strong:
            return ConfidenceResult(VerificationStatus.CONFIRMED, None, round(combined, 6), rel, ent, breakdown)
        if not any(ev.quote_verified for ev in evidence):
            reason = ReviewReason.NO_QUOTE
        elif all(ev.source_class is SourceClass.THIRD_PARTY for ev in evidence):
            reason = ReviewReason.THIRD_PARTY_ONLY
        else:
            reason = ReviewReason.LOW_CONFIDENCE
        return ConfidenceResult(VerificationStatus.REVIEW_REQUIRED, reason, round(combined, 6), rel, ent, breakdown)
