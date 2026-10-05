# Phase 0 / M0 project foundation: independent gate review

M0 delivers the `corpdisc` package: Pydantic Settings config, stdlib logging with redaction, a 17-table SQLite schema with writer/read-only connections and an `init-db` CLI, strict Pydantic enums and six row models, and a 110-test hermetic suite. Every verification command was re-run independently from a clean state and passed. The schema, enums and config defaults were compared against ARCHITECTURE.md Part I section 5, section 12, Part II 22.2 and 26.5 and match. No out-of-scope code exists (no agents, contracts, gates, graphs, repositories, crawling, LLM, search, Excel).

Watch for: nothing blocking. Two advisory notes only: Python 3.12 itself could not be exercised (venv is 3.13.5; `py --list` shows only 3.11 and 3.13) (confirmed), and no git repository exists, so "ARCHITECTURE.md and steering files unmodified" rests on file timestamps rather than a diff (likely).

**Verdict**: APPROVED

## High-level view

The foundation is small and correctly bounded. Configuration, logging, database and models are each a single module with no cross-dependency beyond logging reading `Settings`. The CLI is argparse with only `init-db`, returning integer exit codes (2 for bad config, 1 for database failure).

The schema is written as final DDL with the Part II additions merged in, not as ALTER statements. It has exactly 17 tables, no views or triggers, and the four key indexes. Single-writer discipline is expressed as one writer connection (FK on, WAL) plus a separate `query_only` reader; no repository layer exists.

Domain models use StrEnums that mirror the schema CHECK lists, with a test that keeps the two in sync. The tests are hermetic: env vars are stripped, cwd is a tmp dir, and socket connects raise.

<details>
<summary>Issues (2, both non-blocking)</summary>

1. **Python 3.12 not directly exercised** — tests ran on 3.13.5. The code uses only 3.11+ features (`StrEnum`, `datetime.UTC`), so 3.12 compatibility is expected (likely). Optionally run the suite once under a 3.12 interpreter.
2. **No VCS baseline for "unmodified" check** — the workspace is not a git repo. ARCHITECTURE.md was last written 11:03:44, before the first M0 file at 11:13:46, and the steering files date from 10:29 to 10:35. Initialize git or snapshot the docs before M1 so future reviews can diff.

</details>

<details>
<summary>Details</summary>

## What was re-run and what it returned

Python in the venv reported `3.13.5`, which satisfies the 3.12+ requirement.

`python -m pytest -q` finished with `110 passed in 6.14s`, exit 0.

From a clean state (`data/csi_hierarchy.db*` deleted), `python main.py --help` exited 0 and lists `init-db` (plus `--version`). `python main.py init-db` ran twice, both exit 0, each printing `Database initialized at data\csi_hierarchy.db: 17 tables, foreign_keys=ON, journal_mode=wal`. The second run did not fail or duplicate anything, so init is idempotent.

Opening the resulting file with sqlite3 showed exactly the 17 expected tables (set equality with the required list), no views, and `journal_mode = wal`. A raw `sqlite3.connect` reports `foreign_keys = 0`, as SQLite does by default, so enforcement is per-connection. Through the project's `connect_writer` it reports 1 and an `entities` insert with `run_id=999` fails with `FOREIGN KEY constraint failed`. The partial unique index `idx_one_primary_parent` is present with the `is_primary = 1 AND verification_status = 'CONFIRMED'` predicate. Indexes present: `idx_entities_norm`, `idx_entities_tree`, `idx_invocations_cache`, `idx_one_primary_parent`, `idx_queue_ready`, `idx_websites_domain`.

The import check for `corpdisc`, `config`, `logging_setup`, `db.connection`, `models.domain` and `cli` printed `imports ok`.

