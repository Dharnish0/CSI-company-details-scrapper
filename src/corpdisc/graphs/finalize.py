"""finalize: deterministic-first integrity pass over the whole run (ARCHITECTURE 24.4).

finalize_init -> global_dedupe -> apply_merges -> run_checks -> validation_agent -> apply_outcomes -> finish

* ``global_dedupe``: the deterministic ladder decides first. Same name under the same parent, or one dedicated
  domain owned by two unrelated entities, merges without any LLM. Same name under different parents and fuzzy
  matches are AMBIGUOUS: only those go to the Deduplication Agent, and they stay separate (with a review item)
  unless the agent says SAME at >= DEDUP_MERGE_CONF with no conflicting signal.
* ``run_checks``: ValidationEngine, always before any semantic review. ERROR findings cannot be overridden.
* ``validation_agent``: reviews non-ERROR suspects only; it can demote, never promote.
* ``apply_outcomes``: deterministic failures (orphans) and agent DEMOTE/NEEDS_HUMAN >= VALIDATION_MIN_CONF are
  applied through the Committer; descendants of a demoted entity become REVIEW_REQUIRED (reason ORPHAN).

No LLM is required: with the agents absent, unavailable or out of budget the deterministic path stands.
Tree IDs and Excel export belong to M9 and are not run here.
"""

from __future__ import annotations

import operator
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Annotated, Any, Literal

from langgraph.graph import END, StateGraph
from pydantic import BaseModel, Field

from corpdisc.agents.base import make_correlation_id
from corpdisc.contracts.agent_io import DuplicatePair, ValidationInput
from corpdisc.contracts.agents import AgentRequest, CallLimits, CallStatus, Meta
from corpdisc.contracts.postgate import (
    CommitPlan,
    CommitRejected,
    DemotionWrite,
    EntityRef,
    FindingWrite,
    MergeWrite,
    ReviewItemWrite,
)
from corpdisc.graphs.deps import ResearchDeps
from corpdisc.graphs.support import DedupService, engine_for
from corpdisc.logging_setup import get_logger
from corpdisc.models.domain import ReviewReason
from corpdisc.services.matcher import MatchCandidate, compute_signals
from corpdisc.services.normalize import normalize_name
from corpdisc.services.validation_engine import Suspect

log = get_logger("graphs.finalize")


class FinalizeState(BaseModel):
    run_id: int
    decisive: list[list[Any]] = Field(default_factory=list)  # [keep, drop, reason]
    ambiguous: list[list[Any]] = Field(default_factory=list)  # [a, b, reason]
    merges: int = 0
    unsure: list[list[Any]] = Field(default_factory=list)
    findings: Annotated[list[dict], operator.add] = Field(default_factory=list)
    suspects: list[dict] = Field(default_factory=list)
    judgements: list[dict] = Field(default_factory=list)
    demotions: int = 0
    cascade: int = 0
    violations: list[str] = Field(default_factory=list)
    outcome: Literal["CLEAN", "FINDINGS", "INVARIANT_VIOLATIONS"] | None = None


@dataclass(frozen=True)
class FinalizeResult:
    outcome: str
    violations: tuple[str, ...]
    findings: int
    confirmed: int
    review_required: int
    rejected: int
    merges: int = 0
    demotions: int = 0
    cascade_demotions: int = 0
    semantic_review: bool = False


