"""Committer: the ONLY writer of accepted business outcomes (ARCHITECTURE 26.2, 26.6; plan 4.M1.11).

``commit(plan)`` applies a whole ``CommitPlan`` in one ``BEGIN IMMEDIATE`` transaction, then checks the
invariants on everything it touched. Any violation or integrity error rolls the transaction back and
returns ``CommitRejected`` (the database is unchanged). It never raises for bad plans.

Invariants: INV1 confirmed relationship has verified first-party/official OWNERSHIP evidence; INV2
verified evidence quote is still in the stored page text; INV3 no cycle in confirmed edges; INV4 a
confirmed non-root entity has exactly one confirmed primary parent; INV5 nothing confirmed touches a
merged entity and no confirmed entity is ``Unknown``; INV6 (if required) confirmed non-root entities have
their own primary website; INV_CONF confirmed confidence >= threshold; INV_EVIDENCE verified evidence
needs a page; INV_PLAN plan-level misuse (run mismatch, root misuse); INV_DUPLICATE same name already
under the same parent.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Collection
from typing import Final

from corpdisc.clock import Clock, iso_utc
from corpdisc.config import Settings
from corpdisc.contracts.common import CONFIRMING_SOURCE_CLASSES
from corpdisc.contracts.postgate import (
    CommitPlan,
    CommitRejected,
    CommitResult,
    EntityRef,
    InvariantViolation,
)
from corpdisc.logging_setup import get_logger
from corpdisc.models.domain import EntityType, VerificationStatus
from corpdisc.services.cycle_guard import CycleGuard
from corpdisc.services.queue import insert_task
from corpdisc.services.quotes import QuoteVerifier

log = get_logger("services.committer")

PageTextLoader = Callable[[int], str | None]
_CONFIRMED: Final = VerificationStatus.CONFIRMED.value
_CONFIRMING: Final = tuple(sorted(s.value for s in CONFIRMING_SOURCE_CLASSES))


class CommitError(RuntimeError):
    """Raised only by ``commit_or_raise``; ``commit`` returns ``CommitRejected`` instead."""

    def __init__(self, rejected: CommitRejected) -> None:
        super().__init__(f"{rejected.failed_invariant}: {rejected.message}")
        self.rejected = rejected


class _Abort(Exception):
    def __init__(self, violations: list[InvariantViolation], duplicate_of: int | None = None) -> None:
        super().__init__(violations[0].message if violations else "aborted")
        self.violations = violations
        self.duplicate_of = duplicate_of


def _v(code: str, message: str, entity_id: int | None = None) -> InvariantViolation:
    return InvariantViolation(code=code, message=message[:500], entity_id=entity_id)


class Committer:
    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        run_id: int,
        page_text: PageTextLoader,
        clock: Clock,
        verifier: QuoteVerifier | None = None,
        cycle_guard: CycleGuard | None = None,
        require_website_for_confirmed: bool = True,
        confirm_threshold: float = 0.75,
    ) -> None:
        self._conn = conn
        self._run_id = run_id
        self._page_text = page_text
        self._clock = clock
        self._verifier = verifier or QuoteVerifier()
        self._cycles = cycle_guard or CycleGuard(conn)
        self._require_website = require_website_for_confirmed
        self._confirm = confirm_threshold

    @classmethod
    def from_settings(cls, conn: sqlite3.Connection, *, run_id: int, page_text: PageTextLoader,
                      settings: Settings, clock: Clock) -> Committer:
        return cls(conn, run_id=run_id, page_text=page_text, clock=clock,
                   require_website_for_confirmed=settings.require_website_for_confirmed,
                   confirm_threshold=settings.confirm_threshold)

    # ------------------------------------------------------------------ public API
    def commit(self, plan: CommitPlan) -> CommitResult | CommitRejected:
        if plan.run_id != self._run_id:
            return CommitRejected(failed_invariant="INV_PLAN",
                                  message=f"plan.run_id {plan.run_id} != committer run {self._run_id}")
        conn = self._conn
        if conn.in_transaction:
            conn.commit()
        try:
            conn.execute("BEGIN IMMEDIATE")
            result, touched = self._apply(plan)
            violations = self.check_invariants(touched)
            if violations:
                raise _Abort(violations)
            conn.commit()
        except _Abort as exc:
            conn.rollback()
            first = exc.violations[0]
            log.info("commit rejected run=%s task=%s %s: %s", plan.run_id, plan.task_id, first.code, first.message)
            return CommitRejected(failed_invariant=first.code, message=first.message,
                                  violations=tuple(exc.violations), duplicate_of=exc.duplicate_of)
        except sqlite3.IntegrityError as exc:
            conn.rollback()
            msg = str(exc)
            code = "INV4" if "idx_one_primary_parent" in msg or "relationships.child_id" in msg else "INV_FIELDS"
            log.info("commit rejected run=%s task=%s integrity: %s", plan.run_id, plan.task_id, msg)
            return CommitRejected(failed_invariant=code, message=f"integrity error: {msg}"[:500])
        except Exception:
            conn.rollback()
            raise
        return result

    def commit_or_raise(self, plan: CommitPlan) -> CommitResult:
        out = self.commit(plan)
        if isinstance(out, CommitRejected):
            raise CommitError(out)
        return out

    # ------------------------------------------------------------------ apply
    def _root_id(self) -> int | None:
        row = self._conn.execute(
            "SELECT entity_id FROM entities WHERE run_id = ? ORDER BY discovery_seq, entity_id LIMIT 1",
            (self._run_id,),
        ).fetchone()
        return None if row is None else int(row[0])

    def _resolve(self, ref: EntityRef | None, ids: dict[str, int]) -> int | None:
        if ref is None:
            return None
        if ref.entity_id is not None:
            row = self._conn.execute("SELECT run_id FROM entities WHERE entity_id = ?", (ref.entity_id,)).fetchone()
            if row is None or row[0] != self._run_id:
                raise _Abort([_v("INV_PLAN", f"entity {ref.entity_id} is not in run {self._run_id}")])
            return ref.entity_id
        return ids[ref.local_ref]  # type: ignore[index]  # plan validator guarantees presence

    def _apply(self, plan: CommitPlan) -> tuple[CommitResult, set[int]]:
        c, now = self._conn, iso_utc(self._clock.now())
        ent: dict[str, int] = {}
        web: dict[str, int] = {}
        rel: dict[str, int] = {}
        touched: set[int] = set()
        new_seq: dict[int, int] = {}

        roots = [e for e in plan.entities if e.is_root]
        existing_root = self._root_id()
        if roots and (len(roots) > 1 or existing_root is not None):
            raise _Abort([_v("INV_PLAN", "a run has exactly one root and it must be the first entity")])
        if existing_root is None and plan.entities and not plan.entities[0].is_root:
            raise _Abort([_v("INV_PLAN", "the first entity committed for a run must be flagged is_root")])

        seq = int(c.execute("SELECT COALESCE(MAX(discovery_seq), 0) FROM entities WHERE run_id = ?",
                            (self._run_id,)).fetchone()[0])
        for e in plan.entities:
            if e.is_root and (e.entity_type is not EntityType.PARENT_COMPANY
                              or e.verification_status is not VerificationStatus.CONFIRMED):
                raise _Abort([_v("INV_PLAN", "the root must be a CONFIRMED Parent Company")])
            seq += 1
            cur = c.execute(
                "INSERT INTO entities (run_id, name, normalized_name, entity_type, description, country, industry, "
                "operating_status, verification_status, classification_confidence, confidence, crawl_state, "
                "discovery_method, discovery_seq, sibling_order, discovered_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (self._run_id, e.name, e.normalized_name, e.entity_type.value, e.description, e.country, e.industry,
                 e.operating_status.value, e.verification_status.value, e.classification_confidence, e.confidence,
                 e.crawl_state.value, e.discovery_method, seq, e.sibling_order, now),
            )  # fmt: skip
            ent[e.local_ref] = int(cur.lastrowid)
            new_seq[ent[e.local_ref]] = seq
            touched.add(ent[e.local_ref])

        for u in plan.entity_updates:
            eid = self._resolve(EntityRef(entity_id=u.entity_id), ent)
            sets, vals = [], []
            for col, val in (("verification_status", u.verification_status), ("confidence", u.confidence),
                             ("entity_type", u.entity_type), ("crawl_state", u.crawl_state)):
                if val is not None:
                    sets.append(f"{col} = ?")
                    vals.append(val.value if hasattr(val, "value") else val)
            if sets:
                c.execute(f"UPDATE entities SET {', '.join(sets)} WHERE entity_id = ?", (*vals, eid))  # noqa: S608
                touched.add(eid)  # type: ignore[arg-type]

        for w in plan.websites:
            eid = self._resolve(w.entity, ent)
            cur = c.execute(
                "INSERT INTO websites (entity_id, url, domain, registrable_domain, kind, resolution_method, reachable, "
                "last_http_status, last_checked_at) VALUES (?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(entity_id, url) DO UPDATE SET reachable = excluded.reachable, "
                "last_http_status = excluded.last_http_status, last_checked_at = excluded.last_checked_at",
                (eid, w.url, w.domain, w.registrable_domain, w.kind.value, w.resolution_method,
                 None if w.reachable is None else int(w.reachable), w.last_http_status, now),
            )  # fmt: skip
            wid = cur.lastrowid or c.execute("SELECT website_id FROM websites WHERE entity_id=? AND url=?",
                                             (eid, w.url)).fetchone()[0]
            web[w.local_ref] = int(wid)
            if w.is_primary:
                c.execute("UPDATE entities SET primary_website_id = ? WHERE entity_id = ?", (wid, eid))
            touched.add(eid)  # type: ignore[arg-type]

        for a in plan.aliases:
            eid = self._resolve(a.entity, ent)
            c.execute("INSERT INTO entity_aliases (entity_id, alias, normalized_alias, source_page_id) VALUES (?,?,?,?) "
                      "ON CONFLICT(entity_id, normalized_alias) DO NOTHING",
                      (eid, a.alias, a.normalized_alias, a.source_page_id))

        duplicates: list[InvariantViolation] = []
        dup_of: int | None = None
        for r in plan.relationships:
            parent, child = self._resolve(r.parent, ent), self._resolve(r.child, ent)
            if r.child.local_ref is not None and r.parent.entity_id is not None:
                norm = next(e.normalized_name for e in plan.entities if e.local_ref == r.child.local_ref)
                hit = c.execute(
                    "SELECT e.entity_id FROM entities e JOIN relationships x ON x.child_id = e.entity_id "
                    "WHERE e.run_id = ? AND e.normalized_name = ? AND x.parent_id = ? AND e.entity_id <> ? "
                    "AND e.merged_into_id IS NULL AND x.verification_status <> 'REJECTED' LIMIT 1",
                    (self._run_id, norm, parent, child),
                ).fetchone()  # fmt: skip
                if hit is not None:
                    dup_of = int(hit[0])
                    duplicates.append(_v("INV_DUPLICATE", f"{norm!r} already exists under parent {parent}", child))
            cur = c.execute(
                "INSERT INTO relationships (parent_id, child_id, relationship_type, verification_status, confidence, "
                "is_primary, source_page_id, discovery_method, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (parent, child, r.relationship_type.value, r.verification_status.value, r.confidence,
                 int(r.is_primary), r.source_page_id, r.discovery_method, now),
            )  # fmt: skip
            rel[r.local_ref] = int(cur.lastrowid)
            touched.update((parent, child))  # type: ignore[arg-type]
        if duplicates:
            raise _Abort(duplicates, duplicate_of=dup_of)

        ev_ids: list[int] = []
        for ev in plan.evidence:
            eid = self._resolve(ev.entity, ent)
            rid = rel[ev.relationship_local_ref] if ev.relationship_local_ref else ev.relationship_id
            x = ev.evidence
            cur = c.execute(
                "INSERT INTO evidence (entity_id, relationship_id, page_id, url, quote, quote_verified, source_class, "
                "supports, invocation_id, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (eid, rid, x.page_id, x.url, x.quote, int(x.quote_verified), x.source_class.value, x.supports.value,
                 x.invocation_id, now),
            )  # fmt: skip
            ev_ids.append(int(cur.lastrowid))
            touched.add(eid)  # type: ignore[arg-type]

        cand_ids: list[int] = []
        for cw in plan.candidates:
            parent = self._resolve(cw.parent, ent)
            resolved = self._resolve(cw.resolved, ent)
            c.execute(
                "INSERT INTO candidates (run_id, source_page_id, parent_entity_id, raw_name, normalized_name, raw_link_url, "
                "context_snippet, dom_hint, candidate_kind, proposed_type, decision, resolved_entity_id, reason, "
                "confidence, extraction_invocation_id, gate_code) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(run_id, source_page_id, normalized_name) DO UPDATE SET decision = excluded.decision, "
                "resolved_entity_id = COALESCE(excluded.resolved_entity_id, candidates.resolved_entity_id), "
                "reason = excluded.reason, confidence = excluded.confidence, gate_code = excluded.gate_code, "
                "candidate_kind = excluded.candidate_kind, proposed_type = excluded.proposed_type, "
                "extraction_invocation_id = COALESCE(excluded.extraction_invocation_id, candidates.extraction_invocation_id)",
                (self._run_id, cw.source_page_id, parent, cw.raw_name, cw.normalized_name, cw.raw_link_url,
                 cw.context_snippet, cw.dom_hint, cw.candidate_kind.value if cw.candidate_kind else None,
                 cw.proposed_type.value if cw.proposed_type else None, cw.decision, resolved, cw.reason,
                 cw.confidence, cw.extraction_invocation_id, cw.gate_code),
            )  # fmt: skip
            cand_ids.append(int(c.execute(
                "SELECT candidate_id FROM candidates WHERE run_id=? AND source_page_id=? AND normalized_name=?",
                (self._run_id, cw.source_page_id, cw.normalized_name)).fetchone()[0]))

        review_ids: list[int] = []
        for ri in plan.review_items:
            cur = c.execute(
                "INSERT INTO review_items (run_id, entity_id, possible_parent_id, reason_code, reason_text, "
                "recommended_action, confidence, created_at) VALUES (?,?,?,?,?,?,?,?)",
                (self._run_id, self._resolve(ri.entity, ent), self._resolve(ri.possible_parent, ent),
                 ri.reason_code.value, ri.reason_text, ri.recommended_action, ri.confidence, now),
            )  # fmt: skip
            review_ids.append(int(cur.lastrowid))

        task_ids: list[int] = []
        for t in plan.enqueue:
            eid = self._resolve(t.entity, ent)
            dseq = new_seq.get(eid) or int(  # type: ignore[arg-type]
                c.execute("SELECT discovery_seq FROM entities WHERE entity_id = ?", (eid,)).fetchone()[0])
            tid = insert_task(c, run_id=self._run_id, entity_id=eid, start_url=t.start_url, depth=t.depth,  # type: ignore[arg-type]
                              discovery_seq=dseq, enqueued_at=self._clock.now())
            if tid is not None:
                task_ids.append(tid)

        rej_ids: list[int] = []
        for g in plan.gate_rejections:
            cur = c.execute(
                "INSERT INTO gate_rejections (run_id, invocation_id, gate_code, item_ref, detail, created_at) "
                "VALUES (?,?,?,?,?,?)", (self._run_id, g.invocation_id, g.gate_code, g.item_ref, g.detail, now))
            rej_ids.append(int(cur.lastrowid))

        for f in plan.findings:
            c.execute("INSERT INTO validation_findings (run_id, check_name, severity, entity_id, relationship_id, "
                      "details, action_taken, created_at) VALUES (?,?,?,?,?,?,?,?)",
                      (self._run_id, f.check_name, f.severity, f.entity_id, f.relationship_id, f.details,
                       f.action_taken, now))

        result = CommitResult(entity_ids=ent, website_ids=web, relationship_ids=rel, evidence_ids=tuple(ev_ids),
                              candidate_ids=tuple(cand_ids), review_ids=tuple(review_ids), task_ids=tuple(task_ids),
                              rejection_ids=tuple(rej_ids))
        return result, touched

    # ------------------------------------------------------------------ invariants
    def check_invariants(self, entity_ids: Collection[int] | None = None) -> list[InvariantViolation]:
        """Check the 26.6 invariants for ``entity_ids`` (None = the whole run). Read-only."""
        c = self._conn
        if entity_ids is None:
            ids = [r[0] for r in c.execute("SELECT entity_id FROM entities WHERE run_id = ?", (self._run_id,))]
        else:
            ids = sorted(set(entity_ids))
        if not ids:
            return []
        marks = ",".join("?" for _ in ids)
        out: list[InvariantViolation] = []
        root = self._root_id()

        rels = c.execute(
            f"SELECT relationship_id, parent_id, child_id, confidence FROM relationships "  # noqa: S608
            f"WHERE verification_status = ? AND (parent_id IN ({marks}) OR child_id IN ({marks}))",
            (_CONFIRMED, *ids, *ids),
        ).fetchall()
        cmarks = ",".join("?" for _ in _CONFIRMING)
        for r in rels:
            good = c.execute(
                f"SELECT 1 FROM evidence WHERE relationship_id = ? AND quote_verified = 1 AND supports = 'OWNERSHIP' "  # noqa: S608
                f"AND source_class IN ({cmarks}) LIMIT 1", (r["relationship_id"], *_CONFIRMING)).fetchone()
            if good is None:
                out.append(_v("INV1", f"confirmed relationship {r['relationship_id']} lacks verified ownership "
                                      "evidence from a first-party or official source", r["child_id"]))
            if r["confidence"] < self._confirm:
                out.append(_v("INV_CONF", f"confirmed relationship {r['relationship_id']} confidence "
                                          f"{r['confidence']} < {self._confirm}", r["child_id"]))
            merged = c.execute("SELECT 1 FROM entities WHERE entity_id IN (?, ?) AND merged_into_id IS NOT NULL",
                               (r["parent_id"], r["child_id"])).fetchone()
            if merged is not None:
                out.append(_v("INV5", f"confirmed relationship {r['relationship_id']} touches a merged entity",
                              r["child_id"]))

        for ev in c.execute(f"SELECT evidence_id, entity_id, page_id, quote FROM evidence "  # noqa: S608
                            f"WHERE quote_verified = 1 AND entity_id IN ({marks})", ids):
            if ev["page_id"] is None:
                out.append(_v("INV_EVIDENCE", f"verified evidence {ev['evidence_id']} has no page", ev["entity_id"]))
                continue
            text = self._page_text(ev["page_id"])
            if text is None or not self._verifier.check(ev["quote"], text).verified:
                out.append(_v("INV2", f"evidence {ev['evidence_id']} quote not found in stored text of page "
                                      f"{ev['page_id']}", ev["entity_id"]))

        cyc = self._cycles.has_cycle_through(ids)
        for e in cyc:
            out.append(_v("INV3", f"entity {e} is its own ancestor through confirmed edges", e))

        for e in c.execute(f"SELECT entity_id, entity_type, confidence, primary_website_id FROM entities "  # noqa: S608
                           f"WHERE verification_status = ? AND entity_id IN ({marks})", (_CONFIRMED, *ids)):
            eid = e["entity_id"]
            if e["entity_type"] == EntityType.UNKNOWN.value:
                out.append(_v("INV5", f"confirmed entity {eid} has entity_type Unknown", eid))
            if e["confidence"] is None or e["confidence"] < self._confirm:
                out.append(_v("INV_CONF", f"confirmed entity {eid} confidence {e['confidence']} < {self._confirm}",
                              eid))
            if eid == root:
                continue
            n = c.execute("SELECT COUNT(*) FROM relationships WHERE child_id = ? AND is_primary = 1 "
                          "AND verification_status = ?", (eid, _CONFIRMED)).fetchone()[0]
            if n != 1:
                out.append(_v("INV4", f"confirmed entity {eid} has {n} confirmed primary parents (need 1)", eid))
            if self._require_website:
                pw = e["primary_website_id"]
                own = pw is not None and c.execute("SELECT 1 FROM websites WHERE website_id = ? AND entity_id = ?",
                                                   (pw, eid)).fetchone() is not None
                if not own:
                    out.append(_v("INV6", f"confirmed entity {eid} has no primary website of its own", eid))
        return out
