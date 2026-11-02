# Slow Crawl Mode: Groq quota findings and pre-flight state

Date: 2026-11-02 (local IST, UTC+5:30; the DB stores UTC). HEAD `4ed9d51`, branch `claude/m8-ollama-e2e-fix`.
Method: code read, one `GET /models`, one 18-80 token chat call per model, the Groq docs page, and read-only DB/log inspection.
No crawler was running at any point (`Get-CimInstance Win32_Process -Filter "Name='python.exe'"` returned nothing, checked twice).
Helper scripts (all read-only or single-shot, in this directory): `slow_crawl_quota_probe.py`, `slow_crawl_db_inspect.py`,
`slow_crawl_429_scan.py`, `slow_crawl_timeline.py`, `slow_crawl_request_sizes.py`. None prints the API key.

## 1. Real Groq limits

### Documented (https://console.groq.com/docs/rate-limits, base limits table)

| Model | RPM | RPD | TPM | TPD |
|---|---|---|---|---|
| `qwen/qwen3.8-27b` | 30 | 1K | 8K | 200K |
| `openai/gpt-oss-120b` | 30 | 1K | 8K | 200K |
| `openai/gpt-oss-20b` | 30 | 1K | 8K | 200K |

Limits apply per organization and per model. The page says exact values are on the account's limits page, which needs a
console login and was not read here.

### Observed response headers (live, 2026-11-02 ~11:27 IST, HTTP 200)

| Model | x-ratelimit-limit-requests | -limit-tokens | -remaining-requests | -remaining-tokens | -reset-requests | -reset-tokens | usage (prompt+completion) |
|---|---|---|---|---|---|---|---|
| `qwen/qwen3.8-27b` | 1000 | 8000 | 992 | 7979 | 11m31.2s | 157ms | 16+2 |
| `openai/gpt-oss-120b` | 1000 | 8000 | 999 | 7920 | 1m26.4s | 600ms | 75+5 |
| `openai/gpt-oss-20b` | 1000 | 8000 | 999 | 7920 | 1m26.4s | 600ms | 75+5 |

`GET /models` returned HTTP 200 with no rate-limit headers; all three models are listed `active: true`, context window 131072, max completion tokens 16384 (qwen) and 65536 (both gpt-oss).
No 429 occurred during the probe, so `retry-after` was absent (the docs say it appears only on 429).

Reading of the headers (the docs table confirms it):
- `x-ratelimit-*-requests` is **requests per day (RPD)**, not per minute. 1000 = RPD.
- `x-ratelimit-*-tokens` is **tokens per minute (TPM)**: 8000.
- The reset-requests values are multiples of 86.4 s (qwen 11m31.2s = 8 x 86.4 s; gpt-oss 1m26.4s = 1 x 86.4 s). The daily request bucket
  refills continuously at 1000/86400, i.e. one request per 86.4 s. qwen had 8 requests outstanding, gpt-oss 1 (my own probe).
  RPD is not close to binding (99.2% remaining on qwen).
- TPD (200K) and RPM (30) are **not exposed in headers**. They appear only in the docs/console and in the 429 message text.
  TPD cannot be read without hitting it, so the planner must track it locally.
- The three models have separate buckets (qwen showed prior usage, the gpt-oss models showed only my probe).

## 2. Configured pacing vs the real allowance

| Setting | Value the runs used | Real allowance | Verdict |
|---|---|---|---|
| `LLM_TPM_LIMIT` | 200000 (P4/P5 runs, process env; see `csi-p4-results.md` line 11) | 8000 TPM | 25x too high, so it never paced anything |
| `LLM_RPM_LIMIT` | 600 (P4/P5 runs) | 30 RPM (1000 RPD) | 20x too high |
| Coverage run (`bench/csi_coverage_test.db`) | -1 / -1 in `runs.config_json` -> `PROVIDER_DEFAULT_LIMITS["groq"] = (30, 0)` | 30 RPM ok, TPM unpaced | RPM right, TPM pacing absent |

