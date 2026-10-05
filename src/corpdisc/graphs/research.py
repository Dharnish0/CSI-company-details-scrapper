"""research_entity: one task = one entity (ARCHITECTURE 24.3).

Deterministic nodes do all control, gating, scoring and committing; agent nodes only call
``AgentRunner.invoke`` and store the proposal. Graph state is small (ids, names, bounded structures);
page bodies stay in the page cache and bulky per-task data lives in ``deps.scratch``.
"""

from __future__ import annotations

import operator
import re
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Annotated, Any, Final, Literal

from bs4 import BeautifulSoup
from langgraph.graph import END, StateGraph
from pydantic import BaseModel, Field

from corpdisc.contracts.agent_io import (
    NON_ENTITY_KINDS,
    ChildProposal,
    DiscoveryInput,
    EntityCard,
    ExtractionInput,
    ExtractionOutput,
    PageExcerpt,
    RelationshipInput,
    WebsiteInput,
)
from corpdisc.contracts.agents import AgentRequest, AgentResponse, CallLimits, CallStatus, Meta
from corpdisc.contracts.common import PageType, SourceClass, Supports, VerifiedEvidence
from corpdisc.contracts.postgate import (
    AliasWrite,
    CandidateWrite,
    CommitPlan,
    CommitRejected,
    EntityRef,
    EntityUpdate,
    EntityWrite,
    EvidenceWrite,
    GateRejectionWrite,
    RelationshipWrite,
    ReviewItemWrite,
    WebsiteWrite,
)
from corpdisc.agents.base import make_correlation_id
from corpdisc.gates import (
    check_g1_id_integrity,
    check_g2_name_grounding,
    check_g3_quote_grounding,
    check_g4_scope,
    check_g5_platform_blocklist,
    check_g6_reachability,
    check_g7_identity_check,
    check_g8_url_canonical,
    check_g10_cycle,
    check_g11_evidence_requirement,
    check_g12_schema_enum,
    check_g14_website_requirement,
)
from corpdisc.graphs.deps import ResearchDeps
from corpdisc.graphs.grounding import ground_entities
from corpdisc.logging_setup import get_logger
from corpdisc.models.domain import (
    CandidateKind,
    CrawlState,
    EntityType,
    RelationshipType,
    ReviewReason,
    VerificationStatus,
    WebsiteKind,
)
from corpdisc.services.confidence import ConfidenceFlag, EvidenceInput
from corpdisc.services.dom_candidates import extract_dom_candidates
from corpdisc.services.links import build_link_index
from corpdisc.services.matcher import MatchCandidate
from corpdisc.services.normalize import normalize_name, normalize_text, registrable_domain
from corpdisc.services.urls import canonicalize_url

log = get_logger("graphs.research")

HIGH_BAND: Final = 0.60  # at or above: straight to the gate
LOW_BAND: Final = 0.20  # at or below: nothing relevant
EXTRACT_MIN_CONF: Final = 0.35
DISCOVERY_MIN_RELEVANCE: Final = 0.50
DISCOVERY_NONE_CONF: Final = 0.70
CHUNK_CHARS: Final = 6000
MAX_CHUNKS_PER_PAGE: Final = 6
REL_BATCH: Final = 15
WINDOW: Final = 1500
CRAWLABLE_TYPES: Final = frozenset({
    EntityType.COMPANY, EntityType.SUBSIDIARY, EntityType.OPERATING_GROUP, EntityType.BUSINESS_GROUP,
    EntityType.PORTFOLIO_COMPANY, EntityType.PORTFOLIO_GROUP, EntityType.ACQUIRED_COMPANY,
})  # fmt: skip
_OWNERSHIP_WORDING: Final = re.compile(
    r"\b(subsidiar\w*|owned|owns|acquired|division of|part of|a company of|member of|portfolio company|"
    r"operating group of|business group of|wholly)\b", re.I)
# Wording that describes something other than ownership. A verified quote containing it can never support CONFIRMED,
# whatever the agent says: the guard only moves toward REVIEW_REQUIRED (deterministic backstop, 20.3).
NEGATIVE_WORDING: Final = re.compile(
    r"\b(partners?|customers?|clients?|vendors?|suppliers?|resellers?|distributors?|competitors?|formerly|former|"
    r"previously|sold to|divested|spun[- ]off|minority (?:stake|interest)|investor in)\b", re.I)
Outcome = Literal["EXPANDED", "TERMINAL", "UNREACHABLE", "PARTIAL", "BUDGET_STOPPED", "SKIPPED", "NO_WEBSITE"]


# ------------------------------------------------------------------ state
class TaskCtx(BaseModel):
    run_id: int
    task_id: int
    entity_id: int
    entity_name: str = ""
    entity_type: str = ""
    start_url: str | None = None
    depth: int = 0
    parent_entity_id: int | None = None
    ancestry: list[int] = Field(default_factory=list)


class Cand(BaseModel):
    """A grounded extraction candidate moving through the graph (bounded text only)."""

    ref: str
    name: str
    norm: str
    page_id: int
    page_url: str
    chunk_key: str
    link_url: str | None = None
    raw_candidate_id: int | None = None
    dom_hint: str | None = None
    kind: str = CandidateKind.COMPANY.value
    proposed_type: str | None = None
    rel_hint: str | None = None
    description: str | None = None
    country: str | None = None
    industry: str | None = None
    ext_conf: float = 0.0
    claim: VerifiedEvidence | None = None
    sibling: int = 0
    # matching
    known_id: int | None = None
    ambiguous_ids: list[int] = Field(default_factory=list)
    duplicate_of: str | None = None
    dropped: str | None = None  # reason when the candidate goes no further
    cycle_with: int | None = None
    # website
    site_url: str | None = None
    site_kind: str | None = None
    site_method: str = "NONE"
    site_reachable: bool | None = None
    site_identity: bool | None = None
    site_status: int | None = None
    child_evidence: list[VerifiedEvidence] = Field(default_factory=list)
    # verification / score
    assess: dict[str, Any] | None = None
    rel_extra: list[VerifiedEvidence] = Field(default_factory=list)
    status: str | None = None
    rel_type: str = RelationshipType.UNKNOWN.value
    rel_conf: float = 0.0
    ent_conf: float = 0.0
    review_reason: str | None = None
    final_type: str | None = None


class ResearchState(BaseModel):
    task: TaskCtx
    route: str = "fetch_entry"
    page_ids: list[int] = Field(default_factory=list)
    entry_page_id: int | None = None
    selections: list[dict[str, Any]] = Field(default_factory=list)
    approved: list[dict[str, Any]] = Field(default_factory=list)
    cands: list[Cand] = Field(default_factory=list)
    committed_ids: list[int] = Field(default_factory=list)
    enqueued_task_ids: list[int] = Field(default_factory=list)
    partial: bool = False
    rel_unavailable: bool = False
    outcome: Outcome | None = None
    budget_scope: Literal["task", "run"] | None = None
    ref_ids: dict[str, int] = Field(default_factory=dict)
    errors: Annotated[list[str], operator.add] = Field(default_factory=list)
    invocation_ids: Annotated[list[int], operator.add] = Field(default_factory=list)
    rejections: Annotated[list[dict[str, Any]], operator.add] = Field(default_factory=list)
    cand_rows: Annotated[list[dict[str, Any]], operator.add] = Field(default_factory=list)
    reviews: Annotated[list[dict[str, Any]], operator.add] = Field(default_factory=list)