`pip list` shows only pydantic 2.13.5, pydantic-settings 2.15.0, pydantic-core, annotated-types, typing-extensions, typing-inspection, python-dotenv (a pydantic-settings dependency), pytest 9.1.1 with pluggy, iniconfig, packaging, Pygments and colorama (pytest dependencies), pip, and `corpdisc 0.1.0` editable. `pyproject.toml` pins exact versions. No extra frameworks are present.

Greps over `src`, `tests`, `main.py`, `.env.example`, `README.md` and `pyproject.toml`:

- No imports of requests, playwright, langgraph, langchain, openpyxl, openai, anthropic, httpx, aiohttp, bs4, selenium or urllib3.
- No secret-shaped literals (sk-..., AKIA..., long Bearer tokens). The only key-like assignments are obviously fake values inside tests (`llm-secret-value-1`, `fake-llm-key-ABC123`, `fake-search-key-999`) used to prove masking. `LLM_API_KEY` and `SEARCH_API_KEY` are blank in `.env.example`.
- The only company names are the configured CSI root values (`Constellation Software Inc.`, `CSI`, `csisoftware.com`), in config defaults, `.env.example`, README and the matching tests. The test ROOT_CODE override `ACME_1` is a synthetic code, not a company name. Hits for "Acquired" are enum values from 22.2.

## Architecture deviations worth knowing

The schema, enums and config defaults were compared with sections 5, 12, 22.2 and 26.5 and match. The only places the implementation goes beyond or differs from the architecture text are deliberate and minimal:

`PageType` includes the six G4-excluded types (PARTNERS, CUSTOMERS, CASE_STUDIES, CAREERS, BLOG, NEWS) that 22.2 lists only in a comment. They are needed as values for `crawl_pages.page_type`, and the domain.py comment says so.

`LOG_DIR` is the one config setting added beyond section 12, and it is documented in the README. The log file is `logs/corpdisc.log`, whereas section 13 describes `logs/run_<id>.log`. Run ids do not exist in M0, so this is the smallest consistent choice; per-run log naming is deferred to the run-lifecycle milestone.

The ARCHITECTURE.md milestone table lists `contracts/` in the M0 row. The Phase 0 brief says agent contracts belong to M2 and must not be created, and the implementation follows the brief. The final report should mention this doc-versus-brief conflict.

The CLI turns a config `ValidationError` into exit 2 with per-field messages that exclude input values, so a bad secret is not echoed.

## Scope

`src/corpdisc` contains only config, logging_setup, cli, `db/`, `models/` and an empty `services/__init__.py`. There are no agents, graphs, gates or contracts packages, and `test_no_agent_contracts_in_m0` asserts no contract types exist. `use_playwright` is a config string with no behaviour, and `output_xlsx` is a path only.

## Test coverage gap

The conftest socket guard is an autouse in-process fixture, so it does not reach the subprocess CLI tests (`test_help_subprocess`, `test_init_db_subprocess*`). Those paths make no network calls by construction, so this is not a defect.
## Cleanup

Removed the verification DB and WAL files, `logs/corpdisc.log`, `.pytest_cache` and `__pycache__` directories outside `.venv`. `data/`, `logs/` and `.cache/` contain only `.gitkeep`. `.venv` and the editable-install `egg-info` were left in place.

## File map

<details>
<summary>Files reviewed</summary>

- `pyproject.toml`: pinned deps, src layout, package data for `schema.sql`, pytest config
- `.env.example`, `.gitignore`, `README.md`: config template, ignores, Phase 0 docs
- `main.py`: thin entry point delegating to `corpdisc.cli.main`
- `src/corpdisc/config.py`: Pydantic Settings with validation
- `src/corpdisc/logging_setup.py`: console and file logging with redaction
- `src/corpdisc/cli.py`: argparse CLI with `init-db`
- `src/corpdisc/db/connection.py`, `schema.sql`: writer and reader connections, init, verify, 17-table DDL
- `src/corpdisc/models/domain.py`: enums and six row models
- `tests/`: conftest plus config, db, logging, models and cli tests

</details>

</details>

