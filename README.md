# Corporate Hierarchy Discovery (corpdisc)

Purpose: discover and verify a corporate hierarchy (operating groups, portfolio companies, subsidiaries and so on) starting from a root company website, with verifiable evidence for every relationship. The default root is Constellation Software Inc. (CSI).

Design reference: [ARCHITECTURE.md](ARCHITECTURE.md). Part II (multi-agent architecture) is the target implementation. Part I (deterministic foundation and data model) is preserved: SQLite is the source of truth, deterministic control stays in Python, and nothing is accepted without verifiable evidence.

## Status: M0-M8 implemented, Excel export (M9) added; real-CSI validation pending on your machine

| Milestone | What | State |
|---|---|---|
| M0 | Foundation: config, logging, SQLite schema, models | done |
| M1 | Deterministic services: URL/fetch/robots/cache, links, DOM candidates, normalize, matcher, confidence, queue, budget, cycle guard, Committer | done |
| M2 | Agent infrastructure: `AgentRunner`, `LLMGateway`, `ToolBroker`, circuit breaker, audit, cache, Fake/Replay/Record providers | done |
| M3 | Gates G1-G14 | done |
| M4 | Supervisor runtime, `research_entity` and `finalize` graphs (LangGraph), fixture hierarchy | done |
| M5-M7 | Extraction, Relationship Verification, Discovery, Website Resolution, Deduplication, Validation, Advisor | done (verified with scripted/fake models) |
| M8 | Real provider adapters (Anthropic, OpenAI-compatible), composition root, `run`/`resume`/`report`, calibration report, opt-in live tests | code and offline tests done; **live calibration not yet run** |
| M9 | Tree IDs, Excel export, `verify` | implemented and tested on fixtures; not yet run on real CSI data |

Agents only *propose*. Deterministic gates, the Committer and its invariants decide what is stored, and SQLite is the source of truth. The default test suite uses scripted fake models and a local in-memory site: it needs no API key, no network and no real website.

## Run it

```powershell
python main.py init-db
python main.py run --yes --max-tasks 1   # new run from START_URL / ROOT_COMPANY (see .env); stops after 1 task, resumable
python main.py resume --yes              # continue after a pause or interruption
python main.py report [--json]           # calls, tokens, cost, cache rate, review ratio, extraction latency/tokens/retries
python main.py verify                    # re-check data quality from SQLite (evidence grounded in STORED pages, cycles, duplicates ...)
python main.py export [--output PATH]    # assign Tree IDs and write CSI_Company_Hierarchy.xlsx from SQLite
```

With no LLM configured, `run` pauses cleanly (the circuit breaker opens) instead of crashing.

### Local Ollama (qwen3:4b)

The provider is **native Ollama** (`/api/chat`): it sends a real output cap (`num_predict`), an explicit context (`OLLAMA_NUM_CTX`, default 12288),
`think=false` for structured extraction (`LLM_EXTRACTION_REASONING=none`), the JSON schema as a grammar and in the prompt, and records
`finish_reason`, tokens and prompt-eval/eval/load times per call. A timeout is never retried identically; extraction stops at the first
transport failure; output cut at the cap (`finish_reason=length`) is reported, not "repaired".

```powershell
# .env: LLM_PROVIDER=ollama  LLM_MODEL=qwen3:4b  START_URL=https://www.constellationsoftware.com/  ROOT_COMPANY="Constellation Software Inc."
#       ROOT_CODE=CSI  DB_PATH=./data/csi_validation.db  OLLAMA_NUM_CTX=12288  LLM_EXTRACTION_REASONING=none  LLM_TIMEOUT_SECONDS=300
ollama pull qwen3:4b
python scripts/ollama_preflight.py            # measures version / model / RAM / GPU / context, runs controlled extraction tests A-C
python main.py init-db
python main.py run --yes --max-tasks 1        # Test D: the exact application request on the REAL pages
python main.py report ; python main.py verify
python main.py run --yes --max-tasks 5        # only if the first task was healthy
python main.py verify ; python main.py export
```

## Live calibration (opt-in, billable)

Live tests are deselected by default and skip unless explicitly enabled. The key is read from the environment only.

```powershell
$env:CORPDISC_LIVE="1"; $env:CORPDISC_LIVE_PROVIDER="anthropic"; $env:CORPDISC_LIVE_API_KEY="..."; $env:CORPDISC_LIVE_MODEL="..."
python -m pytest tests/live -m live -s            # real model on the local fixture hierarchy
# real site, small limits (also needs CORPDISC_LIVE_SITE=1):
python -m pytest tests/live -m live_site -s
```