@dataclass(frozen=True)
class ResearchResult:
    outcome: str
    committed_entity_ids: tuple[int, ...]
    enqueued_task_ids: tuple[int, ...]
    errors: tuple[str, ...]
    budget_scope: str | None
    gate_rejections: int


@dataclass
class Scratch:
    html: str = ""
    base_url: str = ""
    link_index: Any = None
    page_indexes: dict[int, Any] = None  # type: ignore[assignment]
    chunks: dict[str, dict[str, Any]] = None  # type: ignore[assignment]
    extractions: list[tuple[str, ExtractionOutput]] = None  # type: ignore[assignment]
    parent_regs: set[str] = None  # type: ignore[assignment]
    hits: dict[str, dict[int, str]] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        self.page_indexes, self.chunks, self.extractions, self.parent_regs, self.hits = {}, {}, [], set(), {}


def _rej(gate: str, ref: str, detail: str, inv: int | None = None) -> dict[str, Any]:
    return {"gate_code": gate, "item_ref": str(ref)[:300], "detail": str(detail)[:500], "invocation_id": inv}


def _title(html: str | None) -> str | None:
    if not html:
        return None
    t = BeautifulSoup(html, "html.parser").find("title")
    return t.get_text(" ").strip() if t else None


# ------------------------------------------------------------------ graph
class ResearchGraph:
    NODES = ("claim_and_gate", "fetch_entry", "index_links", "route_discovery", "discovery_agent", "gate_pages",
             "fetch_pages", "build_extraction_inputs", "extraction_agent", "gate_extraction", "match_known",
             "route_website", "website_agent", "gate_website", "dedupe_resolve", "relationship_agent",
             "verify_and_score", "commit", "route_enqueue", "mark_terminal", "mark_unreachable", "finish")  # fmt: skip

    def __init__(self, deps: ResearchDeps) -> None:
        self.d = deps
        g = StateGraph(ResearchState)
        for name in self.NODES:
            g.add_node(name, self._wrap(getattr(self, f"n_{name}")))
            g.add_conditional_edges(name, lambda s: END if s.route == END else s.route,
                                    {**{n: n for n in self.NODES}, END: END})
        g.set_entry_point("claim_and_gate")
        self.graph = g.compile()

    @staticmethod
    def _wrap(fn):  # type: ignore[no-untyped-def]
        def node(state: ResearchState) -> dict[str, Any]:
            return fn(state)
        node.__name__ = fn.__name__
        return node

    # --------------------------------------------------------------- public
    def run(self, task: TaskCtx) -> ResearchResult:
        self.d.scratch[task.task_id] = Scratch()
        try:
            final = self.graph.invoke(ResearchState(task=task), {"recursion_limit": 60})
        finally:
            self.d.scratch.pop(task.task_id, None)
        st = final if isinstance(final, ResearchState) else ResearchState(**final)
        return ResearchResult(st.outcome or "TERMINAL", tuple(st.committed_ids), tuple(st.enqueued_task_ids),
                              tuple(st.errors), st.budget_scope, len(st.rejections))

    # --------------------------------------------------------------- helpers
    def sc(self, s: ResearchState) -> Scratch:
        return self.d.scratch[s.task.task_id]

    def card(self, entity_id: int | None, name: str, etype: str | None = None) -> EntityCard:
        if entity_id is None:
            return EntityCard(name=name, entity_type=EntityType(etype) if etype else None)
        row = self.d.conn.execute("SELECT name, entity_type, country, industry, verification_status "
                                  "FROM entities WHERE entity_id=?", (entity_id,)).fetchone()
        doms = [r[0] for r in self.d.conn.execute("SELECT registrable_domain FROM websites WHERE entity_id=?",
                                                  (entity_id,))]
        return EntityCard(entity_id=entity_id, name=row["name"], entity_type=EntityType(row["entity_type"]),
                          country=row["country"], industry=row["industry"], domains=doms[:10],
                          verification_status=VerificationStatus(row["verification_status"]))

    def request(self, s: ResearchState, agent: Any, payload: Any, stage: str) -> AgentRequest:
        t = s.task
        meta = Meta(run_id=t.run_id, task_id=t.task_id, correlation_id=make_correlation_id(t.run_id, t.task_id, stage),
                    agent=agent.name, schema_version=agent.schema_version, prompt_version=agent.prompt_version)
        return AgentRequest(meta=meta, limits=CallLimits(max_output_tokens=agent.max_output_tokens,
                                                          max_tool_calls=agent.max_tool_calls), payload=payload)

    def call(self, s: ResearchState, key: str, payload: Any, stage: str, feedback: str | None = None
             ) -> tuple[AgentResponse, dict[str, Any]]:
        agent = self.d.agents[key]
        resp = self.d.runner.invoke(agent, self.request(s, agent, payload, stage), feedback=feedback)
        extra: dict[str, Any] = {}
        if resp.status is not CallStatus.OK:
            extra["errors"] = [f"{key}:{resp.status}:{resp.error.kind if resp.error else ''}"]
        return resp, extra

    @staticmethod
    def budget_reason(resp: AgentResponse) -> str | None:
        if resp.status is CallStatus.SKIPPED and resp.error is not None:
            return resp.error.message
        return None

    def stop(self, reason: str, scope: Literal["task", "run"]) -> dict[str, Any]:
        return {"outcome": "BUDGET_STOPPED", "budget_scope": scope, "route": "finish", "errors": [reason]}

    def source_class(self, s: ResearchState, url: str) -> SourceClass:
        reg = registrable_domain(url)
        return SourceClass.FIRST_PARTY_PARENT if reg and reg in self.sc(s).parent_regs else SourceClass.THIRD_PARTY

    def max_links(self) -> int:
        n = self.d.settings.max_links_per_entity
        return max(1, n // 2) if self.d.budget.degradation_level() != "NORMAL" else n

    # --------------------------------------------------------------- nodes
    def n_claim_and_gate(self, s: ResearchState) -> dict[str, Any]:
        t, d = s.task, self.d
        row = d.conn.execute("SELECT name, entity_type, crawl_state, merged_into_id FROM entities WHERE entity_id=?",
                             (t.entity_id,)).fetchone()
        qt = d.queue.get(t.task_id)
        if row is None or row["merged_into_id"] is not None or qt is None or qt.status.value == "DONE":
            return {"outcome": "SKIPPED", "route": "finish", "errors": ["entity missing, merged or task done"]}
        if (v := d.budget.check_depth(t.depth)).allow is False:
            return self.stop(f"{v.reason}: {v.detail}", "task")
        anc = list(dict.fromkeys([*d.cycle.ancestors(t.entity_id), t.entity_id]))
        regs = {r[0] for r in d.conn.execute("SELECT registrable_domain FROM websites WHERE entity_id=?",
                                             (t.entity_id,))}
        if t.start_url and (r := registrable_domain(t.start_url)):
            regs.add(r)
        self.sc(s).parent_regs = regs
        task = t.model_copy(update={"entity_name": row["name"], "entity_type": row["entity_type"], "ancestry": anc})
        return {"task": task, "route": "fetch_entry"}

    def n_fetch_entry(self, s: ResearchState) -> dict[str, Any]:
        t, d = s.task, self.d
        url = t.start_url
        if not url:
            row = d.conn.execute("SELECT w.url FROM entities e JOIN websites w ON w.website_id=e.primary_website_id "
                                 "WHERE e.entity_id=?", (t.entity_id,)).fetchone()
            url = row[0] if row else None
        if not url:
            return {"outcome": "NO_WEBSITE", "route": "finish", "errors": ["no start url or primary website"]}
        r = d.fetch.fetch(url, run_id=t.run_id, entity_id=t.entity_id, parent_entity_id=t.parent_entity_id,
                          page_type=PageType.HOME)
        if (r.error_code or "").startswith("BUDGET_"):
            run_wide = "TOTAL_PAGE_LIMIT" in (r.error_code or "")
            return self.stop(r.error_code or "BUDGET", "run" if run_wide else "task")
        gate = check_g6_reachability(SimpleNamespace(status=r.crawl_status.value, text=r.text, error=r.error))
        if not gate.allowed:
            return {"route": "mark_unreachable", "errors": [f"G6 {gate.detail}"],
                    "rejections": [_rej("G6", url, gate.detail or "unreachable")]}
        sc = self.sc(s)
        sc.html = r.html or (d.fetch.load_html(r.page_id) if r.page_id else None) or ""
        sc.base_url = r.final_url or r.canonical_url or url
        sc.parent_regs.add(registrable_domain(sc.base_url) or "")
        return {"entry_page_id": r.page_id, "page_ids": [r.page_id], "route": "index_links"}

    def n_index_links(self, s: ResearchState) -> dict[str, Any]:
        sc = self.sc(s)
        sc.link_index = build_link_index(sc.html, sc.base_url)
        if s.entry_page_id:
            self.d.pages_repo.set_counts(s.entry_page_id, pages_found=len(sc.link_index.links))
        return {"route": "route_discovery"}

    def n_route_discovery(self, s: ResearchState) -> dict[str, Any]:
        idx = self.sc(s).link_index
        same = [lk for lk in idx.links if lk.same_registrable_domain]
        if any(lk.det_score >= HIGH_BAND for lk in same):
            top = [lk for lk in sorted(same, key=lambda x: (-x.det_score, x.link_id)) if lk.det_score >= HIGH_BAND]
            sel = [{"link_id": lk.link_id, "page_type": (lk.page_type_hint or PageType.OTHER).value,
                    "relevance": lk.det_score, "source": "DETERMINISTIC"} for lk in top[: self.max_links()]]
            return {"selections": sel, "route": "gate_pages"}
        if any(LOW_BAND < lk.det_score < HIGH_BAND for lk in same):
            return {"route": "discovery_agent"}
        return {"route": "mark_terminal"}

    def _fallback_selection(self, idx: Any) -> list[dict[str, Any]]:
        pool = [lk for lk in idx.top(self.max_links(), min_score=LOW_BAND + 1e-9, same_domain_only=True)]
        return [{"link_id": lk.link_id, "page_type": (lk.page_type_hint or PageType.OTHER).value,
                 "relevance": lk.det_score, "source": "DETERMINISTIC"} for lk in pool]

    def n_discovery_agent(self, s: ResearchState) -> dict[str, Any]:
        sc, t = self.sc(s), s.task
        idx = sc.link_index
        offered = idx.top(60, min_score=0.0)
        payload = DiscoveryInput(entity=self.card(t.entity_id, t.entity_name), site_domain=registrable_domain(sc.base_url) or "",
                                 page_title=_title(sc.html), links=idx.to_refs([lk.link_id for lk in offered]),
                                 max_selections=self.max_links())
        resp, extra = self.call(s, "discovery", payload, "discovery")
        out: dict[str, Any] = {**extra, "invocation_ids": []}
        if (why := self.budget_reason(resp)) and "TOKEN_BUDGET" in why:
            return {**self.stop(why, "run")}
        sel: list[dict[str, Any]] = []
        rejections = []
        if resp.status is CallStatus.OK and resp.payload is not None:
            o = resp.payload
            if o.no_relevant_pages and o.agent_confidence >= DISCOVERY_NONE_CONF:
                return {**out, "route": "mark_terminal"}
            for p in o.selections:
                g1 = check_g1_id_integrity(str(p.link_id), idx.ids)
                if not g1.allowed:
                    rejections.append(_rej("G1", f"link_id={p.link_id}", g1.detail or "unknown link"))
                elif p.relevance >= DISCOVERY_MIN_RELEVANCE:
                    sel.append({"link_id": p.link_id, "page_type": p.page_type.value, "relevance": p.relevance,
                                "source": "AGENT"})
        if not sel:
            sel = self._fallback_selection(idx)
        return {**out, "selections": sel, "rejections": rejections, "route": "gate_pages"}

    def n_gate_pages(self, s: ResearchState) -> dict[str, Any]:
        sc, d = self.sc(s), self.d
        idx, seen, approved, rej = sc.link_index, {sc.base_url}, [], []
        for p in s.selections:
            ref = f"link_id={p['link_id']}"
            if not (g := check_g1_id_integrity(str(p["link_id"]), idx.ids)).allowed:
                rej.append(_rej("G1", ref, g.detail or ""))
                continue
            lk = idx.get(p["link_id"])
            hint = lk.page_type_hint.value if lk.page_type_hint else None
            checks = [
                ("G4", check_g4_scope(p["page_type"], lk.url)),
                # an agent's page_type label can never launder an excluded page: also check the deterministic hint/URL
                ("G4", check_g4_scope(hint, lk.url)),
                ("G5", check_g5_platform_blocklist(lk.url, frozenset(d.settings.shared_platform_domains))),
                ("G8", check_g8_url_canonical(lk.url, sc.base_url, seen, s.task.entity_id)),
            ]
            if not lk.same_registrable_domain:
                checks.append(("G8", type("R", (), {"allowed": False, "detail": "off-site page"})()))
            bad = next(((c, r) for c, r in checks if not r.allowed), None)
            if bad:
                rej.append(_rej(bad[0], lk.url, bad[1].detail or bad[1].reason or ""))
                continue
            seen.add(lk.url)
            approved.append({"link_id": lk.link_id, "url": lk.url, "page_type": p["page_type"]})
        approved = approved[: self.max_links()]
        if not approved:
            return {"rejections": rej, "route": "mark_terminal"}
        return {"approved": approved, "rejections": rej, "route": "fetch_pages"}

    def n_fetch_pages(self, s: ResearchState) -> dict[str, Any]:
        t, d = s.task, self.d
        ids, rej, run_stop, budget_hits = list(s.page_ids), [], False, 0
        for a in s.approved:
            try:
                ptype = PageType(a["page_type"])
            except ValueError:
                ptype = PageType.OTHER
            r = d.fetch.fetch(a["url"], run_id=t.run_id, entity_id=t.entity_id, parent_entity_id=t.parent_entity_id,
                              page_type=ptype)
            if (r.error_code or "").startswith("BUDGET_"):
                budget_hits += 1
                run_stop = run_stop or "TOTAL_PAGE_LIMIT" in (r.error_code or "")
                continue
            if not check_g6_reachability(SimpleNamespace(status=r.crawl_status.value, text=r.text, error=r.error)).allowed:
                rej.append(_rej("G6", a["url"], r.error or r.error_code or "fetch failed"))
                continue
            ids.append(r.page_id)
        fetched = [i for i in ids if i != s.entry_page_id]
        if not fetched:
            if budget_hits:
                return {**self.stop("page budget exhausted", "run" if run_stop else "task"), "rejections": rej}
            return {"rejections": rej, "route": "mark_unreachable"}
        return {"page_ids": ids, "rejections": rej, "route": "build_extraction_inputs"}

    def n_build_extraction_inputs(self, s: ResearchState) -> dict[str, Any]:
        sc, d = self.sc(s), self.d
        approved = {a["url"]: a for a in s.approved}
        for pid in s.page_ids:
            if pid == s.entry_page_id:
                continue
            row = d.pages_repo.get_page_by_id(pid)
            text, html = d.fetch.load_text(pid), d.fetch.load_html(pid)
            if text is None:
                continue
            idx = build_link_index(html or "", row.final_url or row.canonical_url)
            sc.page_indexes[pid] = idx
            dom = extract_dom_candidates(html or "", idx)
            d.pages_repo.set_counts(pid, pages_found=len(idx.links), entities_found=len(dom))
            ptype = PageType(row.page_type) if row.page_type in PageType._value2member_map_ else PageType.OTHER
            lines, chunks, cur, off, start = text.split("\n"), [], [], 0, 0
            for ln in lines:
                if cur and sum(len(x) + 1 for x in cur) + len(ln) > CHUNK_CHARS:
                    chunks.append(("\n".join(cur), start))
                    start, cur = off, []
                cur.append(ln)
                off += len(ln) + 1
            if cur:
                chunks.append(("\n".join(cur), start))
            truncated = len(chunks) > MAX_CHUNKS_PER_PAGE
            for i, (ctext, coff) in enumerate(chunks[:MAX_CHUNKS_PER_PAGE]):
                low = ctext.lower()
                cdom = [c for c in dom if c.name_text.lower() in low]
                links = [lk for lk in idx.links if lk.anchor_text and lk.anchor_text.lower() in low][:60]
                key = f"{pid}:{i}"
                sc.chunks[key] = {
                    "page_id": pid, "url": row.canonical_url, "page_type": ptype, "text": ctext, "offset": coff,
                    "index": i, "count": min(len(chunks), MAX_CHUNKS_PER_PAGE), "truncated": truncated and i == MAX_CHUNKS_PER_PAGE - 1,
                    "dom": cdom, "link_ids": {lk.link_id for lk in links}, "links": idx.to_refs([lk.link_id for lk in links]),
                    "page_text": text, "source_class": self.source_class(s, row.canonical_url),
                }  # fmt: skip
        return {"route": "extraction_agent" if sc.chunks else "mark_terminal"}

    def _ground(self, out: ExtractionOutput, ch: dict[str, Any]) -> tuple[list[tuple[Any, str]], list[Any]]:
        """G1/G2/G3 for one chunk output (shared pure implementation in graphs.grounding)."""
        rep = ground_entities(out, page_id=ch["page_id"], page_text=ch["page_text"], dom_candidates=ch["dom"],
                              link_ids=ch["link_ids"], verifier=self.d.verifier)
        return list(rep.failed), [*rep.accepted, *rep.non_entities]

    def n_extraction_agent(self, s: ResearchState) -> dict[str, Any]:
        sc, t = self.sc(s), s.task
        parent = self.card(t.entity_id, t.entity_name)
        allowed = [x for x in EntityType if x not in (EntityType.PARENT_COMPANY, EntityType.UNKNOWN)]
        partial, errors, rej, inv_reviews = False, [], [], []
        for key, ch in sc.chunks.items():
            payload = ExtractionInput(
                parent=parent, chunk_index=ch["index"], chunk_count=ch["count"], dom_candidates=ch["dom"],
                links=ch["links"], allowed_entity_types=allowed,
                page=PageExcerpt(page_id=ch["page_id"], url=ch["url"], page_type=ch["page_type"],
                                 source_class=ch["source_class"], text=ch["text"], char_offset=ch["offset"],
                                 is_truncated=ch["truncated"]))
            resp, extra = self.call(s, "extraction", payload, f"extract:{key}")
            errors += extra.get("errors", [])
            if (why := self.budget_reason(resp)) and "TOKEN_BUDGET" in why:
                return self.stop(why, "run")
            if resp.status is not CallStatus.OK or resp.payload is None:
                partial = True
                rej.append(_rej("G12", key, f"extraction failed: {resp.error.message if resp.error else ''}"))
                continue
            fails, _ = self._ground(resp.payload, ch)
            if fails:  # one grounding re-ask listing exactly what failed (25.2)
                msg = "; ".join(f"{e.name!r} failed {g}" for e, g in fails)[:1200]
                again, extra2 = self.call(s, "extraction", payload, f"extract:{key}:reask", feedback=msg)
                errors += extra2.get("errors", [])
                if again.status is CallStatus.OK and again.payload is not None:
                    resp = again
            sc.extractions.append((key, resp.payload))
        route = "gate_extraction" if sc.extractions else "mark_terminal"
        upd: dict[str, Any] = {"errors": errors, "rejections": rej, "partial": partial, "route": route}
        if partial and not sc.extractions:
            upd["outcome"] = "PARTIAL"
        return upd

    def n_gate_extraction(self, s: ResearchState) -> dict[str, Any]:
        sc, d = self.sc(s), self.d
        cands, rows, rej, seen = [], [], [], set()
        sibling = 0
        for key, out in sc.extractions:
            ch = sc.chunks[key]
            if not (g12 := check_g12_schema_enum(out)).allowed:
                rej.append(_rej("G12", key, g12.detail or "schema"))
                continue
            for r in out.rejected:
                rows.append(self._row(ch, r.name, r.kind.value, "REJECTED_NOT_ENTITY", r.reason, None))
            fails, _ = self._ground(out, ch)
            failed = {id(e): g for e, g in fails}
            for e in out.entities:
                norm = normalize_name(e.name)
                if not e.is_corporate_entity or e.kind in NON_ENTITY_KINDS:
                    rows.append(self._row(ch, e.name, e.kind.value, "REJECTED_NOT_ENTITY", f"not a company: {e.kind}", None))
                    continue
                if id(e) in failed:
                    code = failed[id(e)]
                    rej.append(_rej(code, e.name, f"{code} failed for {e.name!r}"))
                    rows.append(self._row(ch, e.name, e.kind.value, "REJECTED_NOT_ENTITY", f"failed {code}", code))
                    continue
                if e.agent_confidence < EXTRACT_MIN_CONF:
                    rows.append(self._row(ch, e.name, e.kind.value, "REVIEW", "low extraction confidence", None, e))
                    continue
                if norm in seen:
                    rows.append(self._row(ch, e.name, e.kind.value, "DUPLICATE", "same name already proposed", None, e))
                    continue
                seen.add(norm)
                ev = d.verifier.verify_claim(e.claim, page_text=ch["page_text"], url=ch["url"],
                                             source_class=ch["source_class"])
                link = sc.page_indexes[ch["page_id"]].get(e.link_id) if e.link_id else None
                dom = next((c for c in ch["dom"] if c.raw_candidate_id == e.raw_candidate_id), None) if e.raw_candidate_id else None
                sibling += 1
                cands.append(Cand(
                    ref=f"n{len(cands) + 1}", name=e.name, norm=norm, page_id=ch["page_id"], page_url=ch["url"],
                    chunk_key=key, link_url=link.url if link else None, raw_candidate_id=e.raw_candidate_id,
                    dom_hint=dom.dom_hint if dom else None, kind=e.kind.value,
                    proposed_type=e.proposed_type.value if e.proposed_type else None,
                    rel_hint=e.relationship_hint.value if e.relationship_hint else None, description=e.description,
                    country=e.country, industry=e.industry, ext_conf=e.agent_confidence, claim=ev, sibling=sibling))
        upd: dict[str, Any] = {"cands": cands, "cand_rows": rows, "rejections": rej}
        upd["route"] = "match_known" if cands else "mark_terminal"
        if not cands and s.partial:
            upd["outcome"] = "PARTIAL"
        return upd

    def _row(self, ch: dict[str, Any], name: str, kind: str, decision: str, reason: str, gate: str | None,
             e: Any = None, resolved_ref: str | None = None, resolved_id: int | None = None) -> dict[str, Any]:
        return {"page_id": ch["page_id"], "name": name[:300], "norm": normalize_name(name) or name.lower()[:300],
                "kind": kind, "decision": decision, "reason": reason[:500], "gate": gate,
                "conf": getattr(e, "agent_confidence", None), "ptype": getattr(getattr(e, "proposed_type", None), "value", None),
                "ref": resolved_ref, "resolved_id": resolved_id}

    def n_match_known(self, s: ResearchState) -> dict[str, Any]:
        t, d, sc = s.task, self.d, self.sc(s)
        anc_names = {r["normalized_name"]: r["entity_id"] for r in d.conn.execute(
            f"SELECT entity_id, normalized_name FROM entities WHERE entity_id IN ({','.join('?' * len(t.ancestry))})",
            t.ancestry)}
        rows, rej, reviews, out = [], [], [], []
        parent_reg = registrable_domain(sc.base_url)
        for c in s.cands:
            ch = sc.chunks[c.chunk_key]
            reg = registrable_domain(c.link_url) if c.link_url else None
            hit: list[int] = []
            if c.norm in anc_names:  # the page names the entity itself or one of its ancestors
                hit = [anc_names[c.norm]]
            else:
                m = d.matcher.find_match(MatchCandidate(
                    name=c.name, normalized_name=c.norm, parent_entity_id=t.entity_id,
                    registrable_domain=reg if reg and reg != parent_reg else None, country=c.country, industry=c.industry))
                if m.outcome == "MATCH":
                    hit = [m.existing_entity_id]  # type: ignore[list-item]
                    c.known_id = m.existing_entity_id
                elif m.outcome == "AMBIGUOUS":
                    hit = list(m.ambiguous_entity_ids)
                    c.ambiguous_ids = hit
            cyc = [(e, g) for e in hit if not (g := check_g10_cycle(d.cycle, t.entity_id, e, tuple(t.ancestry))).allowed]
            if cyc:
                e, g = cyc[0]
                c.dropped, c.cycle_with = "cycle", e
                rej.append(_rej("G10", f"{t.entity_id}->{e}", g.detail or "cycle"))
                rows.append(self._row(ch, c.name, c.kind, "REVIEW", "edge would create a cycle", "G10"))
                reviews.append({"entity_id": e, "possible_parent_id": t.entity_id, "reason": ReviewReason.CYCLE.value,
                                "text": f"{c.name!r} on the page of entity {t.entity_id} would create a cycle",
                                "action": "Verify the ownership direction manually; the edge was not committed."})
            elif c.known_id is not None:
                existing = d.conn.execute("SELECT relationship_id FROM relationships WHERE parent_id=? AND child_id=?",
                                          (t.entity_id, c.known_id)).fetchone()
                c.dropped = "known_same_parent" if existing else "known_other_parent"
                rows.append(self._row(ch, c.name, c.kind, "DUPLICATE", "matched an existing entity", None,
                                      resolved_id=c.known_id))
            out.append(c)
        new = [c for c in out if not c.dropped]
        upd = {"cands": out, "cand_rows": rows, "rejections": rej, "reviews": reviews}
        upd["route"] = "route_website" if new else "commit"
        return upd

    def n_route_website(self, s: ResearchState) -> dict[str, Any]:
        sc, d = self.sc(s), self.d
        blocked = frozenset(d.settings.shared_platform_domains)
        parent_reg = registrable_domain(sc.base_url)
        rej, need = [], False
        for c in s.cands:
            if c.dropped or not c.link_url:
                need = need or (not c.dropped)
                continue
            g5 = check_g5_platform_blocklist(c.link_url, blocked)
            if not g5.allowed:
                rej.append(_rej("G5", c.link_url, g5.detail or "blocklisted"))
                need = True
                continue
            reg = registrable_domain(c.link_url)
            c.site_url, c.site_method = c.link_url, "LINKED_ON_PARENT"
            c.site_kind = WebsiteKind.SECTION_OF_PARENT.value if reg == parent_reg else WebsiteKind.OFFICIAL.value
        return {"cands": s.cands, "rejections": rej, "route": "website_agent" if need else "gate_website"}

    def n_website_agent(self, s: ResearchState) -> dict[str, Any]:
        t, sc, d = s.task, self.sc(s), self.d
        parent = self.card(t.entity_id, t.entity_name)
        errors, rej = [], []
        for c in [c for c in s.cands if not c.dropped and not c.site_url][:10]:
            idx = sc.page_indexes.get(c.page_id)
            refs = idx.to_refs(None, limit=30) if idx else []
            hits = d.search.hits_for(c.name, t.entity_name, country=c.country) if d.search else []
            sc.hits[c.ref] = {h.hit_id: h.url for h in hits}
            payload = WebsiteInput(entity=self.card(None, c.name, c.proposed_type), parent=parent, source_links=refs,
                                   search_hits=hits)
            resp, extra = self.call(s, "website", payload, f"website:{c.ref}")
            errors += extra.get("errors", [])
            if resp.status is not CallStatus.OK or resp.payload is None:
                continue  # fallback: only links found on the source page are used (25.5) -> no website
            o = resp.payload
            if o.decision != "SELECTED" or o.agent_confidence < d.settings.website_min_conf:
                continue
            if o.selected_hit_id is not None:
                known = sc.hits.get(c.ref, {})
                if not check_g1_id_integrity(str(o.selected_hit_id), frozenset(known)).allowed:
                    rej.append(_rej("G1", f"hit_id={o.selected_hit_id}", "website hit id not supplied"))
                    continue
                url, method = known[o.selected_hit_id], "SEARCH_AGENT"
            else:
                if not check_g1_id_integrity(str(o.selected_link_id), idx.ids if idx else frozenset()).allowed:
                    rej.append(_rej("G1", f"link_id={o.selected_link_id}", "website link id not supplied"))
                    continue
                url, method = idx.get(o.selected_link_id).url, "LINKED_ON_PARENT"
            if not check_g5_platform_blocklist(url, frozenset(d.settings.shared_platform_domains)).allowed:
                rej.append(_rej("G5", url, "blocklisted website"))
                continue
            reg = registrable_domain(url)
            c.site_url, c.site_method = url, method
            c.site_kind = (WebsiteKind.SECTION_OF_PARENT if reg == registrable_domain(sc.base_url)
                           else WebsiteKind.OFFICIAL).value
        return {"cands": s.cands, "errors": errors, "rejections": rej, "route": "gate_website"}

    def n_gate_website(self, s: ResearchState) -> dict[str, Any]:
        t, d, sc = s.task, self.d, self.sc(s)
        rej, reviews = [], []
        parent_norm = normalize_text(t.entity_name)
        for c in s.cands:
            if c.dropped or not c.site_url:
                continue
            if c.site_kind == WebsiteKind.SECTION_OF_PARENT.value:
                c.site_reachable, c.site_identity = True, True
                continue
            r = d.fetch.fetch(c.site_url, run_id=t.run_id, entity_id=None, parent_entity_id=t.entity_id,
                              page_type=PageType.HOME)
            if (r.error_code or "").startswith("BUDGET_"):
                c.site_reachable = None
                continue
            c.site_status = r.status_code
            ok = check_g6_reachability(SimpleNamespace(status=r.crawl_status.value, text=r.text, error=r.error))
            c.site_reachable = ok.allowed
            if not ok.allowed:
                rej.append(_rej("G6", c.site_url, ok.detail or "unreachable"))
                continue
            html = r.html or (d.fetch.load_html(r.page_id) if r.page_id else None)
            ident = check_g7_identity_check(c.name, r.text, _title(html))
            c.site_identity = ident.allowed
            if not ident.allowed:
                rej.append(_rej("G7", c.site_url, ident.detail or "identity"))
                continue
            for line in (r.text or "").split("\n"):  # child-side first-party evidence, found deterministically
                line = line.strip()
                if 15 <= len(line) <= 400 and parent_norm and parent_norm in normalize_text(line) and _OWNERSHIP_WORDING.search(line):
                    ev = d.verifier.verify_quote(page_id=r.page_id, quote=line, supports=Supports.OWNERSHIP,
                                                 page_text=r.text, url=r.canonical_url or c.site_url,
                                                 source_class=SourceClass.FIRST_PARTY_CHILD)
                    if ev.quote_verified:
                        c.child_evidence.append(ev)
                        break
        return {"cands": s.cands, "rejections": rej, "route": "dedupe_resolve"}

    def n_dedupe_resolve(self, s: ResearchState) -> dict[str, Any]:
        d, rows = self.d, []
        by_dom: dict[str, Cand] = {}
        for c in s.cands:
            if c.dropped:
                continue
            if c.ambiguous_ids and d.dedup is not None:
                verdict = d.dedup.judge(d, c.name, s.task.entity_id, c.ambiguous_ids)
                if verdict == "SAME":
                    c.known_id, c.dropped = c.ambiguous_ids[0], "known_other_parent"
                    continue
            reg = registrable_domain(c.site_url) if c.site_url and c.site_kind == WebsiteKind.OFFICIAL.value else None
            if reg and reg in by_dom:  # two references to the same company: keep the first, alias the second
                first = by_dom[reg]
                c.dropped, c.duplicate_of = "duplicate_in_batch", first.ref
                ch = self.sc(s).chunks[c.chunk_key]
                rows.append(self._row(ch, c.name, c.kind, "DUPLICATE", f"same website as {first.name!r}", None,
                                      resolved_ref=first.ref))
            elif reg:
                by_dom[reg] = c
        new = [c for c in s.cands if not c.dropped]
        return {"cands": s.cands, "cand_rows": rows, "route": "relationship_agent" if new else "commit"}

    def n_relationship_agent(self, s: ResearchState) -> dict[str, Any]:
        t, sc = s.task, self.sc(s)
        new = [c for c in s.cands if not c.dropped]
        parent = self.card(t.entity_id, t.entity_name)
        unavailable, errors = False, []
        for i in range(0, len(new), REL_BATCH):
            batch = new[i : i + REL_BATCH]
            pages: dict[int, PageExcerpt] = {}
            for c in batch:
                ch = sc.chunks[c.chunk_key]
                text = ch["page_text"]
                pos = max(text.lower().find(c.name.lower()), 0)
                lo = max(0, pos - WINDOW // 2)
                pages.setdefault(c.page_id, PageExcerpt(
                    page_id=c.page_id, url=c.page_url, page_type=ch["page_type"], source_class=ch["source_class"],
                    text=text[lo : lo + WINDOW], char_offset=lo, is_truncated=len(text) > lo + WINDOW))
            payload = RelationshipInput(
                parent=parent, pages=list(pages.values()),
                children=[ChildProposal(local_ref=c.ref, name=c.name, proposed_type=EntityType(c.proposed_type) if c.proposed_type else None,
                                        kind=CandidateKind(c.kind), website=c.site_url,
                                        relationship_hint=RelationshipType(c.rel_hint) if c.rel_hint else None)
                          for c in batch])
            resp, extra = self.call(s, "relationship", payload, f"relationship:{i}")
            errors += extra.get("errors", [])
            if (why := self.budget_reason(resp)) and "TOKEN_BUDGET" in why:
                return self.stop(why, "run")
            if resp.status is CallStatus.OK and resp.payload is not None:
                by_ref = {a.local_ref: a for a in resp.payload.assessments}
                supplied = set(pages)
                missing = [c for c in batch if c.ref in by_ref and any(
                    q.page_id not in supplied or not self.d.verifier.check(q.quote, pages[q.page_id].text).verified
                    for q in by_ref[c.ref].claims)]
                if missing:  # one grounding re-ask listing the quotes that were not found (20.3)
                    msg = "quotes not found verbatim for: " + ", ".join(c.ref for c in missing)
                    again, extra2 = self.call(s, "relationship", payload, f"relationship:{i}:reask", feedback=msg)
                    errors += extra2.get("errors", [])
                    if again.status is CallStatus.OK and again.payload is not None:
                        by_ref = {a.local_ref: a for a in again.payload.assessments}
                for c in batch:
                    a = by_ref.get(c.ref)
                    c.assess = a.model_dump(mode="json") if a else None
                    c.rel_extra = []
            else:
                unavailable = True
        return {"cands": s.cands, "errors": errors, "rel_unavailable": unavailable, "route": "verify_and_score"}

    def n_verify_and_score(self, s: ResearchState) -> dict[str, Any]:
        d, sc, st = self.d, self.sc(s), self.d.settings
        rej = []
        for c in s.cands:
            if c.dropped:
                continue
            ch = sc.chunks[c.chunk_key]
            ev_list: list[VerifiedEvidence] = [c.claim] if c.claim else []
            a = c.assess
            flags: set[ConfidenceFlag] = set()
            agent_conf = c.ext_conf
            rel_claim_ok = False
            if a:
                agent_conf = a["agent_confidence"]
                for q in a["claims"]:
                    if q["page_id"] != c.page_id:
                        rej.append(_rej("G1", f"{c.ref}:page_id={q['page_id']}", "claim page not supplied"))
                        continue
                    rv = d.verifier.verify_quote(
                        page_id=q["page_id"], quote=q["quote"], supports=Supports(q["supports"]),
                        page_text=ch["page_text"], url=c.page_url, source_class=ch["source_class"])
                    rel_claim_ok = rel_claim_ok or rv.quote_verified
                    ev_list.append(rv)
                if a["contradictions"] or a["support"] == "NOT_SUPPORTED":
                    flags.add(ConfidenceFlag.CONTRADICTION)
            ev_list += c.child_evidence
            if any(NEGATIVE_WORDING.search(e.quote) for e in ev_list if e.quote_verified):
                flags.add(ConfidenceFlag.CONTRADICTION)
            c.rel_extra = ev_list
            if c.ambiguous_ids:
                flags.add(ConfidenceFlag.AMBIGUOUS_NAME)
            has_site = bool(c.site_url and c.site_reachable and c.site_identity is not False)
            if st.require_website_for_confirmed and not has_site and c.site_reachable is not False:
                flags.add(ConfidenceFlag.NO_WEBSITE)
            res = d.calc.score([EvidenceInput(source_class=e.source_class, supports=e.supports, quote_verified=e.quote_verified,
                                              page_id=e.page_id, ownership_wording=bool(_OWNERSHIP_WORDING.search(e.quote)))
                                for e in ev_list],
                               agent_confidence=agent_conf if a else 0.0, classification_confidence=c.ext_conf, flags=flags)
            status, reason = res.status, res.review_reason
            # SUPPORTED only counts when at least one of its own quotes verified against the stored page text.
            supported = bool(a and a["support"] == "SUPPORTED" and rel_claim_ok)
            etype = (a or {}).get("final_entity_type") or c.proposed_type or EntityType.UNKNOWN.value
            if status is VerificationStatus.CONFIRMED:
                downgrade = None
                if not supported or s.rel_unavailable:
                    downgrade = (ReviewReason.VERIFICATION_UNAVAILABLE if s.rel_unavailable
                                 else ReviewReason.NO_QUOTE if a and a["support"] == "SUPPORTED"
                                 else ReviewReason.LOW_CONFIDENCE)
                elif not check_g11_evidence_requirement(tuple(ev_list), True).allowed:
                    downgrade = ReviewReason.NO_QUOTE
                elif not check_g14_website_requirement(c.site_url if has_site else None, etype, False,
                                                       require_for_confirmed=st.require_website_for_confirmed).allowed:
                    downgrade = ReviewReason.NO_WEBSITE
                elif etype == EntityType.UNKNOWN.value or res.entity_confidence < st.confirm_threshold:
                    downgrade = ReviewReason.LOW_CONFIDENCE
                if downgrade:
                    status, reason = VerificationStatus.REVIEW_REQUIRED, downgrade
            if s.rel_unavailable and status is not VerificationStatus.REJECTED:
                status, reason = VerificationStatus.REVIEW_REQUIRED, ReviewReason.VERIFICATION_UNAVAILABLE
            c.status, c.review_reason = status.value, reason.value if reason else None
            c.rel_conf, c.ent_conf = res.relationship_confidence, res.entity_confidence
            c.final_type = etype
            c.rel_type = (a["relationship_type"] if supported and status is not VerificationStatus.REJECTED
                          else RelationshipType.UNKNOWN.value)
        return {"cands": s.cands, "rejections": rej, "route": "commit"}

    # --------------------------------------------------------------- commit
    def n_commit(self, s: ResearchState) -> dict[str, Any]:
        t, d, sc = s.task, self.d, self.sc(s)
        parent_ref = EntityRef(entity_id=t.entity_id)
        entities, websites, rels, evs, aliases, cands_w, reviews = [], [], [], [], [], [], []
        rows = list(s.cand_rows)
        for c in s.cands:
            ch = sc.chunks[c.chunk_key]
            if c.dropped == "duplicate_in_batch":
                aliases.append(AliasWrite(entity=EntityRef(local_ref=c.duplicate_of), alias=c.name[:300],
                                          normalized_alias=c.norm, source_page_id=c.page_id))
                continue
            if c.dropped in ("known_same_parent", "known_other_parent") and c.known_id:
                rid = d.conn.execute("SELECT relationship_id FROM relationships WHERE parent_id=? AND child_id=?",
                                     (t.entity_id, c.known_id)).fetchone()
                if c.claim and c.claim.quote_verified and rid:
                    evs.append(EvidenceWrite(entity=EntityRef(entity_id=c.known_id), relationship_id=rid[0], evidence=c.claim))
                elif c.dropped == "known_other_parent":
                    lr = f"k{c.ref}"
                    rels.append(RelationshipWrite(local_ref=lr, parent=parent_ref, child=EntityRef(entity_id=c.known_id),
                                                  relationship_type=RelationshipType.UNKNOWN, verification_status=VerificationStatus.REVIEW_REQUIRED,
                                                  confidence=0.4, is_primary=False, source_page_id=c.page_id, discovery_method=c.dom_hint))
                    if c.claim:
                        evs.append(EvidenceWrite(entity=EntityRef(entity_id=c.known_id), relationship_local_ref=lr, evidence=c.claim))
                    reviews.append(ReviewItemWrite(entity=EntityRef(entity_id=c.known_id), possible_parent=parent_ref,
                                                   reason_code=ReviewReason.MULTI_PARENT, reason_text=f"{c.name!r} is also referenced by entity {t.entity_id}",
                                                   recommended_action="Decide which parent owns this entity."))
                continue
            if c.dropped or c.status is None:
                continue
            if c.status == VerificationStatus.REJECTED.value:
                rows.append(self._row(ch, c.name, c.kind, "REJECTED_NOT_ENTITY", "relationship not supported by evidence", None))
                continue
            dead = c.site_reachable is False
            state = (CrawlState.UNREACHABLE if dead else CrawlState.NO_WEBSITE if not c.site_url else CrawlState.PENDING)
            entities.append(EntityWrite(
                local_ref=c.ref, name=c.name[:300], normalized_name=c.norm,
                entity_type=EntityType(c.final_type or EntityType.UNKNOWN.value),
                verification_status=VerificationStatus(c.status), description=c.description, country=c.country,
                industry=c.industry, classification_confidence=c.ext_conf, confidence=c.ent_conf, crawl_state=state,
                discovery_method=c.dom_hint, sibling_order=c.sibling))
            child = EntityRef(local_ref=c.ref)
            has_site = bool(c.site_url and c.site_reachable and c.site_identity is not False)
            if c.site_url and c.site_reachable is not None:
                reg = registrable_domain(c.site_url) or ""
                websites.append(WebsiteWrite(
                    local_ref=f"w{c.ref}", entity=child, url=c.site_url, domain=canonicalize_url(c.site_url).domain or reg,
                    registrable_domain=reg, kind=WebsiteKind(c.site_kind or WebsiteKind.OFFICIAL.value),
                    resolution_method=c.site_method, reachable=c.site_reachable, last_http_status=c.site_status,
                    is_primary=has_site))
            rels.append(RelationshipWrite(
                local_ref=f"r{c.ref}", parent=parent_ref, child=child, relationship_type=RelationshipType(c.rel_type),
                verification_status=VerificationStatus(c.status), confidence=c.rel_conf, is_primary=True,
                source_page_id=c.page_id, discovery_method=c.dom_hint))
            for ev in c.rel_extra:
                evs.append(EvidenceWrite(entity=child, relationship_local_ref=f"r{c.ref}", evidence=ev))
            rows.append(self._row(ch, c.name, c.kind, "ACCEPTED" if c.status == "CONFIRMED" else "REVIEW",
                                  c.review_reason or "accepted", None, resolved_ref=c.ref))
            if c.status != VerificationStatus.CONFIRMED.value:
                code = ReviewReason.BROKEN_SITE if dead else ReviewReason(c.review_reason or ReviewReason.LOW_CONFIDENCE.value)
                reviews.append(ReviewItemWrite(
                    entity=child, possible_parent=parent_ref, reason_code=code,
                    reason_text=f"{c.name!r}: {code.value}", recommended_action="Review the evidence and the website manually.",
                    confidence=c.rel_conf))
        for r in s.reviews:
            reviews.append(ReviewItemWrite(entity=EntityRef(entity_id=r["entity_id"]),
                                           possible_parent=EntityRef(entity_id=r["possible_parent_id"]) if r["possible_parent_id"] else None,
                                           reason_code=ReviewReason(r["reason"]), reason_text=r["text"][:500], recommended_action=r["action"][:500]))
        if s.partial:
            reviews.append(ReviewItemWrite(entity=parent_ref, reason_code=ReviewReason.EXTRACTION_FAILED,
                                           reason_text="one or more page chunks could not be extracted",
                                           recommended_action="Re-run the task or review the listing pages manually."))
        for r in rows:
            res = (EntityRef(local_ref=r["ref"]) if r["ref"] and any(e.local_ref == r["ref"] for e in entities)
                   else EntityRef(entity_id=r["resolved_id"]) if r["resolved_id"] else None)
            if r["decision"] == "DUPLICATE" and r["ref"] and res is None:
                res = None
            cands_w.append(CandidateWrite(
                source_page_id=r["page_id"], parent=parent_ref, raw_name=r["name"], normalized_name=r["norm"],
                candidate_kind=CandidateKind(r["kind"]), proposed_type=EntityType(r["ptype"]) if r["ptype"] else None,
                decision=r["decision"], resolved=res, reason=r["reason"], confidence=r["conf"], gate_code=r["gate"]))
        plan = CommitPlan(
            run_id=t.run_id, task_id=t.task_id, candidates=tuple(_dedup_candidates(cands_w)), entities=tuple(entities),
            websites=tuple(websites), relationships=tuple(rels), evidence=tuple(evs), aliases=tuple(aliases),
            review_items=tuple(reviews), gate_rejections=tuple(GateRejectionWrite(**{k: v for k, v in r.items()}) for r in s.rejections))
        res = d.committer.commit(plan)
        if isinstance(res, CommitRejected):
            d.pages_repo.add_crawl_error(t.run_id, "REGISTRY", res.failed_invariant, res.message, entity_id=t.entity_id)
            raise RuntimeError(f"commit rejected: {res.failed_invariant}: {res.message}")
        ids = {ref: eid for ref, eid in res.entity_ids.items()}
        return {"committed_ids": list(ids.values()), "ref_ids": ids, "route": "route_enqueue"}

    def n_route_enqueue(self, s: ResearchState) -> dict[str, Any]:
        t, d, st = s.task, self.d, self.d.settings
        level = d.budget.degradation_level()
        enq, updates = [], []
        for c in s.cands:
            eid = s.ref_ids.get(c.ref)
            if eid is None or c.status is None:
                continue
            row = d.conn.execute("SELECT e.crawl_state, e.entity_type, w.url, w.kind FROM entities e LEFT JOIN websites w "
                                 "ON w.website_id=e.primary_website_id WHERE e.entity_id=?", (eid,)).fetchone()
            eligible = c.status == "CONFIRMED" or (c.status == "REVIEW_REQUIRED" and st.recurse_into_review and row["url"])
            if not eligible or row["crawl_state"] != CrawlState.PENDING.value or not row["url"]:
                continue
            etype = EntityType(row["entity_type"])
            shared = row["kind"] != WebsiteKind.OFFICIAL.value
            if shared or etype not in CRAWLABLE_TYPES:  # grey zone (29.7): the Advisor may say CRAWL, default is TERMINAL
                reason = "SHARED_DOMAIN" if shared else {"Brand": "TYPE_BRAND", "Division": "TYPE_DIVISION",
                                                         "Business Unit": "TYPE_DIVISION"}.get(etype.value, "TYPE_UNKNOWN")
                verdict = d.advisor.advise(d, eid, t.depth + 1, reason) if d.advisor else None
                if verdict != "CRAWL":
                    updates.append(EntityUpdate(entity_id=eid, crawl_state=CrawlState.TERMINAL))
                    continue
            if level in ("STOP_ENQUEUE_95", "EXHAUSTED_100"):
                continue
            tid = d.queue.enqueue(run_id=t.run_id, entity_id=eid, start_url=row["url"], depth=t.depth + 1)
            if tid is not None:
                enq.append(tid)
        if updates:
            d.committer.commit(CommitPlan(run_id=t.run_id, task_id=t.task_id, entity_updates=tuple(updates)))
        return {"enqueued_task_ids": enq, "route": "finish"}

    def _flush(self, s: ResearchState) -> None:
        """Persist audit rows (gate rejections, candidate decisions, review items) on terminal paths too."""
        if s.rejections or s.cand_rows or s.reviews or s.partial:
            self.n_commit(s)

    def n_mark_terminal(self, s: ResearchState) -> dict[str, Any]:
        self._flush(s)
        return {"outcome": s.outcome or "TERMINAL", "route": "finish"}

    def n_mark_unreachable(self, s: ResearchState) -> dict[str, Any]:
        self._flush(s)
        return {"outcome": "UNREACHABLE", "route": "finish"}

    def n_finish(self, s: ResearchState) -> dict[str, Any]:
        outcome = s.outcome
        if outcome is None:
            outcome = "PARTIAL" if (s.partial or s.rel_unavailable) else ("EXPANDED" if s.committed_ids else "TERMINAL")
        return {"outcome": outcome, "route": END}


def _dedup_candidates(items: list[CandidateWrite]) -> list[CandidateWrite]:
    """Unique key is (run, page, normalized_name): keep the most decisive row (ACCEPTED > REVIEW > others)."""
    rank = {"ACCEPTED": 0, "REVIEW": 1, "DUPLICATE": 2, "REJECTED_NOT_ENTITY": 3, None: 4}
    best: dict[tuple[int, str], CandidateWrite] = {}
    for c in items:
        k = (c.source_page_id, c.normalized_name)
        if k not in best or rank[c.decision] < rank[best[k].decision]:
            best[k] = c
    return list(best.values())
