"""ValidationEngine: deterministic whole-graph checks and suspect selection (ARCHITECTURE 8, 24.4 run_checks).

Runs BEFORE any semantic review. It produces (a) findings with severity and (b) a suspect list handed to the
Validation Agent as ``ValidationCase`` objects built from the database. It is read-only; the Committer applies
any outcome. ERROR findings can never be overridden by an agent verdict.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from typing import Final

from corpdisc.config import Settings
from corpdisc.contracts.agent_io import EntityCard, EvidenceSummary, ValidationCase
from corpdisc.models.domain import EntityType, VerificationStatus
from corpdisc.services.committer import Committer
from corpdisc.services.cycle_guard import CycleGuard

PRODUCT_NAME_HINT: Final = re.compile(
    r"\b(suite|platform|app|apps|cloud|solutions?|toolkit|dashboard|module|plugin|api|software|service|services)\b", re.I)
_RANK: Final = {
    EntityType.PARENT_COMPANY: 0, EntityType.OPERATING_GROUP: 1, EntityType.BUSINESS_GROUP: 1, EntityType.PORTFOLIO_GROUP: 1,
    EntityType.COMPANY: 2, EntityType.SUBSIDIARY: 2, EntityType.PORTFOLIO_COMPANY: 2, EntityType.ACQUIRED_COMPANY: 2,
    EntityType.DIVISION: 3, EntityType.BUSINESS_UNIT: 3, EntityType.BRAND: 4, EntityType.UNKNOWN: 5,
}  # fmt: skip
_COMPANY_LIKE: Final = frozenset({EntityType.COMPANY, EntityType.SUBSIDIARY, EntityType.PORTFOLIO_COMPANY,
                                  EntityType.ACQUIRED_COMPANY})  # fmt: skip


@dataclass(frozen=True)
class Finding:
    check_name: str
    severity: str  # INFO | WARN | ERROR
    entity_id: int | None
    details: str


@dataclass(frozen=True)
class Suspect:
    entity_id: int
    check_name: str
    findings: tuple[str, ...]
    has_error: bool = False


@dataclass
class EngineResult:
    findings: list[Finding] = field(default_factory=list)
    suspects: list[Suspect] = field(default_factory=list)

    @property
    def violations(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "ERROR"]


class ValidationEngine:
    def __init__(self, conn: sqlite3.Connection, *, run_id: int, settings: Settings, committer: Committer,
                 cycle: CycleGuard) -> None:
        self._c, self._run, self._s, self._committer, self._cycle = conn, run_id, settings, committer, cycle

    # ------------------------------------------------------------------ helpers
    def _root(self) -> int | None:
        r = self._c.execute("SELECT entity_id FROM entities WHERE run_id=? ORDER BY discovery_seq LIMIT 1",
                            (self._run,)).fetchone()
        return None if r is None else int(r[0])

    def path(self, entity_id: int) -> tuple[list[int], float]:
        """Entity ids root -> entity over primary, non-rejected edges, and the minimum edge confidence."""
        ids, conf, cur, seen = [entity_id], 1.0, entity_id, {entity_id}
        while True:
            r = self._c.execute(
                "SELECT parent_id, confidence FROM relationships WHERE child_id=? AND verification_status<>'REJECTED' "
                "ORDER BY is_primary DESC, (verification_status='CONFIRMED') DESC, relationship_id LIMIT 1", (cur,)).fetchone()
            if r is None or r[0] in seen:
                break
            conf = min(conf, float(r[1]))
            cur = int(r[0])
            seen.add(cur)
            ids.append(cur)
        return list(reversed(ids)), conf

    def card(self, entity_id: int) -> EntityCard:
        e = self._c.execute("SELECT * FROM entities WHERE entity_id=?", (entity_id,)).fetchone()
        aliases = [r[0] for r in self._c.execute("SELECT alias FROM entity_aliases WHERE entity_id=? LIMIT 10", (entity_id,))]
        doms = [r[0] for r in self._c.execute("SELECT registrable_domain FROM websites WHERE entity_id=? LIMIT 10", (entity_id,))]
        names = [self._c.execute("SELECT name FROM entities WHERE entity_id=?", (i,)).fetchone()[0]
                 for i in self.path(entity_id)[0][:-1]][:12]
        return EntityCard(entity_id=entity_id, name=e["name"], entity_type=EntityType(e["entity_type"]), aliases=aliases,
                          country=e["country"], industry=e["industry"], description=(e["description"] or None) and e["description"][:300],
                          domains=doms, parent_path=names, verification_status=VerificationStatus(e["verification_status"]))

    # ------------------------------------------------------------------ checks
    def run(self) -> EngineResult:
        c, res, run = self._c, EngineResult(), self._run
        errors: set[int] = set()

        def add(check: str, sev: str, eid: int | None, details: str) -> None:
            res.findings.append(Finding(check, sev, eid, details[:500]))
            if sev == "ERROR" and eid is not None:
                errors.add(eid)

        for v in self._committer.check_invariants(None):
            add(v.code, "ERROR", v.entity_id, v.message)
        ids = [r[0] for r in c.execute("SELECT entity_id FROM entities WHERE run_id=?", (run,))]
        for e in self._cycle.has_cycle_through(ids, include_review=True):
            add("CYCLE", "ERROR", e, "entity is its own ancestor")
        root = self._root()
        for r in c.execute(
            "SELECT e.entity_id, e.name FROM entities e WHERE e.run_id=? AND e.verification_status='CONFIRMED' "
            "AND e.merged_into_id IS NULL AND e.entity_id<>? AND NOT EXISTS (SELECT 1 FROM relationships r JOIN entities p ON p.entity_id=r.parent_id "
            "WHERE r.child_id=e.entity_id AND r.verification_status='CONFIRMED' AND p.verification_status='CONFIRMED')",
                (run, root)):
            add("ORPHAN", "ERROR", r["entity_id"], f"{r['name']!r} is CONFIRMED but has no confirmed parent chain")
        for r in c.execute(
            "SELECT e.normalized_name n, x.parent_id p, COUNT(*) k FROM entities e JOIN relationships x ON x.child_id=e.entity_id "
            "WHERE e.run_id=? AND e.merged_into_id IS NULL AND x.verification_status<>'REJECTED' "
                "GROUP BY 1,2 HAVING k>1", (run,)):
            add("DUPLICATE", "WARN", r["p"], f"{r['k']} entities named {r['n']!r} under one parent")

        suspects: dict[tuple[int, str], list[str]] = {}

        def suspect(eid: int, check: str, why: str) -> None:
            suspects.setdefault((eid, check), []).append(why)

        for e in c.execute("SELECT * FROM entities WHERE run_id=? AND merged_into_id IS NULL AND entity_id<>?",
                           (run, root)):
            eid, etype, conf = e["entity_id"], EntityType(e["entity_type"]), e["confidence"]
            confirmed = e["verification_status"] == "CONFIRMED"
            if not confirmed:  # only CONFIRMED entities can be demoted, so only they are semantic-review suspects
                if e["crawl_state"] == "UNREACHABLE":
                    add("BROKEN_SITE", "WARN", eid, f"{e['name']!r} website was unreachable")
                continue
            if etype in _COMPANY_LIKE and PRODUCT_NAME_HINT.search(e["name"]):
                suspect(eid, "MISCLASSIFIED", f"{e['name']!r} is typed {etype.value} but reads like a product or service")
            if confirmed:
                _, pconf = self.path(eid)
                if pconf < self._s.low_confidence_threshold:
                    suspect(eid, "LOW_CONFIDENCE_PATH", f"weakest edge on the path is {pconf:.2f}")
            n_parents = c.execute("SELECT COUNT(*) FROM relationships WHERE child_id=? AND verification_status<>'REJECTED'",
                                  (eid,)).fetchone()[0]
            if n_parents > 1:
                suspect(eid, "MULTI_PARENT", f"{n_parents} non-rejected parents")
            parent = c.execute("SELECT p.entity_type FROM relationships r JOIN entities p ON p.entity_id=r.parent_id "
                               "WHERE r.child_id=? AND r.verification_status='CONFIRMED' AND r.is_primary=1", (eid,)).fetchone()
            if parent and _RANK[etype] < _RANK[EntityType(parent[0])]:
                suspect(eid, "IMPLAUSIBLE_CHAIN", f"{etype.value} sits below a {parent[0]}")
            if e["crawl_state"] == "UNREACHABLE" or c.execute(
                    "SELECT 1 FROM websites WHERE entity_id=? AND reachable=0", (eid,)).fetchone():
                suspect(eid, "BROKEN_SITE", "website was unreachable")
                add("BROKEN_SITE", "WARN", eid, f"{e['name']!r} website was unreachable")
        for (eid, check), why in sorted(suspects.items()):
            res.suspects.append(Suspect(eid, check, tuple(why[:5]), has_error=eid in errors))
        return res

    # ------------------------------------------------------------------ cases for the Validation Agent
    def cases(self, suspects: list[Suspect], limit: int = 15) -> list[ValidationCase]:
        out: list[ValidationCase] = []
        for i, s in enumerate(suspects[:limit], 1):
            path_ids, pconf = self.path(s.entity_id)
            ev = [EvidenceSummary(quote=r["quote"][:300], source_class=r["source_class"], url=r["url"])
                  for r in self._c.execute("SELECT quote, source_class, url FROM evidence WHERE entity_id=? AND quote_verified=1 "
                                           "ORDER BY evidence_id LIMIT 4", (s.entity_id,))]
            kids = self._c.execute("SELECT COUNT(*) FROM relationships WHERE parent_id=? AND verification_status<>'REJECTED'",
                                   (s.entity_id,)).fetchone()[0]
            names = [self._c.execute("SELECT name FROM entities WHERE entity_id=?", (x,)).fetchone()[0] for x in path_ids]
            out.append(ValidationCase(case_id=i, check_name=s.check_name, entity=self.card(s.entity_id), path=names[:12],  # type: ignore[arg-type]
                                      path_confidence=round(pconf, 4), children_count=kids, evidence=ev,
                                      deterministic_findings=list(s.findings)))
        return out

    def descendants(self, entity_id: int) -> list[int]:
        """CONFIRMED descendants over CONFIRMED edges (they lose their verified path if the entity is demoted)."""
        rows = self._c.execute(
            "WITH RECURSIVE d(id) AS (SELECT child_id FROM relationships WHERE parent_id=? AND verification_status='CONFIRMED' "
            "UNION SELECT r.child_id FROM relationships r JOIN d ON r.parent_id=d.id WHERE r.verification_status='CONFIRMED') "
            "SELECT e.entity_id FROM d JOIN entities e ON e.entity_id=d.id WHERE e.verification_status='CONFIRMED'",
            (entity_id,)).fetchall()
        return [int(r[0]) for r in rows]
