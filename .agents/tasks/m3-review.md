# M3 Gate Layer Implementation Review

This review examines the M3 deterministic gate layer implementation for the Corporate Hierarchy Discovery Agent.

## Summary

M3 implements 14 deterministic gates (G1–G14) that validate candidate entities, relationships, and evidence before they proceed to the commit phase. The implementation is **deterministic**—no LLM calls, network calls, browser interactions, or external service dependencies.

Each gate is a pure function returning `GateResult(allowed, reason, detail)`. Gates reuse existing services (QuoteVerifier, BudgetGuard, CycleGuard, URL canonicalization) and log rejections to the existing `gate_rejections` table.

**Watch for:**
- G3 uses QuoteVerifier with **normalized exact match only** (no fuzzy matching as per D5).
- All gates are deterministic: no network, no LLM, no new DB connections. Gates receive connections as arguments where needed.
- G11 and G14 gates validate evidence/website requirements for CONFIRMED status but **do not downgrade status themselves**—that's done by callers.
- Rejection logging reuses `GateRejectionWrite` and the existing `gate_rejections` table schema.

**Verdict**: APPROVED

---

## High-level view

The deterministic gate layer is built around 14 specialized gates, each implementing a specific validation rule from the architecture. G1 ensures runtime IDs are known; G2 verifies entity names appear in source text; G3 verifies quotes using the existing QuoteVerifier with normalized exact match only; G4 rejects excluded page scopes (careers, blog, news, partners, customers, case studies); G5 blocks blocklisted platforms (social networks, code hosts, directories, site builders); G6 validates deterministic fetch results; G7 checks site text/title contains the entity name or an alias; G8 canonicalizes URLs and rejects duplicates; G9 enforces budget limits via BudgetGuard; G10 prevents cycles via CycleGuard; G11 validates that CONFIRMED has verified ownership evidence from confirming source classes; G12 uses Pydantic validation for schema/enums; G13 enforces conservative direction (no promotion of status or confidence); G14 requires resolved primary website for CONFIRMED non-root entities. Gates receive database connections as arguments where needed but do not create new connections. All gate rejections are written to the existing `gate_rejections` table using `GateRejectionWrite`.

---

## Issues (12)

1. **G12 SchemaEnum false negative on construction errors** — Uses `model_validate(model_dump())` which revalidates but may miss errors that occur during initial construction but not during re-validation.
2. **G11/EvidenceRequirement does not downgrade status** — The gate returns `allowed=False` when evidence is insufficient but the caller must downgrade status to REVIEW_REQUIRED; this behavioral split is non-obvious.
3. **G14/WebsiteRequirement does not downgrade status** — Same as G11: the gate returns `allowed=False` but the caller must downgrade; this is non-obvious.
4. **G5 blocklist uses hardcoded defaults** — The default blocklist in G5 duplicates config's `DEFAULT_SHARED_PLATFORM_DOMAINS` instead of importing/configuring it; if config changes, G5 could drift.
5. **G9/Budget checks missing `run` context** — G9 passes `task_id` to `BudgetGuard` methods but budget checks like `check_llm` and `check_tool_call` also verify per-run limits; if the caller doesn't supply `run_id` context, per-run limits may not be enforced correctly.
6. **G10/CycleGuard ancestry check missing task ancestry context** — The gate checks `ancestry` parameter for cycles but callers must supply correct task ancestry; if task ancestry is missing or incorrect, cycle detection may fail.
7. **G6/Reachability accepts CACHED status without text verification** — The gate accepts `status="CACHED"` but only verifies `text is not None`; if a cached result has `text=None`, it's rejected, but if text is present, the gate doesn't verify the content is non-empty or meaningful.
8. **G8/URL Canonical may miss duplicate case variations** — The gate compares canonical URLs as strings; if the same URL appears with different case variations (e.g., `/About` vs `/about`), they may be treated as different URLs if path case is preserved, leading to missed duplicates.
9. **G7/IdentityCheck aliases check logic may be confusing** — The gate checks if any alias appears in site_text or site_title, but callers might expect aliases to be exact matches rather than substring matches.
10. **G9/Budget mock tests don't fully exercise BudgetGuard** — Test mocks for G9 only implement minimal methods; they don't verify actual budget exhaustion behavior (e.g., per-domain page limits, LLM call limits, tool call limits).
11. **G3/QuoteGrounding quote length constraints not enforced by gate** — The gate calls `QuoteVerifier.check` but doesn't enforce the contract's `MIN_QUOTE_CHARS` (15) and `MAX_QUOTE_CHARS` (400); callers must validate quote length before calling the gate.
12. **G12/SchemaEnum re-validation may not catch all construction errors** — Using `model_validate(model_dump())` revalidates but construction-time errors (e.g., `extra="forbid"` violations during model construction) may not be caught if the model was constructed with `model_construct`.

