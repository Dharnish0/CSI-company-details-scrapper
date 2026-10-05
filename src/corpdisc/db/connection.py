"""SQLite connections and database initialization.

Single-writer principle (ARCHITECTURE.md sections 5 and 26.1): all writes go
through ONE writer connection (foreign keys ON, WAL). Reads for agent tools use
a separate connection with ``PRAGMA query_only=ON``. There is no repository or
CRUD layer in M0; this module only opens, initializes and verifies the database.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

BUSY_TIMEOUT_MS = 5000
MEMORY_DB = ":memory:"

EXPECTED_TABLES: tuple[str, ...] = (
    "runs",
    "entities",
    "entity_aliases",
    "websites",
    "relationships",
    "evidence",
    "crawl_pages",
    "domain_stats",
    "candidates",
    "research_queue",
    "crawl_errors",
    "llm_calls",
    "review_items",
    "validation_findings",
    "agent_invocations",
    "agent_tool_calls",
    "gate_rejections",
)


class DatabaseError(RuntimeError):
    """Raised when the database cannot be opened, initialized or verified."""


@dataclass(frozen=True)
class InitReport:
    db_path: str
    tables: tuple[str, ...]
    foreign_keys: int
    journal_mode: str

    @property
    def table_count(self) -> int:
        return len(self.tables)


def _is_memory(db_path: str | Path) -> bool:
    return str(db_path) == MEMORY_DB


def connect_writer(db_path: str | Path) -> sqlite3.Connection:
    """Open the single writer connection: foreign keys ON, WAL, Row factory.

    In-memory databases cannot use WAL and keep SQLite's ``memory`` mode (tests only).
    """
    memory = _is_memory(db_path)
    try:
        if not memory:
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS};")
        conn.execute("PRAGMA foreign_keys=ON;")
        mode = conn.execute("PRAGMA journal_mode=WAL;").fetchone()[0]
    except (OSError, sqlite3.Error) as exc:
        raise DatabaseError(f"cannot open database {db_path}: {exc}") from exc
    if not memory and str(mode).lower() != "wal":
        conn.close()
        raise DatabaseError(f"could not enable WAL on {db_path} (journal_mode={mode})")
    return conn


def connect_readonly(db_path: str | Path) -> sqlite3.Connection:
    """Open a read-only connection (``PRAGMA query_only=ON``). The file must exist."""
    if not _is_memory(db_path) and not Path(db_path).is_file():
        raise DatabaseError(f"database file not found: {db_path}")
    try:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS};")
        conn.execute("PRAGMA foreign_keys=ON;")
        conn.execute("PRAGMA query_only=ON;")
    except sqlite3.Error as exc:
        raise DatabaseError(f"cannot open database {db_path}: {exc}") from exc
    return conn


def load_schema_sql() -> str:
    return resources.files("corpdisc.db").joinpath("schema.sql").read_text(encoding="utf-8")


def _table_names(conn: sqlite3.Connection) -> tuple[str, ...]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' "
        "ORDER BY name"
    ).fetchall()
    return tuple(r[0] for r in rows)


def verify_db(db_path: str | Path) -> InitReport:
    """Reopen the database and check pragmas, integrity and expected tables."""
    conn = connect_writer(db_path)
    try:
        fk = conn.execute("PRAGMA foreign_keys;").fetchone()[0]
        mode = str(conn.execute("PRAGMA journal_mode;").fetchone()[0]).lower()
        integrity = conn.execute("PRAGMA integrity_check;").fetchone()[0]
        fk_violations = conn.execute("PRAGMA foreign_key_check;").fetchall()
        tables = _table_names(conn)
    except sqlite3.Error as exc:
        raise DatabaseError(f"verification failed for {db_path}: {exc}") from exc
    finally:
        conn.close()

    if fk != 1:
        raise DatabaseError("PRAGMA foreign_keys is not ON")
    if not _is_memory(db_path) and mode != "wal":
        raise DatabaseError(f"journal_mode is {mode}, expected wal")
    if integrity != "ok":
        raise DatabaseError(f"integrity_check failed: {integrity}")
    if fk_violations:
        raise DatabaseError(f"foreign_key_check reported {len(fk_violations)} violation(s)")
    missing = sorted(set(EXPECTED_TABLES) - set(tables))
    if missing:
        raise DatabaseError(f"missing tables: {', '.join(missing)}")
    return InitReport(str(db_path), tables, fk, mode)


def init_db(db_path: str | Path) -> InitReport:
    """Create all tables from schema.sql (idempotent), then verify the database."""
    if _is_memory(db_path):
        raise DatabaseError("init_db needs a file path; in-memory databases cannot be reopened")
    conn = connect_writer(db_path)
    try:
        conn.executescript(load_schema_sql())
    except sqlite3.Error as exc:
        raise DatabaseError(f"schema initialization failed for {db_path}: {exc}") from exc
    finally:
        conn.close()
    return verify_db(db_path)
