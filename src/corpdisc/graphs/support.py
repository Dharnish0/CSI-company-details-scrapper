"""Agent-backed services behind the research/finalize graphs: Advisor policy and Dedup judgement.

Both are ADVISORY and thresholded here, in code: the agent supplies a verdict and a confidence, the runtime applies
the documented policy (20.3) and every result still passes budgets, cycle checks and the Committer invariants.
"""

from __future__ import annotations

from bs4 import BeautifulSoup

from corpdisc.agents.base import make_correlation_id
from corpdisc.contracts.agent_io import DuplicateInput, DuplicatePair, EntityCard, RecursionInput
from corpdisc.contracts.agents import AgentRequest, CallLimits, CallStatus, Meta
from corpdisc.graphs.deps import ResearchDeps
from corpdisc.models.domain import EntityType
from corpdisc.services.matcher import MatchCandidate, compute_signals
from corpdisc.services.normalize import normalize_name
from corpdisc.services.validation_engine import ValidationEngine

_GREY = {"Brand": "TYPE_BRAND", "Division": "TYPE_DIVISION", "Business Unit": "TYPE_DIVISION", "Unknown": "TYPE_UNKNOWN"}


def engine_for(deps: ResearchDeps) -> ValidationEngine:
    return ValidationEngine(deps.conn, run_id=deps.run_id, settings=deps.settings, committer=deps.committer,
                            cycle=deps.cycle)


def _request(deps: ResearchDeps, agent, payload, stage: str, task_id: int | None = None) -> AgentRequest:  # type: ignore[no-untyped-def]
    meta = Meta(run_id=deps.run_id, task_id=task_id, correlation_id=make_correlation_id(deps.run_id, task_id, stage),
                agent=agent.name, schema_version=agent.schema_version, prompt_version=agent.prompt_version)
    return AgentRequest(meta=meta, limits=CallLimits(max_output_tokens=agent.max_output_tokens), payload=payload)


def grey_zone_reason(entity_type: EntityType, shared_domain: bool) -> str:
    if shared_domain:
        return "SHARED_DOMAIN"
    return _GREY.get(entity_type.value, "TYPE_UNKNOWN")


class AdvisorService:
    """Supervisor Advisor policy (A1). Returns "CRAWL", "TERMINAL" or None (None = use the fixed default, 25.5)."""

    def advise(self, deps: ResearchDeps, entity_id: int, depth: int, reason: str) -> str | None:
        agent = deps.agents.get("advisor")
        if agent is None or not deps.settings.advisor_enabled:
            return None
        if deps.budget.degradation_level() != "NORMAL":  # disabled from 80% of the token budget (27.3)
            return None
        row = deps.conn.execute("SELECT w.url, w.kind FROM entities e LEFT JOIN websites w ON w.website_id=e.primary_website_id "
                                "WHERE e.entity_id=?", (entity_id,)).fetchone()
        title, headings = None, []
        if row and row["url"]:
            page = deps.pages_repo.get_page(deps.run_id, row["url"])
            html = deps.fetch.load_html(page.page_id) if page and page.page_id else None
            if html:
                soup = BeautifulSoup(html, "html.parser")
                title = soup.title.get_text(" ").strip()[:200] if soup.title else None
                headings = [h.get_text(" ").strip()[:120] for h in soup.find_all(["h1", "h2"])][:15]
        kind = row["kind"] if row and row["kind"] in ("OFFICIAL", "SECTION_OF_PARENT") else "UNKNOWN"
        payload = RecursionInput(entity=engine_for(deps).card(entity_id), depth=depth, website_kind=kind,  # type: ignore[arg-type]
                                 grey_zone_reason=reason if reason in (
                                     "TYPE_BRAND", "TYPE_DIVISION", "TYPE_UNKNOWN", "SHARED_DOMAIN",
                                     "LOW_RELATIONSHIP_CONF") else "TYPE_UNKNOWN",
                                 homepage_title=title, homepage_headings=headings)
        resp = deps.runner.invoke(agent, _request(deps, agent, payload, f"advisor:{entity_id}"))
        if resp.status is not CallStatus.OK or resp.payload is None:
            return None
        if resp.payload.decision == "CRAWL" and resp.payload.agent_confidence < deps.settings.advisor_min_conf:
            return "TERMINAL"  # low-confidence CRAWL counts as TERMINAL (20.3)
        return resp.payload.decision


class DedupService:
    """Deduplication Agent policy (A6). The deterministic ladder has already run; only AMBIGUOUS pairs reach here."""

    def verdicts(self, deps: ResearchDeps, pairs: list[DuplicatePair]) -> dict[int, str]:
        """pair_id -> SAME | DIFFERENT | UNSURE after applying the thresholds of 20.3."""
        agent = deps.agents.get("dedup")
        if agent is None or not pairs:
            return {p.pair_id: "UNSURE" for p in pairs}
        resp = deps.runner.invoke(agent, _request(deps, agent, DuplicateInput(pairs=pairs[:20]), "dedup"))
        out = {p.pair_id: "UNSURE" for p in pairs}
        if resp.status is not CallStatus.OK or resp.payload is None:
            return out
        supplied = {p.pair_id: {s.name for s in p.signals} for p in pairs}
        s = deps.settings
        for v in resp.payload.verdicts:
            if v.pair_id not in supplied:
                continue  # G1: unknown pair id is dropped
            if not set(v.matched_signals) <= supplied[v.pair_id] or not set(v.conflicting_signals) <= supplied[v.pair_id]:
                continue  # claims signals that were never supplied -> stays UNSURE
            if v.verdict == "SAME" and v.agent_confidence >= s.dedup_merge_conf and not v.conflicting_signals:
                out[v.pair_id] = "SAME"
            elif v.verdict == "DIFFERENT" and v.agent_confidence >= s.dedup_different_conf:
                out[v.pair_id] = "DIFFERENT"
        return out

    def judge(self, deps: ResearchDeps, name: str, parent_id: int, ambiguous_ids: list[int]) -> str:
        """Per-candidate use inside research_entity. Returns SAME only when the single best pair is merged."""
        eng = engine_for(deps)
        cand = MatchCandidate(name=name, normalized_name=normalize_name(name), parent_entity_id=parent_id)
        pairs = []
        for i, eid in enumerate(ambiguous_ids[:3], 1):
            card = eng.card(eid)
            other = MatchCandidate(name=card.name, normalized_name=normalize_name(card.name), parent_entity_id=None,
                                   registrable_domain=card.domains[0] if card.domains else None, country=card.country)
            pairs.append(DuplicatePair(pair_id=i, a=EntityCard(name=name), b=card,
                                       signals=compute_signals(cand, other, same_parent=False)))
        verdicts = self.verdicts(deps, pairs)
        same = [i for i, v in verdicts.items() if v == "SAME"]
        return "SAME" if len(same) == 1 else ("DIFFERENT" if all(v == "DIFFERENT" for v in verdicts.values()) else "UNSURE")