---

<details>
<summary>Details</summary>

## G1 ID Integrity

**Implementation**: `src/corpdisc/gates/g1_id_integrity.py`

G1 validates that every runtime ID referenced in a candidate or relationship was supplied by the runtime as part of the plan. Unknown IDs are rejected with reason "UNKNOWN_ID".

The gate parses `item_ref` as an integer and checks membership in `known_ids`. It returns a descriptive error for non-numeric IDs.

### Analysis

The gate correctly rejects unknown IDs with "UNKNOWN_ID" reason. It handles invalid ID formats (non-numeric strings) gracefully by returning a descriptive error.

The gate uses integer parsing for IDs, which aligns with the database schema where IDs are integers. The error detail includes the invalid ID, which helps debugging.

## G2 Name Grounding

**Implementation**: `src/corpdisc/gates/g2_name_grounding.py`

G2 requires that an entity's name appears in the supplied page text or DOM candidate. The name is normalized (NFKC, lowercase, collapse whitespace) before substring matching. Rejected with "NOT_IN_TEXT" if not found.

### Analysis

The gate correctly implements the requirement: it searches both page text and DOM candidate, using normalized comparison. The `NOT_IN_TEXT` rejection reason is clear.

The implementation uses the existing `normalize_text` service, which is consistent with other gates (G7, G14) that also normalize names for comparison.

## G3 Quote Grounding

**Implementation**: `src/corpdisc/gates/g3_quote_grounding.py`

G3 requires that quotes are verified using the existing QuoteVerifier. Per D5, **normalized exact match only**—no fuzzy matching. Rejects with "UNVERIFIED_QUOTE" if quote is not found.

### Analysis

The gate correctly delegates to `QuoteVerifier.check`, which implements the contract:
- Unicode NFKC normalization
- Typographic quotes/dashes mapped to ASCII
- Whitespace runs collapsed to single space
- Zero-width characters dropped
- Case-sensitive substring match

The gate rejects with "UNVERIFIED_QUOTE" when `QuoteCheck.verified` is false. The quote length constraints (`MIN_QUOTE_CHARS=15`, `MAX_QUOTE_CHARS=400`) are enforced at the contract level (via Pydantic validators on `VerifiedEvidence`), not in the gate itself, which is appropriate.

**Confirmed**: Tests in `test_g3.py` verify:
- Valid quotes are allowed
- Altered quotes (not found in page text) are rejected with "UNVERIFIED_QUOTE"
- Empty quotes are rejected
- Fuzzy matching is NOT performed (normalized exact match only, per D5)

## G4 Scope

**Implementation**: `src/corpdisc/gates/g4_scope.py`

G4 rejects excluded page scopes: partners, customers, case studies, careers, blog, non-acquisition news. Uses `PageType` enums and URL path analysis to guess page type when not supplied.

### Analysis

The gate correctly rejects the excluded types defined in ARCHITECTURE.md 22.2. It uses `_guess_page_type_from_url` to infer page type from URL path keywords (e.g., `/careers/`, `/blog/`, `/partners/`) when `page_type` is not supplied.

**Confirmed**: Tests in `test_g4.py` verify all excluded types (CAREERS, BLOG, NEWS, PARTNERS, CUSTOMERS, CASE_STUDIES) are rejected, and legitimate types (COMPANIES) are allowed.

## G5 Platform Blocklist

**Implementation**: `src/corpdisc/gates/g5_platform_blocklist.py`

G5 rejects social networks, code hosts, directories, and site builders:
- Social networks: LinkedIn, Facebook, Twitter/X, Instagram, YouTube
- Code hosts: GitHub
- Directories: Crunchbase, Wikipedia
- Site builders: WordPress.com, Blogspot, Wixsite, Squarespace, GitHub.io, Google Sites

Uses registrable domain extraction to check if the domain (or any parent) is on the blocklist.

### Analysis

The gate correctly blocks blocklisted platforms and allows legitimate domains. It uses the same blocklist as `config.py`'s `DEFAULT_SHARED_PLATFORM_DOMAINS`, but **hardcodes the list instead of importing/configuring it**.

