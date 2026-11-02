# Slow Crawl Mode: final verification results

Branch `claude/m8-ollama-e2e-fix`. The user's crawl was not started and the configured model was not changed.

## Safety checks
- No `python.exe` process was running before or after the live benchmarks.
- `bench\csi_coverage_test.db`: 1,863,680 bytes, LastWriteTime 02-11-2026 11:15:30, before and after. Unchanged.
- `data\csi_hierarchy.db` and `data\backups` have no changes (`git status` clean, newest file times are from October).
- Benchmarks wrote only fresh `bench\slow_crawl_*` files. TLS verification untouched. The API key was never printed.

## Tests
| Suite | Baseline | Now |
|---|---|---|
| `python -m pytest tests/unit -q` | 246 passed | 246 passed |
| `python -m pytest -q` | 997 passed, 3 deselected | 1220 passed, 3 deselected (388 s) |

The full suite gained 223 tests (the `tests/slow_crawl/` scenarios and related tests). No failures.

## Micro-benchmark (`scripts/slow_crawl_benchmark.py micro`, qwen/qwen3.8-27b, interval 20 s for the benchmark only)
Report: `bench/slow_crawl_benchmark_20261102-160422.json`, DB `bench/slow_crawl_micro_20261102-160422.db`.
- 9 requests, all completed. Start gaps (s): 20.001, 20.0, 20.001, 20.001, 20.0, 20.0, 20.0, 20.0, 40.0, 40.0. Minimum gap 20.0 s, so the interval was respected.
- Maximum requests in flight: 1.
- Resume proof: a synthetic 429 (minute scope, Retry-After 20 s) was injected on request 9. The limiter cooled down and doubled the gap to 40 s. The wait before retry was 40 s (not shorter than Retry-After), and request 9 then succeeded. Final limiter state: 11 provider requests, 9 successes, 1 rate-limited.
- Real HTTP 429 count: 0. One transient `ConnectionError` occurred after the synthetic 429. The retry handled it, and it is why the script exited with code 1 even though all checks passed.
- Tokens: 904.

## Model benchmark (`models`, same 3 pages, same grounding rule: quote found verbatim in the page; 65 s interval)
Report: `bench/slow_crawl_benchmark_20261102-160851.json`.

| Model | Schema valid | Emitted | Grounded (precision) | Tokens (in / out / ledger) | Latency total | HTTP 429 | Cost |
|---|---|---|---|---|---|---|---|
| qwen/qwen3.8-27b | 3/3 | 19 | 17 (89.5%) | 8,383 / 4,868 / 13,251 | 978.7 s* | 1 | $0.00 (no price set) |
| openai/gpt-oss-120b | 3/3 | 19 | 15 (78.9%) | 11,187 / 5,512 / 16,699 | 349.6 s | 0 | $0.00 (no price set) |
| openai/gpt-oss-20b | 3/3 | 19 | 19 (100%) | 11,187 / 4,897 / 16,084 | 296.7 s | 0 | $0.00 (no price set) |

\* Includes a 778 s cooldown after the one real minute-quota 429 on qwen (sample 2 latency 842 s). Model-side latency for sample 1 was 4.2 s.

Notes: all three models were available on the account. Costs show $0.00 because no `--price MODEL=IN,OUT` was supplied, so they are unpriced, not free. Grounded counts measure quote presence only; ownership is still confirmed by the pipeline's separate gates. The sample is 3 pages, so the differences are indicative only. No model change was made to the crawl.

## Token spend against the 150,000 cap
Ledger total 47,116 (178 prior + 904 micro + 46,034 models). Soft stop 120,000; hard cap 150,000. Remaining under the cap: 102,884.

## Remaining blockers
- None for the code. Model choice is the user's decision; the sample favors gpt-oss-20b on grounding, but 3 pages is a small sample.
- Set model prices (`--price`, or the cost settings) if cost reporting or a cost ceiling is wanted.
- The earlier crawl process must be confirmed stopped before resuming (none was running at this check).
- An untracked stray copy `slow_crawl_benchmark.py` exists at the repo root; the real script is `scripts/slow_crawl_benchmark.py`. It was left uncommitted.
- A 24 h run can still be cut short by the daily token quota (TPD 200,000); slow mode waits and resumes, and the 250,000 token ceiling stops it gracefully.

## Command to resume `bench\csi_coverage_test.db` in Slow Crawl Mode
Run only after confirming no previous crawler is running.

```powershell
cd "C:\Users\Dharnish\OneDrive - TPF Software\python\Company_web_scrapping"
Remove-Item Env:LLM_TPM_LIMIT,Env:LLM_RPM_LIMIT -ErrorAction SilentlyContinue
$env:DB_PATH=".\bench\csi_coverage_test.db"; $env:MAX_DEPTH="3"; $env:MAX_TOTAL_PAGES="100"; $env:MAX_LLM_CALLS_PER_RUN="150"; $env:SLOW_CRAWL_TOKEN_CEILING="250000"
python main.py resume --run-id 1 --slow-crawl --provider groq --model qwen/qwen3.8-27b --yes
```
