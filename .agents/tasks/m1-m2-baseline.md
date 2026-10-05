# M1 + M2 Baseline (before any M1/M2 change)

## Pytest baseline
Command: `.venv\Scripts\python -m pytest -q`
Result: **110 passed in 3.01s** (0 failed, 0 skipped). GREEN.

`python main.py --help` works: usage printed, lists `init-db`, `--version`, `-h`. Exit 0.

## M0 files (all present)
- main.py: found
- README.md: found
- src/corpdisc/config.py: found
- src/corpdisc/logging_setup.py: found
- src/corpdisc/cli.py: found
- src/corpdisc/models/domain.py: found
- src/corpdisc/db/connection.py: found
- src/corpdisc/db/schema.sql: found
- tests/: found
- .agents/tasks/m0-report.md: found
Missing: none.

Temporary `.agents/tasks/_wait_for_m0.py` was deleted.

## M0 decisions that REMAIN IN FORCE for M1/M2
1. Steering files live in the project root (product.md, tech.md, Corporate-Discovery.md, Agent-Workflow.md), not .kiro/steering. Only .kiro/agents/corporate-research-agent.md is under .kiro.
2. `LOG_DIR` setting added (default `logs/`); log file is `logs/corpdisc.log` (per-run log naming deferred).
3. `candidates.normalized_name` column exists (section 25.7).
4. `PageType` enum includes the six excluded types (PARTNERS, CUSTOMERS, CASE_STUDIES, CAREERS, BLOG, NEWS) for `crawl_pages.page_type`.
5. Root defaults: START_URL / ROOT_COMPANY / ROOT_CODE default to the CSI values (Constellation Software Inc., CSI, csisoftware.com). No other company names.
6. Enums live in `models/domain.py` (contracts are M2); a test keeps enum values in sync with schema CHECK lists. `test_no_agent_contracts_in_m0` asserts no contract types exist in M0 and will need to be updated when M2 adds contracts.
7. `v_llm_cost_by_agent` view deferred to M9; no views or triggers in schema.
8. Schema is final DDL (Part II additions merged into CREATE TABLE, no ALTER).
9. Single-writer principle = one writer connection (FK on, WAL) plus a separate `query_only` reader; no repository layer yet.
10. `USE_PLAYWRIGHT` and `OUTPUT_XLSX` are inert settings/paths.
11. `LLM_API_KEY` / `SEARCH_API_KEY` are redacted in logs and never echoed in CLI errors.
12. 17 tables, 6 indexes (incl. unique partial `idx_one_primary_parent`, `idx_entities_tree`). Pinned deps: pydantic 2.13.5, pydantic-settings 2.15.0, pytest 9.1.1; no requests/Playwright/LangGraph/LLM SDKs installed yet.
13. Tests are hermetic (env stripped, tmp cwd, autouse socket-connect blocker). M1 network tests must mock HTTP or opt out explicitly.
14. Workspace is not a git repo.
