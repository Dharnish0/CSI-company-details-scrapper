#!/usr/bin/env python
"""Check current database tables."""

import sqlite3

conn = sqlite3.connect("data/csi_hierarchy.db")
cursor = conn.cursor()
cursor.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
tables = [r[0] for r in cursor.fetchall()]
print("Tables:", tables)
conn.close()
