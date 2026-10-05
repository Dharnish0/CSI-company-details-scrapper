from __future__ import annotations

import sqlite3
from enum import StrEnum
from pathlib import Path

import pytest

from corpdisc.db.connection import (
    EXPECTED_TABLES,
    DatabaseError,
    connect_readonly,
    connect_writer,
    init_db,
    verify_db,
)
from corpdisc.models.domain import (
    CrawlState,
    CrawlStatus,
    EntityType,
    QueueStatus,
    RelationshipType,
    RunStatus,
    SourceClass,
    Supports,
    VerificationStatus,
    WebsiteKind,
)

NOW = "2026-01-01T00:00:00+00:00"


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "db" / "test.db"
    init_db(path)
    return path


@pytest.fixture
def conn(db_path: Path):
    c = connect_writer(db_path)
    yield c
    c.close()


def insert(conn: sqlite3.Connection, table: str, **cols) -> int:
    names = ", ".join(cols)
    marks = ", ".join("?" for _ in cols)
    cur = conn.execute(f"INSERT INTO {table} ({names}) VALUES ({marks})", tuple(cols.values()))
    return cur.lastrowid


def add_run(conn, **over) -> int:
    cols = dict(root_name="Root", root_url="https://example.com/", root_code="ROOT",
                config_json="{}", status="RUNNING", started_at=NOW)
    cols.update(over)
    return insert(conn, "runs", **cols)


def add_entity(conn, run_id: int, name: str = "E", **over) -> int:
    cols = dict(run_id=run_id, name=name, normalized_name=name.lower(), entity_type="Company",
                verification_status="CONFIRMED", discovery_seq=1, discovered_at=NOW)
    cols.update(over)
    return insert(conn, "entities", **cols)


def add_page(conn, run_id: int, url: str = "https://example.com/a", **over) -> int:
    cols = dict(run_id=run_id, url=url, canonical_url=url, domain="example.com",
                crawl_status="OK", crawled_at=NOW)
    cols.update(over)
    return insert(conn, "crawl_pages", **cols)


def add_rel(conn, parent: int, child: int, **over) -> int:
    cols = dict(parent_id=parent, child_id=child, relationship_type="Part of",
                verification_status="CONFIRMED", confidence=0.9, created_at=NOW)
    cols.update(over)
    return insert(conn, "relationships", **cols)


# ---------------------------------------------------------------- init
def test_init_creates_exactly_expected_tables(db_path: Path):
    assert len(EXPECTED_TABLES) == 17
    report = verify_db(db_path)
    assert sorted(report.tables) == sorted(EXPECTED_TABLES)
    assert report.table_count == 17
    assert report.foreign_keys == 1
    assert report.journal_mode == "wal"


def test_init_creates_parent_directory(tmp_path: Path):
    path = tmp_path / "a" / "b" / "c.db"
    init_db(path)
    assert path.is_file()


def test_init_is_idempotent_and_keeps_data(db_path: Path):
    c = connect_writer(db_path)
    add_run(c)
    c.commit()
    c.close()
    report = init_db(db_path)
    assert report.table_count == 17
    c = connect_writer(db_path)
    assert c.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1
    c.close()


def test_init_rejects_memory_path():
    with pytest.raises(DatabaseError):
        init_db(":memory:")


def test_expected_indexes_exist(conn):
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    for idx in ("idx_entities_norm", "idx_entities_tree", "idx_websites_domain",
                "idx_one_primary_parent", "idx_queue_ready", "idx_invocations_cache"):
        assert idx in names


def test_no_views_or_triggers(conn):
    rows = conn.execute("SELECT type, name FROM sqlite_master WHERE type IN ('view','trigger')").fetchall()
    assert rows == []


def test_part2_schema_additions(conn):
    cols = lambda t: {r["name"] for r in conn.execute(f"PRAGMA table_info({t})")}  # noqa: E731
    assert {"llm_token_budget", "llm_tokens_used", "llm_cost_usd"} <= cols("runs")
    assert "not_before" in cols("research_queue")
    assert "invocation_id" in cols("llm_calls")
    assert "invocation_id" in cols("evidence") and "llm_call_id" not in cols("evidence")
    assert {"extraction_invocation_id", "gate_code", "normalized_name"} <= cols("candidates")