Safety properties (no product as entity, no partner confirmed, no cycle edge, every CONFIRMED relationship has verified evidence) are hard assertions. Quality numbers are printed as a calibration report for tuning prompts and thresholds.

## Slow crawl mode
`--slow-crawl` (on `run` and `resume`) lets one process work through a hierarchy for up to 24 hours on a rate-limited provider such as Groq, without repeated HTTP 429 pauses. It is off by default; without the flag behavior is unchanged.

Groq base limits for `qwen/qwen3.8-27b`, `openai/gpt-oss-120b` and `openai/gpt-oss-20b` (see `.agents/tasks/slow-crawl-quota-findings.md`): 8K tokens per minute (TPM), 30 requests per minute (RPM), 1K requests per day (RPD) and 200K tokens per day (TPD). The `x-ratelimit-*-requests` headers report the daily request limit, not RPM. Do not set `LLM_TPM_LIMIT` or `LLM_RPM_LIMIT` above these values. In slow mode both are ignored.

How pacing works:
- One provider request is in flight at a time, shared by every agent (extraction, website resolution, relationship verification and the rest). Starts are at least `SLOW_CRAWL_INTERVAL_SECONDS` apart (default 90).
- A token bucket sized from `SLOW_CRAWL_TPM_LIMIT` (or the provider header) and `SLOW_CRAWL_TPM_TARGET_FRACTION` adapts the wait to the observed request size. The daily token bucket is seeded from the last 24 hours in the database.
- After a minute-scope 429 the gap doubles (up to 8 times the interval) and `Retry-After` is honored exactly. After a daily-scope 429 the pending tasks are deferred (`SLOW_CRAWL_DAILY_RETRY_SECONDS` when the provider gives no time) and the process sleeps until the next eligible task instead of exiting `PAUSED (RETRY_WAITING)`.
- Sleeping happens in short chunks, so Ctrl+C or SIGTERM stops it promptly. Progress is committed after every task.

Ceilings apply per session, counted from its start: `SLOW_CRAWL_MAX_RUNTIME_HOURS` (24), `SLOW_CRAWL_TOKEN_CEILING` and optionally `SLOW_CRAWL_COST_CEILING_USD` (needs `LLM_PRICE_IN_PER_MTOK` and `LLM_PRICE_OUT_PER_MTOK`). Page, depth, call and token budgets of the run still apply. A 24-hour runtime is not permission for unlimited spending.

Monitor from a second terminal (read-only, safe while the crawl runs):
```powershell
python main.py progress --watch 30        # or: --run-id N, --json
```
It shows companies discovered, confirmed versus review-required, operating groups and portfolio companies, maximum depth, tasks done, pending and waiting for retry, API requests, successful calls, cache hits and 429s, tokens and estimated cost, the next retry time and the remaining budgets.

Exit codes: 0 completed, 1 failed, 2 usage error, 3 incomplete (runtime limit, token or cost ceiling reached, work remains), 4 partial, 5 interrupted by Ctrl+C or SIGTERM (state saved), 6 another slow crawl holds the lock.

A lock file `<DB_PATH>.slowcrawl.lock` (JSON with pid and heartbeat) prevents overlapping crawlers on one database. A stale lock from a dead process is replaced automatically; if a live process holds it the command exits 6 and names the pid. Resume reuses the same database, completed tasks, cached responses and retry schedule, and does not duplicate tasks. When a session stops with work left, it prints the exact resume command. API keys are never written to logs, the session file or the progress output.

Resume an existing database (run only after the previous process is confirmed stopped):
```powershell
cd "C:\Users\Dharnish\OneDrive - TPF Software\python\Company_web_scrapping"
Remove-Item Env:LLM_TPM_LIMIT,Env:LLM_RPM_LIMIT -ErrorAction SilentlyContinue
$env:DB_PATH=".\bench\csi_coverage_test.db"; $env:MAX_DEPTH="3"; $env:MAX_TOTAL_PAGES="100"; $env:MAX_LLM_CALLS_PER_RUN="150"; $env:SLOW_CRAWL_TOKEN_CEILING="250000"
python main.py resume --run-id 1 --slow-crawl --provider groq --model qwen/qwen3.8-27b --yes
```

Benchmarks (billable, use `bench/slow_crawl_*` databases only): `python scripts/slow_crawl_benchmark.py micro|models`. Add `--dry-run` to run offline with a fake transport. A persistent ledger `bench/slow_crawl_token_ledger.json` refuses any request that could take total benchmark spend past 120,000 tokens. The script never changes the configured model.

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
