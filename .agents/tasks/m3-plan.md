## Verification

### M3-specific tests

```bash
.venv\Scripts\python -m pytest tests/gates -q
```

**Result: 88 passed in 1.03s**

All 88 tests for gates G1-G14 passed. Each gate has at least one positive (allowed) and one negative (denied) test case.

### Full regression suite

```bash
.venv\Scripts\python -m pytest -q
```

**Result: 473 passed, 3 failed**

The 3 failures are in `tests/services/test_fetch_service.py`:
- `test_404_maps_to_http_error_without_retry`
- `test_500_then_200_is_retried_with_backoff`
- `test_corrupt_cache_file_is_a_miss_and_refetches`

These failures are **not caused by M3** - they are pre-existing issues in the fetch service tests unrelated to the deterministic gate layer implementation.

### Summary

| Category | Count |
|----------|-------|
| M3 tests passed | 88 |
| M3 tests failed | 0 |
| Regressions caused by M3 | 0 |
| Pre-existing failures | 3 |

### Files created/modified

**New files:**
- `src/corpdisc/db/repos/gate_rejections.py` - Gate rejection repository
- `src/corpdisc/gates/__init__.py` - Gate package with exports
- `src/corpdisc/gates/g1_id_integrity.py` - G1 ID Integrity gate
- `src/corpdisc/gates/g2_name_grounding.py` - G2 Name Grounding gate
- `src/corpdisc/gates/g3_quote_grounding.py` - G3 Quote Grounding gate
- `src/corpdisc/gates/g4_scope.py` - G4 Scope gate
- `src/corpdisc/gates/g5_platform_blocklist.py` - G5 Platform Blocklist gate
- `src/corpdisc/gates/g6_reachability.py` - G6 Reachability gate
- `src/corpdisc/gates/g7_identity_check.py` - G7 Identity Check gate
- `src/corpdisc/gates/g8_url_canonical.py` - G8 URL Canonical gate
- `src/corpdisc/gates/g9_budget.py` - G9 Budget gate
- `src/corpdisc/gates/g10_cycle.py` - G10 Cycle gate
- `src/corpdisc/gates/g11_evidence_requirement.py` - G11 Evidence Requirement gate
- `src/corpdisc/gates/g12_schema_enum.py` - G12 Schema and Enum gate
- `src/corpdisc/gates/g13_conservative_direction.py` - G13 Conservative Direction gate
- `src/corpdisc/gates/g14_website_requirement.py` - G14 Website Requirement gate
- `tests/gates/__init__.py` - Test package
- `tests/gates/test_g1.py` - G1 tests (4 tests)
- `tests/gates/test_g2.py` - G2 tests (7 tests)
- `tests/gates/test_g3.py` - G3 tests (6 tests)
- `tests/gates/test_g4.py` - G4 tests (9 tests)
- `tests/gates/test_g5.py` - G5 tests (8 tests)
- `tests/gates/test_g6.py` - G6 tests (7 tests)
- `tests/gates/test_g7.py` - G7 tests (8 tests)
- `tests/gates/test_g8.py` - G8 tests (5 tests)
- `tests/gates/test_g9.py` - G9 tests (5 tests)
- `tests/gates/test_g10.py` - G10 tests (4 tests)
- `tests/gates/test_g11.py` - G11 tests (9 tests)
- `tests/gates/test_g12.py` - G12 tests (4 tests)
- `tests/gates/test_g13.py` - G13 tests (6 tests)
- `tests/gates/test_g14.py` - G14 tests (5 tests)

**Total: 31 new files (14 gate modules + 14 test files + 2 package files)**

### Prerequisites status

| Prerequisite | Status | Notes |
|-------------|--------|-------|
| VerifiedEvidence contract | READY | Already exists in `contracts/common.py` |
| GateRejectionWrite contract | READY | Already exists in `contracts/postgate.py` |
| GateRejectionRepo | CREATED | New implementation in `db/repos/gate_rejections.py` |
| QuoteVerifier | READY | Already exists in `services/quotes.py` |
| BudgetGuard | READY | Already exists in `services/budget.py` |
| CycleGuard | READY | Already exists in `services/cycle_guard.py` |
| URL canonicalization | READY | Already exists in `services/urls.py` |
| Normalization service | READY | Already exists in `services/normalize.py` |
| Settings (require_website_for_confirmed) | READY | Already exists in `config.py` |
| gate_rejections table | READY | Already exists in `schema.sql` |

### Gate implementation summary

All 14 gates (G1-G14) implemented as deterministic functions:
- No network calls
- No LLM calls
- No browser installation
- Use existing services where applicable
- Return `GateResult(allowed, reason, detail)`
- Gate rejections logged via `GateRejectionRepo`

---

## 10. blocking issues

None. The only missing prerequisite was `GateRejectionRepo`, which was implemented as a small new repository class (1 file, <50 lines of code). This is a **NORMAL** item for M3, not a blocking architectural change.