# ---------------------------------------------------------------- pragmas
def test_foreign_keys_on_for_connections(db_path: Path):
    for opener in (connect_writer, connect_readonly):
        c = opener(db_path)
        assert c.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        c.close()


def test_wal_mode_on_file_db(conn):
    assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"


def test_memory_connection_works():
    c = connect_writer(":memory:")
    assert c.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    c.close()


def test_readonly_rejects_writes(db_path: Path):
    c = connect_readonly(db_path)
    assert c.execute("PRAGMA query_only").fetchone()[0] == 1
    assert c.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
    with pytest.raises(sqlite3.OperationalError):
        add_run(c)
    c.close()


def test_readonly_missing_file(tmp_path: Path):
    with pytest.raises(DatabaseError):
        connect_readonly(tmp_path / "missing.db")


# ---------------------------------------------------------------- foreign keys
def test_fk_relationship_missing_entity(conn):
    with pytest.raises(sqlite3.IntegrityError):
        add_rel(conn, 999, 1000)


def test_fk_evidence_missing_entity(conn):
    with pytest.raises(sqlite3.IntegrityError):
        insert(conn, "evidence", entity_id=999, url="u", quote="q", quote_verified=1,
               source_class="THIRD_PARTY", supports="EXISTENCE", created_at=NOW)


def test_fk_website_missing_entity(conn):
    with pytest.raises(sqlite3.IntegrityError):
        insert(conn, "websites", entity_id=999, url="https://a.example/", domain="a.example",
               registrable_domain="a.example", kind="OFFICIAL", resolution_method="FOOTER")


def test_fk_entity_missing_run(conn):
    with pytest.raises(sqlite3.IntegrityError):
        add_entity(conn, 999)


# ---------------------------------------------------------------- constraints
@pytest.fixture
def world(conn):
    run = add_run(conn)
    a, b, c = (add_entity(conn, run, n, discovery_seq=i) for i, n in enumerate("abc", 1))
    return dict(run=run, a=a, b=b, c=c)


def test_invalid_entity_type(conn, world):
    with pytest.raises(sqlite3.IntegrityError):
        add_entity(conn, world["run"], "x", entity_type="Product")


def test_invalid_relationship_type(conn, world):
    with pytest.raises(sqlite3.IntegrityError):
        add_rel(conn, world["a"], world["b"], relationship_type="Friend of")


def test_invalid_verification_status(conn, world):
    with pytest.raises(sqlite3.IntegrityError):
        add_rel(conn, world["a"], world["b"], verification_status="MAYBE")


def test_self_relationship_rejected(conn, world):
    with pytest.raises(sqlite3.IntegrityError):
        add_rel(conn, world["a"], world["a"])


def test_duplicate_edge_rejected(conn, world):
    add_rel(conn, world["a"], world["b"])
    with pytest.raises(sqlite3.IntegrityError):
        add_rel(conn, world["a"], world["b"], is_primary=0)


def test_second_confirmed_primary_parent_rejected(conn, world):
    add_rel(conn, world["a"], world["c"])
    with pytest.raises(sqlite3.IntegrityError):
        add_rel(conn, world["b"], world["c"])


def test_second_parent_allowed_when_not_primary_or_not_confirmed(conn, world):
    add_rel(conn, world["a"], world["c"])
    add_rel(conn, world["b"], world["c"], is_primary=0)
    other = add_entity(conn, world["run"], "d", discovery_seq=9)
    add_rel(conn, world["a"], other, verification_status="REVIEW_REQUIRED")
    add_rel(conn, world["b"], other, verification_status="REVIEW_REQUIRED")


def test_duplicate_canonical_url_per_run(conn, world):
    add_page(conn, world["run"])
    with pytest.raises(sqlite3.IntegrityError):
        add_page(conn, world["run"], url="https://example.com/a?x=1", canonical_url="https://example.com/a")