The "250K TPM / 1K RPM" figure in `csi-p4-plan.md` line 111 and `.env.example` is wrong for this account: 1K is the daily request limit, and the
token limit is 8K per minute. `.env.example` still recommends `LLM_TPM_LIMIT=200000` / `LLM_RPM_LIMIT=600`; that text needs correcting.

## 3. Were the past 429s per-minute or per-day?

The stored data cannot say directly, because the 429 body is thrown away: `http_common.classify()` builds
`ProviderTransportError("groq transient error 429", ...)` without the response `detail` (only non-429 errors include it), and the gateway logs only
"rate limited (429): sleeping Ns before retry k/5". `agent_invocations.response_json` stores
`{"error":{"message":"rate limited: groq transient error 429"},"transport":{"errors":["RATE_LIMITED" x6],"requests":0}}`. The inference below uses timing and token arithmetic.

Evidence from the coverage run (`logs/corpdisc.log`, 2026-11-02 09:53-11:12 IST; `agent_invocations` in the DB, UTC):

1. **Early 429s were minute-level (TPM 8000).** The first 429 invocation started 04:25:45 UTC, ~100 s after the run began, with only 17,406 tokens spent
   in total. Two or three requests of 4-7K tokens inside one minute exceed 8,000 TPM. The in-call sleeps (1.2, 2.3, 3.8, 7.0, 16.1 s; `log` lines 09:56:25-09:56:40)
   follow base 1 s x 2^n with jitter, so no `Retry-After` header was applied on those 429s. 160 such sleep lines were logged, grouped in 5-sleep bursts.
2. **Late 429s were daily-level (TPD 200K).** At 11:12:49 IST the gateway logged `Retry-After 575s exceeds the 120s ceiling`. A per-minute bucket cannot
   need more than 60 s to reset, so a 575 s wait implies a daily bucket. TPD refills continuously at 200000/86400 = 2.31 tokens/s, so a 575 s wait corresponds to
   a deficit of about 1,330 tokens. By then the DB showed **210,224 tokens used, above the 200K TPD** for qwen (all of it on qwen: `runs.config_json` model).
   The run's 24 TRANSPORT-failed invocations (23 with 6 x RATE_LIMITED) are spread over 04:25-05:42 UTC; every 10-minute bucket after 04:50 UTC contains some.
3. Supervisor deferrals then compounded it: tasks 3, 5, 6 were rescheduled at 60/120/240/480/960 s and ended at "deferral 6/6" / "5/6". The 960 s steps and the
   575 s Retry-After show the run was waiting for a token bucket that was nearly empty, and short in-process retries could not fix that.
4. Older evidence points the same way: `bench/groq_p5_resume3.err.log` has 73 "rate limited (429)" lines, `groq_p5.db` has 33 TRANSPORT failures (31 all-429),
   `groq_p4.db` 15 (all 429). The Gemini log `bench/gemini_p2_resume3.log` shows `Retry-After 9850s`; that is Gemini, not Groq.
5. The probe shows TPD is not exhausted right now: a tiny qwen request succeeded 14 minutes after the last 429. This is consistent with the rolling refill (2.31 tok/s x 840 s ~ 1,900 tokens).

Conclusion: **both**. The initial 429s are TPM (8K/min) because the run had no token pacing; the sustained pauses are TPD (200K/day) because one 53-request
run spent 210K tokens. RPM (30) and RPD (1000) were never the problem.

## 4. What this means for a 24-hour Slow Crawl (numbers for the planner)

- Measured live (non-cache) requests in the coverage DB: 53 requests, 210,224 tokens, **average 3,966 tokens/request**
  (extraction OK: 36 requests, avg 3,018 in + 996 out; extraction SCHEMA-failed: 7 requests, avg 3,277 in + 3,000 out; relationship: 5 requests ~2K; website: 5 requests ~2K, 128 s average latency).
