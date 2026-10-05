"""Deterministic grounding of Extraction Agent output (gates G1, G2, G3). Pure: no DB, no network, no LLM.

An extracted entity is accepted only if (G1) every id it references was supplied in this chunk, (G2) its name
appears in the page text or a supplied DOM candidate, and (G3) its quote appears verbatim (normalised) in the
page text. Non-entities (products, services, ...) are passed through for the caller to record as rejected.
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from dataclasses import dataclass

from corpdisc.contracts.agent_io import NON_ENTITY_KINDS, ExtractedEntity, ExtractionOutput
from corpdisc.contracts.common import RawCandidate
from corpdisc.gates import check_g2_name_grounding, check_g3_quote_grounding
from corpdisc.services.quotes import QuoteVerifier


@dataclass(frozen=True)
class GroundingReport:
    accepted: tuple[ExtractedEntity, ...]  # corporate entities that passed G1-G3
    failed: tuple[tuple[ExtractedEntity, str], ...]  # (entity, gate code)
    non_entities: tuple[ExtractedEntity, ...]  # agent itself said product/service/etc.


def is_non_entity(e: ExtractedEntity) -> bool:
    return (not e.is_corporate_entity) or e.kind in NON_ENTITY_KINDS


def ground_entities(
    out: ExtractionOutput,
    *,
    page_id: int,
    page_text: str,
    dom_candidates: Sequence[RawCandidate],
    link_ids: Collection[int],
    verifier: QuoteVerifier,
) -> GroundingReport:
    dom_names = " ".join(c.name_text for c in dom_candidates) or None
    dom_ids = {c.raw_candidate_id for c in dom_candidates}
    accepted, failed, non = [], [], []
    for e in out.entities:
        if is_non_entity(e):
            non.append(e)
        elif e.claim.page_id != page_id or (e.link_id is not None and e.link_id not in link_ids) or (
                e.raw_candidate_id is not None and e.raw_candidate_id not in dom_ids):
            failed.append((e, "G1"))
        elif not check_g2_name_grounding(e.name, page_text, dom_names).allowed:
            failed.append((e, "G2"))
        elif not check_g3_quote_grounding(e.claim.quote, page_text, verifier).allowed:
            failed.append((e, "G3"))
        else:
            accepted.append(e)
    return GroundingReport(tuple(accepted), tuple(failed), tuple(non))
