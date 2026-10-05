# Implementation Plan: M1 (deterministic services) + M2 (agent infrastructure)

Project root: `c:\Users\Dharnish\OneDrive - TPF Software\python\Company_web_scrapping` (all paths below are relative to it).
Python: `.venv\Scripts\python` (3.13.5 in the venv; project floor is 3.12, use only 3.12-safe syntax).
Test command (always from the project root): `.venv\Scripts\python -m pytest -q` (baseline: 110 passed).
Do NOT edit: `ARCHITECTURE.md`, `product.md`, `tech.md`, `Corporate-Discovery.md`, `Agent-Workflow.md`, `.kiro\agents\corporate-research-agent.md`, `src/corpdisc/db/schema.sql` (D1).
This plan FIXES the interfaces. Where this plan paraphrases a D-decision and the full D-text in your coder prompt says something different, the full D-text wins; keep names and signatures from this plan unless they would violate a D-text.

Workflow note: I did NOT restructure the workflow tail. The existing `m1-loop` (stops on `.agents/tasks/m1-review.json` verdict == APPROVED) and `m2-loop` (stops on `.agents/tasks/m2-review.json` verdict == APPROVED) are already the natural decomposition (M1 = items 1-12, M2 = items 13-20), and their prompts carry the full D1-D18 text that a replacement tail would have to re-author. Phase M1 implements items 1-12. Phase M2 implements items 13-20. M2 must not start before M1 is APPROVED (M2 reuses `contracts/common.py`, `clock.py`, `services/budget.py`, `db/repos`).

---------------------------------------------------------------------------------------------------

## 0. What exploration established (ground truth, not assumptions)

- M0 code: `config.py` (pydantic-settings `Settings`, `load_settings`), `logging_setup.py` (`get_logger`, `setup_logging`, redaction filter), `cli.py` (only `init-db`), `models/domain.py` (StrEnums + strict row models `Run, Entity, Relationship, Evidence, Website, ResearchTask`, base `DomainModel`), `db/connection.py` (`connect_writer`, `connect_readonly`, `init_db`, `verify_db`, `EXPECTED_TABLES`, `DatabaseError`), `db/schema.sql` (17 tables, final DDL), `services/__init__.py` (empty).
- `connect_writer` returns a `sqlite3.Connection` with `row_factory=sqlite3.Row`, FK ON, WAL, busy_timeout 5000, Python-default (legacy) isolation. Explicit `conn.execute("BEGIN IMMEDIATE")` then `conn.commit()` / `conn.rollback()` works in this mode and is what the Committer uses. Repos that do single statements use `with conn:`.
- `tests/conftest.py`: autouse `_hermetic_env` (strips every `Settings` env var, chdir to tmp), autouse `_block_network` (patches `socket.socket.connect`, `connect_ex`, `socket.create_connection` to raise `RuntimeError`), fixture `settings` (paths inside tmp). `CONFIG_ENV_VARS` derives from `Settings.model_fields`, so new settings are automatically stripped from env in tests.
- `tests/test_models.py::test_no_agent_contracts_in_m0` asserts `AgentRequest/AgentResponse/VerifiedEvidence/CommitPlan` are NOT attributes of `models.domain`. New contracts live in `corpdisc/contracts/`, so this test keeps passing unchanged. Do not edit it.
- Installed in the venv: pydantic 2.13.5, pydantic-settings 2.15.0, pytest 9.1.1. NOT installed: requests, beautifulsoup4, tldextract, playwright. Latest versions confirmed via `pip index`: requests 2.34.2, beautifulsoup4 4.15.0, tldextract 5.4.0, playwright 1.63.0.
- Schema facts the code must honour: `crawl_pages.fetch_method IN ('requests','playwright','cache')`; `crawl_pages.crawl_status IN ('OK','CACHED','BLOCKED_ROBOTS','HTTP_ERROR','TIMEOUT','SSL_ERROR','SKIPPED_LIMIT','SKIPPED_FILTER')`; `UNIQUE(run_id, canonical_url)`; `research_queue.status IN ('PENDING','RUNNING','DONE','FAILED','SKIPPED')`, `UNIQUE(run_id, entity_id)`, has `not_before`; `agent_invocations.status IN ('OK','PARTIAL','FAILED','SKIPPED')`, `failure_kind` nullable, `agent` CHECK = the 7 names; `idx_one_primary_parent` is unique on `relationships(child_id) WHERE is_primary=1 AND verification_status='CONFIRMED'`; `candidates UNIQUE(run_id, source_page_id, normalized_name)`; `evidence.invocation_id`, `candidates.extraction_invocation_id`, `gate_rejections.invocation_id` all FK to `agent_invocations`.
- `agent_invocations` has NO column for provider, model, temperature, error message, or transport-retry detail. Per D1 these go into `request_json` / `response_json` (shapes pinned in section 4.M2.3).

---------------------------------------------------------------------------------------------------

## 1. Decisions applied (D1-D18) and plan-level decisions (P1-P14)

### 1.1 Binding decisions, as applied

| # | Applied as |
|---|---|
| D1 | `schema.sql` untouched. `agent_invocations` + `agent_tool_calls` are THE audit and cache tables. `llm_calls` is never written. Extra audit fields live in `request_json` / `response_json` (pinned in 4.M2.3). A test asserts `schema.sql` is byte-identical in content to the M0 version (hash pinned in the test) and that `llm_calls` row count stays 0 after agent runs. |
| D2 | Queue states exactly PENDING/RUNNING/DONE/FAILED/SKIPPED (`COMPLETED` appears only in docs text as alias, never in code). `parent_entity_id` is derived by query, never stored. `priority = depth * 1_000_000 + discovery_seq` via `compute_priority`. `UNIQUE(run_id, entity_id)` enforces visited_entities; `enqueue` returns `None` for duplicates. |
| D3 | NO `services/sitemap.py`, `services/search.py`, `services/merge.py`. |
| D4 | Layering in section 2. `agents/` defines PROTOCOLS only (`AuditSink`, `ResponseCache`, `BudgetPort`, `LLMProvider`; Clock/Sleeper re-exported, see P2). SQLite implementations: `db/repos/audit.py` (`SqliteAuditSink`, `SqliteResponseCache`) and `services/budget.py` (`BudgetGuard` implements `BudgetPort`). Filesystem access inside `agents/` only in `agents/providers/`. AST import-linter test enforces it. `ToolBroker` receives injected `ToolSpec` objects. |
| D5 | `QuoteVerifier`: NFKC + typographic normalization + whitespace collapse, case-sensitive substring, NO fuzzy. Returns `char_start`/`char_end` as offsets into the ORIGINAL page text passed in. (This supersedes the fuzzy 0.95 fallback in Part I 10.3 and Part II G3.) |
| D6 | Transport retries ONLY in `LLMGateway`. Schema repair ONLY in `AgentRunner` (exactly 1 re-ask). `MAX_PROVIDER_REQUESTS_PER_INVOCATION` (default 6) counts every raw provider request (transport retries + repair + tool-loop continuations). FetchService retries network errors only. |
| D7 | One `agent_invocations` row per provider attempt, all sharing the invocation `correlation_id`. Cache key = sha256 over canonical JSON of `{agent, provider, model, schema_version, prompt_version, temperature, max_output_tokens, input}`; stored in `input_hash`. Invalid responses are never cached (cache lookup only reads `status='OK'` rows and the payload is re-validated on read). See P5 for how "provider attempt" is defined. |
| D8 | `CircuitBreaker` states closed / open / half-open, in memory, in `agents/gateway.py`. No schema table (flag F1). |
| D9 | `contracts/common.py`, `contracts/agents.py`, `contracts/postgate.py`. Postgate models are concrete and mirror schema columns. `AgentName` = exactly the 7 schema-CHECK values. Toy agents exist only under `tests/`. |
| D10 | `Settings.require_website_for_confirmed: bool = True` (flag F2); enforced by Committer invariant 6. |
| D11 | tldextract OFFLINE: `TLDExtract(suffix_list_urls=(), cache_dir=<CACHE_DIR>/tldextract, fallback_to_snapshot=True)`. `difflib.SequenceMatcher` for fuzzy name similarity. No other fuzzy library. |
| D12 | `tests/conftest.py` network guard changed to allow only `127.0.0.1`, `::1`, `localhost`; everything else still raises. Guard self-test added. |
| D13 | Confidence values and thresholds per ARCHITECTURE Part I section 7 with the simpler reject rule (flag F3). |
| D14 | Playwright optional, lazily imported inside `PlaywrightRenderer.render`, behind the `Renderer` Protocol. Tests marked `@pytest.mark.playwright` skip cleanly when the package or browser is missing. All default tests use a fake renderer. |
| D15 | Fetch status mapping table in 4.M1.6 (no schema change). Page bodies on disk under `CACHE_DIR/pages/`, atomic writes (temp file + `os.replace`), content-hash verified on every read. Never store bodies in SQLite. |
| D16 | Provider factory `build_provider` supports `fake`, `replay`, `record` only. `openai`, `anthropic`, `gemini`, `google` raise `ProviderNotImplementedError("provider '<name>' is not implemented in M2")`. No SDK is imported anywhere. |
| D17 | `ToolBroker` is tested with toy read-only tools defined in `tests/`; no real tool handlers are written in M2. |
| D18 | `FetchService` may write via `PageRepo` (`crawl_pages`, `domain_stats`, `crawl_errors` only). |

### 1.2 Plan-level decisions (one or two sentences each)

- P1. Enums stay in `models/domain.py` (M0). `contracts/common.py` RE-EXPORTS them and adds the shared value objects; contracts import only `models`, `clock` and pydantic. This keeps one source of truth for enum values and the existing schema-sync test.
- P2. `Clock` / `Sleeper` protocols live in a new neutral module `corpdisc/clock.py` (stdlib only). `agents/ports.py` re-exports them. D4 says agents define them; a neutral module is needed because services also need them and services may not import agents. Agents still only see protocols. Only `clock.py` may call `time.sleep/time.time/time.monotonic/datetime.now` (lint-tested), which makes every service deterministic under a `FakeClock`.
- P3. Contract placement: `common.py` = `Strict`, re-exported enums, shared value objects (`EntityCard, PageExcerpt, LinkRef, RawCandidate, EvidenceClaim, VerifiedEvidence, Signal, BudgetVerdict`). Created in M1 because M1 services return `LinkRef`, `RawCandidate`, `VerifiedEvidence`, `Signal`. `postgate.py` (M1) = post-gate models and `CommitPlan`. `agents.py` (M2) = agent envelope (`AgentName, CallStatus, FailureKind, Meta, CallLimits, Usage, AgentError, AgentRequest, AgentResponse`) and audit record models (`InvocationStart, InvocationFinish, ToolCallRecord, CachedResponse`). Per-agent input/output models (Section 22.4-22.10) are M5-M7 and are NOT created.
- P4. Page URL "domain" everywhere (`crawl_pages.domain`, `domain_stats.domain`) means lowercase `host` plus `:port` only when the port is non-default. `registrable_domain` (websites) is the tldextract registered domain. This lets local test servers on different ports stay independent.
- P5. "Provider attempt" (D7) = one `LLMGateway.complete` call made by the AgentRunner (first ask, schema repair ask, or tool-loop continuation). The gateway's internal transport retries are nested inside that call and recorded in the row's JSON (`transport` object), and they count toward `MAX_PROVIDER_REQUESTS_PER_INVOCATION`. AgentRunner writes the rows (26.2). Flag F5.
- P6. Audit rows are written start/finish: `start_invocation` inserts a row with `status='FAILED'`, `failure_kind=NULL`, `finished_at=NULL` (an unfinished row is therefore fail-closed and detectable), `finish_invocation` updates it. This gives tool calls a real `invocation_id` (FK NOT NULL) while the attempt is still running. Only `status='OK'` rows are cache entries.
- P7. Task retry budget: `QueueService.max_attempts = 3` (first run + 2 retries, Part II 25.1/A1 "retries: 2").
- P8. Root entity: the first entity committed for a run (lowest `discovery_seq`) must be flagged `is_root=True` in the plan; it is `Parent Company`, `CONFIRMED`, confidence 1.0, with no relationship. Invariant 4 exempts exactly that entity.
- P9. Committer defence in depth: besides the six architecture invariants it also rejects `CONFIRMED` with `confidence < Settings.confirm_threshold` and any `evidence.quote_verified=1` row that has no `page_id` or fails re-verification.
- P10. Trailing slash: canonical URL removes a trailing slash from non-root paths (`/about/` -> `/about`); root stays `/`. Only a fixed tracking-parameter denylist is stripped (`utm_*`, `fbclid`, `gclid`, `dclid`, `msclkid`, `mc_cid`, `mc_eid`, `yclid`, `igshid`, `_hsenc`, `_hsmi`); all other query parameters are kept and sorted.
- P11. SSRF guard (crawled content is untrusted): new `Settings.allow_private_hosts: bool = False`. FetchService refuses IP-literal hosts in private/loopback/link-local/reserved ranges and the names `localhost`, `*.localhost`, `*.local`, `*.internal` unless the flag is true. Tests that use the local server set it true. DNS-rebinding is a documented limitation.
- P12. `cli.py` and `main.py` are NOT changed (no new CLI commands).
- P13. Existing M0 tests stay where they are (`tests/test_*.py`). New tests go in `tests/unit`, `tests/contracts`, `tests/services`, `tests/agents`, `tests/integration` (each with `__init__.py`), helpers in `tests/helpers/`, fixtures in `tests/fixtures/`.
- P14. All timestamps stored as ISO-8601 UTC text produced by `clock.iso_utc(dt)`; parsed by `clock.parse_iso_utc(s)`.

---------------------------------------------------------------------------------------------------

## 2. Import-boundary layering (D4), enforced by `tests/unit/test_import_boundaries.py`

Allowed internal imports (anything else under `corpdisc.` is a violation):