- **TPD is the binding limit, not RPD.** 200K tokens/day / 3,966 = ~50 requests per day per model. To spread that over 24 h needs ~1,714 s (~29 min) between requests.
  The requested 90 s start interval equals the RPD pace (86.4 s) but would use up the 200K TPD in about **1.26 hours**.
  Pacing must therefore be driven by a rolling 24 h token ledger (and ideally the observed `Retry-After`), with 90 s as a floor, not as the sustained rate.
- Over a 24 h run on qwen alone, the spendable total is about the 200K bucket plus its refill (~200K), so at most ~200-400K tokens. The hard token ceiling for tests in this workflow (150K) is below that.
- **TPM headroom per request:** extraction input reaches 5,719 tokens and `max_output_tokens=3000` (`agents/extraction.py:17`), so the gateway estimate
  (`estimate_tokens(input) + max_output_tokens`, `gateway.py:247`) reaches ~8,700, above the 8,000 TPM window. Groq is generally understood to count the requested completion
  tokens toward the limit up front (assumption: not confirmed in the docs page read here, and no request was sent to test it). Slow Crawl should cap or shrink `max_output_tokens` for qwen, or the estimate will block a request forever.
- 7 of 43 extraction requests (16%) ended `SCHEMA` failed with `tokens_out` exactly 3000: the output hit the cap and the JSON was truncated.
  That wasted 44,000 tokens (21% of the TPD-sized budget). This directly affects how fast the daily budget burns.
- `x-ratelimit-reset-tokens` is only about the minute bucket (157-600 ms after a tiny call); it says nothing about TPD, so the pacer must keep its own daily ledger.

## 5. Code facts verified (and corrections to the brief)

- `agents/gateway.py`
  - `RequestPacer`: min spacing `60/rpm`, sliding 60 s window of **estimated** tokens, `observe()` blocks until the reported reset when remaining requests<=0 or
    remaining tokens < the estimate, `penalize(s)` after a 429. In-memory, one instance per gateway, built in `app.make_pacer()`. `PROVIDER_DEFAULT_LIMITS = {"groq": (30, 0)}`: RPM 30, no TPM. **Confirmed.**
    Correction: `rl_remaining_requests`/`rl_limit_requests` are **daily** counts on Groq, so `observe()` blocks for up to the daily-bucket reset (86.4 s/request) when they reach 0; that is acceptable but the names are misleading.
  - 429 path: `rate_limit_retries=5` in-call retries; `_backoff` returns `Retry-After` unchanged if present, else 1 s x 2^n +-25% jitter capped at 30 s;
    `retry_after_s > retry_after_ceiling_seconds (120)` returns `GatewayFailure(rate_limited=True, retryable=True, retry_after_s=...)` immediately. A 429 does not open the breaker, does not count toward `max_requests`, and records **no** token usage. **Confirmed.**
    Gap: the 429 message/body is discarded (see section 3).
  - The gateway is synchronous, so only one request is ever in flight per process; there is no global minimum interval between requests, and the pacer's wait is applied per attempt including 429 retries.
- `graphs/supervisor.py`: for `RETRY_LATER` the delay is `max(retry_after_s, queue_retry_base_seconds(60) * 2**min(used, 4))` for a free 429 deferral (60/120/240/480/960 s) or
  `2**(attempts-1)` otherwise. `_rl_deferrals` is an **in-memory** dict, reset on every process start, up to `queue_max_rate_limit_deferrals=6` (the "deferral N/6" text).
  When no task is claimable it waits in-process only if `ready_at - now <= queue_retry_wait_max_seconds (300)` (log "queue: waiting Ns for rescheduled tasks", max 1000 idle waits); otherwise it sets
  `runs.status=PAUSED` with `stopped_reason="RETRY_WAITING"` and returns. **Confirmed.** `not_before` lives in `research_queue.not_before` (UTC ISO) and `claim_next` honors it; `next_ready_time` = MIN(not_before) of PENDING tasks.
