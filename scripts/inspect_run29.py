#!/usr/bin/env python
"""Inspect Run 29 database details."""

import sqlite3
import json

conn = sqlite3.connect("data/csi_hierarchy.db")
cursor = conn.cursor()

# List tables
cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
tables = cursor.fetchall()
print("Tables:", [t[0] for t in tables])
print()

# Get run info
cursor.execute("SELECT * FROM runs WHERE run_id = 29")
run = cursor.fetchone()
print("Run 29:")
print(f"  run_id: {run[0]}")
print(f"  root_company: {run[1]}")
print(f"  status: {run[2]}")
print(f"  started_at: {run[3]}")
print(f"  finished_at: {run[4]}")
settings = json.loads(run[5])
print(f"  llm_provider: {settings.get('llm_provider')}")
print(f"  llm_model: {settings.get('llm_model')}")
print(f"  groq_model: {settings.get('groq_model')}")
print()

# Get audit records
cursor.execute("SELECT * FROM audit ORDER BY id DESC LIMIT 20")
records = cursor.fetchall()
print("Recent audit records:")
for rec in records:
    print(f"  id={rec[0]}, run_id={rec[1]}, corr={rec[2]}, agent={rec[3]}, provider={rec[4]}, model={rec[5]}")
    print(f"    status={rec[6]}, error_type={rec[7]}, error_message={rec[8]}")
    print(f"    tokens_in={rec[9]}, tokens_out={rec[10]}, latency_ms={rec[11]}")
    print(f"    cache_hit={rec[12]}, transport_failed={rec[13]}, schema_failed={rec[14]}")
    print(f"    req_bytes={rec[15]}, response_bytes={rec[16]}")
    print()

conn.close()
