"""finalize: deterministic integrity pass over the whole run (ARCHITECTURE 24.4, M4 subset).

finalize_init -> run_checks -> apply_outcomes -> finish. No LLM, no Excel. ``run_checks`` re-runs the Committer
invariants on the whole run and adds graph checks (cycles, orphans, duplicates). Findings are recorded through
the Committer. Demotion cascades and semantic validation belong to the Validation Agent (M7).
"""

from __future__ import annotations

import operator
from dataclasses import dataclass
from typing import Annotated, Literal

from langgraph.graph import END, StateGraph
from pydantic import BaseModel, Field

from corpdisc.contracts.postgate import CommitPlan, FindingWrite
from corpdisc.graphs.deps import ResearchDeps


class FinalizeState(BaseModel):
    run_id: int
    findings: Annotated[list[dict], operator.add] = Field(default_factory=list)
    violations: Annotated[list[str], operator.add] = Field(default_factory=list)
    outcome: Literal["CLEAN", "FINDINGS", "INVARIANT_VIOLATIONS"] | None = None


@dataclass(frozen=True)
class FinalizeResult:
    outcome: str
    violations: tuple[str, ...]
    findings: int
    confirmed: int
    review_required: int
    rejected: int


class FinalizeGraph:
    def __init__(self, deps: ResearchDeps) -> None:
        self.d = deps
        g = StateGraph(FinalizeState)
        g.add_node("finalize_init", self.n_init)
        g.add_node("run_checks", self.n_checks)
        g.add_node("apply_outcomes", self.n_apply)
        g.add_node("finish", self.n_finish)
        g.set_entry_point("finalize_init")
        g.add_edge("finalize_init", "run_checks")
        g.add_edge("run_checks", "apply_outcomes")
        g.add_edge("apply_outcomes", "finish")
        g.add_edge("finish", END)
        self.graph = g.compile()

    def run(self) -> FinalizeResult:
        st = FinalizeState(**self.graph.invoke(FinalizeState(run_id=self.d.run_id)))
        c = self.d.conn
        counts = dict(c.execute("SELECT verification_status, COUNT(*) FROM entities WHERE run_id=? GROUP BY 1",
                                (self.d.run_id,)).fetchall())
        return FinalizeResult(st.outcome or "CLEAN", tuple(st.violations), len(st.findings),
                              counts.get("CONFIRMED", 0), counts.get("REVIEW_REQUIRED", 0), counts.get("REJECTED", 0))

    def n_init(self, s: FinalizeState) -> dict:
        return {}

    def n_checks(self, s: FinalizeState) -> dict:
        d, c = self.d, self.d.conn
        findings, violations = [], []
        for v in d.committer.check_invariants(None):
            violations.append(f"{v.code}: {v.message}")
            findings.append({"check_name": v.code, "severity": "ERROR", "entity_id": v.entity_id, "details": v.message})
        ids = [r[0] for r in c.execute("SELECT entity_id FROM entities WHERE run_id=?", (d.run_id,))]
        for e in d.cycle.has_cycle_through(ids, include_review=True):
            findings.append({"check_name": "CYCLE", "severity": "ERROR", "entity_id": e, "details": "entity is its own ancestor"})
            violations.append(f"CYCLE: entity {e}")
        for r in c.execute(
            "SELECT e.entity_id, e.name FROM entities e WHERE e.run_id=? AND e.verification_status='CONFIRMED' "
            "AND e.entity_id <> (SELECT entity_id FROM entities WHERE run_id=? ORDER BY discovery_seq LIMIT 1) "
            "AND NOT EXISTS (SELECT 1 FROM relationships r JOIN entities p ON p.entity_id=r.parent_id "
            "WHERE r.child_id=e.entity_id AND r.verification_status='CONFIRMED' AND p.verification_status='CONFIRMED')",
            (d.run_id, d.run_id)):
            findings.append({"check_name": "ORPHAN", "severity": "ERROR", "entity_id": r["entity_id"],
                             "details": f"{r['name']!r} is CONFIRMED but has no confirmed parent chain"})
        for r in c.execute(
            "SELECT e.normalized_name, x.parent_id, COUNT(*) n FROM entities e JOIN relationships x ON x.child_id=e.entity_id "
            "WHERE e.run_id=? AND e.merged_into_id IS NULL AND x.verification_status<>'REJECTED' "
            "GROUP BY e.normalized_name, x.parent_id HAVING n>1", (d.run_id,)):
            findings.append({"check_name": "DUPLICATE", "severity": "WARN", "entity_id": r["parent_id"],
                             "details": f"{r['n']} entities named {r['normalized_name']!r} under one parent"})
        for r in c.execute("SELECT entity_id, name FROM entities WHERE run_id=? AND crawl_state='UNREACHABLE'", (d.run_id,)):
            findings.append({"check_name": "BROKEN_SITE", "severity": "WARN", "entity_id": r["entity_id"],
                             "details": f"{r['name']!r} website was unreachable"})
        return {"findings": findings, "violations": violations}

    def n_apply(self, s: FinalizeState) -> dict:
        if s.findings:
            self.d.committer.commit(CommitPlan(run_id=self.d.run_id, findings=tuple(
                FindingWrite(check_name=f["check_name"], severity=f["severity"], entity_id=f.get("entity_id"),
                             details=f["details"][:500]) for f in s.findings)))
        return {}

    def n_finish(self, s: FinalizeState) -> dict:
        out = "INVARIANT_VIOLATIONS" if s.violations else ("FINDINGS" if s.findings else "CLEAN")
        return {"outcome": out}
