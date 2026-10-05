# Corporate Hierarchy Discovery (corpdisc)

Purpose: discover and verify a corporate hierarchy (operating groups, portfolio companies, subsidiaries and so on) starting from a root company website, with verifiable evidence for every relationship. The default root is Constellation Software Inc. (CSI).

Design reference: [ARCHITECTURE.md](ARCHITECTURE.md). Part II (multi-agent architecture) is the target implementation. Part I (deterministic foundation and data model) is preserved: SQLite is the source of truth, deterministic control stays in Python, and nothing is accepted without verifiable evidence.

## Status: M0-M8 implemented (live calibration pending)

| Milestone | What | State |
|---|---|---|
| M0 | Foundation: config, logging, SQLite schema, models | done |
| M1 | Deterministic services: URL/fetch/robots/cache, links, DOM candidates, normalize, matcher, confidence, queue, budget, cycle guard, Committer | done |
| M2 | Agent infrastructure: `AgentRunner`, `LLMGateway`, `ToolBroker`, circuit breaker, audit, cache, Fake/Replay/Record providers | done |
| M3 | Gates G1-G14 | done |
| M4 | Supervisor runtime, `research_entity` and `finalize` graphs (LangGraph), fixture hierarchy | done |
| M5-M7 | Extraction, Relationship Verification, Discovery, Website Resolution, Deduplication, Validation, Advisor | done (verified with scripted/fake models) |
| M8 | Real provider adapters (Anthropic, OpenAI-compatible), composition root, `run`/`resume`/`report`, calibration report, opt-in live tests | code and offline tests done; **live calibration not yet run** |
| M9 | Tree IDs and Excel export | not started |

Agents only *propose*. Deterministic gates, the Committer and its invariants decide what is stored, and SQLite is the source of truth. The default test suite uses scripted fake models and a local in-memory site: it needs no API key, no network and no real website.

## Run it

```powershell
python main.py init-db
python main.py run                 # new run from START_URL / ROOT_COMPANY (see .env)
python main.py resume              # continue after a pause or interruption
python main.py report [--json]     # calls, tokens, cost, cache rate, review ratio, gate rejections
```

With no LLM configured, `run` pauses cleanly (the circuit breaker opens) instead of crashing. A billable provider needs `LLM_PROVIDER` (`anthropic` or `openai`), `LLM_API_KEY`, `LLM_MODEL` in `.env` (never committed) and an explicit `--yes`. Set `LLM_TOKEN_BUDGET`, `MAX_TOTAL_PAGES` and `MAX_DEPTH` first. `LLM_RECORD_TO=path.jsonl` records every successful response; `LLM_PROVIDER=replay` replays that cassette and fails closed on a miss (it never falls through to a live provider).

## Live calibration (opt-in, billable)

Live tests are deselected by default and skip unless explicitly enabled. The key is read from the environment only.

```powershell
$env:CORPDISC_LIVE="1"; $env:CORPDISC_LIVE_PROVIDER="anthropic"; $env:CORPDISC_LIVE_API_KEY="..."; $env:CORPDISC_LIVE_MODEL="..."
python -m pytest tests/live -m live -s            # real model on the local fixture hierarchy
# real site, small limits (also needs CORPDISC_LIVE_SITE=1):
python -m pytest tests/live -m live_site -s
```

Safety properties (no product as entity, no partner confirmed, no cycle edge, every CONFIRMED relationship has verified evidence) are hard assertions. Quality numbers are printed as a calibration report for tuning prompts and thresholds.

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

Tests use temporary directories and databases, and need no network, API keys or external services. `-m 'not live and not live_site'` is set in `pyproject.toml`, so real-provider tests never run in an ordinary invocation.

## Layout

```
main.py                     CLI entry point
pyproject.toml              package metadata and pinned dependencies
.env.example                configuration template (keys are blank)
src/corpdisc/
  app.py                    composition root (the only place a real HTTP client / LLM transport is built)
  config.py, logging_setup.py, cli.py, clock.py, transport.py
  models/, contracts/       domain models; agent envelope, per-agent I/O, post-gate and commit contracts
  db/                       schema, connections, repos (pages, runs, audit, gate rejections)
  services/                 deterministic services: fetch, robots, links, matcher, confidence, queue, budget,
                            cycle guard, committer, validation engine, search port, calibration report
  gates/                    G1-G14
  agents/                   agent classes, prompts, runner, gateway, broker, providers (fake, replay, record, anthropic, openai)
  graphs/                   research_entity, finalize, supervisor, grounding, tools, support
tests/                      pytest suite; tests/live is opt-in
data/  logs/  .cache/       runtime output (git-ignored contents)
```
