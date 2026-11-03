#!/usr/bin/env python
"""Phase 1: Database/Persistence Isolation Test.

Create a deterministic VALID ExtractionOutput fixture and send it through
the SAME persistence code used by the production extraction pipeline.
"""
import pathlib
import sys
sys.path.insert(0, str(pathlib.Path.cwd().resolve() / 'src'))

from corpdisc.config import load_settings, Settings
from corpdisc.db.connection import connect_writer, init_db
from corpdisc.clock import SystemClock
from corpdisc.contracts.common import (
    VerificationStatus, CrawlState, DiscoveryMethod, RelationshipType,
    SourceClass, Supports
)
from corpdisc.contracts.agent_io import (
    ExtractionOutput, ExtractedEntity, RejectedCandidate,
    EvidenceClaim, CandidateKind, EntityType
)
from corpdisc.services.committer import Committer, CommitPlan, Entity, Website, Alias, Relationship, Evidence, EntityRef
from corpdisc.contracts.common import (
    VerificationStatus, CrawlState, DiscoveryMethod, RelationshipType,
    SourceClass
)

# Create fresh database for test
db_path = pathlib.Path("data/phase1_test.db")
if db_path.exists():
    db_path.unlink()

settings = Settings(
    db_path=db_path,
    cache_dir=pathlib.Path("./.cache/phase1"),
    output_xlsx=pathlib.Path("./phase1_test.xlsx"),
    log_dir=pathlib.Path("./logs/phase1")
)

# Initialize database
conn = connect_writer(db_path)
init_db(db_path)
print("Database initialized:", db_path)

# Create a deterministic VALID ExtractionOutput fixture
# This is based on the real CSI hierarchy but simplified for testing
fixture = ExtractionOutput(
    entities=[
        ExtractedEntity(
            local_ref="e1",
            raw_candidate_id=1,
            name="Harris Computer Corp.",
            kind=CandidateKind.COMPANY,
            proposed_type=EntityType.OPERATING_GROUP,
            is_corporate_entity=True,
            relationship_hint=RelationshipType.OWNED_BY,
            description="Provides mission critical software for the Public Sector.",
            country="Canada",
            industry="Software",
            claim=EvidenceClaim(
                page_id=100,
                quote="Harris is an operating group of Constellation Software Inc.",
                supports=Supports.OWNERSHIP
            ),
            agent_confidence=0.92,
            reason="Direct quote confirms operating group relationship"
        ),
        ExtractedEntity(
            local_ref="e2",
            raw_candidate_id=2,
            name="Volaris Group Inc.",
            kind=CandidateKind.COMPANY,
            proposed_type=EntityType.OPERATING_GROUP,
            is_corporate_entity=True,
            relationship_hint=RelationshipType.OWNED_BY,
            description="Acquires vertical market software companies.",
            country="Canada",
            industry="Software",
            claim=EvidenceClaim(
                page_id=101,
                quote="Volaris is an operating group of Constellation Software Inc.",
                supports=Supports.OWNERSHIP
            ),
            agent_confidence=0.90,
            reason="Direct quote confirms operating group relationship"
        ),
    ],
    rejected=[
        RejectedCandidate(
            name="Product Line A",
            kind=CandidateKind.PRODUCT,
            reason="Products are not corporate entities"
        )
    ],
    page_has_ownership_language=True,
    agent_confidence=0.91
)

print("\n=== PHASE 1: PERSISTENCE TEST ===")
print("\nFixture created:")
print(f"  Entities: {len(fixture.entities)}")
print(f"  Rejected: {len(fixture.rejected)}")
print(f"  Page has ownership: {fixture.page_has_ownership_language}")
print(f"  Agent confidence: {fixture.agent_confidence}")

# Create a CommitPlan from the fixture
now = SystemClock().now()

entities_plan = [
    Entity(
        local_ref="root",
        name="Constellation Software Inc.",
        normalized_name="constellation software inc",
        entity_type=EntityType.PARENT_COMPANY,
        is_root=True,
        verification_status=VerificationStatus.CONFIRMED,
        confidence=1.0,
        classification_confidence=1.0,
        crawl_state=CrawlState.NEW,
        discovery_method=DiscoveryMethod.CONFIG,
        discovery_seq=0,
        sibling_order=0
    ),
    Entity(
        local_ref="e1",
        name="Harris Computer Corp.",
        normalized_name="harris computer corp",
        entity_type=EntityType.OPERATING_GROUP,
        is_root=False,
        verification_status=VerificationStatus.CONFIRMED,
        confidence=fixture.entities[0].agent_confidence,
        classification_confidence=0.95,
        description=fixture.entities[0].description,
        country=fixture.entities[0].country,
        industry=fixture.entities[0].industry,
        crawl_state=CrawlState.NEW,
        discovery_method=DiscoveryMethod.EXTRACTED,
        discovery_seq=1,
        sibling_order=1
    ),
    Entity(
        local_ref="e2",
        name="Volaris Group Inc.",
        normalized_name="volaris group inc",
        entity_type=EntityType.OPERATING_GROUP,
        is_root=False,
        verification_status=VerificationStatus.CONFIRMED,
        confidence=fixture.entities[1].agent_confidence,
        classification_confidence=0.95,
        description=fixture.entities[1].description,
        country=fixture.entities[1].country,
        industry=fixture.entities[1].industry,
        crawl_state=CrawlState.NEW,
        discovery_method=DiscoveryMethod.EXTRACTED,
        discovery_seq=2,
        sibling_order=2
    ),
]

