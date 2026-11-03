#!/usr/bin/env python
"""Check run state."""
import pathlib
import sys
sys.path.insert(0, str(pathlib.Path.cwd().resolve() / 'src'))

from corpdisc.db.connection import connect_readonly
from corpdisc.config import load_settings

settings = load_settings()
conn = connect_readonly(settings.db_path)

# Get the latest run
latest_run = conn.execute("SELECT run_id, status FROM runs ORDER BY run_id DESC LIMIT 1").fetchone()
if latest_run:
    print(f"Latest run: {latest_run[0]} (status: {latest_run[1]})")
else:
    print("No runs found")

# Check research queue
queue = conn.execute("SELECT * FROM research_queue LIMIT 5").fetchall()
print(f"\nResearch queue sample:")
for r in queue:
    print(f"  {r}")

# Check entities
entities = conn.execute("SELECT entity_id, name, verification_status FROM entities LIMIT 10").fetchall()
print(f"\nEntities ({len(entities)} total):")
for e in entities:
    print(f"  ID {e[0]}: {e[1]} ({e[2]})")

# Check agent invocations
invocations = conn.execute("SELECT agent, status, COUNT(*) FROM agent_invocations GROUP BY agent, status").fetchall()
print(f"\nAgent invocations:")
for i in invocations:
    print(f"  {i[0]} {i[1]}: {i[2]}")

conn.close()
