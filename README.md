# Corporate Hierarchy Discovery (corpdisc)

Purpose: discover and verify a corporate hierarchy (operating groups, portfolio companies, subsidiaries and so on) starting from a root company website, with verifiable evidence for every relationship. The default root is Constellation Software Inc. (CSI).

Design reference: [ARCHITECTURE.md](ARCHITECTURE.md). Part II (multi-agent architecture) is the target implementation. Part I (deterministic foundation and data model) is preserved: SQLite is the source of truth, deterministic control stays in Python, and nothing is accepted without verifiable evidence.

## Status: Phase 0 / Milestone M0 (project foundation)

Phase 0 does NOT yet perform corporate discovery. There is no crawling, no LLM access, no search and no Excel export. What exists today:

- Pydantic Settings configuration (`.env`)
- Logging foundation (console and file, secret redaction)
- SQLite schema, writer and read-only connections, and `init-db`
- Strict Pydantic domain models and enums
- A small CLI (`--help`, `init-db`)
- Tests

## Requirements

- Python 3.12 or newer
- Dependencies (pinned in `pyproject.toml`): `pydantic`, `pydantic-settings`; `pytest` for development

## Setup

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
Copy-Item .env.example .env
```

Edit `.env` as needed. It is git-ignored; never commit it or put secrets in `.env.example`. API key settings (`LLM_API_KEY`, `SEARCH_API_KEY`) are blank by default and are not used in Phase 0.

### Retargeting

Defaults describe the CSI run. To point the engine at another root company, change `START_URL`, `ROOT_COMPANY` and `ROOT_CODE` (uppercase letters, digits, `_` or `-`; used as the Tree ID prefix later).

### Paths

Relative paths (`DB_PATH`, `CACHE_DIR`, `OUTPUT_XLSX`, `LOG_DIR`) resolve against the current working directory, so run commands from the project root. `LOG_DIR` (default `./logs`) is the only setting added beyond ARCHITECTURE.md section 12.

Caution: this folder may be synced by OneDrive. SQLite in WAL mode creates `-wal` and `-shm` side files that can conflict with file sync. For real runs, consider pointing `DB_PATH` at a folder outside OneDrive.

## Initialize the database

```powershell
python main.py init-db
```

This reads `DB_PATH`, creates the directory, builds all 17 tables from `src/corpdisc/db/schema.sql`, verifies the database (foreign keys on, WAL mode, integrity check) and prints a success line. It is safe to run repeatedly.

```powershell
python main.py --help
```

## Run the tests

```powershell
python -m pytest
```

Tests use temporary directories and databases, and need no network, API keys or external services.

## Layout

```
main.py                     CLI entry point
pyproject.toml              package metadata and pinned dependencies
.env.example                configuration template
src/corpdisc/
  config.py                 Pydantic Settings
  logging_setup.py          stdlib logging setup and redaction
  cli.py                    argparse CLI
  models/domain.py          enums and row models
  db/connection.py          writer / read-only connections, init-db
  db/schema.sql             SQLite schema
  services/                 empty placeholder for later milestones
tests/                      pytest suite
data/  logs/  .cache/       runtime output (git-ignored contents)
```
