#!/usr/bin/env python
"""Check database structure."""
import pathlib
import sys
sys.path.insert(0, str(pathlib.Path.cwd().resolve() / 'src'))

from corpdisc.db.connection import connect_readonly
from corpdisc.config import load_settings

settings = load_settings()
print(f"Database: {settings.db_path}")

conn = connect_readonly(settings.db_path)
tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()
print("\nTables:")
for t in tables:
    print(f"  {t[0]}")

conn.close()