def test_same_canonical_url_other_run_ok(conn, world):
    add_page(conn, world["run"])
    add_page(conn, add_run(conn))


def test_duplicate_queue_row_rejected(conn, world):
    cols = dict(run_id=world["run"], entity_id=world["a"], depth=0, priority=1,
                status="PENDING", enqueued_at=NOW)
    insert(conn, "research_queue", **cols)
    with pytest.raises(sqlite3.IntegrityError):
        insert(conn, "research_queue", **cols)


def test_duplicate_candidate_key_rejected(conn, world):
    page = add_page(conn, world["run"])
    cols = dict(run_id=world["run"], source_page_id=page, parent_entity_id=world["a"],
                raw_name="Foo", normalized_name="foo")
    insert(conn, "candidates", **cols)
    with pytest.raises(sqlite3.IntegrityError):
        insert(conn, "candidates", **cols)


def test_duplicate_prompt_hash_rejected(conn):
    cols = dict(task_type="t", model="m", prompt_hash="h", request_json="{}", valid=1, created_at=NOW)
    insert(conn, "llm_calls", **cols)
    with pytest.raises(sqlite3.IntegrityError):
        insert(conn, "llm_calls", **cols)


def test_invocation_check_constraints(conn, world):
    base = dict(run_id=world["run"], agent="extraction", correlation_id="c", schema_version="1",
                prompt_version="1", input_hash="h", request_json="{}", status="OK", started_at=NOW)
    insert(conn, "agent_invocations", **base)
    for bad in (dict(agent="free_roaming"), dict(status="DONE"), dict(failure_kind="OOPS")):
        with pytest.raises(sqlite3.IntegrityError):
            insert(conn, "agent_invocations", **{**base, **bad})


# ---------------------------------------------------------------- enum / schema drift
def _drift_cases(conn, w) -> dict[type[StrEnum], callable]:
    page = add_page(conn, w["run"], url="https://example.com/drift")
    counter = iter(range(1000, 100000))

    def entity_col(col):
        return lambda v: add_entity(conn, w["run"], f"n{next(counter)}", **{col: v})

    def rel(v):
        child = add_entity(conn, w["run"], f"c{next(counter)}", discovery_seq=next(counter))
        return add_rel(conn, w["a"], child, relationship_type=v, verification_status="REVIEW_REQUIRED")

    def evidence(col):
        def f(v):
            cols = dict(entity_id=w["a"], url="u", quote="q", quote_verified=1,
                        source_class="THIRD_PARTY", supports="EXISTENCE", created_at=NOW)
            cols[col] = v
            return insert(conn, "evidence", **cols)
        return f

    return {
        EntityType: entity_col("entity_type"),
        VerificationStatus: entity_col("verification_status"),
        CrawlState: entity_col("crawl_state"),
        RelationshipType: rel,
        RunStatus: lambda v: add_run(conn, status=v),
        WebsiteKind: lambda v: insert(
            conn, "websites", entity_id=w["a"], url=f"https://{next(counter)}.example/",
            domain="d", registrable_domain="d", kind=v, resolution_method="FOOTER"),
        QueueStatus: lambda v: insert(
            conn, "research_queue", run_id=add_run(conn), entity_id=w["a"], depth=0, priority=0,
            status=v, enqueued_at=NOW),
        CrawlStatus: lambda v: add_page(conn, w["run"], url=f"https://example.com/{next(counter)}",
                                        crawl_status=v),
        SourceClass: evidence("source_class"),
        Supports: evidence("supports"),
    }


def test_enum_values_match_schema_checks(conn, world):
    cases = _drift_cases(conn, world)
    assert len(cases) == 10
    for enum_cls, insert_value in cases.items():
        for member in enum_cls:
            insert_value(member.value)  # DB accepts every enum value
            assert enum_cls(member.value) is member  # model layer accepts it too
        with pytest.raises(sqlite3.IntegrityError):
            insert_value("BOGUS_VALUE")  # DB rejects a bogus value
        with pytest.raises(ValueError):
            enum_cls("BOGUS_VALUE")  # model layer rejects it as well
