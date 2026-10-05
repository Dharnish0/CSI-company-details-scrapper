from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from corpdisc.models import domain
from corpdisc.models.domain import (
    CandidateKind,
    Entity,
    EntityType,
    Evidence,
    PageType,
    Relationship,
    RelationshipType,
    ResearchTask,
    ReviewReason,
    Run,
    RunStatus,
    SourceClass,
    Supports,
    VerificationStatus,
    Website,
    WebsiteKind,
)

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def entity(**over):
    data = dict(run_id=1, name="Example Co", normalized_name="example co", entity_type="Company",
                verification_status="REVIEW_REQUIRED", discovery_seq=1, discovered_at=NOW)
    data.update(over)
    return Entity(**data)


def relationship(**over):
    data = dict(parent_id=1, child_id=2, relationship_type="Part of",
                verification_status="CONFIRMED", confidence=0.8, created_at=NOW)
    data.update(over)
    return Relationship(**data)


def evidence(**over):
    data = dict(entity_id=1, url="https://example.com/a", quote="Example is part of Root.",
                quote_verified=True, source_class="FIRST_PARTY_PARENT", supports="OWNERSHIP",
                created_at=NOW)
    data.update(over)
    return Evidence(**data)


# ---------------------------------------------------------------- enums
def test_enum_sizes_and_values():
    assert len(EntityType) == 12 and EntityType("Parent Company") is EntityType.PARENT_COMPANY
    assert len(RelationshipType) == 10 and RelationshipType("Portfolio company of")
    assert {v.value for v in VerificationStatus} == {"CONFIRMED", "REVIEW_REQUIRED", "REJECTED"}
    assert len(SourceClass) == 5 and len(Supports) == 4
    assert len(CandidateKind) == 11 and len(ReviewReason) == 13
    assert len(PageType) == 18
    assert {"PARTNERS", "CUSTOMERS", "CASE_STUDIES", "CAREERS", "BLOG", "NEWS"} <= {p.value for p in PageType}
    assert len(RunStatus) == 6 and len(WebsiteKind) == 3


@pytest.mark.parametrize("enum_cls", [
    domain.EntityType, domain.RelationshipType, domain.VerificationStatus, domain.SourceClass,
    domain.Supports, domain.CandidateKind, domain.ReviewReason, domain.PageType, domain.RunStatus,
    domain.WebsiteKind, domain.QueueStatus, domain.CrawlStatus, domain.CrawlState,
    domain.OperatingStatus,
])
def test_invalid_enum_value_fails(enum_cls):
    with pytest.raises(ValueError):
        enum_cls("not-a-value")


def test_invalid_enum_in_models():
    with pytest.raises(ValidationError):
        entity(entity_type="Product")
    with pytest.raises(ValidationError):
        entity(verification_status="MAYBE")
    with pytest.raises(ValidationError):
        relationship(relationship_type="Friend of")
    with pytest.raises(ValidationError):
        evidence(source_class="RUMOUR")
    with pytest.raises(ValidationError):
        evidence(supports="VIBES")
    with pytest.raises(ValidationError):
        Website(entity_id=1, url="u", domain="d", registrable_domain="d", kind="MIRROR",
                resolution_method="FOOTER")


def test_string_enum_values_accepted():
    e = entity()
    assert e.entity_type is EntityType.COMPANY
    assert e.operating_status.value == "Unknown"
    assert e.crawl_state.value == "PENDING"


# ---------------------------------------------------------------- models
def test_extra_fields_forbidden():
    with pytest.raises(ValidationError):
        entity(bogus=1)
    with pytest.raises(ValidationError):
        relationship(bogus=1)


def test_frozen():
    e = entity()
    with pytest.raises(ValidationError):
        e.name = "Other"  # type: ignore[misc]


def test_blank_names_rejected():
    with pytest.raises(ValidationError):
        entity(name="   ")
    with pytest.raises(ValidationError):
        entity(normalized_name="")
    with pytest.raises(ValidationError):
        evidence(quote="  ")


def test_whitespace_stripped():
    assert entity(name="  Example Co  ").name == "Example Co"


@pytest.mark.parametrize("value", [-0.01, 1.01])
def test_confidence_range(value):
    with pytest.raises(ValidationError):
        relationship(confidence=value)
    with pytest.raises(ValidationError):
        entity(confidence=value)
    with pytest.raises(ValidationError):
        entity(classification_confidence=value)


def test_confidence_bounds_accepted():
    assert relationship(confidence=0).confidence == 0
    assert relationship(confidence=1).confidence == 1


def test_relationship_parent_equals_child_rejected():
    with pytest.raises(ValidationError):
        relationship(parent_id=5, child_id=5)


def test_counters_non_negative():
    with pytest.raises(ValidationError):
        entity(discovery_seq=-1)
    with pytest.raises(ValidationError):
        entity(level=-1)
    with pytest.raises(ValidationError):
        ResearchTask(run_id=1, entity_id=1, depth=-1, priority=0, status="PENDING", enqueued_at=NOW)
    with pytest.raises(ValidationError):
        ResearchTask(run_id=1, entity_id=1, depth=0, priority=0, status="PENDING", enqueued_at=NOW,
                     attempts=-1)
    with pytest.raises(ValidationError):
        Run(root_name="R", root_url="u", root_code="R", config_json="{}", status="RUNNING",
            started_at=NOW, llm_tokens_used=-1)


def test_timestamps_must_be_timezone_aware_and_become_utc():
    with pytest.raises(ValidationError):
        entity(discovered_at=datetime(2026, 1, 1))
    plus2 = timezone(timedelta(hours=2))
    e = entity(discovered_at=datetime(2026, 1, 1, 2, tzinfo=plus2))
    assert e.discovered_at == NOW and e.discovered_at.utcoffset() == timedelta(0)
    assert entity(discovered_at="2026-01-01T00:00:00+00:00").discovered_at == NOW


def test_scalars_are_strict():
    with pytest.raises(ValidationError):
        entity(discovery_seq="1")
    with pytest.raises(ValidationError):
        evidence(quote_verified="yes")


def test_ids_optional_until_persisted():
    assert entity().entity_id is None
    assert entity(entity_id=3).entity_id == 3
    with pytest.raises(ValidationError):
        entity(entity_id=0)


def test_other_models_construct():
    Run(root_name="R", root_url="https://example.com/", root_code="R", config_json="{}",
        status="PAUSED", started_at=NOW)
    Website(entity_id=1, url="https://a.example/", domain="a.example", registrable_domain="a.example",
            kind="OFFICIAL", resolution_method="LINKED_ON_PARENT", reachable=None)
    with pytest.raises(ValidationError):
        Website(entity_id=1, url="https://a.example/", domain="a", registrable_domain="a",
                kind="OFFICIAL", resolution_method="X", last_http_status=99)
    ResearchTask(run_id=1, entity_id=2, depth=0, priority=1, status="DONE", enqueued_at=NOW)
    assert evidence().quote_verified is True


def test_no_agent_contracts_in_m0():
    for name in ("AgentRequest", "AgentResponse", "VerifiedEvidence", "CommitPlan"):
        assert not hasattr(domain, name)
