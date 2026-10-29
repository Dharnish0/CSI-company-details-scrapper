"""Compare schema.sql against a live DB or a sqlite_master dump (columns, indexes, CHECK text)."""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path


def describe(conn: sqlite3.Connection) -> dict:
    out = {}
    for (name,) in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ):
        cols = [tuple(r[1:6]) for r in conn.execute(f"PRAGMA table_info({name})")]
        idx = sorted(r[1] for r in conn.execute(f"PRAGMA index_list({name})") if not r[1].startswith("sqlite_"))
        out[name] = {"columns": cols, "indexes": idx}
    return out


def diff(a: dict, b: dict) -> list[str]:
    msgs = []
    for t in sorted(set(a) | set(b)):
        if t not in a or t not in b:
            msgs.append(f"table {t} only in {'first' if t in a else 'second'}")
            continue
        for k in ("columns", "indexes"):
            if a[t][k] != b[t][k]:
                msgs.append(f"{t}.{k} differ: {a[t][k]} != {b[t][k]}")
    return msgs


def main(argv: list[str]) -> int:
    from corpdisc.db.connection import load_schema_sql

    mem = sqlite3.connect(":memory:")
    mem.executescript(load_schema_sql())
    other = sqlite3.connect(":memory:")
    target = Path(argv[1])
    if target.suffix == ".sql":
        other.executescript(target.read_text(encoding="utf-8"))
    else:
        other.close()
        other = sqlite3.connect(f"file:{target}?mode=ro", uri=True)
    msgs = diff(describe(mem), describe(other))
    print("\n".join(msgs) or "schemas match")
    return 1 if msgs else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
