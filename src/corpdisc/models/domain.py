"""Foundational domain enums and row models (strict Pydantic v2).

Enum values must stay in sync with the CHECK constraints in db/schema.sql
(ARCHITECTURE.md Part II section 22.2 and Part I section 5). Agent contracts
belong to milestone M2 and are intentionally absent here.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated

from pydantic import AfterValidator, AwareDatetime, BaseModel, ConfigDict, Field, model_validator


# ---------------------------------------------------------------- enums
class EntityType(StrEnum):
    PARENT_COMPANY = "Parent Company"
    OPERATING_GROUP = "Operating Group"
    BUSINESS_GROUP = "Business Group"
    PORTFOLIO_GROUP = "Portfolio Group"
    PORTFOLIO_COMPANY = "Portfolio Company"
    SUBSIDIARY = "Subsidiary"
    COMPANY = "Company"
    DIVISION = "Division"
    BUSINESS_UNIT = "Business Unit"
    BRAND = "Brand"
    ACQUIRED_COMPANY = "Acquired Company"
    UNKNOWN = "Unknown"


class RelationshipType(StrEnum):
    OWNED_BY = "Owned by"
    PART_OF = "Part of"
    SUBSIDIARY_OF = "Subsidiary of"
    OPERATING_UNDER = "Operating under"
    DIVISION_OF = "Division of"
    PORTFOLIO_COMPANY_OF = "Portfolio company of"
    ACQUIRED_BY = "Acquired by"
    MEMBER_OF = "Member of"
    MANAGED_BY = "Managed by"
    UNKNOWN = "Unknown"


class VerificationStatus(StrEnum):
    CONFIRMED = "CONFIRMED"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    REJECTED = "REJECTED"


class OperatingStatus(StrEnum):
    ACTIVE = "Active"
    UNKNOWN = "Unknown"


class CrawlState(StrEnum):
    PENDING = "PENDING"
    EXPANDED = "EXPANDED"
    TERMINAL = "TERMINAL"
    NO_WEBSITE = "NO_WEBSITE"
    UNREACHABLE = "UNREACHABLE"
    BUDGET_STOPPED = "BUDGET_STOPPED"
    SKIPPED = "SKIPPED"


class SourceClass(StrEnum):
    FIRST_PARTY_PARENT = "FIRST_PARTY_PARENT"
    FIRST_PARTY_CHILD = "FIRST_PARTY_CHILD"
    FIRST_PARTY_OTHER = "FIRST_PARTY_OTHER"
    OFFICIAL_DOCUMENT = "OFFICIAL_DOCUMENT"
    THIRD_PARTY = "THIRD_PARTY"


class Supports(StrEnum):
    OWNERSHIP = "OWNERSHIP"
    EXISTENCE = "EXISTENCE"
    WEBSITE = "WEBSITE"
    CLASSIFICATION = "CLASSIFICATION"


class CandidateKind(StrEnum):
    COMPANY = "COMPANY"
    GROUP = "GROUP"
    BRAND = "BRAND"
    PRODUCT = "PRODUCT"
    SERVICE = "SERVICE"
    INDUSTRY = "INDUSTRY"
    SOLUTION = "SOLUTION"
    TECHNOLOGY = "TECHNOLOGY"
    FEATURE = "FEATURE"
    LOCATION = "LOCATION"
    OTHER = "OTHER"


class ReviewReason(StrEnum):
    NO_QUOTE = "NO_QUOTE"
    THIRD_PARTY_ONLY = "THIRD_PARTY_ONLY"
    LOW_CONFIDENCE = "LOW_CONFIDENCE"
    AMBIGUOUS_NAME = "AMBIGUOUS_NAME"
    NO_WEBSITE = "NO_WEBSITE"
    MULTI_PARENT = "MULTI_PARENT"
    ORPHAN = "ORPHAN"
    POSSIBLE_PRODUCT = "POSSIBLE_PRODUCT"
    CYCLE = "CYCLE"
    BROKEN_SITE = "BROKEN_SITE"
    CONTRADICTION = "CONTRADICTION"
    EXTRACTION_FAILED = "EXTRACTION_FAILED"
    VERIFICATION_UNAVAILABLE = "VERIFICATION_UNAVAILABLE"


class PageType(StrEnum):
    HOME = "HOME"
    ABOUT = "ABOUT"
    COMPANIES = "COMPANIES"
    PORTFOLIO = "PORTFOLIO"
    BRANDS = "BRANDS"
    SUBSIDIARIES = "SUBSIDIARIES"
    ACQUISITIONS = "ACQUISITIONS"
    OPERATING_GROUPS = "OPERATING_GROUPS"
    LEADERSHIP = "LEADERSHIP"
    LOCATIONS = "LOCATIONS"
    INVESTOR = "INVESTOR"
    OTHER = "OTHER"
    # Excluded from candidate extraction by default (gate G4). Section 22.2 lists
    # them only in a comment; they are needed as values for crawl_pages.page_type.
    PARTNERS = "PARTNERS"
    CUSTOMERS = "CUSTOMERS"
    CASE_STUDIES = "CASE_STUDIES"
    CAREERS = "CAREERS"
    BLOG = "BLOG"
    NEWS = "NEWS"


class RunStatus(StrEnum):
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class WebsiteKind(StrEnum):
    OFFICIAL = "OFFICIAL"
    SECTION_OF_PARENT = "SECTION_OF_PARENT"
    EXTERNAL_PROFILE = "EXTERNAL_PROFILE"


class QueueStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    DONE = "DONE"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


class CrawlStatus(StrEnum):
    OK = "OK"
    CACHED = "CACHED"
    BLOCKED_ROBOTS = "BLOCKED_ROBOTS"
    HTTP_ERROR = "HTTP_ERROR"
    TIMEOUT = "TIMEOUT"
    SSL_ERROR = "SSL_ERROR"
    SKIPPED_LIMIT = "SKIPPED_LIMIT"
    SKIPPED_FILTER = "SKIPPED_FILTER"


# ---------------------------------------------------------------- field types
def _to_utc(value: datetime) -> datetime:
    return value.astimezone(UTC)


UtcDatetime = Annotated[AwareDatetime, AfterValidator(_to_utc)]
Text = Annotated[str, Field(strict=True, min_length=1)]
Id = Annotated[int, Field(strict=True, ge=1)]
Count = Annotated[int, Field(strict=True, ge=0)]
Confidence = Annotated[float, Field(strict=True, ge=0, le=1)]
Flag = Annotated[bool, Field(strict=True)]


class DomainModel(BaseModel):
    """Base: unknown fields rejected, strings stripped, instances immutable.

    Enum and datetime fields accept their string forms so rows read from SQLite
    validate; invalid values fail. Scalars (ids, counts, floats, bools, text) are strict.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, frozen=True)


