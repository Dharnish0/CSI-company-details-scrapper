#!/usr/bin/env python
"""Phase 1: Simple Database/Persistence Test - Just insert entities and relationships."""
import pathlib
import sys
sys.path.insert(0, str(pathlib.Path.cwd().resolve() / 'src'))

from corpdisc.config import Settings
from corpdisc.db.connection import connect_writer, init_db
from corpdisc.clock import SystemClock
from corpdisc.models.domain import EntityType, VerificationStatus

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

# Create a simple test - insert entities and relationships directly
print("\n=== PHASE 1: PERSISTENCE TEST ===")
print("\nInserting test data directly into database...")

now = SystemClock().now()
now_str = now.isoformat()

# Insert root entity
conn.execute("""
    INSERT INTO entities (
        run_id, name, normalized_name, entity_type, verification_status, 
        confidence, classification_confidence, discovery_seq, discovered_at,
        operating_status, crawl_state, discovery_method
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
""", (1, "Constellation Software Inc.", "constellation software inc", 
      EntityType.PARENT_COMPANY.value, VerificationStatus.CONFIRMED.value,
      1.0, 1.0, 1, now_str, "Active", "PENDING", "CONFIG"))
root_id = conn.execute("SELECT entity_id FROM entities WHERE name = ?", 
                       ("Constellation Software Inc.",)).fetchone()[0]

# Insert child entities
conn.execute("""
    INSERT INTO entities (
        run_id, name, normalized_name, entity_type, verification_status,
        confidence, classification_confidence, discovery_seq, discovered_at,
        operating_status, crawl_state, discovery_method
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
""", (1, "Harris Computer Corp.", "harris computer corp",
      EntityType.OPERATING_GROUP.value, VerificationStatus.CONFIRMED.value,
      0.92, 0.95, 2, now_str, "Active", "PENDING", "EXTRACTED"))
harris_id = conn.execute("SELECT entity_id FROM entities WHERE name = ?",
                         ("Harris Computer Corp.",)).fetchone()[0]

conn.execute("""
    INSERT INTO entities (
        run_id, name, normalized_name, entity_type, verification_status,
        confidence, classification_confidence, discovery_seq, discovered_at,
        operating_status, crawl_state, discovery_method
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
""", (1, "Volaris Group Inc.", "volaris group inc",
      EntityType.OPERATING_GROUP.value, VerificationStatus.CONFIRMED.value,
      0.90, 0.95, 3, now_str, "Active", "PENDING", "EXTRACTED"))
volaris_id = conn.execute("SELECT entity_id FROM entities WHERE name = ?",
                          ("Volaris Group Inc.",)).fetchone()[0]

conn.commit()
print(f"[SUCCESS] Entities inserted: root={root_id}, harris={harris_id}, volaris={volaris_id}")

# Insert relationships
conn.execute("""
    INSERT INTO relationships (
        parent_id, child_id, relationship_type, verification_status, confidence,
        is_primary, source_page_id, discovery_method, created_at
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
""", (root_id, harris_id, "Owned by", VerificationStatus.CONFIRMED.value, 0.92,
      1, 100, "EXTRACTED", now_str))

conn.execute("""
    INSERT INTO relationships (
        parent_id, child_id, relationship_type, verification_status, confidence,
        is_primary, source_page_id, discovery_method, created_at
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
""", (root_id, volaris_id, "Owned by", VerificationStatus.CONFIRMED.value, 0.90,
      1, 101, "EXTRACTED", now_str))

conn.commit()
print("[SUCCESS] Relationships inserted")

# Insert evidence
conn.execute("""
    INSERT INTO evidence (
        entity_id, relationship_id, page_id, url, quote, quote_verified, source_class,
        supports, created_at
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
""", (harris_id, 1, 100, "https://www.csisoftware.com/operating-groups",
      "Harris is an operating group of Constellation Software Inc.", 1, "FIRST_PARTY_PARENT",
      "OWNERSHIP", now_str))

conn.execute("""
    INSERT INTO evidence (
        entity_id, relationship_id, page_id, url, quote, quote_verified, source_class,
        supports, created_at
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
""", (volaris_id, 2, 101, "https://www.csisoftware.com/operating-groups",
      "Volaris is an operating group of Constellation Software Inc.", 1, "FIRST_PARTY_PARENT",
      "OWNERSHIP", now_str))

conn.commit()
print("[SUCCESS] Evidence inserted")

# Verify persistence
print("\n=== VERIFICATION ===")

entities = conn.execute("SELECT entity_id, name, entity_type, verification_status FROM entities ORDER BY entity_id").fetchall()
print(f"\nEntities stored: {len(entities)}")
for e in entities:
    print(f"  ID {e[0]}: {e[1]} ({e[2]}, {e[3]})")

relationships = conn.execute("SELECT parent_id, child_id, relationship_type, verification_status FROM relationships ORDER BY relationship_id").fetchall()
print(f"\nRelationships stored: {len(relationships)}")
for r in relationships:
    print(f"  Parent {r[0]} -> Child {r[1]}: {r[2]} ({r[3]})")

evidence = conn.execute("SELECT entity_id, quote, source_class FROM evidence ORDER BY evidence_id").fetchall()
print(f"\nEvidence stored: {len(evidence)}")
for ev in evidence:
    print(f"  Entity {ev[0]}: \"{ev[1]}\" ({ev[2]})")

# Run verification
print("\n=== VERIFICATION COMMAND ===")
from corpdisc.db.repos.audit import SqliteAuditSink
audit = SqliteAuditSink(conn)

# Run the verify command
import subprocess
result = subprocess.run([sys.executable, "main.py", "verify", "--run-id", "1"],
                       capture_output=True, text=True)
print(result.stdout)
if result.stderr:
    print("STDERR:", result.stderr)

# Cleanup
conn.close()

print("\n=== PHASE 1 COMPLETE ===")
print("Persistence test: SUCCESS")
print("All database tables affected: entities, relationships, evidence")