| Package | May import from `corpdisc.` |
|---|---|
| `models` | nothing |
| `clock` | nothing |
| `config` | nothing |
| `logging_setup` | `config` |
| `contracts` | `models`, `clock`, `contracts` |
| `db` (incl. `db/repos`) | `models`, `contracts`, `clock`, `db` |
| `services` | `models`, `contracts`, `clock`, `config`, `logging_setup`, `db`, `services` |
| `agents` | `models`, `contracts`, `clock`, `config`, `logging_setup`, `agents` |
| `cli` | anything (composition root) |

Additional AST rules:
- `agents/` EXCEPT `agents/providers/`: must not import `sqlite3, requests, urllib.request, urllib3, http, httpx, socket, ssl, playwright, bs4, os, pathlib, io, shutil, tempfile, glob`; must not call builtins `open, eval, exec, compile, __import__`; no function parameter named `conn, connection, cursor, db, session, http_client, fetch_service, fetcher`.
- `agents/providers/`: may use `pathlib`, `os`, `json`; still must not import `sqlite3, requests, urllib3, http, httpx, socket, ssl, playwright, bs4`.
- Whole `src/`: must not import `openai, anthropic, google.generativeai, google.genai, langgraph, langchain, openpyxl, sqlalchemy`. `time.sleep/time.time/time.monotonic/datetime.now/datetime.utcnow` only in `clock.py`.
- No class in `src/` subclasses `BaseAgent` (toy agents live in `tests/` only).
- Forbidden files must not exist under `src/corpdisc`: `discovery.py extraction.py relationship.py website.py dedup.py validation.py advisor.py sitemap.py search.py merge.py`, and no `gates/` or `graphs/` directories.
- The linter is a function `check_source(module_name: str, source: str) -> list[str]` in `tests/helpers/import_lint.py`; the test runs it over the real tree AND over synthetic violating snippets (so the linter is proven able to fail).
- Write-ownership test `tests/unit/test_write_ownership.py` scans `src/corpdisc` for SQL `INSERT INTO|UPDATE|DELETE FROM <table>` string literals and asserts: business tables (`entities, entity_aliases, websites, relationships, evidence, candidates, review_items, gate_rejections, validation_findings`) are mutated only in `services/committer.py`; `research_queue` only in `services/queue.py` (plus `UPDATE entities SET crawl_state` there); `crawl_pages, domain_stats, crawl_errors` only in `db/repos/pages.py`; `agent_invocations, agent_tool_calls` only in `db/repos/audit.py`; `runs` token columns only in `services/budget.py`; `llm_calls` nowhere (D1).

---------------------------------------------------------------------------------------------------

## 3. New dependencies and settings

`pyproject.toml` (exact pins):
```
dependencies = ["pydantic==2.13.5", "pydantic-settings==2.15.0", "requests==2.34.2", "beautifulsoup4==4.15.0", "tldextract==5.4.0"]
[project.optional-dependencies]  dev = ["pytest==9.1.1"]   browser = ["playwright==1.63.0"]
[tool.pytest.ini_options]  markers = ["playwright: needs the playwright package and an installed chromium (skipped otherwise)"]
```
Install: `.venv\Scripts\python -m pip install -e ".[dev]"`. Do NOT install the `browser` extra by default. If pip cannot reach the index, stop and report; do not hand-roll replacements.

`Settings` additions (all with defaults; add to `.env.example` and README; add tests to `tests/test_config.py` style file `tests/unit/test_config_m1_m2.py`):

| Field (env var) | Type / default | Used by |
|---|---|---|
| `max_response_bytes` (MAX_RESPONSE_BYTES) | `int = Field(5_000_000, ge=1024)` | FetchService |
| `allow_private_hosts` (ALLOW_PRIVATE_HOSTS) | `bool = False` | FetchService (P11) |
| `require_website_for_confirmed` (REQUIRE_WEBSITE_FOR_CONFIRMED) | `bool = True` | Committer (D10) |
| `llm_token_budget` (LLM_TOKEN_BUDGET) | `int \| None = Field(None, ge=1)`; blank env string -> None (add a `mode="before"` validator like the existing secret one) | copied to `runs.llm_token_budget` by M4; M2 tests seed it directly |
| `max_llm_calls_per_task` (MAX_LLM_CALLS_PER_TASK) | `int = Field(12, ge=1)` | BudgetGuard |
| `max_provider_requests_per_invocation` (MAX_PROVIDER_REQUESTS_PER_INVOCATION) | `int = Field(6, ge=1)` | AgentRunner |
| `llm_breaker_threshold` (LLM_BREAKER_THRESHOLD) | `int = Field(5, ge=1)` | CircuitBreaker |
| `llm_transport_retries` (LLM_TRANSPORT_RETRIES) | `int = Field(3, ge=0)` | LLMGateway |
| `llm_backoff_base_seconds` / `llm_backoff_max_seconds` | `float = 1.0` / `30.0`, both `ge=0` | LLMGateway |
| `llm_temperature` (LLM_TEMPERATURE) | `float = Field(0.0, ge=0, le=2)` | LLMGateway |
| `llm_cassette_dir` (LLM_CASSETTE_DIR) | `Path = Path("./.cache/llm_cassettes")` | providers factory |

Existing `use_playwright` ("auto"|"true"|"false") becomes live in M1: auto = render only when the JS-shell heuristic fires; true = render every HTML page; false = never.

---------------------------------------------------------------------------------------------------

## 4. Module interface catalog

Conventions: `from __future__ import annotations`; type hints everywhere; frozen dataclasses for service results, `Strict` pydantic models for contracts; services never raise on untrusted input (return structured results); every SQL statement is parameterized; loggers via `get_logger("services.<name>")` / `get_logger("agents.<name>")`; never log page text beyond 200 chars or any secret.

### 4.M1 Deterministic services

#### 4.M1.0 `src/corpdisc/clock.py`
```python
class Clock(Protocol):
    def now(self) -> datetime: ...          # aware, UTC
    def monotonic(self) -> float: ...
class Sleeper(Protocol):
    def sleep(self, seconds: float) -> None: ...
class SystemClock:                           # implements Clock and Sleeper
    def now(self) -> datetime; def monotonic(self) -> float; def sleep(self, seconds: float) -> None
def iso_utc(dt: datetime) -> str
def parse_iso_utc(value: str) -> datetime
```
Test helper `tests/helpers/clock.py::FakeClock(start: datetime | None = None)` with `now()`, `monotonic()`, `sleep(seconds)` (advances both and records `sleeps: list[float]`), `advance(seconds)`.

#### 4.M1.1 `contracts/common.py` (no logic beyond validators)
```python
class Strict(BaseModel):   # ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)
# re-exported from corpdisc.models.domain: EntityType, RelationshipType, VerificationStatus, OperatingStatus,
#   CrawlState, SourceClass, Supports, CandidateKind, ReviewReason, PageType, WebsiteKind, QueueStatus, CrawlStatus, RunStatus
MIN_QUOTE_CHARS: Final[int] = 15
class EntityCard(Strict)      # exactly ARCHITECTURE 22.3
class PageExcerpt(Strict)     # 22.3
class LinkRef(Strict)         # 22.3 (anchor_text max 200; location Literal["nav","footer","body","sitemap"])
class RawCandidate(Strict)    # 22.3 (name_text max 200; context max 400; dom_hint Literal[...7 values])
class EvidenceClaim(Strict)   # page_id:int, quote: str(min_length=MIN_QUOTE_CHARS, max_length=400), supports: Supports
class VerifiedEvidence(Strict):
    page_id: int; url: str; quote: str; quote_verified: bool
    char_start: int | None; char_end: int | None
    source_class: SourceClass; supports: Supports
    invocation_id: int | None = None      # flag F7: 22.3 says int; None needed for deterministic (non-agent) evidence
    # validator: quote_verified=True requires 0 <= char_start < char_end; False requires both None
class Signal(Strict):         # 22.9 (name Literal[12 names], value float|str|bool); shared because the matcher produces it
class BudgetVerdict(Strict): allowed: bool; reason: str | None = None; limit: float | None = None; used: float | None = None
```
Consumed by: links, dom_candidates, quotes, matcher, budget, committer. Produced by: contracts tests only.

#### 4.M1.2 `contracts/postgate.py`
Models per ARCHITECTURE 22.11, all `Strict`: `ApprovedPage, GroundedCandidate, ResolvedWebsite, MatchDecision, ScoredRelationship, ValidationOutcome` (fields exactly as 22.11; `ScoredRelationship.evidence: list[VerifiedEvidence]`). Plus the concrete CommitPlan parts:
```python
class EntityRef(Strict):          # exactly one of the two must be set (validator)
    entity_id: int | None = None; local_ref: str | None = None
class NewEntity(Strict):          # local_ref, name, normalized_name, entity_type, description, country, industry,
    local_ref: str                # operating_status=Unknown, verification_status, classification_confidence, confidence,
    is_root: bool = False         # crawl_state=PENDING, discovery_method, sibling_order  (discovery_seq & discovered_at: Committer assigns)
class EntityUpdate(Strict):       # entity_id, crawl_state|None, verification_status|None, confidence|None
class NewAlias(Strict):           # entity: EntityRef, alias, normalized_alias, source_page_id|None
class NewWebsite(Strict):         # local_ref, entity: EntityRef, url, domain, registrable_domain, kind: WebsiteKind,
                                  # resolution_method, reachable|None, last_http_status|None, is_primary: bool = False
class NewRelationship(Strict):    # local_ref, parent: EntityRef, child: EntityRef, relationship_type, verification_status,
                                  # confidence, is_primary=True, source_page_id|None, discovery_method|None
class PlanEvidence(Strict):       # entity: EntityRef, relationship_id: int|None, relationship_local_ref: str|None,
                                  # evidence: VerifiedEvidence   (at most one of the two relationship fields)
class NewCandidate(Strict):       # source_page_id, parent: EntityRef, raw_name, normalized_name, raw_link_url, context_snippet,
                                  # dom_hint, candidate_kind, proposed_type, decision, resolved: EntityRef|None, reason,
                                  # confidence, extraction_invocation_id, gate_code
class NewReviewItem(Strict):      # entity, possible_parent: EntityRef|None, reason_code: ReviewReason, reason_text,
                                  # recommended_action, confidence|None
class EnqueueRequest(Strict):     # entity: EntityRef, start_url: str|None, depth: int
class GateRejectionItem(Strict):  # gate_code: str, item_ref: str, detail: str, invocation_id: int|None
class CommitPlan(Strict):
    run_id: int; task_id: int | None
    candidates: list[NewCandidate] = []; entities: list[NewEntity] = []; entity_updates: list[EntityUpdate] = []
    aliases: list[NewAlias] = []; websites: list[NewWebsite] = []; relationships: list[NewRelationship] = []
    evidence: list[PlanEvidence] = []; review_items: list[NewReviewItem] = []
    enqueue: list[EnqueueRequest] = []; gate_rejections: list[GateRejectionItem] = []
    # validators: unique local_refs; EntityRef.local_ref must resolve to a NewEntity in the same plan
```
A plan containing only `candidates` + `gate_rejections` is legal (26.2 "stage candidates").

#### 4.M1.3 `services/urls.py` (pure; brief section 5)
```python
MAX_URL_LENGTH: Final = 2048
class UrlRejectReason(StrEnum): EMPTY; TOO_LONG; UNSUPPORTED_SCHEME; MALFORMED; MISSING_HOST; INVALID_HOST; INVALID_PORT; RELATIVE_WITHOUT_BASE
@dataclass(frozen=True, slots=True)
class UrlResult:
    original: str; url: str | None; reason: UrlRejectReason | None; detail: str | None = None
    scheme: str | None = None; host: str | None = None; port: int | None = None   # port None when default
    @property ok -> bool;  @property domain -> str | None     # host[:port], P4
def canonicalize_url(url: str | None, base: str | None = None) -> UrlResult   # NEVER raises
def domain_of(url: str) -> str | None
```
Rules: strip whitespace/control chars; resolve against `base` (urljoin) incl. scheme-relative; reject `javascript:, mailto:, tel:, data:, ftp:, file:` etc. as `UNSUPPORTED_SCHEME`; lowercase scheme/host; IDNA -> punycode; strip trailing host dot; drop default ports 80/443; drop userinfo (never keep credentials); remove fragment; remove dot segments; collapse duplicate slashes in path; empty path -> `/`; strip trailing slash of non-root (P10); percent-encoding: uppercase hex, decode only unreserved chars, encode non-ASCII (UTF-8) and spaces; query: drop ONLY the tracking denylist, keep blank values, sort pairs by (key, value), re-encode; wrap all parsing in try/except `ValueError` -> `MALFORMED`.

#### 4.M1.4 `services/normalize.py` (D11)
```python
DEFAULT_LEGAL_SUFFIXES: Final[frozenset[str]]   # inc incorporated ltd limited llc gmbh bv ab as oy sa pty corp corporation co plc ag nv srl sarl spa pte lp llp ... (generic only)
def normalize_text(text: str) -> str            # NFKC, casefold, strip accents, collapse whitespace
def normalize_name(name: str, *, extra_legal_suffixes: Iterable[str] = ()) -> str
    # NFKC, lowercase, strip accents, '&'->'and', drop punctuation, drop leading 'the', drop TRAILING legal-suffix tokens
    # repeatedly, collapse whitespace; never returns '' for non-empty input (fall back to unsuffixed form)
def name_similarity(a: str, b: str) -> float    # normalize both, difflib.SequenceMatcher ratio, 0..1
def make_domain_extractor(cache_dir: Path | None = None) -> tldextract.TLDExtract   # offline, suffix_list_urls=()
def registrable_domain(host_or_url: str, extractor: tldextract.TLDExtract | None = None) -> str | None
    # IP literal -> the IP; single-label host (localhost) -> that label; empty -> None
```
#### 4.M1.5 `services/quotes.py` (D5)
```python
@dataclass(frozen=True, slots=True)
class QuoteCheck:
    verified: bool; char_start: int | None; char_end: int | None     # offsets into the ORIGINAL page_text, end exclusive
    reason: Literal["OK","EMPTY_QUOTE","QUOTE_TOO_SHORT","NOT_FOUND","EMPTY_TEXT"]
def normalize_with_offsets(text: str) -> tuple[str, list[int], list[int]]   # normalized, start_offsets, end_offsets per normalized char
class QuoteVerifier:
    def __init__(self, min_quote_chars: int = MIN_QUOTE_CHARS) -> None
    def check(self, quote: str, page_text: str) -> QuoteCheck
    def verify_claim(self, claim: EvidenceClaim, *, page_text: str, url: str,
                     source_class: SourceClass, invocation_id: int | None = None) -> VerifiedEvidence
```
Normalization: NFKC per char with offset map, curly quotes->ASCII, en/em/minus dashes->`-`, NBSP/thin spaces->space, drop zero-width/soft-hyphen/BOM, `…`->`...`, collapse whitespace runs to one space, strip ends. Case-sensitive `str.find`. No fuzzy anywhere. `verify_claim` never raises; unverified -> `quote_verified=False`, offsets `None`.

