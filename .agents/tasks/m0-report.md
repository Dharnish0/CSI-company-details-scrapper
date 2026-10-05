# M0 Final Report: Project Foundation

Status: **APPROVED** by the independent gate review (`m0-review.md` / `m0-review.json`).
Scope: Phase 0 / Milestone M0 only. Phase 0 does NOT perform corporate discovery.

## 1. Files created

Project root
- `pyproject.toml` (pinned deps, src layout, package data for `schema.sql`, pytest config)
- `main.py` (thin entry point delegating to `corpdisc.cli.main`)
- `.env.example`
- `.gitignore`
- `README.md`

Package `src/corpdisc/`
- `__init__.py`
- `config.py` (Pydantic Settings)
- `logging_setup.py` (stdlib logging, console + file, secret redaction)
- `cli.py` (argparse; `init-db`)
- `db/__init__.py`
- `db/connection.py` (writer connection, read-only connection, init, verify)
- `db/schema.sql` (17 tables, 6 indexes)
- `models/__init__.py`
- `models/domain.py` (StrEnums and six strict row models)
- `services/__init__.py` (empty placeholder)

Tests `tests/`
- `__init__.py`, `conftest.py`, `test_config.py`, `test_db.py`, `test_logging.py`, `test_models.py`, `test_cli.py`

Placeholders: `data/.gitkeep`, `logs/.gitkeep`, `.cache/.gitkeep`

Workflow artifacts: `.agents/tasks/m0-review.md`, `.agents/tasks/m0-review.json`, `.agents/tasks/m0-report.md` (this file), `.agents/tasks/_wait_for_m0.py` (helper script).

Build/env artifacts (not source): `.venv/`, `src/corpdisc.egg-info/` (editable install).

## 2. Files modified

None of the pre-existing docs were modified. Confirmed untouched, by last-write timestamps:

| File | Last written |
|---|---|
| ARCHITECTURE.md | 11:03:44 |
| product.md | 10:29:37 |
| tech.md | 10:29:58 |
| Corporate-Discovery.md | 10:30:38 |
| Agent-Workflow.md | 10:32:00 |
| .kiro/agents/corporate-research-agent.md | 10:35:25 |

The first M0 file was written at 11:13:46, after all of these. The workspace is not a git repository, so this rests on timestamps, not a diff.

## 3. Dependencies added

Pinned in `pyproject.toml`:
- `pydantic==2.13.5`
- `pydantic-settings==2.15.0`
- dev extra: `pytest==9.1.1`

Resolved transitive versions as reported by the reviewer's `pip list`:
- pydantic-core, annotated-types, typing-extensions, typing-inspection (via pydantic)
- python-dotenv (via pydantic-settings)
- pluggy, iniconfig, packaging, Pygments, colorama (via pytest)

The review did not record exact versions for the transitive packages, so they are listed without versions here. No other frameworks are installed (no requests, Playwright, LangGraph, openpyxl, LLM SDKs).

## 4. Database tables created (17)

runs, entities, entity_aliases, websites, relationships, evidence, crawl_pages, domain_stats, candidates, research_queue, crawl_errors, llm_calls, review_items, validation_findings, agent_invocations, agent_tool_calls, gate_rejections

Indexes: `idx_entities_norm`, `idx_entities_tree` (unique, partial), `idx_websites_domain`, `idx_one_primary_parent` (unique, partial on `is_primary = 1 AND verification_status = 'CONFIRMED'`), `idx_queue_ready`, `idx_invocations_cache`. No views or triggers.

## 5. CLI commands available

- `python main.py --help` (also `--version`)
- `python main.py init-db`

Exit codes: 0 success, 2 invalid configuration (per-field messages, input values not echoed), 1 database failure.

## 6. Tests created

Files: `test_config.py` (29), `test_db.py` (30), `test_logging.py` (10), `test_models.py` (31), `test_cli.py` (10), plus `conftest.py`.
Total: **110 tests**.

Tests are hermetic: env vars are stripped, the working directory is a tmp dir, and an autouse fixture makes socket connects raise. Database tests use temporary SQLite files.

## 7. Test results

