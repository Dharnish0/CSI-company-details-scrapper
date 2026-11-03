"""Create empty, schema-only benchmark databases by copying DDL (no rows) from the live DB."""
import os
import sqlite3

src = sqlite3.connect("data/csi_hierarchy.db")
rows = src.execute(
    "SELECT type, sql FROM sqlite_master WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%'"
).fetchall()
ddl = [s for t, s in rows if t == "table"] + [s for t, s in rows if t != "table"]
for name in ("gemini", "groq", "ollama_gemini", "ollama_mistral"):
    path = f"bench/{name}.db"
    for ext in ("", "-wal", "-shm"):
        if os.path.exists(path + ext):
            os.remove(path + ext)
    c = sqlite3.connect(path)
    for s in ddl:
        c.execute(s)
    c.commit()
    tables = c.execute("select count(*) from sqlite_master where type='table'").fetchone()[0]
    runs = c.execute("select count(*) from runs").fetchone()[0]
    print(name, tables, "tables", runs, "runs")
    c.close()