#### 4.M1.6 `db/repos/pages.py`, `services/robots.py`, `services/fetch.py`

`db/repos/pages.py` (D18; only file that writes `crawl_pages`, `domain_stats`, `crawl_errors`):
```python
@dataclass(frozen=True) class PageRow: page_id: int; run_id: int; url: str; canonical_url: str; final_url: str | None; domain: str; entity_id: int | None; parent_entity_id: int | None; page_type: str | None; http_status: int | None; fetch_method: str | None; content_hash: str | None; text_path: str | None; html_path: str | None; robots_allowed: bool | None; pages_found: int; entities_found: int; crawl_status: CrawlStatus; error: str | None; crawled_at: datetime
@dataclass(frozen=True) class PageUpsert: (all writable PageRow fields except page_id/pages_found/entities_found)
@dataclass(frozen=True) class DomainStatsRow: domain: str; robots_txt: str | None; robots_fetched_at: datetime | None; crawl_delay_s: float | None; pages_fetched: int; error_count: int; last_request_at: datetime | None
class PageRepo:
    def __init__(self, conn: sqlite3.Connection, clock: Clock) -> None
    def get_page(self, run_id: int, canonical_url: str) -> PageRow | None
    def get_page_by_id(self, page_id: int) -> PageRow | None
    def upsert_page(self, row: PageUpsert) -> int                 # ON CONFLICT(run_id, canonical_url) DO UPDATE; returns page_id
    def set_counts(self, page_id: int, *, pages_found: int | None = None, entities_found: int | None = None) -> None
    def count_budgeted_pages(self, run_id: int, domain: str | None = None) -> int   # crawl_status NOT IN (BLOCKED_ROBOTS, SKIPPED_LIMIT, SKIPPED_FILTER)
    def get_domain_stats(self, domain: str) -> DomainStatsRow | None
    def save_robots(self, domain: str, robots_txt: str, fetched_at: datetime, crawl_delay_s: float | None) -> None
    def record_request(self, domain: str, at: datetime, *, error: bool) -> None   # pages_fetched/error_count/last_request_at upsert
    def add_crawl_error(self, run_id: int, stage: str, error_type: str, message: str, *, url: str | None = None, page_id: int | None = None, entity_id: int | None = None) -> int
```
`services/robots.py` (no dependency on fetch.py):
```python
class RobotsReason(StrEnum): ALLOWED; DISALLOWED; NO_ROBOTS; UNAVAILABLE_DENY; DISABLED
@dataclass(frozen=True) class RobotsDecision: url: str; domain: str; allowed: bool; reason: RobotsReason; crawl_delay: float | None; from_cache: bool; robots_status: int | None; checked_at: datetime
@dataclass(frozen=True) class RobotsFetchResult: status_code: int | None; text: str | None; error: str | None
RobotsFetcher = Callable[[str], RobotsFetchResult]               # arg: absolute robots.txt URL
class RobotsStore(Protocol):
    def load(self, domain: str) -> tuple[str, datetime, float | None] | None     # robots_txt, fetched_at, crawl_delay
    def save(self, domain: str, robots_txt: str, fetched_at: datetime, crawl_delay: float | None) -> None
    def record_failure(self, domain: str, at: datetime) -> None
class RobotsService:
    def __init__(self, *, fetcher: RobotsFetcher, store: RobotsStore, user_agent: str, clock: Clock,
                 respect_robots: bool = True, ttl_seconds: float = 86_400.0) -> None
    def is_allowed(self, url: str) -> RobotsDecision                  # never raises; no override parameter exists
    def crawl_delay(self, url: str) -> float | None
    @property decisions -> tuple[RobotsDecision, ...]                 # bounded audit ring (last 1000)
```
Behaviour: one robots fetch per domain per process (in-memory cache) and per TTL across processes (via store). Status 2xx -> parse with `urllib.robotparser.RobotFileParser().parse(lines)` (first 500 KiB), decision from `can_fetch(user_agent, url)`, `crawl_delay(user_agent)`. 4xx (incl. 401/403, 404) -> `NO_ROBOTS`, allow, cached as empty text. 5xx / timeout / network error -> `UNAVAILABLE_DENY`, deny ALL urls on that domain for the life of this service instance (ARCHITECTURE 10.1), NOT persisted as a success (`store.record_failure` only increments `error_count`), logged at WARNING. `/robots.txt` itself is always allowed. `respect_robots=False` -> `DISABLED` allowed, still recorded. `PageRepo` is adapted to `RobotsStore` by a small adapter class `PageRepoRobotsStore` defined in `services/robots.py`.

`services/fetch.py`:
```python
class HttpClient(Protocol):
    def get(self, url: str, *, headers: Mapping[str, str], timeout: float, max_bytes: int) -> RawResponse  # never raises; no redirects followed
@dataclass(frozen=True) class RawResponse: url: str; status_code: int | None; headers: dict[str, str]  # lowercase keys
                                           body: bytes; truncated: bool; error_code: str | None; error: str | None  # TIMEOUT|SSL_ERROR|CONNECTION_ERROR
class RequestsHttpClient:   # requests.Session, verify=not allow_insecure_ssl, allow_redirects=False, stream=True, bounded iter_content
    def __init__(self, *, verify_ssl: bool = True, trust_env: bool = True) -> None

@dataclass(frozen=True) class RenderedPage: final_url: str; status_code: int | None; html: str
class RendererError(Exception); class RendererUnavailable(RendererError)
class Renderer(Protocol):
    def render(self, url: str, *, timeout: float, user_agent: str) -> RenderedPage
    def close(self) -> None
class PlaywrightRenderer:   # lazy: imports playwright inside render(); one shared browser; new context per render; blocks image/font/media
def looks_like_js_shell(html: str, text: str, link_count: int) -> bool   # pure: text < 200 chars AND (SPA root marker | noscript "enable javascript" | link_count < 3)

def html_to_text(html: str) -> str      # bs4 html.parser; drop script/style/noscript/template; "\n" separators; collapse blank lines

@dataclass(frozen=True) class CacheMeta: canonical_url: str; final_url: str | None; status_code: int; content_type: str | None; headers: dict[str, str]; retrieved_at: str; origin_method: str; content_hash: str; html_hash: str | None
@dataclass(frozen=True) class StoredPage: text_path: str; html_path: str | None; meta: CacheMeta       # paths relative to cache_dir, posix separators
class PageCache:
    def __init__(self, cache_dir: Path) -> None                     # files under cache_dir/"pages"
    def key(self, canonical_url: str) -> str                        # sha256 hex of the canonical URL
    def put(self, canonical_url: str, *, html: str | None, text: str, final_url: str | None, status_code: int, content_type: str | None, headers: Mapping[str, str], retrieved_at: datetime, origin_method: str) -> StoredPage
    def get(self, canonical_url: str) -> StoredPage | None          # None on missing files OR hash mismatch (corruption = miss, WARNING logged)
    def read_text(self, text_path: str) -> str | None               # verifies sha256 against meta; None on mismatch
    def read_html(self, html_path: str) -> str | None
# write order: html, text, meta.json LAST (meta presence = commit marker); every file written to a temp name in the same dir then os.replace()

class Throttle:
    def __init__(self, clock: Clock, sleeper: Sleeper, default_delay: float) -> None
    def wait(self, domain: str, crawl_delay: float | None = None) -> float   # sleeps max(default, crawl_delay) since last request to that domain; returns seconds slept
    def mark(self, domain: str) -> None

@dataclass(frozen=True) class FetchConfig: timeout: float; max_retries: int; request_delay: float; user_agent: str; respect_robots: bool; allow_insecure_ssl: bool; use_playwright: Literal["auto","true","false"]; max_response_bytes: int; allow_private_hosts: bool; max_redirects: int = 5; backoff_base: float = 1.0; backoff_max: float = 30.0; retry_after_cap: float = 30.0
    @classmethod def from_settings(cls, settings: Settings) -> FetchConfig

class FetchResult(BaseModel):      # frozen pydantic model (NOT a contract; lives in services/fetch.py)
    requested_url: str; canonical_url: str | None; final_url: str | None; status_code: int | None
    content_type: str | None; retrieved_at: datetime; response_headers: dict[str, str]
    text: str | None; html: str | None                 # bodies only in memory, never in SQLite
    fetch_method: Literal["requests","playwright","cache"]; origin_method: str | None; cache_hit: bool; already_visited: bool
    crawl_status: CrawlStatus; error_code: str | None; error: str | None
    page_id: int | None; content_hash: str | None; robots_allowed: bool | None; redirect_chain: list[str]
    @property ok -> bool        # crawl_status in (OK, CACHED) and text is not None

class PageBudget(Protocol):          # defined in fetch.py so fetch.py does not import budget.py; BudgetGuard satisfies it structurally
    def check_page(self, domain: str) -> BudgetVerdict: ...

class FetchService:
    def __init__(self, *, config: FetchConfig, pages: PageRepo, cache: PageCache, robots: RobotsService, http: HttpClient,
                 throttle: Throttle, clock: Clock, sleeper: Sleeper, budget: PageBudget | None = None, renderer: Renderer | None = None) -> None
    def fetch(self, url: str, *, run_id: int, entity_id: int | None = None, parent_entity_id: int | None = None,
              page_type: PageType | None = None, base_url: str | None = None,
              render: Literal["auto","always","never"] | None = None, refetch_failed: bool = False) -> FetchResult   # never raises
    def load_text(self, page_id: int) -> str | None
    def load_html(self, page_id: int) -> str | None
def make_page_text_loader(pages: PageRepo, cache: PageCache) -> Callable[[int], str | None]   # used by Committer
def make_robots_fetcher(http: HttpClient, throttle: Throttle, *, user_agent: str, timeout: float) -> RobotsFetcher
```
`fetch()` order: canonicalize (reject -> `SKIPPED_FILTER` result, no `crawl_pages` row, `crawl_errors` row) -> private-host guard (P11) -> already-visited lookup in `crawl_pages` for `(run_id, canonical_url)` (return stored result; failed pages are NOT refetched unless `refetch_failed`) -> `BudgetGuard.check_page(domain)` (deny -> `SKIPPED_LIMIT` row) -> page-cache hit (`CACHED`, `fetch_method='cache'`, no network, no throttle) -> robots (`BLOCKED_ROBOTS`) -> throttle wait (`max(request_delay, crawl_delay)`) -> manual redirect loop (max 5 hops; every hop re-canonicalized, SSRF-checked, robots-checked, throttled; cross-domain hops recorded in `redirect_chain`) -> classify -> store in `PageCache` -> optional Playwright render -> write `crawl_pages` + `record_request` (+ `crawl_errors` on failure). Retries ONLY for timeout, connection error, 429 (honour `Retry-After`, capped at `retry_after_cap`), 502, 503, 504, GET only, `config.max_retries` times, deterministic exponential backoff via the injected `Sleeper`; never for SSL errors or other 4xx. Allowed content types: `text/html`, `application/xhtml+xml`, `text/plain`; a missing content type is sniffed (leading `<`) else filtered. Size: check `Content-Length`, then bounded streaming read (counts DECODED bytes, so compression bombs are capped). Decode: declared charset else UTF-8 with `errors="replace"`. Playwright: `render="auto"` (default from `use_playwright`) uses the renderer only when `looks_like_js_shell(...)` is true; `"always"` renders every OK HTML page; a renderer failure keeps the requests result and records `RENDERER_UNAVAILABLE`/`RENDERER_ERROR` once per process. Rendered HTML is passed through the same `html_to_text` and stored; `fetch_method='playwright'`.

Status mapping (D15, no schema change):

| Situation | `crawl_status` | `status_code` | `error_code` |
|---|---|---|---|
| 2xx allowed content | OK | the status | None |
| Served from page cache | CACHED | stored status | None |
| robots disallows | BLOCKED_ROBOTS | None | ROBOTS_DISALLOWED |
| robots unavailable (5xx/timeout) | BLOCKED_ROBOTS | None | ROBOTS_UNAVAILABLE |
| Final 4xx / 5xx | HTTP_ERROR | the status | HTTP_<status> |
| Redirect without Location / loop / > max hops | HTTP_ERROR | last 3xx | REDIRECT_ERROR / TOO_MANY_REDIRECTS |
| DNS / connect / other transport | HTTP_ERROR | None | CONNECTION_ERROR |
| Timeout | TIMEOUT | None | TIMEOUT |
| TLS failure | SSL_ERROR | None | SSL_ERROR |
| Content type not allowed | SKIPPED_FILTER | the status | UNSUPPORTED_CONTENT_TYPE |
| Un-canonicalizable URL | SKIPPED_FILTER (no page row) | None | URL_<UrlRejectReason> |
| Private/loopback host (P11) | SKIPPED_FILTER | None | PRIVATE_HOST_BLOCKED |
| Body over `max_response_bytes` | SKIPPED_LIMIT | the status | RESPONSE_TOO_LARGE |
| Budget denied | SKIPPED_LIMIT | None | BUDGET_<verdict reason> |