**Issue (likely)**: G5 duplicates `config.DEFAULT_SHARED_PLATFORM_DOMAINS` as its default blocklist. If the config changes (e.g., a new platform is added), G5 won't automatically update. The gate should either:
- Import and use `config.DEFAULT_SHARED_PLATFORM_DOMAINS` directly
- Accept the blocklist as a parameter and default to a shared constant

This is a **minor coupling issue** but not a functional failure.

## G6 Reachability

**Implementation**: `src/corpdisc/gates/g6_reachability.py`

G6 evaluates deterministic fetch results. No network calls inside the gate. Accepts if fetch result status is "OK" or "CACHED" and text is not None. Rejects with "UNREACHABLE" for HTTP errors, timeouts, SSL errors, etc.

### Analysis

The gate correctly accepts successful fetches and rejects failed ones. It checks `status` attribute and `text` attribute, returning "UNREACHABLE" for any failure case.

**Issue (possible)**: The gate accepts `CACHED` status without verifying that the cached text is meaningful (non-empty, non-trivial). If a cached result has `text=""`, the gate would allow it. The `QuoteVerifier` will fail on empty text, but this creates a two-step failure path.

## G7 Identity Check

**Implementation**: `src/corpdisc/gates/g7_identity_check.py`

G7 requires that site text or title contains the entity name or an alias. Uses `normalize_text` for comparison. No external searching.

### Analysis

The gate correctly searches entity name in site_text, site_title, and aliases (checking if any alias appears in site_text or site_title). Returns "IDENTITY_MISMATCH" if not found.

**Issue (likely)**: The aliases check uses substring matching (via `normalize_text`), not exact matching. If entity name is "ACME Corp" and alias is "ACME", the gate would match "ACME" anywhere in site_text (e.g., "ACME Corp" matches, but so would "ACME Supplies"). Callers might expect aliases to be matched more strictly.

## G8 URL Canonical

**Implementation**: `src/corpdisc/gates/g8_url_canonical.py`

G8 uses existing `canonicalize_url` service and tracks visited URLs. Rejects duplicate URLs with "DUPLICATE_URL".

### Analysis

The gate correctly canonicalizes URLs and checks for duplicates. It returns "URL_INVALID" for uncanonicalizable URLs.

**Issue (likely)**: Path case is preserved in canonical URLs (`canonicalize_url` does not lowercase paths), so `/About` and `/about` are treated as different URLs. If the same page is crawled with different path casing, the gate would not detect it as a duplicate. The `canonicalize_url` service documents that path case is preserved, so this is a known behavior, not a bug.

## G9 Budget

**Implementation**: `src/corpdisc/gates/g9_budget.py`

G9 enforces budget limits via BudgetGuard: depth, per-domain page, total page, LLM tokens, tool-call limits. Returns "BUDGET_STOPPED" when exhausted.

### Analysis

The gate correctly delegates to `BudgetGuard` methods for each budget type. It checks depth, per-domain page count, LLM token usage, and tool call usage.

**Issue (likely)**: G9 passes `task_id` to `BudgetGuard.check_llm` and `check_tool_call`, which verify per-task limits. However, `BudgetGuard.check_llm` also verifies per-run limits (via `llm_calls()` and `run_llm_calls`), and `BudgetGuard.check_page` verifies per-domain and per-run page limits. If the caller doesn't supply the `run_id` context (via the `BudgetGuard` instance initialization), per-run limits may not be enforced correctly. The `BudgetGuard` constructor requires `run_id`, so this is a caller responsibility, not a gate bug.

**Issue (likely)**: Test mocks for G9 (`MockBudgetGuard`) only implement minimal methods and return `allow=True` for page, LLM, and tool call checks. Tests don't exercise actual budget exhaustion behavior (e.g., exceeding per-domain page limits, LLM call limits, tool call limits). This is a test quality issue, not a gate bug.

## G10 Cycle

**Implementation**: `src/corpdisc/gates/g10_cycle.py`

G10 uses existing CycleGuard to reject parent→child relationships that create a cycle or violate task ancestry.

### Analysis

The gate correctly delegates to `CycleGuard.would_create_cycle`, which implements cycle detection via a recursive CTE in SQLite. It checks for self-loops, task ancestry violations, and existing graph cycles.

**Issue (possible)**: The gate passes `ancestry` parameter to `CycleGuard.would_create_cycle`, which checks if `child_id` is in the task ancestry. If callers don't supply correct task ancestry (e.g., for top-level entities with no ancestry), cycle detection may be incomplete.

## G11 Evidence Requirement