# ---------------------------------------------------------------- row models
class Run(DomainModel):
    run_id: Id | None = None
    root_name: Text
    root_url: Text
    root_code: Text
    config_json: Text
    status: RunStatus
    started_at: UtcDatetime
    finished_at: UtcDatetime | None = None
    llm_token_budget: Count | None = None
    llm_tokens_used: Count = 0
    llm_cost_usd: Annotated[float, Field(strict=True, ge=0)] = 0.0


class Entity(DomainModel):
    entity_id: Id | None = None
    run_id: Id
    name: Text
    normalized_name: Text
    entity_type: EntityType
    description: str | None = None
    country: str | None = None
    industry: str | None = None
    operating_status: OperatingStatus = OperatingStatus.UNKNOWN
    verification_status: VerificationStatus
    classification_confidence: Confidence | None = None
    confidence: Confidence | None = None
    primary_website_id: Id | None = None
    crawl_state: CrawlState = CrawlState.PENDING
    discovery_method: str | None = None
    discovery_seq: Count
    sibling_order: Count | None = None
    level: Count | None = None
    tree_id: Text | None = None
    hierarchy_path: Text | None = None
    merged_into_id: Id | None = None
    discovered_at: UtcDatetime


class Relationship(DomainModel):
    relationship_id: Id | None = None
    parent_id: Id
    child_id: Id
    relationship_type: RelationshipType
    verification_status: VerificationStatus
    confidence: Confidence
    is_primary: Flag = True
    source_page_id: Id | None = None
    discovery_method: str | None = None
    created_at: UtcDatetime

    @model_validator(mode="after")
    def _no_self_parent(self) -> Relationship:
        if self.parent_id == self.child_id:
            raise ValueError("parent_id and child_id must differ")
        return self


class Evidence(DomainModel):
    evidence_id: Id | None = None
    entity_id: Id
    relationship_id: Id | None = None
    page_id: Id | None = None
    url: Text
    quote: Text
    quote_verified: Flag
    source_class: SourceClass
    supports: Supports
    invocation_id: Id | None = None
    created_at: UtcDatetime


class Website(DomainModel):
    website_id: Id | None = None
    entity_id: Id
    url: Text
    domain: Text
    registrable_domain: Text
    kind: WebsiteKind
    resolution_method: Text
    reachable: Flag | None = None
    last_http_status: Annotated[int, Field(strict=True, ge=100, le=599)] | None = None
    last_checked_at: UtcDatetime | None = None


class ResearchTask(DomainModel):
    """A research_queue row."""

    task_id: Id | None = None
    run_id: Id
    entity_id: Id
    start_url: str | None = None
    depth: Count
    priority: Count
    status: QueueStatus
    attempts: Count = 0
    enqueued_at: UtcDatetime
    started_at: UtcDatetime | None = None
    finished_at: UtcDatetime | None = None
    error: str | None = None
    not_before: UtcDatetime | None = None