`crawl_pages.error` = `"<error_code>: <message>"` truncated to 500 chars. Only 2xx allowed-content pages enter the page cache.

#### 4.M1.7 `services/links.py` and `services/dom_candidates.py`
```python
@dataclass(frozen=True, slots=True)
class IndexedLink: link_id: int; url: str; raw_href: str; anchor_text: str; title_attr: str | None; rel: tuple[str, ...]
                   location: Literal["nav","footer","body"]; same_domain: bool; same_registrable_domain: bool
                   det_score: float; score_reasons: tuple[str, ...]; page_type_hint: PageType | None; position: int
class LinkIndex:
    base_url: str; links: tuple[IndexedLink, ...]
    def get(self, link_id: int) -> IndexedLink | None
    def by_url(self, canonical_url: str) -> IndexedLink | None
    def top(self, n: int, *, min_score: float = 0.0, same_domain_only: bool = False) -> list[IndexedLink]   # score desc, link_id asc
    def to_refs(self, link_ids: Iterable[int] | None = None, *, limit: int = 60) -> list[LinkRef]
@dataclass(frozen=True) class LinkScoringConfig: anchor_keywords: Mapping[str, float]; path_keywords: Mapping[str, float]; negative_keywords: frozenset[str]; nav_bonus: float; footer_bonus: float; external_factor: float
DEFAULT_SCORING: Final[LinkScoringConfig]      # generic words only: companies, portfolio, brands, subsidiaries, group(s), businesses, divisions, acquisitions, about, investors ...; negatives: careers, privacy, cookie, login, contact, blog, news, press, partners, customers, terms
def build_link_index(html: str, base_url: str, *, config: LinkScoringConfig = DEFAULT_SCORING,
                     extractor: tldextract.TLDExtract | None = None, max_links: int = 500) -> LinkIndex
def guess_page_type(url: str, anchor_text: str = "") -> PageType | None     # deterministic hint, includes the excluded types (PARTNERS, CUSTOMERS, CAREERS, BLOG, NEWS, CASE_STUDIES)
```
`link_id` = 1-based ordinal in document order after de-duplicating by canonical URL (first occurrence wins; keep first non-empty anchor). Skip fragment-only links to the base page, non-http(s) schemes, static-asset extensions (images, css, js, fonts, archives; keep `.pdf`). Location by ancestor: `nav`/`[role=navigation]`/`header` -> nav; `footer`/`[role=contentinfo]` -> footer; else body. Score in [0,1], deterministic, with reasons; negatives cap at 0.1; external links x `external_factor`. Band thresholds (HIGH/LOW) are NOT defined here (M4).

```python
def extract_dom_candidates(html: str, link_index: LinkIndex, *, max_candidates: int = 300) -> list[RawCandidate]
```
Strategies in fixed order, `dom_hint` values: `card` (repeated sibling blocks with a heading), `list_item` (short `li` with text, outside nav/header), `table_row` (first cell), `logo_alt` (`img alt/title` inside anchors), `footer` (generic statements like "a <X> company", "part of <X>", "<X> is a subsidiary/division/member of ..." via regexes), `jsonld` (`application/ld+json` Organization / subOrganization / brand / parentOrganization names via `json.loads` only). Items inside `nav`/`header` are excluded; a generic stop-list of boilerplate labels (home, about, contact, login, menu, search, read more, learn more, privacy, cookies, terms, careers ...) is dropped. Dedupe by `normalize_name`, keep first. `raw_candidate_id` = 1-based sequence; `link_id` resolved via `link_index.by_url(canonicalize(anchor href))` else `None`; `context` ≤ 400 chars; names 2-200 chars with at least one letter. Never executes or follows anything from the page.

#### 4.M1.8 `services/budget.py`, `services/cycle_guard.py`, `services/queue.py`
```python
# budget.py
@dataclass(frozen=True) class BudgetLimits: max_depth: int; max_pages_per_domain: int; max_total_pages: int; max_llm_calls_per_task: int
    @classmethod def from_settings(cls, settings: Settings) -> BudgetLimits
DegradationLevel = Literal["NORMAL","WARN_80","STOP_ENQUEUE_95","EXHAUSTED_100"]
class BudgetGuard:                           # satisfies agents.ports.BudgetPort structurally
    def __init__(self, conn: sqlite3.Connection, *, run_id: int, limits: BudgetLimits) -> None
    def check_depth(self, depth: int) -> BudgetVerdict                       # depth > max_depth denied, reason "DEPTH_LIMIT"
    def check_page(self, domain: str) -> BudgetVerdict                       # "DOMAIN_PAGE_LIMIT" / "TOTAL_PAGE_LIMIT" using PageRepo-equivalent count query
    def check_llm(self, *, task_id: int | None, estimated_tokens: int) -> BudgetVerdict   # "TOKEN_BUDGET_EXHAUSTED" (reads runs.llm_token_budget/llm_tokens_used; NULL budget = unlimited) / "TASK_LLM_CALL_LIMIT"
    def record_llm_usage(self, *, tokens_in: int, tokens_out: int, cost_usd: float) -> None   # UPDATE runs SET llm_tokens_used += ..., llm_cost_usd += ...
    def token_usage_fraction(self) -> float | None
    def degradation_level(self) -> DegradationLevel                          # thresholds 0.80 / 0.95 / 1.00 (27.3)
```
Task LLM-call count = `agent_invocations` rows with that `task_id`, `attempt = 1`, `cache_hit = 0`, `status <> 'SKIPPED'`, `failure_kind IS NULL OR failure_kind NOT IN ('BUDGET','CIRCUIT_OPEN')`. Page counts use the same `crawl_status NOT IN (...)` rule as `PageRepo.count_budgeted_pages`.
```python
# cycle_guard.py
@dataclass(frozen=True) class CycleCheck: creates_cycle: bool; reason: str | None; path: tuple[int, ...]
class CycleGuard:
    def __init__(self, conn: sqlite3.Connection) -> None
    def would_create_cycle(self, parent_id: int, child_id: int, *, ancestry: Sequence[int] = (), include_review: bool = False) -> CycleCheck
        # self-loop -> True; child in ancestry -> True (fast path); else recursive CTE up from parent over CONFIRMED edges (UNION, so it terminates even on existing cycles)
    def ancestors(self, entity_id: int, *, include_review: bool = False) -> list[int]
    def has_cycle_through(self, entity_ids: Iterable[int], *, include_review: bool = False) -> list[int]   # post-insert check used by Committer invariant 3
def find_cycle(edges: Iterable[tuple[int, int]]) -> list[int] | None       # pure helper
```
```python
# queue.py
QUEUE_DEPTH_FACTOR: Final = 1_000_000
def compute_priority(depth: int, discovery_seq: int) -> int                  # ValueError if depth < 0 or not 0 <= discovery_seq < 1_000_000
@dataclass(frozen=True) class ClaimedTask: task_id: int; run_id: int; entity_id: int; start_url: str | None; depth: int; priority: int; attempts: int; parent_entity_id: int | None
def insert_task(conn: sqlite3.Connection, *, run_id: int, entity_id: int, start_url: str | None, depth: int, discovery_seq: int, enqueued_at: datetime, not_before: datetime | None = None) -> int | None
    # no commit; INSERT ... ON CONFLICT(run_id, entity_id) DO NOTHING; returns None when already queued (used inside the Committer transaction)
class QueueService:
    def __init__(self, conn: sqlite3.Connection, clock: Clock, *, max_attempts: int = 3) -> None
    def enqueue(self, *, run_id: int, entity_id: int, start_url: str | None, depth: int, not_before: datetime | None = None) -> int | None   # looks up discovery_seq from entities; commits
    def claim_next(self, run_id: int) -> ClaimedTask | None     # BEGIN IMMEDIATE; lowest priority then task_id where status='PENDING' and (not_before IS NULL OR not_before <= now); -> RUNNING, attempts += 1, started_at
    def complete(self, task_id: int, outcome: CrawlState) -> None            # DONE + entities.crawl_state = outcome, one transaction
    def skip(self, task_id: int, reason: str, outcome: CrawlState = CrawlState.SKIPPED) -> None
    def fail(self, task_id: int, error: str, *, retry: bool = True) -> QueueStatus   # PENDING if attempts < max_attempts and retry, else FAILED
    def defer(self, task_id: int, not_before: datetime) -> None             # per-domain cool-down (25.6): back to PENDING with not_before
    def recover_stale(self, run_id: int) -> int                              # RUNNING -> PENDING on startup (25.7)
    def counts(self, run_id: int) -> dict[QueueStatus, int]
    def get(self, task_id: int) -> ResearchTask | None
    def parent_entity_id(self, entity_id: int) -> int | None                 # derived: is_primary=1, non-REJECTED, CONFIRMED first, then lowest relationship_id (D2)
```
#### 4.M1.9 `services/confidence.py` (D13)
```python
class ConfidenceFlag(StrEnum): CONTRADICTION; MULTI_PARENT; AMBIGUOUS_NAME; POSSIBLE_PRODUCT; CYCLE; NO_WEBSITE   # mapped 1:1 to ReviewReason names
class EvidenceInput(Strict): source_class: SourceClass; supports: Supports; quote_verified: bool; page_id: int; page_type: PageType | None = None; ownership_wording: bool = False
@dataclass(frozen=True) class ConfidenceResult:
    status: VerificationStatus; review_reason: ReviewReason | None
    evidence_score: float; relationship_confidence: float; entity_confidence: float
    breakdown: dict[str, float]          # evidence_i scores, combined, llm_factor, caps applied (audit)
class ConfidenceCalculator:
    def __init__(self, *, confirm_threshold: float = 0.75, review_threshold: float = 0.40, unverified_cap: float = 0.40, third_party_cap: float = 0.40, max_score: float = 0.99) -> None
    @classmethod def from_settings(cls, settings: Settings) -> ConfidenceCalculator
    def evidence_score(self, ev: EvidenceInput) -> float
    def score(self, evidence: Sequence[EvidenceInput], *, agent_confidence: float, classification_confidence: float, flags: Collection[ConfidenceFlag] = ()) -> ConfidenceResult
```
Base scores: FIRST_PARTY_PARENT + ownership wording 0.85; FIRST_PARTY_PARENT without wording 0.75; FIRST_PARTY_CHILD + OWNERSHIP 0.85; OFFICIAL_DOCUMENT 0.85; FIRST_PARTY_OTHER 0.50 (flag F4); THIRD_PARTY 0.40 cap; `quote_verified=False` -> capped at 0.40. Combine per page (max within a `page_id`), then across pages `1 - Π(1 - s_i)` capped at 0.99. If no verified first-party/official evidence exists the total is capped at 0.40 (so stacked third-party hits can never reach 0.75). `relationship_confidence = evidence_score * (0.5 + 0.5 * agent_confidence)`; `entity_confidence = min(classification_confidence, relationship_confidence)`. Status: `REJECTED` iff `relationship_confidence < review_threshold` (D13 simple rule, flag F3); else `CONFIRMED` iff `relationship_confidence >= confirm_threshold` AND at least one verified `OWNERSHIP` evidence from `FIRST_PARTY_PARENT|FIRST_PARTY_CHILD|OFFICIAL_DOCUMENT` AND no flags; otherwise `REVIEW_REQUIRED`. Any flag forces `REVIEW_REQUIRED` with `review_reason` chosen by fixed priority CYCLE > CONTRADICTION > MULTI_PARENT > AMBIGUOUS_NAME > POSSIBLE_PRODUCT > NO_WEBSITE; no evidence at all -> REJECTED. Unverified-only evidence sets `review_reason=NO_QUOTE`, third-party-only `THIRD_PARTY_ONLY`, else below-confirm `LOW_CONFIDENCE`. Agent confidence can only scale the score down (R6).

