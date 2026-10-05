"""SQLite helpers for tests: a fresh initialized database and seeding functions."""
from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from corpdisc.clock import iso_utc
from corpdisc.db.connection import connect_writer, init_db

T0 = datetime(2025, 1, 1, 12, 0, 0, tzinfo=UTC)


def make_db(tmp_path: Path, name: str = "test.db") -> sqlite3.Connection:
    path = tmp_path / "data" / name
    init_db(path)
    return connect_writer(path)


def seed_run(conn: sqlite3.Connection, *, root_name: str = "Example Holdings", root_code: str = "EX",
             token_budget: int | None = None) -> int:
    cur = conn.execute(
        "INSERT INTO runs(root_name, root_url, root_code, config_json, status, started_at, llm_token_budget) "
        "VALUES (?,?,?,?,?,?,?)",
        (root_name, "https://example.test/", root_code, "{}", "RUNNING", iso_utc(T0), token_budget),
    )
    conn.commit()
    return int(cur.lastrowid)


def seed_entity(conn: sqlite3.Connection, run_id: int, name: str, *, normalized: str | None = None,
                entity_type: str = "Company", status: str = "CONFIRMED", seq: int | None = None,
                confidence: float | None = 0.9, country: str | None = None,
                merged_into: int | None = None, crawl_state: str = "PENDING") -> int:
    if seq is None:
        seq = conn.execute("SELECT COALESCE(MAX(discovery_seq), 0) + 1 FROM entities WHERE run_id=?",
                           (run_id,)).fetchone()[0]
    cur = conn.execute(
        "INSERT INTO entities(run_id, name, normalized_name, entity_type, verification_status, confidence, "
        "classification_confidence, discovery_seq, discovered_at, country, merged_into_id, crawl_state) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (run_id, name, normalized or name.lower(), entity_type, status, confidence, confidence, seq,
         iso_utc(T0), country, merged_into, crawl_state),
    )
    conn.commit()
    return int(cur.lastrowid)


def seed_relationship(conn: sqlite3.Connection, parent: int, child: int, *, status: str = "CONFIRMED",
                      rtype: str = "Owned by", confidence: float = 0.9, primary: int = 1) -> int:
    cur = conn.execute(
        "INSERT INTO relationships(parent_id, child_id, relationship_type, verification_status, confidence, "
        "is_primary, created_at) VALUES (?,?,?,?,?,?,?)",
        (parent, child, rtype, status, confidence, primary, iso_utc(T0)),
    )
    conn.commit()
    return int(cur.lastrowid)


def seed_website(conn: sqlite3.Connection, entity_id: int, url: str, registrable: str,
                 kind: str = "OFFICIAL") -> int:
    cur = conn.execute(
        "INSERT INTO websites(entity_id, url, domain, registrable_domain, kind, resolution_method) "
        "VALUES (?,?,?,?,?,?)",
        (entity_id, url, registrable, registrable, kind, "TEST"),
    )
    conn.commit()
    return int(cur.lastrowid)


def seed_page(conn: sqlite3.Connection, run_id: int, url: str, *, domain: str = "example.test",
              status: str = "OK", text_path: str | None = None) -> int:
    cur = conn.execute(
        "INSERT INTO crawl_pages(run_id, url, canonical_url, domain, crawl_status, text_path, crawled_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (run_id, url, url, domain, status, text_path, iso_utc(T0)),
    )
    conn.commit()
    return int(cur.lastrowid)


def count(conn: sqlite3.Connection, table: str) -> int:
    return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])  # test-only, fixed names
