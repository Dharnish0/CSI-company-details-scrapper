#!/usr/bin/env python
"""Check run 22 details."""
import pathlib
import sys
sys.path.insert(0, str(pathlib.Path.cwd().resolve() / 'src'))

from corpdisc.db.connection import connect_readonly
from corpdisc.config import load_settings

settings = load_settings()
conn = connect_readonly(settings.db_path)

# Get run 22
run22 = conn.execute("SELECT * FROM runs WHERE run_id=22").fetchone()
print(f"Run 22: id={run22['run_id']}, status={run22['status']}, run_at={run22['run_at']}")

# Check agent invocations
invocations = conn.execute("SELECT * FROM agent_invocations WHERE run_id=22").fetchall()
print(f"\nAgent invocations for run 22 ({len(invocations)}):")
for i in invocations:
    print(f"  agent={i['agent']}, status={i['status']}, kind={i['kind']}")
    print(f"    started_at={i['started_at']}, finished_at={i['finished_at']}")

conn.close()