- `cli.py`: `run`/`resume` take `--max-tasks --yes --provider --model` (`resume` adds `--run-id`); there is no `--slow-crawl`. Exit codes OK 0, FAILED 1, USAGE 2, INCOMPLETE 3 (PAUSED/RUNNING/BUDGET_EXHAUSTED), PARTIAL 4.
  `resume` always calls `init_db()` (idempotent schema script + `migrate_db`), then `Supervisor.resume()` -> `breaker.half_open()` and `requeue_retryable_partials()` (writes: sets attempts=0, not_before=NULL for `PARTIAL:RETRYABLE:` tasks; none exist in the user DB).
- `config.py`: `PROVIDER_MODEL_FIELDS = {"groq": "groq_model", "gemini": "gemini_model"}`; `llm_token_budget: int | None` (None = unlimited); `llm_rpm_limit`/`llm_tpm_limit` default -1; no daily-limit, interval or runtime-cap settings exist.
- `services/llm_http.py`: `RequestsJsonTransport.post` returns `HttpReply(status, {lowercased headers}, text)`; TLS verification on (`verify_ssl=True`), no redirects. **Confirmed.**
- `agents/providers/groq.py`: `usage.prompt_tokens/completion_tokens` -> `ProviderUsage`; `ratelimit_metrics(reply.headers)` fills `rl_remaining_requests`, `rl_remaining_tokens`, `rl_limit_*`, `rl_reset_*_s` (durations like `11m31.2s` parsed by `_to_seconds`). Sends `reasoning_effort: "none"` for qwen3 models. **Confirmed.**
- `services/budget.py`: `BudgetGuard.check_llm` reads the token budget from the **`runs` row** (`llm_token_budget`, `llm_tokens_used`) and call limits from `Settings`. `llm_calls()` for the run counts every non-cache, non-SKIPPED invocation that reached the gateway, **including 429-failed ones** (the per-task count excludes them since `4ed9d51`). So a token ceiling from a new setting must not rely on `runs.llm_token_budget` unless the row is updated.
- `db/connection.py` + `schema.sql`: no `user_version`; `init_db` runs `executescript(schema.sql)` (all `CREATE TABLE/INDEX IF NOT EXISTS`), then `migrate_db`, which only rebuilds `runs` when its CHECK lacks `'PARTIAL'`
  (with an automatic file backup to `<db dir>/backups/<stem>-<UTC stamp>-premigrate.db`). New tables can be added by `CREATE TABLE IF NOT EXISTS`; new columns on existing tables have no migration path yet. `connect_writer` uses WAL; `connect_readonly` uses `query_only`.
- `.env.example`: documents `LLM_RPM_LIMIT/LLM_TPM_LIMIT` (-1) and the incorrect "250K TPM / 1K RPM -> 200000/600" example; `.env` itself sets `LLM_PROVIDER=gemini`, `GROQ_MODEL=qwen/qwen3.8-27b`, `MAX_LLM_CALLS_PER_RUN=5000`, no pacing, no token budget.
  The coverage run was started with process-env overrides (config_json: `max_llm_calls_per_run=150`, `max_depth=3`, `max_total_pages=100`).

## 6. State of the user's DB: `bench/csi_coverage_test.db` (opened `mode=ro`; nothing was written)

File before/after inspection: 1,863,680 bytes, last write 02-11-2026 11:15:30 (unchanged). `PRAGMA integrity_check` = ok. Opening a WAL database read-only
created zero-byte `csi_coverage_test.db-wal` and a 32 KB `-shm` next to it (SQLite housekeeping, no data change; the same files exist beside other bench DBs).

- **Active run id: 1** (the only run). `runs.status = PAUSED`, `started_at 2026-11-02T04:24:04Z`, `finished_at NULL`.
- `runs.llm_tokens_used = 210224`, `runs.llm_token_budget = NULL` (unlimited), `llm_cost_usd = 0.0` (prices are 0 in `.env`). Sum of `agent_invocations` tokens = 210,224 (matches).
- Stored run config: provider `groq`, model `qwen/qwen3.8-27b`, `max_depth=3`, `max_total_pages=100`, `max_llm_calls_per_run=150`, `llm_rpm_limit=llm_tpm_limit=-1`, `queue_max_rate_limit_deferrals=6`.
- Entities: 15 total. **CONFIRMED 7** (Constellation Software Inc. [Parent Company], Harris, Volaris, Jonas Software, Vela, Modaxo, Andromeda [6 Operating Groups]).
  **REVIEW_REQUIRED 8** (3 Operating Group, 4 Portfolio Group, 1 Subsidiary). crawl_state: EXPANDED 2, NO_WEBSITE 5, PENDING 5, TERMINAL 1, UNREACHABLE 2. Max task depth 1.