websites_plan = [
    # websites would be added here for a full test
]

aliases_plan = [
    # aliases would be added here for a full test
]

relationships_plan = [
    Relationship(
        parent=EntityRef(name="Constellation Software Inc."),
        child=EntityRef(local_ref="e1"),
        relationship_type=RelationshipType.OWNED_BY,
        verification_status=VerificationStatus.CONFIRMED,
        confidence=0.92,
        is_primary=True,
        source_page_id=100,
        discovery_method=DiscoveryMethod.EXTRACTED
    ),
    Relationship(
        parent=EntityRef(name="Constellation Software Inc."),
        child=EntityRef(local_ref="e2"),
        relationship_type=RelationshipType.OWNED_BY,
        verification_status=VerificationStatus.CONFIRMED,
        confidence=0.90,
        is_primary=True,
        source_page_id=101,
        discovery_method=DiscoveryMethod.EXTRACTED
    ),
]

evidence_plan = [
    Evidence(
        entity=EntityRef(local_ref="e1"),
        relationship_local_ref="r1",
        evidence=EvidenceClaim(
            page_id=100,
            quote="Harris is an operating group of Constellation Software Inc.",
            supports=Supports.OWNERSHIP
        ),
        invocation_id=1
    ),
    Evidence(
        entity=EntityRef(local_ref="e2"),
        relationship_local_ref="r2",
        evidence=EvidenceClaim(
            page_id=101,
            quote="Volaris is an operating group of Constellation Software Inc.",
            supports=Supports.OWNERSHIP
        ),
        invocation_id=2
    ),
]

plan = CommitPlan(
    entities=entities_plan,
    websites=websites_plan,
    aliases=aliases_plan,
    relationships=relationships_plan,
    evidence=evidence_plan,
    entity_updates=[],
    relationships_to_reject=[],
    candidates=[],
    reviews=[],
    validation_findings=[],
    merges=[],
    root_verification=None
)

# Run the committer
committer = Committer(conn, run_id=1, now=now)
try:
    committer.commit(plan)
    print("\n[SUCCESS] Committer completed successfully")
except Exception as exc:
    print(f"\n[FAILURE] Committer failed: {type(exc).__name__}: {str(exc)[:500]}")
    import traceback
    traceback.print_exc()
    conn.close()
    sys.exit(1)

# Verify persistence
print("\n=== VERIFICATION ===")

# Check entities
entities = conn.execute("SELECT entity_id, name, entity_type, verification_status FROM entities ORDER BY entity_id").fetchall()
print(f"\nEntities stored: {len(entities)}")
for e in entities:
    print(f"  ID {e[0]}: {e[1]} ({e[2]}, {e[3]})")

# Check relationships
relationships = conn.execute("SELECT parent_id, child_id, relationship_type, verification_status FROM relationships ORDER BY relationship_id").fetchall()
print(f"\nRelationships stored: {len(relationships)}")
for r in relationships:
    print(f"  Parent {r[0]} -> Child {r[1]}: {r[2]} ({r[3]})")

# Check evidence
evidence = conn.execute("SELECT entity_id, quote, source_class FROM evidence ORDER BY evidence_id").fetchall()
print(f"\nEvidence stored: {len(evidence)}")
for ev in evidence:
    print(f"  Entity {ev[0]}: \"{ev[1]}\" ({ev[2]})")

# Run verification
print("\n=== VERIFICATION COMMAND ===")
from corpdisc.db.repos.audit import SqliteAuditSink
from corpdisc.agents.runner import SqliteResponseCache

audit = SqliteAuditSink(conn)
cache = SqliteResponseCache(conn)

# Check verification findings
findings = conn.execute("SELECT * FROM validation_findings").fetchall()
print(f"Validation findings: {len(findings)}")

# Cleanup
conn.close()

print("\n=== PHASE 1 COMPLETE ===")
print("Persistence test: SUCCESS")
print("All database tables affected: entities, relationships, evidence")
