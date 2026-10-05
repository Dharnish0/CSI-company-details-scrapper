"""Deterministic entity matcher ladder (ARCHITECTURE Part I 6.2, plan 4.M1.10). Read-only.

Steps (first decisive wins; merged entities are ignored; ties go to the lowest entity_id):
  1 dedicated official domain -> MATCH       (domain belongs to exactly one entity, which is not the
                                              candidate's parent or an ancestor, and differs from their domains;
                                              shared platforms such as social networks never count)
  2 exact normalized name + same parent -> MATCH
  3 alias hit + (same parent or same country) -> MATCH
  4 exact normalized name, different parent -> AMBIGUOUS
  5 fuzzy name similarity >= threshold -> AMBIGUOUS
  6 otherwise -> NO_MATCH
Two different entities decisive in the same step -> AMBIGUOUS. A shared domain alone never merges.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass, field
from difflib import SequenceMatcher

from corpdisc.config import DEFAULT_SHARED_PLATFORM_DOMAINS, Settings
from corpdisc.contracts.common import Signal
from corpdisc.contracts.postgate import MatchResult


@dataclass(frozen=True)
class MatchCandidate:
    name: str
    normalized_name: str
    parent_entity_id: int | None
    registrable_domain: str | None = None
    country: str | None = None
    industry: str | None = None
    aliases: tuple[str, ...] = field(default=())  # normalized


def _same(a: str | None, b: str | None) -> bool | None:
    if not a or not b:
        return None
    return a.strip().casefold() == b.strip().casefold()


def compute_signals(a: MatchCandidate, b: MatchCandidate, *, same_parent: bool) -> list[Signal]:
    """Pairwise signals used by the ladder and handed to the Dedup Agent (22.9)."""
    out: list[Signal] = []
    if a.name == b.name:
        out.append(Signal(name="NAME_EXACT", value=True))
    if a.normalized_name == b.normalized_name:
        out.append(Signal(name="NAME_NORMALIZED", value=True))
    else:
        out.append(Signal(name="NAME_FUZZY",
                          value=round(SequenceMatcher(None, a.normalized_name, b.normalized_name).ratio(), 4)))
    if a.normalized_name in b.aliases or b.normalized_name in a.aliases:
        out.append(Signal(name="ALIAS", value=True))
    dom = _same(a.registrable_domain, b.registrable_domain)
    if dom is not None:
        out.append(Signal(name="DOMAIN_SAME" if dom else "DOMAIN_DIFF", value=True))
    out.append(Signal(name="PARENT_SAME" if same_parent else "PARENT_DIFF", value=True))
    country = _same(a.country, b.country)
    if country is not None:
        out.append(Signal(name="COUNTRY_SAME" if country else "COUNTRY_DIFF", value=True))
    industry = _same(a.industry, b.industry)
    if industry is not None:
        out.append(Signal(name="INDUSTRY_SAME" if industry else "INDUSTRY_DIFF", value=True))
    return out


class Matcher:
    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        run_id: int,
        fuzzy_threshold: float = 0.92,
        shared_platform_domains: Iterable[str] = DEFAULT_SHARED_PLATFORM_DOMAINS,
    ) -> None:
        self._conn = conn
        self._run_id = run_id
        self._fuzzy = fuzzy_threshold
        self._shared = frozenset(d.lower() for d in shared_platform_domains)

    @classmethod
    def from_settings(cls, conn: sqlite3.Connection, run_id: int, settings: Settings) -> Matcher:
        return cls(conn, run_id=run_id, fuzzy_threshold=settings.fuzzy_match_threshold,
                   shared_platform_domains=settings.shared_platform_domains)

    # ------------------------------------------------------------------ queries
    def _live(self) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT entity_id, name, normalized_name, country, industry FROM entities "
            "WHERE run_id = ? AND merged_into_id IS NULL ORDER BY entity_id",
            (self._run_id,),
        ).fetchall()

    def _has_parent(self, entity_id: int, parent_id: int | None) -> bool:
        if parent_id is None:
            return False
        return self._conn.execute(
            "SELECT 1 FROM relationships WHERE parent_id = ? AND child_id = ? AND verification_status <> 'REJECTED'",
            (parent_id, entity_id),
        ).fetchone() is not None

    def _lineage(self, parent: int | None) -> list[int]:
        """The parent and its ancestors over non-rejected edges (their domains are never 'dedicated')."""
        if parent is None:
            return []
        rows = self._conn.execute(
            "WITH RECURSIVE up(id) AS (SELECT ? UNION SELECT r.parent_id FROM relationships r JOIN up "
            "ON r.child_id = up.id WHERE r.verification_status <> 'REJECTED') SELECT id FROM up",
            (parent,),
        ).fetchall()
        return [int(r[0]) for r in rows]

    def _domains_of(self, entity_id: int) -> set[str]:
        rows = self._conn.execute("SELECT registrable_domain FROM websites WHERE entity_id = ?", (entity_id,))
        return {r[0].lower() for r in rows if r[0]}

    def _aliases_of(self, entity_id: int) -> set[str]:
        rows = self._conn.execute("SELECT normalized_alias FROM entity_aliases WHERE entity_id = ?", (entity_id,))
        return {r[0] for r in rows}

    def _card(self, row: sqlite3.Row) -> MatchCandidate:
        doms = sorted(self._domains_of(row["entity_id"]))
        return MatchCandidate(name=row["name"], normalized_name=row["normalized_name"], parent_entity_id=None,
                              registrable_domain=doms[0] if doms else None, country=row["country"],
                              industry=row["industry"], aliases=tuple(sorted(self._aliases_of(row["entity_id"]))))

    def _result(self, outcome: str, step: int | None, ids: list[int], cand: MatchCandidate,
                reason: str, live: dict[int, sqlite3.Row]) -> MatchResult:
        ids = sorted(set(ids))
        signals: tuple[Signal, ...] = ()
        if ids:
            first = ids[0]
            signals = tuple(compute_signals(cand, self._card(live[first]),
                                            same_parent=self._has_parent(first, cand.parent_entity_id)))
        if outcome == "MATCH" and len(ids) > 1:
            outcome, reason = "AMBIGUOUS", f"step {step}: {len(ids)} entities matched decisively"
        return MatchResult(
            outcome=outcome,  # type: ignore[arg-type]
            existing_entity_id=ids[0] if outcome == "MATCH" else None,
            step=step, signals=signals,
            ambiguous_entity_ids=tuple(ids) if outcome == "AMBIGUOUS" else (),
            reason=reason,
        )

    # ------------------------------------------------------------------ ladder
    def find_match(self, cand: MatchCandidate) -> MatchResult:
        rows = self._live()
        live = {r["entity_id"]: r for r in rows}
        parent = cand.parent_entity_id
        lineage = self._lineage(parent)
        parent_domains: set[str] = set().union(*(self._domains_of(e) for e in lineage)) if lineage else set()

        # 1. dedicated official domain
        dom = (cand.registrable_domain or "").lower()
        if dom and dom not in self._shared and dom not in parent_domains:
            owners = sorted({
                r[0] for r in self._conn.execute(
                    "SELECT w.entity_id FROM websites w JOIN entities e ON e.entity_id = w.entity_id "
                    "WHERE e.run_id = ? AND e.merged_into_id IS NULL AND lower(w.registrable_domain) = ?",
                    (self._run_id, dom),
                )
            })  # fmt: skip
            if len(owners) == 1 and owners[0] not in lineage:
                return self._result("MATCH", 1, owners, cand, f"dedicated domain {dom}", live)

        exact = [r["entity_id"] for r in rows if r["normalized_name"] == cand.normalized_name]
        # 2. exact normalized name + same parent
        same_parent = [e for e in exact if self._has_parent(e, parent)]
        if same_parent:
            return self._result("MATCH", 2, same_parent, cand, "same name and same parent", live)

        # 3. alias hit (either direction) + same parent or same country
        cand_aliases = set(cand.aliases)
        alias_hits = [
            r["entity_id"] for r in rows
            if r["entity_id"] != parent
            and (cand.normalized_name in self._aliases_of(r["entity_id"]) or r["normalized_name"] in cand_aliases)
            and (self._has_parent(r["entity_id"], parent) or bool(_same(r["country"], cand.country)))
        ]  # fmt: skip
        if alias_hits:
            return self._result("MATCH", 3, alias_hits, cand, "alias with same parent or country", live)

        # 4. exact name, different parent
        others = [e for e in exact if e != parent]
        if others:
            return self._result("AMBIGUOUS", 4, others, cand, "same name under a different parent", live)

        # 5. fuzzy
        fuzzy: list[int] = []
        for r in rows:
            if r["entity_id"] == parent:
                continue
            sm = SequenceMatcher(None, cand.normalized_name, r["normalized_name"], autojunk=False)
            if sm.real_quick_ratio() >= self._fuzzy and sm.quick_ratio() >= self._fuzzy and sm.ratio() >= self._fuzzy:
                fuzzy.append(r["entity_id"])
        if fuzzy:
            return self._result("AMBIGUOUS", 5, fuzzy, cand, f"fuzzy name >= {self._fuzzy}", live)
        return self._result("NO_MATCH", 6, [], cand, "no match", live)
