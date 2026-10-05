"""Shared contract primitives (ARCHITECTURE.md Part II sections 22.1 to 22.3).

Imports only pydantic and ``corpdisc.models`` (enums stay the single source of
truth there and are re-exported here). Contracts never import db, services or agents.
"""

from __future__ import annotations

from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from corpdisc.models.domain import (
    CandidateKind,
    CrawlState,
    CrawlStatus,
    EntityType,
    OperatingStatus,
    PageType,
    QueueStatus,
    RelationshipType,
    ReviewReason,
    RunStatus,
    SourceClass,
    Supports,
    VerificationStatus,
    WebsiteKind,
)

__all__ = [
    "CandidateKind", "CrawlState", "CrawlStatus", "EntityType", "OperatingStatus", "PageType",
    "QueueStatus", "RelationshipType", "ReviewReason", "RunStatus", "SourceClass", "Supports",
    "VerificationStatus", "WebsiteKind",
    "MIN_QUOTE_CHARS", "MAX_QUOTE_CHARS", "Strict", "LinkRef", "RawCandidate", "EvidenceClaim",
    "VerifiedEvidence", "Signal", "ToolCall", "BudgetDecision",
]  # fmt: skip

MIN_QUOTE_CHARS: Final[int] = 15
MAX_QUOTE_CHARS: Final[int] = 400

# Source classes that may support a CONFIRMED ownership claim (ARCHITECTURE 26.6 invariant 1).
CONFIRMING_SOURCE_CLASSES: Final[frozenset[SourceClass]] = frozenset(
    {SourceClass.FIRST_PARTY_PARENT, SourceClass.FIRST_PARTY_CHILD, SourceClass.OFFICIAL_DOCUMENT}
)


class Strict(BaseModel):
    """Base for every contract: unknown fields rejected, immutable, strings stripped."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class LinkRef(Strict):
    link_id: int
    url: str  # canonical
    anchor_text: str = Field(max_length=200)
    location: Literal["nav", "footer", "body", "sitemap"]
    same_domain: bool
    det_score: float = Field(ge=0, le=1)  # deterministic keyword score


class RawCandidate(Strict):
    raw_candidate_id: int
    name_text: str = Field(max_length=200)
    link_id: int | None
    context: str = Field(max_length=400)
    dom_hint: Literal["card", "list_item", "table_row", "logo_alt", "footer", "jsonld", "text"]


class EvidenceClaim(Strict):
    """What an agent asserts. Not trusted until QuoteVerifier runs."""

    page_id: int
    quote: str = Field(min_length=MIN_QUOTE_CHARS, max_length=MAX_QUOTE_CHARS)
    supports: Supports


class VerifiedEvidence(Strict):
    """Produced only by QuoteVerifier. The Committer accepts only this type and re-verifies it.

    ``invocation_id`` is optional because deterministic (non-agent) evidence has none.
    """

    page_id: int
    url: str
    quote: str = Field(min_length=1, max_length=1000)
    quote_verified: bool
    char_start: int | None = None
    char_end: int | None = None
    source_class: SourceClass
    supports: Supports
    invocation_id: int | None = None

    @model_validator(mode="after")
    def _offsets_match_verification(self) -> VerifiedEvidence:
        if self.quote_verified:
            if self.char_start is None or self.char_end is None:
                raise ValueError("a verified quote needs char_start and char_end")
            if not 0 <= self.char_start < self.char_end:
                raise ValueError("require 0 <= char_start < char_end")
        elif self.char_start is not None or self.char_end is not None:
            raise ValueError("an unverified quote must not carry offsets")
        return self


class Signal(Strict):
    name: Literal[
        "NAME_EXACT", "NAME_NORMALIZED", "NAME_FUZZY", "ALIAS", "DOMAIN_SAME", "PARENT_SAME",
        "COUNTRY_SAME", "COUNTRY_DIFF", "INDUSTRY_SAME", "INDUSTRY_DIFF", "PARENT_DIFF",
        "DOMAIN_DIFF",
    ]  # fmt: skip
    value: float | str | bool


class ToolCall(Strict):
    """A tool request made through the ToolBroker (M2 executes it; M1 only defines the shape)."""

    tool: str = Field(min_length=1, max_length=100)
    args: dict[str, Any] = Field(default_factory=dict)


class BudgetDecision(Strict):
    """Deterministic outcome of a budget check."""

    allow: bool
    reason: str | None = None  # machine code when denied, e.g. DOMAIN_PAGE_LIMIT
    detail: str | None = None  # human-readable, never contains secrets
    limit: float | None = None
    used: float | None = None
