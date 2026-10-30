"""Deterministic data-quality verification of a finished (or partial) run. Read-only; never changes a row.

Re-checks from the database alone what the pipeline claims: evidence is grounded in the STORED page text, confirmed edges are
backed by first-party ownership evidence, nothing is a cycle or a duplicate, and no product / partner / social page became a
company. Each check returns PASS, WARN (needs a human look) or FAIL (a defect).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

from corpdisc.models.domain import EntityType, RelationshipType
from corpdisc.services.committer import Committer
from corpdisc.services.cycle_guard import CycleGuard
from corpdisc.services.normalize import normalize_text
from corpdisc.services.quotes import QuoteVerifier
from corpdisc.services.validation_engine import PRODUCT_NAME_HINT
from corpdisc.services.wording import NEGATIVE_WORDING

_COMPANY_LIKE: Final = ("Company", "Subsidiary", "Portfolio Company", "Acquired Company")
_OWNERSHIP_CLASSES: Final = ("FIRST_PARTY_PARENT", "FIRST_PARTY_CHILD", "OFFICIAL_DOCUMENT")


@dataclass(frozen=True)
class Check:
    name: str
    status: str  # PASS | WARN | FAIL
    detail: str


def check_run(conn: sqlite3.Connection, run_id: int, *, committer: Committer, cycle: CycleGuard, page_text: Callable[[int], str | None],
              shared_domains: frozenset[str]) -> list[Check]:
    q = lambda sql, *p: conn.execute(sql, p).fetchall()  # noqa: E731
    out: list[Check] = []

    def add(name: str, bad: list, *, warn: bool = False, ok: str = "none found") -> None:
        shown = "; ".join(str(x) for x in bad[:6]) + (f" ... (+{len(bad) - 6})" if len(bad) > 6 else "")
        out.append(Check(name, "PASS" if not bad else ("WARN" if warn else "FAIL"), ok if not bad else f"{len(bad)}: {shown}"))

    live = "run_id=? AND merged_into_id IS NULL"
    ids = [r[0] for r in q(f"SELECT entity_id FROM entities WHERE {live}", run_id)]
    root = q("SELECT entity_id FROM entities WHERE run_id=? ORDER BY discovery_seq LIMIT 1", run_id)
    root_id = root[0][0] if root else None

    add("no duplicate canonical entities (same name under one parent)",
        [f"{r['n']!r} x{r['k']} under {r['p']}" for r in q(
            "SELECT e.normalized_name n, x.parent_id p, COUNT(*) k FROM entities e JOIN relationships x ON x.child_id=e.entity_id "
            "WHERE e.run_id=? AND e.merged_into_id IS NULL AND x.verification_status<>'REJECTED' GROUP BY 1,2 HAVING k>1", run_id)])
    add("no duplicate entity names across the run (a second spelling would need a merge)",
        [f"{r['n']!r} x{r['k']}" for r in q(f"SELECT normalized_name n, COUNT(*) k FROM entities WHERE {live} GROUP BY 1 HAVING k>1", run_id)], warn=True)
    add("no duplicate websites (dedicated domain owned by two entities)",
        [f"{r['d']} -> {r['k']} entities" for r in q(
            "SELECT lower(w.registrable_domain) d, COUNT(DISTINCT w.entity_id) k FROM websites w JOIN entities e ON e.entity_id=w.entity_id "
            "WHERE e.run_id=? AND e.merged_into_id IS NULL AND w.kind='OFFICIAL' GROUP BY 1 HAVING k>1", run_id) if r["d"] not in shared_domains])
    add("no cycles (including REVIEW edges)", [f"entity {e}" for e in cycle.has_cycle_through(ids, include_review=True)])
    add("no CONFIRMED relationship without verified first-party/official ownership evidence",
        [f"relationship {r[0]}" for r in q(
            "SELECT x.relationship_id FROM relationships x JOIN entities e ON e.entity_id=x.child_id WHERE e.run_id=? AND x.verification_status='CONFIRMED' "
            "AND NOT EXISTS (SELECT 1 FROM evidence v WHERE v.relationship_id=x.relationship_id AND v.quote_verified=1 AND v.supports='OWNERSHIP' "
            f"AND v.source_class IN {_OWNERSHIP_CLASSES})", run_id)])

    ungrounded, verifier = [], QuoteVerifier()
    for r in q("SELECT v.evidence_id, v.page_id, v.quote FROM evidence v JOIN entities e ON e.entity_id=v.entity_id WHERE e.run_id=? AND v.quote_verified=1", run_id):
        text = page_text(r["page_id"]) if r["page_id"] else None
        if text is None or not verifier.check(r["quote"], text).verified:
            ungrounded.append(f"evidence {r['evidence_id']}")
    add("every verified quote is found in the STORED page text", ungrounded)

    fk = [f"{r[0]}.{r[3]}" for r in q("PRAGMA foreign_key_check")]
    bad_refs = [f"relationship {r[0]}" for r in q(
        "SELECT x.relationship_id FROM relationships x LEFT JOIN entities p ON p.entity_id=x.parent_id LEFT JOIN entities c ON c.entity_id=x.child_id "
        "WHERE (c.run_id=? OR p.run_id=?) AND (p.entity_id IS NULL OR c.entity_id IS NULL OR p.run_id<>c.run_id OR x.parent_id=x.child_id)", run_id, run_id)]
    bad_merge = [f"entity {r[0]}" for r in q("SELECT e.entity_id FROM entities e LEFT JOIN entities t ON t.entity_id=e.merged_into_id WHERE e.run_id=? "
                                             "AND e.merged_into_id IS NOT NULL AND t.entity_id IS NULL", run_id)]
    add("no unknown IDs / broken parent-child references (foreign keys, runs, merges)", fk + bad_refs + bad_merge)

    types = {t.value for t in RelationshipType}
    etypes = {t.value for t in EntityType}
    add("no invalid relationship or entity types",
        [f"rel {r[0]}={r[1]}" for r in q("SELECT x.relationship_id, x.relationship_type FROM relationships x JOIN entities e ON e.entity_id=x.child_id WHERE e.run_id=?", run_id) if r[1] not in types]
        + [f"entity {r[0]}={r[1]}" for r in q(f"SELECT entity_id, entity_type FROM entities WHERE {live}", run_id) if r[1] not in etypes])

    rejected = {r[0] for r in q("SELECT normalized_name FROM candidates WHERE run_id=? AND decision='REJECTED_NOT_ENTITY'", run_id)}
    add("no product/service rejected by extraction became an entity",
        [f"{r['name']!r}" for r in q(f"SELECT name, normalized_name FROM entities WHERE {live}", run_id) if r["normalized_name"] in rejected])
    add("no company-typed entity whose name reads like a product/service",
        [f"{r['name']!r} ({r['entity_type']})" for r in q(f"SELECT name, entity_type FROM entities WHERE {live} AND verification_status='CONFIRMED'", run_id)
         if r["entity_type"] in _COMPANY_LIKE and PRODUCT_NAME_HINT.search(r["name"])], warn=True)
    add("no partner/customer/vendor/former-owner wording in the quotes behind CONFIRMED relationships",
        [f"relationship {r['relationship_id']}: {r['quote'][:70]!r}" for r in q(
            "SELECT x.relationship_id, v.quote FROM relationships x JOIN entities e ON e.entity_id=x.child_id JOIN evidence v ON v.relationship_id=x.relationship_id "
            "WHERE e.run_id=? AND x.verification_status='CONFIRMED' AND v.quote_verified=1", run_id) if NEGATIVE_WORDING.search(r["quote"])])
    add("no social/network/shared-platform site used as a company website",
        [f"entity {r['entity_id']}: {r['registrable_domain']}" for r in q(
            "SELECT w.entity_id, lower(w.registrable_domain) registrable_domain FROM websites w JOIN entities e ON e.entity_id=w.entity_id WHERE e.run_id=?", run_id)
         if r["registrable_domain"] in shared_domains])

    unsupported = []
    for r in q(f"SELECT entity_id, name, normalized_name FROM entities WHERE {live} AND entity_id<>?", run_id, root_id or -1):
        pages = [x[0] for x in q("SELECT source_page_id FROM candidates WHERE run_id=? AND resolved_entity_id=?", run_id, r["entity_id"])]
        pages += [x[0] for x in q("SELECT page_id FROM evidence WHERE entity_id=? AND page_id IS NOT NULL", r["entity_id"])]
        key = normalize_text(r["name"])
        if not any(key and key in normalize_text(page_text(p) or "") for p in set(pages)):
            unsupported.append(f"{r['name']!r}")
    add("no fabricated entities (every entity name is found in a stored page that proposed or evidenced it)", unsupported)

    add("CONFIRMED entities with open review items (never silently promoted)",
        [f"{r['name']!r}: {r['reason_code']}" for r in q(
            "SELECT e.name, r.reason_code FROM review_items r JOIN entities e ON e.entity_id=r.entity_id WHERE e.run_id=? AND e.verification_status='CONFIRMED' "
            "AND r.reason_code IN ('CONTRADICTION','CYCLE','NO_QUOTE','THIRD_PARTY_ONLY','NO_WEBSITE','BROKEN_SITE','VERIFICATION_UNAVAILABLE')", run_id)], warn=True)
    add("no TERMINAL/UNREACHABLE entity without a recorded reason",
        [f"{r['name']!r} ({r['crawl_state']})" for r in q(
            "SELECT e.name, e.crawl_state FROM entities e JOIN research_queue t ON t.entity_id=e.entity_id "
            "WHERE e.run_id=? AND e.merged_into_id IS NULL AND e.crawl_state IN ('TERMINAL','UNREACHABLE') "
            "AND t.status='DONE' AND COALESCE(t.error,'')=''", run_id)], warn=True)
    add("Committer invariants INV1-INV6 hold for the whole run", [f"{v.code}: {v.message}" for v in committer.check_invariants(None)])
    return out


def format_checks(checks: list[Check]) -> str:
    mark = {"PASS": "PASS", "WARN": "WARN", "FAIL": "FAIL"}
    lines = [f"  [{mark[c.status]}] {c.name}" + ("" if c.status == "PASS" else f"\n         -> {c.detail}") for c in checks]
    n = {s: sum(1 for c in checks if c.status == s) for s in ("PASS", "WARN", "FAIL")}
    return "\n".join(lines) + f"\n  => {n['PASS']} passed, {n['WARN']} warnings, {n['FAIL']} FAILED"