- `research_queue`: **DONE 3** (task 1 root, task 2 Harris = `TERMINAL:NO_CANDIDATES`, task 4 Jonas Software), **PENDING 4**, no FAILED/RUNNING/SKIPPED:

| task | entity | depth | attempts | not_before (UTC) | error |
|---|---|---|---|---|---|
| 3 | Volaris | 1 | 0 | 2026-11-02T05:52:36Z | EXTRACTION_RETRY_PENDING (attempt 1/3, rate limited, deferral 6/6) |
| 5 | Vela | 1 | 0 | 2026-11-02T05:53:39Z | ... deferral 5/6 |
| 6 | Modaxo | 1 | 0 | 2026-11-02T05:54:34Z | ... deferral 5/6 |
| 7 | Andromeda | 1 | 0 | 2026-11-02T05:52:24Z | ... deferral 4/6 |

  All four `not_before` times are already in the past (it is ~05:58 UTC), so they are eligible now; the supervisor's deferral counters are in memory and restart at 0 on resume.
- Invocations: 122 total; run-level `llm_calls()` as BudgetGuard counts it = **77 of the stored 150 cap** (the resume reads the cap from current Settings/env, not from the stored config).
  Live token-bearing requests 53 (36 extraction OK, 7 extraction SCHEMA-failed, 5 relationship, 5 website); 45 extraction cache hits; 24 TRANSPORT failures (all 429); 52 `crawl_pages`; 2 FETCH errors (HTTP 403 on csiperseus.com and luminegroup.com).
- Caveat for the planner: resuming this DB always writes to it (`init_db`, run status, new invocations). Per the safety rules, any test that needs this state must copy it first with the sqlite backup API to
  `bench/csi_coverage_test_backup_<yyyymmdd-hhmmss>.db`.

## 7. Live spend and safety log for this step

- GROQ calls made: 1 x `GET /models`, 3 x chat completion (`max_completion_tokens=5`). Tokens: 18 + 80 + 80 = **178 tokens** (qwen 18, gpt-oss-120b 80, gpt-oss-20b 80); about $0.00. This leaves ~149,820 of the 150,000-token workflow allowance.
- No 429 was triggered, no retry loop exists in the probe, TLS verification stayed on, and the API key was never printed.
- `data/csi_hierarchy.db` and `data/backups/` were not touched. `bench/csi_coverage_test.db` was not modified.

## 8. Recommendations carried to the planner

1. Capture the 429 body (message, `retry-after`, `x-ratelimit-*`) in the provider error and classify it as TPM/RPM (minute) or TPD/RPD (day) from the text ("per minute", "per day", "tokens per day (TPD)", "try again in ...").
2. Add a persisted rolling 24 h token ledger (TPD default 200,000 per model, configurable) and pace so projected tokens/day stay under it; keep the 90 s floor and the 86.4 s RPD pace as lower bounds.
3. Add a TPM guard of 8,000 (configurable) that accounts for `input + max_output_tokens`, and cap qwen extraction output so one request fits in the window.
4. Correct `.env.example` (200000/600 is wrong for this account; RPD is not RPM) and expose a slow-crawl default of RPM 30, TPM 8000, TPD 200000 for Groq.
5. Make the token ceiling independent of `runs.llm_token_budget` (which is NULL for the user's run) or update the row deliberately at resume.
6. Persist rate-limit deferral counts or ignore the in-memory 6-deferral cap in slow mode, since a 24 h run must keep waiting instead of burning attempts.