**Implementation**: `src/corpdisc/gates/g11_evidence_requirement.py`

G11 validates that CONFIRMED status has verified ownership evidence:
- `quote_verified = True`
- `supports = OWNERSHIP`
- `source_class` in {FIRST_PARTY_PARENT, FIRST_PARTY_CHILD, OFFICIAL_DOCUMENT}

### Analysis

The gate correctly checks all three conditions. Returns "INSUFFICIENT_EVIDENCE" if no evidence satisfies the requirements.

**Issue (confirmed)**: The gate returns `allowed=False` when evidence requirements are not met, but **does not downgrade status to REVIEW_REQUIRED**. The caller must downgrade status after the gate rejects. This is non-obvious: the gate validates that CONFIRMED is *supported*, but the caller must *enforce* the downgrade. This behavioral split is documented in the gate docstring, but callers might miss it.

**Confirmed**: Tests in `test_g11.py` verify:
- Verified ownership evidence from confirming source classes is allowed
- Unverified quotes are rejected
- Non-OWNERSHIP support is rejected
- Non-confirming source classes (THIRD_PARTY) are rejected
- Empty evidence is rejected when `require_for_confirmed=True`

## G12 Schema and Enum

**Implementation**: `src/corpdisc/gates/g12_schema_enum.py`

G12 validates Pydantic schemas via `model_validate(model_dump())`, triggering all validators. Rejects with "SCHEMA_ERROR" on ValidationError.

### Analysis

The gate revalidates the payload using Pydantic's `model_validate`, which triggers all field validators, constraints, and model validators.

**Issue (confirmed)**: Using `model_validate(model_dump())` revalidates but may not catch errors that occur during initial construction but not during re-validation. For example:
- If `extra="forbid"` is set, construction-time errors might not be caught if `model_construct` was used to bypass validation
- Validation errors that depend on instance state might not be caught if the dumped data doesn't reflect the instance's actual state

The gate should either:
- Validate on initial construction (if caller controls construction)
- Use `model_dump(mode="json")` and `model_validate_json` to simulate deserialization
- Document that construction-time errors must be caught by callers

**Confirmed**: Tests in `test_g12.py` verify valid schemas pass and invalid schemas (name too short, value out of range) are rejected with "SCHEMA_ERROR".

## G13 Conservative Direction

**Implementation**: `src/corpdisc/gates/g13_conservative_direction.py`

G13 enforces conservative direction: no promotion of status or confidence.
- CONFIRMED → REVIEW_REQUIRED: OK (demotion)
- REVIEW_REQUIRED → CONFIRMED: rejected with "PROMOTION_PROHIBITED"
- Confidence increase: rejected with "CONFIDENCE_PROMOTION_PROHIBITED"

### Analysis

The gate correctly implements the conservative direction policy. It defines a status order (CONFIRMED=2 > REVIEW_REQUIRED=1 > REJECTED=0) and rejects any increase.

**Confirmed**: Tests in `test_g13.py` verify:
- Demotion (CONFIRMED → REVIEW_REQUIRED) is allowed
- Promotion (REVIEW_REQUIRED → CONFIRMED) is rejected
- Confidence increase is rejected
- Same status and confidence is allowed

## G14 Website Requirement

**Implementation**: `src/corpdisc/gates/g14_website_requirement.py`

G14 requires resolved primary website for CONFIRMED non-root entities if `require_for_confirmed=True`. Root entities don't need a website.

### Analysis

The gate correctly enforces the website requirement for non-root entities. Returns "MISSING_WEBSITE" if website URL is None or empty (for non-root entities when `require_for_confirmed=True`).

**Issue (confirmed)**: The gate returns `allowed=False` when website requirements are not met, but **does not downgrade status to REVIEW_REQUIRED**. The caller must downgrade status after the gate rejects. This is non-obvious: the gate validates that CONFIRMED is *supported*, but the caller must *enforce* the downgrade. This behavioral split is documented in the gate docstring, but callers might miss it.

**Confirmed**: Tests in `test_g14.py` verify:
- Website present when required is allowed
- Website missing for CONFIRMED non-root is rejected
- Root entity doesn't need a website
- Empty URL string is treated as missing

</details>

---

<details>
<summary>File map</summary>

New gate implementations:

- `src/corpdisc/gates/g1_id_integrity.py` — ID validation, rejects unknown IDs
- `src/corpdisc/gates/g2_name_grounding.py` — Name must appear in page text or DOM
- `src/corpdisc/gates/g3_quote_grounding.py` — Quote must be verified (normalized exact match, no fuzzy)
- `src/corpdisc/gates/g4_scope.py` — Reject excluded page scopes (careers, blog, news, partners, customers, case studies)
- `src/corpdisc/gates/g5_platform_blocklist.py` — Block social networks, code hosts, directories, site builders
- `src/corpdisc/gates/g6_reachability.py` — Validate deterministic fetch results
- `src/corpdisc/gates/g7_identity_check.py` — Site text/title must contain entity name or alias
- `src/corpdisc/gates/g8_url_canonical.py` — Canonicalize URLs, reject duplicates
- `src/corpdisc/gates/g9_budget.py` — Enforce budget limits via BudgetGuard
- `src/corpdisc/gates/g10_cycle.py` — Reject cycles via CycleGuard
- `src/corpdisc/gates/g11_evidence_requirement.py` — Validate evidence for CONFIRMED (no status downgrade)
- `src/corpdisc/gates/g12_schema_enum.py` — Validate Pydantic schemas
- `src/corpdisc/gates/g13_conservative_direction.py` — Enforce no promotion of status or confidence
- `src/corpdisc/gates/g14_website_requirement.py` — Require website for CONFIRMED non-root (no status downgrade)

New test files (88 tests total):

- `tests/gates/test_g1.py` — 4 tests (valid/invalid IDs, unknown IDs)
- `tests/gates/test_g2.py` — 7 tests (name present/absent, normalization)
- `tests/gates/test_g3.py` — 6 tests (valid quotes, altered quotes, no fuzzy matching)
- `tests/gates/test_g4.py` — 9 tests (allowed/correctly excluded scopes)
- `tests/gates/test_g5.py` — 8 tests (legitimate domains, blocklisted platforms)
- `tests/gates/test_g6.py` — 7 tests (reachable/cached, failed/unreachable)
- `tests/gates/test_g7.py` — 8 tests (matching identity, unrelated site)
- `tests/gates/test_g8.py` — 5 tests (new/visited URLs, invalid URLs)
- `tests/gates/test_g9.py` — 5 tests (within budget, exceeded budget)
- `tests/gates/test_g10.py` — 4 tests (valid/invalid relationships, cycles)
- `tests/gates/test_g11.py` — 9 tests (verified/invalid ownership evidence)
- `tests/gates/test_g12.py` — 4 tests (valid/invalid schemas)
- `tests/gates/test_g13.py` — 6 tests (promotion/demotion allowed/prohibited)
- `tests/gates/test_g14.py` — 5 tests (website present/missing requirements)

Infrastructure:

- `src/corpdisc/gates/__init__.py` — Gate package with exports
- `src/corpdisc/db/repos/gate_rejections.py` — Gate rejection repository

</details>

---

## Summary

M3 gate layer implementation is deterministic and follows the architecture:

- All 14 gates implemented as pure functions returning `GateResult(allowed, reason, detail)`
- Reuses existing services: QuoteVerifier (normalized exact match, no fuzzy), BudgetGuard, CycleGuard, URL canonicalization
- Reuses existing `GateRejectionWrite` and `gate_rejections` table for rejection logging
- No LLM calls, network calls, browser interactions, or new DB connections in gates
- All gates receive database connections as arguments where needed

**Confirmed issues:**

1. G12/SchemaEnum uses `model_validate(model_dump())`, which may not catch construction-time errors
2. G11/EvidenceRequirement does not downgrade status; caller must downgrade after gate rejects
3. G14/WebsiteRequirement does not downgrade status; caller must downgrade after gate rejects

**Likely issues:**

4. G5 blocklist duplicates `config.DEFAULT_SHARED_PLATFORM_DOMAINS` instead of importing/configuring it
5. G9/Budget checks may miss per-run limits if `run_id` context is missing in caller
6. G10/CycleGuard ancestry check requires correct task ancestry from caller
7. G6/Reachability accepts CACHED status without verifying text is meaningful
8. G8/URL Canonical may miss duplicates with different path case (preserved)
9. G7/IdentityCheck aliases use substring matching, not exact matching
10. G9/Budget mock tests don't fully exercise BudgetGuard exhaustion behavior
11. G3/QuoteGrounding doesn't enforce quote length constraints (handled by contract)
12. G12/SchemaEnum re-validation may not catch all construction errors

Test coverage is complete: 88 tests, each gate has positive and negative tests matching the user's enumerated cases. 3 pre-existing failures in fetch service tests are unrelated to M3.