- `python -m pytest -q`: `110 passed in 6.14s` (reviewer run). My re-run for this report: `110 passed in 8.66s`.
- `python main.py --help`: exit 0, lists `init-db`.
- `python main.py init-db` from a clean state, run twice: both exit 0. Each printed `Database initialized at data\csi_hierarchy.db: 17 tables, foreign_keys=ON, journal_mode=wal`.
- Idempotency: the second run neither failed nor duplicated anything. Table set equals the required 17, with no views.
- Foreign keys: a raw `sqlite3.connect` reports `foreign_keys = 0` (SQLite default, per-connection). Through `connect_writer` it reports 1, and inserting an `entities` row with `run_id=999` fails with `FOREIGN KEY constraint failed`.
- WAL: `journal_mode = wal`.
- Imports: `corpdisc`, `config`, `logging_setup`, `db.connection`, `models.domain`, `cli` all import cleanly.
- Greps over `src`, `tests`, `main.py`, `.env.example`, `README.md`, `pyproject.toml`:
  - No imports of network, browser, LLM, Excel or orchestration libraries.
  - No secret-shaped literals. The only key-like values are fake strings in tests that prove masking. `LLM_API_KEY` and `SEARCH_API_KEY` are blank in `.env.example`.
  - Company names appear only as the configured CSI root values.
- Python: tests ran on 3.13.5. Python 3.12 was not exercised because only 3.11 and 3.13 are installed. The code uses only 3.11+ features (`StrEnum`, `datetime.UTC`), so 3.12 compatibility is expected but unverified.

## 8. Architectural ambiguities and choices

1. **Steering files location.** The brief names `.kiro/steering/*.md`. The files live in the project root (`product.md`, `tech.md`, `Corporate-Discovery.md`, `Agent-Workflow.md`). Only `.kiro/agents/corporate-research-agent.md` is under `.kiro`. I read them from the root and did not move or copy them.
2. **`LOG_DIR` added.** Section 12 lists no log directory setting, but section 13 implies `logs/`. I added `LOG_DIR` (default `logs/`) as the only config setting beyond section 12 and documented it in the README. The log file is `logs/corpdisc.log`, not section 13's `logs/run_<id>.log`, because run ids do not exist in M0. Per-run log naming is deferred to the run-lifecycle milestone.
3. **`candidates.normalized_name`.** Added per section 25.7.
4. **`PageType` includes the six excluded types.** PARTNERS, CUSTOMERS, CASE_STUDIES, CAREERS, BLOG and NEWS are listed only in a comment in 22.2. They are needed as values for `crawl_pages.page_type`.
5. **Root defaults.** `START_URL`, `ROOT_COMPANY` and `ROOT_CODE` default to the Constellation Software values (`Constellation Software Inc.`, `CSI`, `csisoftware.com`), per the brief. No other company names are introduced.
6. **Enums live in `models/domain.py`.** Part II also sketches `contracts/common.py`. Contracts belong to M2, so the enums stay in `domain.py`. A test keeps the enum values in sync with the schema CHECK lists.
7. **`v_llm_cost_by_agent` view deferred to M9.** No views are created in M0.
8. **Milestone table vs. brief.** The ARCHITECTURE.md milestone table lists `contracts/` in the M0 row, while the Phase 0 brief says agent contracts belong to M2. I followed the brief. `test_no_agent_contracts_in_m0` asserts no contract types exist.
9. **Schema as final DDL.** The Part II additions are merged into the `CREATE TABLE` statements, not applied as `ALTER` statements.
10. **Single-writer principle.** It is expressed as one writer connection (FK on, WAL) plus a separate `query_only` reader. No repository layer exists.
11. **Inert settings.** `USE_PLAYWRIGHT` is a config value with no behaviour, and `OUTPUT_XLSX` is a path only.
12. **Secrets.** `LLM_API_KEY` and `SEARCH_API_KEY` are redacted in logs and never echoed in CLI validation errors.

Follow-up advised by the reviewer (non-blocking): initialize git or snapshot the docs before M1 so future reviews can diff, and optionally run the suite once under Python 3.12.

## 9. Deferred to M1 and later

- LLM agents and real LLM calls (Supervisor, Discovery, Entity Extraction, Relationship Verification, Website Resolution, Deduplication, Validation)
- Agent contracts, deterministic gates and the Committer (M2)
- Web crawling, Requests fetching, CSI website crawling
- Playwright
- Search providers
- LangGraph research workflow
- Excel export
- Repositories and services with logic (`services/` is an empty placeholder)
- Per-run log files, and the `v_llm_cost_by_agent` view (M9)
- Any production research logic

M1 has not been started.