#### 4.M1.10 `services/matcher.py` (Part I 6.2 ladder; deterministic)
```python
@dataclass(frozen=True) class MatchCandidate: name: str; normalized_name: str; parent_entity_id: int | None; registrable_domain: str | None; country: str | None = None; industry: str | None = None; aliases: tuple[str, ...] = ()
class MatchResult(Strict): outcome: Literal["NEW","MATCH","AMBIGUOUS"]; step: int | None; entity_id: int | None; ambiguous_entity_ids: list[int]; signals: list[Signal]; reason: str
class Matcher:
    def __init__(self, conn: sqlite3.Connection, *, run_id: int, fuzzy_threshold: float = 0.92) -> None
    @classmethod def from_settings(cls, conn: sqlite3.Connection, run_id: int, settings: Settings) -> Matcher
    def find_match(self, cand: MatchCandidate) -> MatchResult
def compute_signals(a: MatchCandidate, b: MatchCandidate, *, same_parent: bool) -> list[Signal]
```
Ladder (first decisive wins, ties broken by lowest `entity_id`, merged entities with `merged_into_id` set are ignored): 1 dedicated official domain (the domain's websites belong to exactly ONE entity, that entity is not the candidate's parent and the domain differs from the parent's registrable domain) -> MATCH; 2 exact `normalized_name` + same parent (a non-REJECTED relationship parent->entity exists) -> MATCH; 3 alias hit with same parent OR same (case-insensitive) country -> MATCH; 4 exact normalized name, different parent -> AMBIGUOUS; 5 `name_similarity >= fuzzy_threshold` -> AMBIGUOUS (signal `NAME_FUZZY` carries the ratio); 6 -> NEW. A shared domain alone never merges. Two different entities matching decisively in the same step -> AMBIGUOUS with both ids. Use `SequenceMatcher.real_quick_ratio()/quick_ratio()` as cheap pre-filters before `ratio()`.

#### 4.M1.11 `services/committer.py` (the ONLY writer of accepted business outcomes)
```python
PageTextLoader = Callable[[int], str | None]
class CommitError(RuntimeError)
@dataclass(frozen=True) class InvariantViolation: code: str; message: str; entity_id: int | None = None   # codes: INV1..INV6, INV_CONF, INV_EVIDENCE, INV_PLAN
class InvariantError(CommitError): violations: list[InvariantViolation]
@dataclass(frozen=True) class CommitResult: entity_ids: dict[str, int]; website_ids: dict[str, int]; relationship_ids: dict[str, int]; evidence_ids: list[int]; candidate_ids: list[int]; review_ids: list[int]; task_ids: list[int]; rejection_ids: list[int]
class Committer:
    def __init__(self, conn: sqlite3.Connection, *, run_id: int, page_text: PageTextLoader, verifier: QuoteVerifier, cycle_guard: CycleGuard,
                 clock: Clock, require_website_for_confirmed: bool, confirm_threshold: float) -> None
    @classmethod def from_settings(cls, conn, *, run_id, page_text, settings: Settings, clock) -> Committer
    def commit(self, plan: CommitPlan) -> CommitResult                   # ONE transaction; raises InvariantError/CommitError after rollback
    def check_invariants(self, entity_ids: Collection[int] | None = None) -> list[InvariantViolation]   # None = whole run; reused later by run_checks (M3+)
```
`commit` flow: validate `plan.run_id == self.run_id` -> `BEGIN IMMEDIATE` -> insert in FK order (entities [assign `discovery_seq = max+1`, `discovered_at = clock`], updates, websites [+`primary_website_id`], aliases, relationships, evidence, candidates [upsert on the unique key], review_items, enqueue via `queue.insert_task`, gate_rejections) -> `check_invariants(touched)` -> `COMMIT`; any exception (including `sqlite3.IntegrityError`, e.g. a second CONFIRMED primary parent) -> `ROLLBACK` and re-raise as `CommitError`/`InvariantError`. Invariants (ARCHITECTURE 26.6): INV1 every CONFIRMED relationship has >=1 evidence row with `quote_verified=1`, `supports='OWNERSHIP'`, `source_class` in (FIRST_PARTY_PARENT, FIRST_PARTY_CHILD, OFFICIAL_DOCUMENT); INV2 every evidence row with `quote_verified=1` has a `page_id` whose stored text (via `page_text`) still contains the quote per `QuoteVerifier` (forged `VerifiedEvidence` fails); INV3 no cycle through touched CONFIRMED edges (CycleGuard, post-insert); INV4 every CONFIRMED non-root entity has exactly one CONFIRMED primary relationship as child; INV5 no CONFIRMED relationship touches an entity with `merged_into_id`, and no CONFIRMED entity has `entity_type='Unknown'`; INV6 if `require_website_for_confirmed`, every CONFIRMED non-root entity has `primary_website_id` pointing at one of its own websites; INV_CONF (P9) CONFIRMED relationship/entity confidence >= `confirm_threshold`. Unknown `EntityRef`, duplicate `local_ref`, root flag misuse (P8) -> `INV_PLAN`.

### 4.M2 Agent infrastructure

#### 4.M2.1 `contracts/agents.py`
```python
class AgentName(StrEnum)      # 7 values exactly as the schema CHECK
class CallStatus(StrEnum)     # OK PARTIAL FAILED SKIPPED
class FailureKind(StrEnum)    # TRANSPORT SCHEMA GROUNDING BUDGET CIRCUIT_OPEN TOOL
class Meta(Strict); class CallLimits(Strict); class Usage(Strict); class AgentError(Strict)      # ARCHITECTURE 22.1 exactly
In = TypeVar("In", bound=BaseModel); Out = TypeVar("Out", bound=BaseModel)
class AgentRequest(Strict, Generic[In]): meta: Meta; limits: CallLimits; payload: In
class AgentResponse(Strict, Generic[Out]): meta: Meta; status: CallStatus; payload: Out | None; warnings: list[str] = []; usage: Usage; error: AgentError | None = None
    # validator: OK/PARTIAL need payload; FAILED/SKIPPED need payload None and error
# audit records (consumed by db/repos/audit.py, produced by agents/runner.py)
class InvocationStart(Strict): run_id: int; task_id: int | None; agent: AgentName; correlation_id: str; schema_version: str; prompt_version: str; attempt: int; input_hash: str; request_json: str; started_at: datetime
class InvocationFinish(Strict): status: CallStatus; failure_kind: FailureKind | None; response_json: str | None; cache_hit: bool; tokens_in: int | None; tokens_out: int | None; cost_usd: float | None; latency_ms: int | None; finished_at: datetime
class ToolCallRecord(Strict): invocation_id: int; tool: str; args_json: str; allowed: bool; result_summary: str | None; duration_ms: int | None; created_at: datetime
class CachedResponse(Strict): invocation_id: int; response_json: str; tokens_in: int | None; tokens_out: int | None
```
#### 4.M2.2 `agents/ports.py`, `agents/base.py`
```python
# ports.py  (Protocols and small dataclasses only; re-exports Clock, Sleeper from corpdisc.clock)
class ProviderError(Exception): retryable: bool; retry_after_s: float | None
class ProviderTransportError(ProviderError); class ProviderAuthError(ProviderError); class ProviderNotImplementedError(ProviderError)
@dataclass(frozen=True) class Message: role: Literal["system","user","assistant","tool"]; content: str
@dataclass(frozen=True) class ToolDescriptor: name: str; description: str; args_schema: dict[str, Any]
@dataclass(frozen=True) class ToolCallRequest: call_id: str; name: str; arguments: dict[str, Any]
@dataclass(frozen=True) class ProviderUsage: tokens_in: int; tokens_out: int; cost_usd: float = 0.0
@dataclass(frozen=True) class ProviderRequest: model: str; system: str; messages: tuple[Message, ...]; response_schema: dict[str, Any]; schema_name: str
                                              temperature: float; max_output_tokens: int; tools: tuple[ToolDescriptor, ...] = (); metadata: Mapping[str, str] = field(default_factory=dict)
@dataclass(frozen=True) class ProviderResponse: content: str | None; tool_calls: tuple[ToolCallRequest, ...]; usage: ProviderUsage; model: str
class LLMProvider(Protocol): name: str;  def complete(self, request: ProviderRequest) -> ProviderResponse   # raises ProviderError subclasses only
class AuditSink(Protocol):
    def start_invocation(self, rec: InvocationStart) -> int
    def finish_invocation(self, invocation_id: int, rec: InvocationFinish) -> None
    def record_tool_call(self, rec: ToolCallRecord) -> int
class ResponseCache(Protocol):
    def lookup(self, *, agent: AgentName, schema_version: str, prompt_version: str, input_hash: str) -> CachedResponse | None
class BudgetPort(Protocol):
    def check_llm(self, *, task_id: int | None, estimated_tokens: int) -> BudgetVerdict
    def record_llm_usage(self, *, tokens_in: int, tokens_out: int, cost_usd: float) -> None

# base.py
def make_correlation_id(run_id: int, task_id: int | None, stage: str) -> str      # "run:task:stage", task None -> "-"
def canonical_json(obj: object) -> str                                              # sort_keys, separators (",", ":"), ensure_ascii=False
def estimate_tokens(text: str) -> int                                               # ceil(len/4)
class AgentContractError(RuntimeError)                                              # programmer errors only (never LLM output)
class BaseAgent(ABC, Generic[In, Out]):
    name: ClassVar[AgentName]; schema_version: ClassVar[str]; prompt_version: ClassVar[str]
    input_model: ClassVar[type[In]]; output_model: ClassVar[type[Out]]
    allowed_tools: ClassVar[Mapping[str, int]] = {}      # tool name -> per-invocation cap (R(n), 23.2)
    max_output_tokens: ClassVar[int]; max_tool_calls: ClassVar[int] = 0
    @abstractmethod def system_prompt(self) -> str
    def user_message(self, payload: In) -> str                      # default: canonical_json of payload.model_dump(mode="json")
    @abstractmethod def on_failure(self, request: AgentRequest[In], error: AgentError) -> Out | None   # safe fallback applied by the CALLER (graph node), never by the runner
```
#### 4.M2.3 `db/repos/audit.py` (D1, D7) — JSON column shapes
```python
class SqliteAuditSink:            # satisfies AuditSink structurally; db/ never imports agents/
    def __init__(self, conn: sqlite3.Connection) -> None
    def start_invocation(self, rec: InvocationStart) -> int           # INSERT status='FAILED', failure_kind NULL, finished_at NULL (P6)
    def finish_invocation(self, invocation_id: int, rec: InvocationFinish) -> None
    def record_tool_call(self, rec: ToolCallRecord) -> int
class SqliteResponseCache:        # satisfies ResponseCache; reads agent_invocations only
    def __init__(self, conn: sqlite3.Connection) -> None
    def lookup(self, *, agent: AgentName, schema_version: str, prompt_version: str, input_hash: str) -> CachedResponse | None
        # WHERE agent/schema_version/prompt_version/input_hash match AND status='OK' AND cache_hit=0 ORDER BY invocation_id DESC LIMIT 1 (uses idx_invocations_cache)
```
`request_json` = canonical JSON `{"meta": Meta, "limits": CallLimits, "input": <payload>, "llm": {"provider","model","temperature","max_output_tokens","cache_key"}, "attempt_kind": "INITIAL|REPAIR|TOOL_CONTINUATION", "messages_sha256": "..."}` (no secrets, no API keys). `response_json`: OK -> `{"payload": <validated output>, "warnings": [...], "model": "...", "provider": "...", "transport": {"requests": n, "errors": ["TIMEOUT", ...]}}`; PARTIAL (tool-request attempt) -> `{"tool_calls": [...], "transport": {...}}`; FAILED -> `{"error": {"kind","message"}, "raw_excerpt": "<first 2000 chars of invalid provider output or null>", "transport": {...}}`. `agent_tool_calls.args_json` = canonical JSON; `result_summary` = `"OK: <first 480 chars>"`, `"DENIED:NOT_ALLOWLISTED"`, `"DENIED:CAP_EXCEEDED"`, `"ERROR:INVALID_ARGS: ..."`, `"ERROR:TOOL_ERROR:<ExcType>"`.

#### 4.M2.4 `agents/providers/` (D16) and record/replay
```python
# providers/fake.py
class FakeLLMProvider:            # name = "fake"; deterministic scripted provider
    def __init__(self, script: Sequence[FakeStep] = (), *, model: str = "fake-model", usage: ProviderUsage = ProviderUsage(100, 50, 0.0)) -> None
    requests: list[ProviderRequest]; @property call_count -> int
    def complete(self, request: ProviderRequest) -> ProviderResponse
FakeStep = str | dict | BaseModel | ToolCalls | ProviderError | Callable[[ProviderRequest], str | ProviderResponse]
@dataclass(frozen=True) class ToolCalls: calls: tuple[ToolCallRequest, ...]
# script exhausted -> raises ProviderTransportError(retryable=False, "fake script exhausted")
# providers/cassette.py
def request_key(req: ProviderRequest) -> str                      # sha256 of canonical JSON of (model, temperature, max_output_tokens, schema_name, system, messages, tools)
class RecordingProvider:          # name = f"record({inner.name})"; wraps an LLMProvider, appends JSONL entries {key, response} on SUCCESS only
    def __init__(self, inner: LLMProvider, cassette_path: Path) -> None
class ReplayProvider:             # name = "replay"; serves entries by key (FIFO per key, last repeats); a miss raises ProviderTransportError(retryable=False, "cassette miss") -> fail closed, never live
    def __init__(self, cassette_path: Path) -> None
# providers/__init__.py
def build_provider(name: str, *, cassette_path: Path | None = None, inner: LLMProvider | None = None) -> LLMProvider
    # "fake" -> FakeLLMProvider(); "replay" -> ReplayProvider(cassette_path); "record" -> RecordingProvider(inner, cassette_path) (both required, else ValueError)
    # "openai"|"anthropic"|"gemini"|"google" -> ProviderNotImplementedError("provider '<name>' is not implemented in M2"); other/blank -> ValueError
```
#### 4.M2.5 `agents/gateway.py`
```python
class BreakerState(StrEnum): CLOSED; OPEN; HALF_OPEN
class CircuitBreaker:
    def __init__(self, threshold: int = 5) -> None
    state: BreakerState (property); consecutive_failures: int
    def allow_request(self) -> bool            # CLOSED/HALF_OPEN -> True (HALF_OPEN permits exactly one probe), OPEN -> False
    def record_success(self) -> None           # -> CLOSED, counter 0
    def record_failure(self, *, auth: bool = False) -> None   # auth -> OPEN immediately; else consecutive TRANSPORT failures >= threshold -> OPEN; failure in HALF_OPEN -> OPEN
    def half_open(self) -> None                # called by the resume path (M4): OPEN -> HALF_OPEN
@dataclass(frozen=True) class GatewayPolicy: transport_retries: int = 3; backoff_base: float = 1.0; backoff_max: float = 30.0; jitter_ratio: float = 0.25; temperature: float = 0.0; absolute_max_output_tokens: int = 8192
    @classmethod def from_settings(cls, settings: Settings) -> GatewayPolicy
@dataclass(frozen=True) class GatewayFailure: kind: FailureKind; message: str; auth: bool = False      # TRANSPORT | CIRCUIT_OPEN
@dataclass(frozen=True) class GatewayResult: response: ProviderResponse | None; failure: GatewayFailure | None; requests_used: int; latency_ms: int; transport_errors: tuple[str, ...]
class LLMGateway:
    def __init__(self, provider: LLMProvider, *, model: str, policy: GatewayPolicy, breaker: CircuitBreaker, budget: BudgetPort | None,
                 clock: Clock, sleeper: Sleeper, rng: random.Random | None = None) -> None
    provider_name: str (property); model: str (property)
    def cache_key(self, *, agent: AgentName, schema_version: str, prompt_version: str, max_output_tokens: int, input_payload: Mapping[str, Any], model: str | None = None) -> str   # D7 formula
    def complete(self, request: ProviderRequest, *, max_requests: int) -> GatewayResult   # never raises
```
`complete`: breaker open -> `CIRCUIT_OPEN` with 0 requests; else loop: clamp `max_output_tokens` to the policy ceiling; call provider; `ProviderAuthError` -> `breaker.record_failure(auth=True)`, failure (no retry); `ProviderTransportError` retryable -> sleep `min(backoff_max, backoff_base * 2**n)` ± jitter (or `retry_after_s`) via the injected sleeper and retry up to `transport_retries` AND while `requests_used < max_requests`; non-retryable -> failure; exhausted -> `breaker.record_failure()` once and `FailureKind.TRANSPORT`. Success -> `breaker.record_success()`, `budget.record_llm_usage(...)` (tokens are paid even if the output later fails validation), return response. The gateway never parses JSON and never touches the DB directly.

#### 4.M2.6 `agents/broker.py` (D4, D17)
```python
@dataclass(frozen=True) class ToolSpec: name: str; description: str; args_model: type[BaseModel]; handler: Callable[[BaseModel], Mapping[str, Any]]; read_only: bool = True; max_result_chars: int = 4000
@dataclass(frozen=True) class ToolResult: ok: bool; data: Mapping[str, Any] | None; error: str | None; truncated: bool = False
class ToolSession:                 # per-invocation counters; created by the runner
    def __init__(self, agent: AgentName, allowed: Mapping[str, int], max_total: int) -> None
    counts: dict[str, int]; total: int
class ToolBroker:
    def __init__(self, specs: Sequence[ToolSpec], *, audit: AuditSink, clock: Clock) -> None     # ValueError if a spec is not read_only or a name is duplicated
    def descriptors_for(self, allowed: Mapping[str, int]) -> tuple[ToolDescriptor, ...]           # JSON schema from args_model, only allowed names
    def new_session(self, agent: BaseAgent) -> ToolSession
    def call(self, session: ToolSession, invocation_id: int, request: ToolCallRequest) -> ToolResult   # never raises
```
`call` order: name not in `session.allowed` or unknown -> denied `NOT_ALLOWLISTED`; per-tool cap or `max_total` reached -> denied `CAP_EXCEEDED`; args validated with `args_model.model_validate` (`extra="forbid"` required) -> `INVALID_ARGS`; handler executed in try/except -> `TOOL_ERROR:<ExcType>` (no stack leaked); result must be JSON-serializable and is truncated to `max_result_chars`. Every outcome (allowed or denied) writes one `agent_tool_calls` row via `AuditSink.record_tool_call` and returns a `ToolResult` (never an exception) so the agent sees a tool error message. The broker never exposes a handle; handlers are injected closures.

#### 4.M2.7 `agents/runner.py`
```python
@dataclass(frozen=True) class RunnerLimits: max_provider_requests: int = 6; max_repair_attempts: int = 1; max_tool_result_chars: int = 4000
    @classmethod def from_settings(cls, settings: Settings) -> RunnerLimits
class AgentRunner:
    def __init__(self, *, gateway: LLMGateway, audit: AuditSink, cache: ResponseCache | None, budget: BudgetPort, broker: ToolBroker | None,
                 limits: RunnerLimits, clock: Clock, model_overrides: Mapping[AgentName, str] | None = None) -> None
    def invoke(self, agent: BaseAgent[In, Out], request: AgentRequest[In]) -> AgentResponse[Out]
```
`invoke` algorithm (ARCHITECTURE 24.2 order, D6/D7):
0. Contract checks (raise `AgentContractError` only for programmer errors: `meta.agent != agent.name`, version mismatch, payload not `agent.input_model`, tool allowlist names unknown to the broker).
1. `effective_model`; build `input_payload`, `cache_key = gateway.cache_key(...)`; `estimated = estimate_tokens(system + user) + max_output_tokens`.
2. Budget: `budget.check_llm(task_id, estimated)`; denied -> write one audit row (`attempt=1`, `SKIPPED`, `BUDGET`) -> return `SKIPPED` response (`error.kind=BUDGET`).
3. Circuit: `gateway` breaker open -> audit row `FAILED/CIRCUIT_OPEN` -> return FAILED.
4. Cache: `cache.lookup(...)`; hit -> re-validate `response_json["payload"]` with `output_model`; valid -> audit row `OK`, `cache_hit=1`, tokens 0 -> return `OK` with `usage.cache_hit=True`; invalid or unreadable -> treat as miss (log WARNING), never serve.
5. Loop (`requests_used` starts 0; stop when `>= limits.max_provider_requests` -> FAILED/BUDGET "MAX_PROVIDER_REQUESTS_PER_INVOCATION reached"): `attempt += 1`; `start_invocation`; `gateway.complete(req, max_requests=remaining)`; add `requests_used`.
   - gateway failure -> `finish_invocation(FAILED, kind)` -> return FAILED (TRANSPORT / CIRCUIT_OPEN).
   - provider returned tool calls -> if the agent has no tools or `max_tool_calls == 0` every call is denied by the broker; else each call goes through `broker.call(session, invocation_id, call)`; finish this row as `PARTIAL`; append assistant + tool messages (tool results are wrapped as data, never as instructions) and continue the loop.
   - provider returned content -> `output_model.model_validate_json(content)`; valid -> `finish OK` (response_json per 4.M2.3) -> return `OK`; invalid -> `finish FAILED/SCHEMA` (store `raw_excerpt`); if `repairs_used < max_repair_attempts` append the assistant text and ONE user repair message containing only pydantic error locations/messages (no input echo, ≤ 500 chars) and continue; else return FAILED/SCHEMA.
6. Return `AgentResponse` with summed `Usage` (tokens, cost, latency, `tool_calls`), `meta.attempt` = last attempt ordinal. Any unexpected exception inside the loop is caught, logged with traceback, and converted to FAILED with message `INTERNAL: <ExcType>`; it never feeds the breaker and never propagates. Log one line per invocation with the 28.5 fields (`run_id task_id correlation_id agent invocation_id attempt status failure_kind cache_hit tokens_in tokens_out latency_ms`), INFO; truncated prompts at DEBUG only.

---------------------------------------------------------------------------------------------------

## 5. Ordered implementation checklist

Rules for every item: leave the codebase buildable; run the listed verification; run the FULL suite (`.venv\Scripts\python -m pytest -q`) before moving on when the item touches shared files (conftest, config, pyproject, contracts). Use only fictional company names in fixtures (for example "Northwind Holdings", "Aurora Analytics Ltd."); never Vela/Jonas/Volaris/Harris/Perseus/Constellation outside `config.py` defaults.

### PHASE M1 (items 1-12; the m1-loop builds these)

- [ ] 1. Environment, settings, test infrastructure (D12).
      Pin and install dependencies in `pyproject.toml` (section 3), add the `playwright` marker, add the new `Settings` fields with validators (section 3), update `.env.example`, create `clock.py` (4.M1.0), create package dirs `db/repos/__init__.py`, create `tests/{unit,contracts,services,agents,integration,helpers,fixtures}/` with `__init__.py` where importable, write `tests/helpers/clock.py` (`FakeClock`), `tests/helpers/db.py` (`make_db(tmp_path) -> sqlite3.Connection` using `init_db` + `connect_writer`; `seed_run(conn, ...) -> int`; `seed_entity(conn, ...) -> int`), `tests/helpers/http_server.py` (threaded `http.server` on `127.0.0.1:0` with a routes dict supporting status, headers, body bytes, redirects, delay, per-path hit counters, and a `robots.txt` route; pytest fixture `local_site` in `tests/conftest.py`). Change `tests/conftest.py::_block_network` to allow only `127.0.0.1`, `::1`, `localhost` (wrap `socket.socket.connect`, `connect_ex`, `socket.create_connection`, `socket.getaddrinfo`; everything else still raises `RuntimeError`). Add `tests/unit/test_network_guard.py` (external host raises; local `http.server` request succeeds) and `tests/unit/test_config_m1_m2.py` (defaults, bounds, blank `LLM_TOKEN_BUDGET` -> None, `.env.example` lists every new field).
      Files: `pyproject.toml`, `.env.example`, `src/corpdisc/config.py`, `src/corpdisc/clock.py`, `src/corpdisc/db/repos/__init__.py`, `tests/conftest.py`, `tests/helpers/*`, `tests/unit/test_network_guard.py`, `tests/unit/test_config_m1_m2.py`.
      Verify: `.venv\Scripts\python -m pip install -e ".[dev]"` succeeds; `.venv\Scripts\python -m pytest -q` — all 110 baseline tests plus the new ones pass; `.venv\Scripts\python main.py --help` still exits 0.

- [ ] 2. Contracts for M1 (D9, P1, P3). Depends on 1.
      Create `contracts/__init__.py`, `contracts/common.py` (4.M1.1), `contracts/postgate.py` (4.M1.2). Tests in `tests/contracts/`: JSON round-trip of every model, `extra="forbid"`, frozen, `EvidenceClaim` min length 15, `VerifiedEvidence` offset validator, `EntityRef` exactly-one validator, `CommitPlan` local-ref validators, enum re-exports equal `models.domain` members, `CommitPlan` sub-model fields match the schema columns of the tables they mirror (introspect `PRAGMA table_info` for entities/websites/relationships/candidates/review_items/gate_rejections).
      Files: `src/corpdisc/contracts/{__init__,common,postgate}.py`, `tests/contracts/test_common.py`, `tests/contracts/test_postgate.py`.
      Verify: `.venv\Scripts\python -m pytest tests/contracts -q` passes; `.venv\Scripts\python -m pytest -q` still green (confirms `test_no_agent_contracts_in_m0` unaffected).

- [ ] 3. URL service. Depends on 1.
      Implement `services/urls.py` (4.M1.3). Tests `tests/unit/test_urls.py` (parametrized): relative URL with base, absolute URL, scheme-relative, fragment removal, duplicate slash collapse, trailing slash, default port 80/443 and non-default kept, uppercase host/scheme, IDN host, percent-encoding normalization (`%7e` -> `~`, lowercase hex uppercased, reserved kept encoded, spaces/non-ASCII encoded), tracking params removed but `page=2` and unknown params kept and sorted, blank query values kept, userinfo stripped, `javascript:`/`mailto:`/`tel:`/`data:`/`ftp:`/`file:` rejected with `UNSUPPORTED_SCHEME`, malformed (`http://`, `http://[::1`, `http://host:99999`, empty, control chars, over-long) return structured rejections and never raise, relative without base, idempotence (`canonicalize(canonicalize(x).url).url == canonicalize(x).url`) over a corpus.
      Files: `src/corpdisc/services/urls.py`, `tests/unit/test_urls.py`.
      Verify: `.venv\Scripts\python -m pytest tests/unit/test_urls.py -q` passes.

- [ ] 4. Normalization and domains (D11). Depends on 1.
      Implement `services/normalize.py` (4.M1.4). Tests `tests/unit/test_normalize.py`: suffix stripping (`Aurora Analytics Ltd.` == `aurora analytics`), `&` -> `and`, accents, punctuation, leading "the", repeated suffixes, non-empty guarantee, `name_similarity` symmetry and bounds, `registrable_domain` for `www.example.com`, `a.b.example.co.uk` -> `example.co.uk`, `foo.com.au`, IP literal, `localhost`, empty -> None; assert the extractor is built with `suffix_list_urls=()` and makes no network call (the autouse network guard would raise).
      Files: `src/corpdisc/services/normalize.py`, `tests/unit/test_normalize.py`.
      Verify: `.venv\Scripts\python -m pytest tests/unit/test_normalize.py -q` passes.

- [ ] 5. Quote verifier (D5). Depends on 2.
      Implement `services/quotes.py` (4.M1.5). Tests `tests/unit/test_quotes.py`: exact hit with correct offsets into the original text (slice equals the matched original substring), curly quotes/dashes/NBSP/zero-width/soft-hyphen/ellipsis normalization, whitespace and newline collapse, case mismatch -> NOT verified (case-sensitive), one-word change -> NOT verified (no fuzzy), empty/short quote, empty text, NFKC expansion offset mapping (`ﬁ` ligature), `verify_claim` returns `VerifiedEvidence` with `quote_verified` False and offsets None when missing, property-style test over random slices of a text (any slice ≥15 chars verifies and maps back).
      Files: `src/corpdisc/services/quotes.py`, `tests/unit/test_quotes.py`.
      Verify: `.venv\Scripts\python -m pytest tests/unit/test_quotes.py -q` passes.

- [ ] 6. Page repository and robots (D15 persistence, D18). Depends on 1, 3.
      Implement `db/repos/pages.py` (4.M1.6) and `services/robots.py` with `PageRepoRobotsStore`. Tests `tests/services/test_pages_repo.py` (upsert idempotent on `(run_id, canonical_url)`, counts exclude BLOCKED/SKIPPED rows, domain stats upsert, FK violations surface) and `tests/services/test_robots.py` using a scripted `RobotsFetcher` and `FakeClock`: allowed URL, disallowed URL, missing robots (404 -> allow), 403 -> allow, 503 -> deny-all for the life of the instance and not persisted as success, timeout -> deny, crawl-delay parsed (`Crawl-delay: 3` -> 3.0), cached decision (fetcher called exactly once for N checks on a domain; second `RobotsService` on the same DB reuses the stored text within TTL and refetches after TTL), `/robots.txt` always allowed, `respect_robots=False` -> `DISABLED` allowed, `decisions` audit ring records each decision, no API exists to override a decision.
      Files: `src/corpdisc/db/repos/pages.py`, `src/corpdisc/services/robots.py`, `tests/services/test_pages_repo.py`, `tests/services/test_robots.py`.
      Verify: `.venv\Scripts\python -m pytest tests/services/test_pages_repo.py tests/services/test_robots.py -q` passes.

- [ ] 7. Fetch building blocks: page cache, throttle, HTTP client, text extraction. Depends on 1, 3.
      In `services/fetch.py` implement `html_to_text`, `PageCache`, `Throttle`, `RawResponse`, `HttpClient`, `RequestsHttpClient`, `looks_like_js_shell`, `FetchConfig`, `make_robots_fetcher`, and the `Renderer` protocol + `PlaywrightRenderer` (lazy import; no FetchService yet). Tests: `tests/services/test_page_cache.py` (put/get round trip, relative posix paths, atomic write leaves no temp files, corrupted text file -> `get` returns None and logs, meta written last so a half-written entry is a miss, content hash verified), `tests/services/test_throttle.py` (FakeClock: first request no wait, second waits `max(default, crawl_delay)`, domains independent), `tests/services/test_html_to_text.py` (scripts/styles/noscript removed, whitespace collapsed, non-ASCII preserved), `tests/services/test_requests_client.py` against the `local_site` fixture with `trust_env=False` (200 body, 404, redirect not followed, oversize body truncated at `max_bytes` with `truncated=True`, timeout -> `TIMEOUT`, connection refused -> `CONNECTION_ERROR`, gzip body decoded and bounded), `tests/unit/test_js_shell.py`, `tests/services/test_playwright_renderer.py` marked `@pytest.mark.playwright` (skips cleanly when playwright or chromium is absent; a non-marked test asserts that importing `corpdisc.services.fetch` and constructing `PlaywrightRenderer()` does NOT import playwright).
      Files: `src/corpdisc/services/fetch.py` (part 1), the listed test files.
      Verify: `.venv\Scripts\python -m pytest tests/services/test_page_cache.py tests/services/test_throttle.py tests/services/test_html_to_text.py tests/services/test_requests_client.py tests/unit/test_js_shell.py tests/services/test_playwright_renderer.py -q` — passes, with the playwright test reported as skipped (not failed) when the package is absent.

- [ ] 8. `FetchService` orchestration + Playwright fallback (D14, D15, D18, P11). Depends on 6, 7. The budget dependency is the `PageBudget` Protocol defined in `fetch.py`, so item 8 builds without `budget.py`; test it with a tiny fake `PageBudget` here, and item 9 adds the real `BudgetGuard` integration test.
      Add `FetchService` and `make_page_text_loader` to `services/fetch.py` exactly per 4.M1.6. Tests `tests/services/test_fetch_service.py` using `tests/helpers/fakes.py::FakeHttpClient` (scripted per-URL responses) + `FakeRenderer` + `FakeClock` + a real tmp SQLite: one test per row of the status-mapping table; robots enforced before any page request (fake records call order: robots.txt first, page only if allowed); cache hit on second fetch (no second HTTP call, `cache_hit=True`, `fetch_method='cache'`, `crawl_status=CACHED`); already-visited failure not refetched unless `refetch_failed`; retry only on timeout/connection error/429/502/503/504 (backoff sleeps asserted on FakeClock; `Retry-After` honoured and capped; 404/403/SSL never retried; `max_retries=0` disables); redirect chain with per-hop robots check and cross-domain recording, > 5 hops -> `TOO_MANY_REDIRECTS`; content-type filter and sniffing; size cap via Content-Length and via streamed length; private host blocked unless `allow_private_hosts`; `use_playwright` auto/true/false behaviour with the fake renderer (JS-shell page triggers render in auto, normal page does not, false never, renderer failure keeps requests result and logs once); rendered text stored and `fetch_method='playwright'`; `crawl_pages` row and `crawl_errors` row written correctly; no page body ever stored in SQLite (assert no column value longer than 2000 chars across all tables); `load_text/load_html`.
      Files: `src/corpdisc/services/fetch.py` (part 2), `tests/helpers/fakes.py`, `tests/services/test_fetch_service.py`.
      Verify: `.venv\Scripts\python -m pytest tests/services/test_fetch_service.py -q` passes.

- [ ] 9. Budget guard, cycle guard, queue (D2). Depends on 1, 2.
      Implement `services/budget.py`, `services/cycle_guard.py`, `services/queue.py` (4.M1.8). Tests: `tests/services/test_budget.py` (depth, per-domain and total page limits counted from `crawl_pages` with the exclusion rule, token budget NULL = unlimited, exhaustion, `record_llm_usage` accumulates in `runs`, degradation levels at 79/80/95/100 %, task LLM-call cap counting only non-cache, attempt-1, non-SKIPPED rows — seed `agent_invocations` rows directly), `tests/services/test_cycle_guard.py` (self loop, direct cycle, long chain, ancestry fast path, REVIEW edges ignored by default and included with `include_review=True`, termination on a pre-existing cycle, `find_cycle` pure helper), `tests/services/test_queue.py` (`compute_priority` values and bounds, breadth-first order by depth then discovery_seq, duplicate `enqueue` returns None, claim atomicity and status changes, `not_before` respected, `fail` retries until `max_attempts=3` then FAILED, `complete` sets entity `crawl_state` in the same transaction, `defer`, `recover_stale`, `parent_entity_id` derivation incl. no parent, `counts`, no `parent_entity_id` column exists). Add the `FetchService`+`BudgetGuard` integration test (`SKIPPED_LIMIT` row with `BUDGET_DOMAIN_PAGE_LIMIT`) to `tests/services/test_fetch_service.py`.
      Files: `src/corpdisc/services/{budget,cycle_guard,queue}.py`, `tests/services/test_{budget,cycle_guard,queue}.py`.
      Verify: `.venv\Scripts\python -m pytest tests/services/test_budget.py tests/services/test_cycle_guard.py tests/services/test_queue.py tests/services/test_fetch_service.py -q` passes.

- [ ] 10. Link indexer and DOM candidate extraction. Depends on 2, 3, 4.
      Implement `services/links.py` and `services/dom_candidates.py` (4.M1.7). Add fixture HTML under `tests/fixtures/html/`: `portfolio_cards.html`, `list_page.html`, `table_page.html`, `logo_grid.html`, `footer_statement.html`, `jsonld_org.html`, `nav_only.html`, `partners_customers.html`, `malicious.html` (javascript: links, huge attributes, nested JSON-LD, script content) — all with fictional names. Tests `tests/services/test_links.py` (document order, dedupe by canonical URL, fragment-only links skipped, `javascript:`/`mailto:` skipped, location nav/footer/body, same_domain and same_registrable_domain, scoring: portfolio/companies anchors score high, careers/privacy/login low, nav bonus, external factor, deterministic ordering, `top`, `to_refs` yields valid `LinkRef` with anchors ≤ 200 chars, `guess_page_type` incl. excluded types, `max_links` cap) and `tests/services/test_dom_candidates.py` (each `dom_hint` strategy from its fixture, nav/header items excluded, boilerplate labels dropped, dedupe, `link_id` resolved only when the anchor is in the index, context ≤ 400 chars, candidate ids sequential, JSON-LD parsed with `json.loads` only, malicious page yields no exception and bounded output, nothing extracted from `<script>` text).
      Files: `src/corpdisc/services/{links,dom_candidates}.py`, `tests/fixtures/html/*.html`, `tests/services/test_links.py`, `tests/services/test_dom_candidates.py`.
      Verify: `.venv\Scripts\python -m pytest tests/services/test_links.py tests/services/test_dom_candidates.py -q` passes.

- [ ] 11. Confidence calculator and matcher ladder (D13). Depends on 2, 4.
      Implement `services/confidence.py` and `services/matcher.py` (4.M1.9, 4.M1.10). Tests `tests/unit/test_confidence.py` (every base score row of ARCHITECTURE section 7, unverified cap 0.40, third-party cap, stacked third-party never ≥ 0.75, independent-evidence combination `1-Π(1-s)` capped at 0.99, same-page evidence not double counted, llm factor `0.5+0.5*c` monotonic, agent confidence can only lower, entity = min(classification, relationship), thresholds 0.75/0.40 boundaries exact, REJECTED rule, each flag forces REVIEW_REQUIRED with the right reason and priority, CONFIRMED requires verified OWNERSHIP first-party/official evidence, `from_settings`) and `tests/services/test_matcher.py` (one test per ladder step 1-6 against a seeded SQLite registry: dedicated domain vs shared domain vs parent's domain, same name same parent, alias+parent, alias+country, same name different parent -> AMBIGUOUS with signals, fuzzy ≥ 0.92 ambiguous and 0.91 new, merged entities ignored, deterministic tie-break, two decisive matches -> AMBIGUOUS, no hardcoded names).
      Files: `src/corpdisc/services/{confidence,matcher}.py`, `tests/unit/test_confidence.py`, `tests/services/test_matcher.py`.
      Verify: `.venv\Scripts\python -m pytest tests/unit/test_confidence.py tests/services/test_matcher.py -q` passes.

- [ ] 12. Committer with invariants, M1 integration, M1 guard tests, docs. Depends on 2, 5, 8, 9, 10, 11.
      Implement `services/committer.py` (4.M1.11). Tests `tests/services/test_committer.py`: happy path (root + child + website + alias + verified evidence + candidate + review item + enqueue in one commit; `local_ref` resolution; `discovery_seq` monotonic), atomic rollback (a late failure leaves zero rows in every table), each invariant INV1-INV6, INV_CONF, INV_EVIDENCE/INV2 (forged `VerifiedEvidence` whose quote is not in stored text is rejected; altered page text after the fact is caught by `check_invariants`), duplicate second CONFIRMED primary parent rejected, cycle rejected, unknown `Unknown` type CONFIRMED rejected, `REQUIRE_WEBSITE_FOR_CONFIRMED` true/false, candidates-only plan, idempotent candidate upsert, enqueue duplicate ignored, `plan.run_id` mismatch, `check_invariants(None)` whole-run scan. Add `tests/integration/test_m1_pipeline.py` on the `local_site` fixture (fictional multi-page group site + `robots.txt` with a disallowed path and a crawl-delay): FetchService (allow_private_hosts=True, trust_env off) -> `LinkIndex` -> `extract_dom_candidates` -> `QuoteVerifier.verify_claim` on fetched text -> `Matcher` -> `ConfidenceCalculator` -> `Committer.commit` -> `QueueService.enqueue/claim_next/complete`; assert robots blocked path never requested (server hit counter), second fetch served from cache with zero extra server hits, confirmed relationship carries verified evidence, cycle plan rejected. Add `tests/unit/test_import_boundaries.py` (services/db/contracts/models rules of section 2 plus synthetic-violation tests of `tests/helpers/import_lint.py`), `tests/unit/test_write_ownership.py`, `tests/unit/test_no_hardcoded_names.py` (word-boundary scan of `src/` and `tests/` for `vela|jonas|volaris|harris|perseus|peakhull`; `constellation|csisoftware` allowed only in `config.py`, `.env.example` and README), `tests/unit/test_scope_guard.py` (forbidden files/dirs absent; `schema.sql` content hash equals the M0 hash recorded in the test; `cli.py` still exposes only `init-db`). Update `README.md` (status now M0+M1, new dependencies, new settings table rows, how to run tests, Playwright is optional: `pip install -e ".[browser]"` then `python -m playwright install chromium`).
      Files: `src/corpdisc/services/committer.py`, `tests/services/test_committer.py`, `tests/integration/test_m1_pipeline.py`, `tests/unit/test_{import_boundaries,write_ownership,no_hardcoded_names,scope_guard}.py`, `tests/helpers/import_lint.py`, `README.md`.
      Verify: `.venv\Scripts\python -m pytest -q` — entire suite green with zero failures (playwright tests skipped only); `.venv\Scripts\python main.py --help` exits 0 and lists only `init-db`. Record the pytest summary line in the M1 report. M1 review gate: `m1-review.json` APPROVED.

### PHASE M2 (items 13-20; the m2-loop builds these; start only after M1 APPROVED)

- [ ] 13. Agent contracts, ports, base (D4, D9, P2, P3). Depends on M1.
      Create `contracts/agents.py` (4.M2.1), `agents/__init__.py`, `agents/ports.py`, `agents/base.py` (4.M2.2). Tests `tests/contracts/test_agents_contracts.py` (envelope round-trip, `AgentName` values equal the schema CHECK list parsed from `schema.sql`, `CallStatus`/`FailureKind` equal the schema CHECK lists, `AgentResponse` status/payload/error validators, generics `AgentRequest[SomeModel]` validate payload type, audit record models round-trip) and `tests/agents/test_base.py` (`make_correlation_id`, `canonical_json` stability under key order, `estimate_tokens`, `BaseAgent` cannot be instantiated without abstract methods, a toy subclass in `tests/agents/toy_agents.py` defines `ToyEchoAgent` (name `AgentName.DISCOVERY`, test-only), `ToyToolAgent`).
      Files: `src/corpdisc/contracts/agents.py`, `src/corpdisc/agents/{__init__,ports,base}.py`, `tests/agents/toy_agents.py`, the listed tests.
      Verify: `.venv\Scripts\python -m pytest tests/contracts tests/agents -q` passes; full suite green.

- [ ] 14. Audit repository and response cache on SQLite (D1, D7, P6). Depends on 13.
      Implement `db/repos/audit.py` (4.M2.3). Tests `tests/services/test_audit_repo.py`: start inserts an unfinished FAILED row (`finished_at` NULL), finish updates it, tool call rows FK to the invocation, FK violation on unknown invocation, every column round-trips, JSON shapes documented in 4.M2.3 are what gets stored (helper builders tested), cache `lookup` returns only `status='OK'`, `cache_hit=0` rows for the exact `(agent, schema_version, prompt_version, input_hash)`, newest wins, FAILED/PARTIAL/SKIPPED/cache-hit rows never returned, `llm_calls` stays empty, lookup uses `idx_invocations_cache` (`EXPLAIN QUERY PLAN` contains the index name). Also add `BudgetGuard` as `BudgetPort` conformance test (`isinstance` not possible on Protocol without runtime_checkable: use a typed assignment `port: BudgetPort = BudgetGuard(...)` checked by a runtime signature comparison helper in `tests/helpers/`).
      Files: `src/corpdisc/db/repos/audit.py`, `tests/services/test_audit_repo.py`.
      Verify: `.venv\Scripts\python -m pytest tests/services/test_audit_repo.py -q` passes.

- [ ] 15. Providers, record/replay, factory (D16). Depends on 13.
      Implement `agents/providers/{__init__,fake,cassette}.py` (4.M2.4). Tests `tests/agents/test_providers.py`: Fake returns scripted strings/dicts/models in order, raises scripted `ProviderError`s, callable steps see the request, exhausted script fails closed, request log recorded; `request_key` stable and sensitive to every field; Record writes JSONL only for successful responses and leaves inner errors unrecorded; Replay serves recorded responses, FIFO-per-key, miss raises non-retryable error (no fall-through), corrupt JSONL line -> clear error; record-then-replay round trip reproduces identical responses; `build_provider` matrix: fake/replay/record ok, missing args `ValueError`, `openai/anthropic/gemini/google` -> `ProviderNotImplementedError` with the exact "not implemented in M2" text, unknown/blank -> `ValueError`; cassette file contains no secret-shaped strings.
      Files: `src/corpdisc/agents/providers/{__init__,fake,cassette}.py`, `tests/agents/test_providers.py`.
      Verify: `.venv\Scripts\python -m pytest tests/agents/test_providers.py -q` passes.

- [ ] 16. LLMGateway and circuit breaker (D6, D7, D8). Depends on 13, 15.
      Implement `agents/gateway.py` (4.M2.5). Tests `tests/agents/test_gateway.py` with `FakeLLMProvider`, `FakeClock`, seeded `random.Random`: success path records usage via a fake `BudgetPort`; transport retry with exponential backoff and jitter bounds asserted on `FakeClock.sleeps` (`retry_after_s` honoured and capped); retries stop at `transport_retries` and at `max_requests`; non-retryable error no retry; auth error no retry and opens breaker immediately; `requests_used` counted correctly; `max_output_tokens` clamped; `cache_key` deterministic, insensitive to dict key order, and changes with each of agent/provider/model/schema_version/prompt_version/temperature/max_output_tokens/input; tokens recorded even when later validation fails (runner test covers it); `tests/agents/test_circuit_breaker.py`: closed -> open after N consecutive TRANSPORT failures (success resets the count), auth opens immediately, open blocks provider calls (0 requests, `CIRCUIT_OPEN`), `half_open()` permits exactly one probe, probe success closes, probe failure reopens, no time-based auto recovery.
      Files: `src/corpdisc/agents/gateway.py`, `tests/agents/test_gateway.py`, `tests/agents/test_circuit_breaker.py`.
      Verify: `.venv\Scripts\python -m pytest tests/agents/test_gateway.py tests/agents/test_circuit_breaker.py -q` passes.

- [ ] 17. ToolBroker (D4, D17). Depends on 13, 14.
      Implement `agents/broker.py` (4.M2.6). Tests `tests/agents/test_broker.py` with toy read-only tools (`echo`, `lookup_number`, `boom` that raises, `big` that returns a huge string) defined in `tests/agents/toy_tools.py`: allowed call succeeds and is audited (`allowed=1`); unknown tool and non-allowlisted tool denied (`allowed=0`, result is a `ToolResult` error, no exception); per-tool cap enforced per session and sessions are independent; `max_total` cap; invalid args (`extra` field, wrong type) -> `INVALID_ARGS`; handler exception -> `TOOL_ERROR:<ExcType>` with no traceback text; oversize result truncated with `truncated=True`; non-JSON-serializable result -> `TOOL_ERROR`; registering a spec with `read_only=False` or a duplicate name raises `ValueError`; `descriptors_for` exposes only allowed tools with a JSON schema; audit rows carry canonical args JSON, duration and summary strings per 4.M2.3; assert `ToolSpec` handlers receive validated model instances only.
      Files: `src/corpdisc/agents/broker.py`, `tests/agents/toy_tools.py`, `tests/agents/test_broker.py`.
      Verify: `.venv\Scripts\python -m pytest tests/agents/test_broker.py -q` passes.

- [ ] 18. AgentRunner (D1, D6, D7). Depends on 14, 16, 17.
      Implement `agents/runner.py` (4.M2.7). Tests `tests/agents/test_runner.py` using the toy agents, `FakeLLMProvider`, `FakeClock`, an in-memory sink `tests/helpers/audit.py::InMemoryAudit` (implements `AuditSink` and `ResponseCache`) AND, in a second parametrization, `SqliteAuditSink`/`SqliteResponseCache`/`BudgetGuard` on a tmp DB: OK on first valid JSON; repair retry (invalid JSON then valid -> OK, two rows sharing `correlation_id` with `attempt` 1 and 2, first row `FAILED/SCHEMA`, repair message contains no input echo); second invalid -> FAILED/SCHEMA after exactly 1 repair (no third ask); cache hit on identical input (no provider call, row `cache_hit=1`, tokens 0, `usage.cache_hit`), cache miss when any key field changes (input, schema_version, prompt_version, model, max_output_tokens), invalid responses never cached and a stored payload that no longer validates is ignored; budget exhausted -> `SKIPPED/BUDGET` with an audit row and no provider call; task LLM-call cap; circuit open -> FAILED/CIRCUIT_OPEN with an audit row; transport failure -> FAILED/TRANSPORT and the breaker counts it once per gateway call; `MAX_PROVIDER_REQUESTS_PER_INVOCATION` cap across transport retries + repair + tool continuation -> FAILED/BUDGET; tool loop: allowed tool call executed through the broker and the result fed back, denied tool returns an error message and the loop continues, per-tool cap, agent with no tools has every call denied, `max_tool_calls`, PARTIAL intermediate rows never cached, `agent_tool_calls` rows FK to the right attempt row; token usage recorded through `BudgetPort` even for invalid outputs; `runs.llm_tokens_used` updated in the SQLite parametrization; `invoke` never raises for provider/validation/tool failures (unexpected exception -> FAILED `INTERNAL:`); contract errors (`meta.agent` mismatch) DO raise `AgentContractError`; LLM output can never produce a `CommitPlan` or touch the DB (runner module has no DB import — covered by the boundary test); log line contains the 28.5 fields (caplog) and no API key/secret; `llm_calls` stays empty.
      Files: `src/corpdisc/agents/runner.py`, `tests/helpers/audit.py`, `tests/agents/test_runner.py`.
      Verify: `.venv\Scripts\python -m pytest tests/agents/test_runner.py -q` passes.

- [ ] 19. Record/replay end to end and M2 integration. Depends on 15, 18.
      Add `tests/integration/test_m2_agents.py`: wire `AgentRunner` + `LLMGateway` + `SqliteAuditSink` + `SqliteResponseCache` + `BudgetGuard` + `ToolBroker` (toy tools) on a tmp DB with run row and `runs.llm_token_budget` set; scenario 1 record: `RecordingProvider(FakeLLMProvider(script))` runs a multi-step toy invocation (tool call -> repair -> OK) and writes a cassette; scenario 2 replay: `ReplayProvider(cassette)` reproduces identical `AgentResponse` payloads with zero live calls on a fresh DB; scenario 3 budget ladder: token budget crossing 80/95/100 % flips `BudgetGuard.degradation_level()` and the 100 % case makes the next invoke `SKIPPED`; scenario 4 breaker: N transport failures open the breaker, `half_open()` + success closes it; scenario 5 cache across two `AgentRunner` instances on the same DB; assert `agent_invocations` audit completeness (no row left with `finished_at` NULL after normal completion), `llm_calls` empty, all writes went through the sink.
      Files: `tests/integration/test_m2_agents.py`.
      Verify: `.venv\Scripts\python -m pytest tests/integration -q` passes.

- [ ] 20. M2 boundary and scope tests, docs, final full verification. Depends on 13-19.
      Extend `tests/unit/test_import_boundaries.py` with the `agents/` rules of section 2 (banned imports, banned builtins, handle-named parameters, `providers/` exemption, `BaseAgent` subclass ban in `src/`, no SDK imports, clock-only time calls) including synthetic violating snippets for each rule; extend `tests/unit/test_write_ownership.py` for `db/repos/audit.py` and `services/budget.py`; extend `tests/unit/test_scope_guard.py` (no M3/M4+ files, no new CLI commands, `llm_calls` never written, provider factory refuses real providers). Update `README.md` (status M0-M2, module map, the agent infrastructure overview, D16 provider policy, how to run record/replay in tests). Do not add CLI commands.
      Files: `tests/unit/test_import_boundaries.py`, `tests/unit/test_write_ownership.py`, `tests/unit/test_scope_guard.py`, `README.md`.
      Verify: `.venv\Scripts\python -m pytest -q` — whole suite green, zero failures, only `@pytest.mark.playwright` tests may be skipped; `.venv\Scripts\python main.py --help` exits 0; `.venv\Scripts\python -c "import corpdisc.agents.runner, corpdisc.services.committer"` succeeds. Record the pytest summary line in the M2 report. M2 review gate: `m2-review.json` APPROVED.

---------------------------------------------------------------------------------------------------

## 6. Requirements traceability (brief -> plan items)

| Brief requirement | Item |
|---|---|
| URL canonicalization + tests | 3 |
| robots.txt (fetch, cache, respect, crawl-delay, failure behaviour, audit, no LLM override) + tests | 6, 8 |
| FetchService (timeout, max size, content-type, status handling, throttling, safe retries, robots, page cache, metadata, no bodies in SQLite) | 7, 8 |
| Playwright fallback | 7, 8 |
| Link indexing, DOM candidates | 10 |
| Quote verification | 5 |
| Normalization, matcher ladder, confidence | 4, 11 |
| Cycle guard, budget guard, queue | 9 |
| Committer with invariants | 12 |
| Agent base, AgentRunner, LLMGateway, providers, FakeLLMProvider, structured validation, repair, cache, audits | 13-18 |
| ToolBroker allowlist/caps/budget/circuit breaker/record-replay | 15-17, 19 |
| Import-boundary enforcement of "agents have no DB/HTTP/filesystem/fetch" | 12, 20 |
| No hard-coded company names / nothing CSI-specific | 12 (test), all fixtures |
| Not started: M3 gates, M4 supervisor/LangGraph, business agents, live LLMs, CSI crawl, Excel, search, sitemap, merge | 12, 20 (scope guard) |

---------------------------------------------------------------------------------------------------

## 7. Flags and assumptions (surface these in the milestone reports)

- F1 (D8). Circuit-breaker state is in memory only; no schema table. A paused run after a restart depends on M4 persisting `runs.status='PAUSED'`.
- F2 (D10). `REQUIRE_WEBSITE_FOR_CONFIRMED` defaults to true (Part II 29.2). Pending user confirmation; may send many small subsidiaries to review.
- F3 (D13). Simplified reject rule: `REJECTED` iff `relationship_confidence < review_threshold`. Part I would keep a sub-0.40 item in REVIEW when it has any supporting evidence. Consequence: third-party-only evidence with `agent_confidence < 1` is REJECTED (kept in `candidates`, not exported). Revisit at M3.
- F4. `FIRST_PARTY_OTHER` has no base score in Part I section 7; this plan uses 0.50 (below confirm, above reject).
- F5 (D7). "Provider attempt" is implemented as one `LLMGateway.complete` call (initial / repair / tool continuation); transport retries are nested in that row's JSON and count toward `MAX_PROVIDER_REQUESTS_PER_INVOCATION`. If the D7 full text means one row per raw transport retry, change only `AgentRunner` row-writing plus the gateway's `transport_errors` plumbing.
- F6 (P2). Clock/Sleeper protocols are in a neutral `corpdisc/clock.py` and re-exported from `agents/ports.py` (deviation from "agents define them", needed to keep services free of agents imports).
- F7. `VerifiedEvidence.invocation_id` is `int | None` (22.3 says `int`) so deterministic, non-agent evidence (for example footer statements found by the DOM pass) is representable without inventing an invocation.
- F8. Part II G3 and Part I 10.3 mention a fuzzy 0.95 fallback for quotes. D5 overrides: exact normalized substring only.
- F9. Trailing-slash removal (P10) may conflict with a rare server that treats `/x` and `/x/` differently; redirects normally resolve it. Canonical form is chosen for visited-set stability.
- F10. ARCHITECTURE 24.2 orders budget check before cache lookup; the plan follows that order, so an exhausted token budget also skips would-be cache hits. Revisit in M4 if cache-first is preferred.
- F11. Python: the venv is 3.13.5; 3.12 is unverified (same as M0).
- F12. OneDrive sync of `.cache/` and SQLite WAL side files can lock files on Windows; tests use tmp dirs only. `os.replace` on Windows may raise `PermissionError` if a sync client holds a file: PageCache retries up to 3 times with 50 ms sleeps through the injected Sleeper, then returns a failed write (treated as cache miss, logged).
- F13. SSRF guard checks IP literals and well-known private names only; it does not resolve DNS (documented limitation; the network guard in tests would block DNS anyway).
- F14. Playwright/Chromium are not installed by default; the marked tests skip. The lazy adapter is therefore verified only structurally (no import at construction, error mapping with a stub) unless the optional extra is installed.
- F15. Decision D9 says toy agents are test-only; they must borrow one of the 7 `AgentName` values because the DB CHECK restricts `agent_invocations.agent`.

---------------------------------------------------------------------------------------------------

## 8. Explicit non-goals (do not create)

Gates G1-G14 or a `gates/` package; Supervisor, Advisor, LangGraph graphs, run loop; any business agent class, prompt, or per-agent I/O contract (Section 22.4-22.10 models); `discovery.py extraction.py relationship.py website.py dedup.py validation.py advisor.py sitemap.py search.py merge.py`; new CLI commands; real LLM provider SDKs or any live LLM call; search providers; CSI crawling or any live run; Excel/openpyxl; changes to `schema.sql`, `ARCHITECTURE.md`, or the steering files; LangGraph, ORM, or a new agent framework.
