# CSI Ollama Local Model Test Report

Date: 2026-10-30. All figures below were measured in this session. "Not measured" means the test did not get that far.

## 1. Environment

| Item | Measured |
|---|---|
| Ollama | 0.40.12, server reachable at http://localhost:11434 |
| Installed models | `qwen3:4b` (4.0B, Q4_K_M, 2.3 GB), `mistral:latest` (7.2B, Q4_K_M, 4.1 GB). Nothing was downloaded. |
| Model max context | qwen3: 262,144; mistral: 32,768. App uses `num_ctx=12288` (setting `OLLAMA_NUM_CTX`). |
| CPU | Intel Core i5-1235U, 10 cores / 12 threads |
| GPU | None usable (Intel UHD integrated). `ollama ps` shows **100% CPU** for both models. |
| RAM | 15.7 GB total; 3.2 GB free before the tests |
| Simple-prompt check | both answered; ~2.8 tok/s generation each (qwen3 spent its 40-token budget on reasoning text with `think:false` in my raw probe; mistral replied `{"ok": true}`) |

App wiring check: the CLI `--model` flows through `provider_settings_for_run` to the provider and to the gateway, so every agent uses the selected model. The Ollama provider uses the native API (`num_predict` cap, explicit `num_ctx`, JSON Schema as `format` grammar, `think` off for extraction).

## 2. Tests and commands

Each test used a fresh isolated DB under `bench/` (`init-db` then run), 1 task, `LLM_TIMEOUT_SECONDS=1200` (raised from 300; documented adjustment), a resource sampler every 10 s.

```
$env:DB_PATH="./bench/ollama_qwen.db"; $env:LLM_TIMEOUT_SECONDS="1200"
python main.py run --yes --max-tasks 1 --provider ollama --model qwen3:4b
$env:DB_PATH="./bench/ollama_mistral2.db"; $env:LLM_TIMEOUT_SECONDS="1200"
python main.py run --yes --max-tasks 1 --provider ollama --model mistral:latest
```

Qwen was stopped manually after 32 min (11:24 to 11:56) as an explicit bound; mistral ended on its own by timeout after 1,212 s.

## 3. Results

| Metric | Qwen3 4B | Mistral 7B |
|---|---|---|
| Model loading | not separately logged; probe: 0.0 s warm | probe: 13.2 s cold load |
| Extraction successful | **Yes**, schema-valid (status OK) | **No**, `transport TIMEOUT` at 1,200 s |
| Extraction time / tokens | 944 s; 3,051 in / 1,654 out (~1.75 tok/s) | 1,202 s then timeout; 0 tokens recorded |
| JSON schema valid | extraction yes; website_resolution 3 of 3 attempts invalid | not measured |
| Operating groups extracted | **9 of 9** (Harris, Volaris, Jonas Software, Topicus.com, Perseus, Vela, Lumine Group, Modaxo, Andromeda); heading "Constellation Software's operating groups" correctly typed `OTHER` | 0 |
| Relationships verified | 0 (never reached) | 0 |
| Entities stored | 1 (the root only) | 1 (the root only) |
| Child tasks queued | 0 | 0 |
| Portfolio companies discovered | 0 | 0 |
| Maximum hierarchy depth | 0 | 0 |
| Website-resolution calls | 3 failed `SCHEMA`: output cut at the 600-token cap (finish_reason=length) after ~250 s each; 4th attempt in flight when stopped | not reached |
| CPU | avg 82%, max 100% | avg 72%, max 100% |
| Free RAM (minimum) | 0.87 GB | **0.05 GB** (near exhaustion) |
| Total execution time | ~32 min until manually stopped | 20.2 min |
| Errors | 3x SCHEMA truncation | 1x TIMEOUT |
| Overall | **Partial**: extraction correct but too slow; pipeline did not complete | **Fail** |

Memory caveat: other processes (including the Groq repair workflow's test runs) were using RAM at the same time, so the RAM minimums are not purely the models' footprint. My "Ollama working set" column measured the launcher process, not the model runner, and is discarded.

## 4. Analysis

- **Throughput is the blocker.** On this CPU-only laptop both models generate about 2 tokens/s. One extraction (≈1,650 output tokens) costs 15 to 20 minutes. One CSI page chunk needs extraction, then website resolution (one call per candidate, 9 candidates), then relationship verification. A single root task would need hours; a few dozen tasks would take days.
- **Mistral timeout cause.** Consistent with throughput: ~3,000 prompt tokens plus ~1,500 output tokens at <2 tok/s exceeds even 1,200 s, and the machine was close to out of memory (0.05 GB free). The earlier 300 s failure is explained by the same arithmetic. I did not test smaller prompts or a different `num_ctx`, so I cannot say which factor dominates.
- **Qwen quality is good, not the problem.** It found all nine groups and correctly excluded a heading. It was not checked for ownership relationships because it never reached verification.
- **Website-resolution truncation (unverified cause).** Qwen3 is a reasoning model. The runner only turns thinking off for the extraction agent (`reasoning_overrides` has only `EXTRACTION`), so other agents may be spending the 600-token cap on reasoning. I did not confirm this; it is the first thing to test.
- Agents not tested on either model: Relationship Verification, Discovery, Deduplication, Validation (Website Resolution only failed on Qwen). Vela and Jonas multi-level tests were **not run** because the initial pipeline did not complete.

## 5. Recommendation

1. **Do not run the full CSI discovery on these local models on this machine.** Hours per task make a bounded 300-page crawl impractical.
2. **Keep Ollama for small offline or development tests.** If used, `qwen3:4b` is the better choice (correct extraction), with `LLM_TIMEOUT_SECONDS=1200` and thinking disabled for all structured agents (needs a code change, then a retest).
3. **Use Groq for the real run.** Its TLS now verifies, and it completed root extraction plus relationship verification in minutes (see the separate Groq results). Gemini free tier allows only 20 requests per day for `gemini-3.8-flash`, so it is not suitable for the full crawl without billing.
4. A more capable local model would need a GPU with enough VRAM; that is a hardware and approval decision.

## 6. Remaining blockers before full CSI discovery

- Local models: throughput, memory, and the Website Resolution truncation (see §4).
- Child-extraction defects found in the Groq run (duplicate-relationship commit failure, child start pages skipped, long-listing truncation) are being repaired in workflow `wf_7faf48ec1baaf723`; that must finish and pass its re-test first.
- Groq 429 rate limits slow runs; Perseus and Lumine return HTTP 403 to the crawler (left REVIEW_REQUIRED).
- Playwright is installed but not yet proven on a page that needs JavaScript.

Raw data: `bench/ollama_qwen.db`, `bench/ollama_mistral2.db`, `bench/qwen_resources.csv`, `bench/mistral_resources.csv`, `bench/ollama_probe.log`.