class FinalizeGraph:
    def __init__(self, deps: ResearchDeps) -> None:
        self.d = deps
        self.dedup = DedupService()
        self._semantic = False
        g = StateGraph(FinalizeState)
        names = ["finalize_init", "global_dedupe", "apply_merges", "run_checks", "validation_agent", "apply_outcomes",
                 "finish"]
        for n in names:
            g.add_node(n, getattr(self, f"n_{n}"))
        g.set_entry_point(names[0])
        for a, b in zip(names, names[1:]):
            g.add_edge(a, b)
        g.add_edge("finish", END)
        self.graph = g.compile()

    def run(self) -> FinalizeResult:
        self._semantic = False
        st = FinalizeState(**self.graph.invoke(FinalizeState(run_id=self.d.run_id)))
        counts = dict(self.d.conn.execute("SELECT verification_status, COUNT(*) FROM entities WHERE run_id=? "
                                          "AND merged_into_id IS NULL GROUP BY 1", (self.d.run_id,)).fetchall())
        return FinalizeResult(st.outcome or "CLEAN", tuple(st.violations), len(st.findings), counts.get("CONFIRMED", 0),
                              counts.get("REVIEW_REQUIRED", 0), counts.get("REJECTED", 0), st.merges, st.demotions,
                              st.cascade, self._semantic)

    # ------------------------------------------------------------------ nodes
    def n_finalize_init(self, s: FinalizeState) -> dict:
        return {}

    def _related(self, a: int, b: int) -> bool:
        cyc = self.d.cycle
        return b in cyc.ancestors(a, include_review=True) or a in cyc.ancestors(b, include_review=True)

    def n_global_dedupe(self, s: FinalizeState) -> dict:
        d, conn = self.d, self.d.conn
        rows = conn.execute(
            "SELECT entity_id, name, normalized_name FROM entities WHERE run_id=? AND merged_into_id IS NULL "
            "ORDER BY discovery_seq", (d.run_id,)).fetchall()
        root = rows[0]["entity_id"] if rows else None
        rows = [r for r in rows if r["entity_id"] != root]
        shared = {x.lower() for x in d.settings.shared_platform_domains}
        decisive: dict[tuple[int, int], str] = {}
        ambiguous: dict[tuple[int, int], str] = {}

        def parents(eid: int) -> set[int]:
            return {r[0] for r in conn.execute("SELECT parent_id FROM relationships WHERE child_id=? AND "
                                               "verification_status<>'REJECTED'", (eid,))}

        by_dom: dict[str, set[int]] = {}
        for r in conn.execute("SELECT w.entity_id, lower(w.registrable_domain) dom FROM websites w JOIN entities e "
                              "ON e.entity_id=w.entity_id WHERE e.run_id=? AND e.merged_into_id IS NULL", (d.run_id,)):
            if r["dom"] not in shared and r["entity_id"] != root:
                by_dom.setdefault(r["dom"], set()).add(r["entity_id"])
        for dom, ids in by_dom.items():
            ordered = sorted(ids)
            for i, a in enumerate(ordered):
                for b in ordered[i + 1:]:
                    if not self._related(a, b):
                        decisive[(a, b)] = f"dedicated domain {dom} owned by both"
        for i, a in enumerate(rows):
            for b in rows[i + 1:]:
                key = (a["entity_id"], b["entity_id"])
                if key in decisive or self._related(*key):
                    continue
                if a["normalized_name"] == b["normalized_name"]:
                    if parents(key[0]) & parents(key[1]):
                        decisive[key] = "same name under the same parent"
                    else:
                        ambiguous[key] = "same name under different parents"
                else:
                    sm = SequenceMatcher(None, a["normalized_name"], b["normalized_name"], autojunk=False)
                    if sm.real_quick_ratio() >= d.settings.fuzzy_match_threshold and sm.ratio() >= d.settings.fuzzy_match_threshold:
                        ambiguous[key] = f"fuzzy name similarity {sm.ratio():.2f}"
        return {"decisive": [[a, b, why] for (a, b), why in decisive.items()],
                "ambiguous": [[a, b, why] for (a, b), why in ambiguous.items()]}

    def _commit_merge(self, keep: int, drop: int, why: str) -> bool:
        res = self.d.committer.commit(CommitPlan(run_id=self.d.run_id, merges=(MergeWrite(keep_id=keep, drop_id=drop, reason=why),)))
        if isinstance(res, CommitRejected):
            log.warning("merge %s->%s rejected: %s", drop, keep, res.failed_invariant)
            return False
        return True

    def n_apply_merges(self, s: FinalizeState) -> dict:
        d = self.d
        merged: set[int] = set()
        n = 0
        for keep, drop, why in s.decisive:  # deterministic ladder: no LLM involved
            if keep in merged or drop in merged:
                continue
            if self._commit_merge(keep, drop, why):
                merged.add(drop)
                n += 1
        eng = engine_for(d)
        pairs, meta = [], {}
        for a, b, why in s.ambiguous:
            if a in merged or b in merged or len(pairs) >= 20:
                continue
            ca, cb = eng.card(a), eng.card(b)
            sa = MatchCandidate(ca.name, normalize_name(ca.name), None, ca.domains[0] if ca.domains else None, ca.country, ca.industry)
            sb = MatchCandidate(cb.name, normalize_name(cb.name), None, cb.domains[0] if cb.domains else None, cb.country, cb.industry)
            pid = len(pairs) + 1
            pairs.append(DuplicatePair(pair_id=pid, a=ca, b=cb, signals=compute_signals(sa, sb, same_parent=False)))
            meta[pid] = (a, b, why)
        verdicts = self.dedup.verdicts(d, pairs) if pairs else {}
        unsure, reviews = [], []
        for pid, verdict in verdicts.items():
            a, b, why = meta[pid]
            if verdict == "SAME" and a not in merged and b not in merged and self._commit_merge(a, b, f"agent: {why}"):
                merged.add(b)
                n += 1
            elif verdict != "DIFFERENT":
                unsure.append([a, b, why])
                reviews.append(ReviewItemWrite(
                    entity=EntityRef(entity_id=b), reason_code=ReviewReason.AMBIGUOUS_NAME,
                    reason_text=f"possible duplicate of entity {a}: {why}"[:500],
                    recommended_action="Decide manually whether these two entities are the same organisation."))
        if reviews:
            d.committer.commit(CommitPlan(run_id=d.run_id, review_items=tuple(reviews)))
        return {"merges": n, "unsure": unsure}

    def n_run_checks(self, s: FinalizeState) -> dict:
        res = engine_for(self.d).run()  # deterministic validation ALWAYS precedes semantic validation
        return {"findings": [{"check_name": f.check_name, "severity": f.severity, "entity_id": f.entity_id,
                              "details": f.details} for f in res.findings],
                "suspects": [{"entity_id": x.entity_id, "check_name": x.check_name, "findings": list(x.findings),
                              "has_error": x.has_error} for x in res.suspects],
                "violations": [f"{f.check_name}: {f.details}" for f in res.violations]}

    def n_validation_agent(self, s: FinalizeState) -> dict:
        d, agent = self.d, self.d.agents.get("validation")
        todo = [x for x in s.suspects if not x["has_error"]]  # ERROR suspects are already decided by the engine
        if not todo:
            return {}
        eng = engine_for(d)
        cases = eng.cases([Suspect(x["entity_id"], x["check_name"], tuple(x["findings"])) for x in todo])
        if d.budget.degradation_level() != "NORMAL":  # from 80%: only the lowest-confidence suspects (27.3)
            cases = sorted(cases, key=lambda c: c.path_confidence)[:5]
        unreviewed = [{"check_name": "NOT_SEMANTICALLY_REVIEWED", "severity": "WARN", "entity_id": c.entity.entity_id,
                       "details": f"{c.check_name}: not semantically reviewed"} for c in cases]
        if agent is None or not cases:
            return {"findings": unreviewed}
        meta = Meta(run_id=d.run_id, task_id=None, correlation_id=make_correlation_id(d.run_id, None, "validation"),
                    agent=agent.name, schema_version=agent.schema_version, prompt_version=agent.prompt_version)
        resp = d.runner.invoke(agent, AgentRequest(meta=meta, limits=CallLimits(max_output_tokens=agent.max_output_tokens),
                                                   payload=ValidationInput(cases=cases)))
        if resp.status is not CallStatus.OK or resp.payload is None:
            return {"findings": unreviewed}
        self._semantic = True
        by_case = {c.case_id: c for c in cases}
        out = [{"entity_id": by_case[j.case_id].entity.entity_id, "verdict": j.verdict, "conf": j.agent_confidence,
                "reason_code": j.reason_code.value if j.reason_code else None, "reason": j.reason,
                "action": j.recommended_manual_action}
               for j in resp.payload.judgements if j.case_id in by_case]  # G1: unknown case ids are dropped
        return {"judgements": out}

    def n_apply_outcomes(self, s: FinalizeState) -> dict:
        d, eng = self.d, engine_for(self.d)
        plan: dict[int, DemotionWrite] = {}
        for f in s.findings:  # deterministic failures stand regardless of any agent verdict
            if f["check_name"] == "ORPHAN" and f.get("entity_id"):
                plan[f["entity_id"]] = DemotionWrite(entity_id=f["entity_id"], reason_code=ReviewReason.ORPHAN,
                                                     reason_text=f["details"][:500],
                                                     recommended_action="Find a verified parent or reject this entity.")
        for j in s.judgements:
            if j["verdict"] in ("DEMOTE", "NEEDS_HUMAN") and j["conf"] >= d.settings.validation_min_conf:
                code = ReviewReason(j["reason_code"]) if j["reason_code"] else ReviewReason.LOW_CONFIDENCE
                plan.setdefault(j["entity_id"], DemotionWrite(entity_id=j["entity_id"], reason_code=code,
                                                              reason_text=j["reason"][:500] or "validation",
                                                              recommended_action=j["action"][:500] or "Review manually."))
        live = {r[0] for r in d.conn.execute("SELECT entity_id FROM entities WHERE run_id=? AND verification_status='CONFIRMED' "
                                             "AND merged_into_id IS NULL", (d.run_id,))}
        plan = {k: v for k, v in plan.items() if k in live}  # demotion only ever lowers a CONFIRMED entity
        cascade = 0
        for eid in list(plan):
            for desc in eng.descendants(eid):
                if desc not in plan:
                    plan[desc] = DemotionWrite(entity_id=desc, reason_code=ReviewReason.ORPHAN,
                                               reason_text=f"ancestor {eid} was demoted; its path to the root is unverified",
                                               recommended_action="Re-verify after the ancestor is resolved.")
                    cascade += 1
        extra: list[dict] = []
        applied = len(plan) - cascade
        if plan:
            res = d.committer.commit(CommitPlan(run_id=d.run_id, demotions=tuple(plan.values())))
            if isinstance(res, CommitRejected):
                log.warning("demotions rejected: %s", res.failed_invariant)
                extra.append({"check_name": "DEMOTION_REJECTED", "severity": "ERROR", "entity_id": None,
                              "details": res.message[:400]})
                applied = cascade = 0
        all_findings = [*s.findings, *extra]
        if all_findings:
            d.committer.commit(CommitPlan(run_id=d.run_id, findings=tuple(
                FindingWrite(check_name=f["check_name"][:60], severity=f["severity"], entity_id=f.get("entity_id"),
                             details=f["details"][:500]) for f in all_findings)))
        return {"demotions": applied, "cascade": cascade, "findings": extra}

    def n_finish(self, s: FinalizeState) -> dict:
        final = engine_for(self.d).run()  # re-check after outcomes were applied
        viol = [f"{f.check_name}: {f.details}" for f in final.violations]
        out = "INVARIANT_VIOLATIONS" if viol else ("FINDINGS" if s.findings or final.findings else "CLEAN")
        return {"violations": viol, "outcome": out}
