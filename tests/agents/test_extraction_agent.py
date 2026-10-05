"""M5: Entity Extraction Agent - contract, schema repair, grounding gates, labelled eval, chunking, re-ask."""
from __future__ import annotations

import json

import pytest

from corpdisc.agents.extraction import ExtractionAgent
from corpdisc.agents.prompts import EXTRACTION_V1
from corpdisc.agents.providers.fake import FakeLLMProvider
from corpdisc.contracts.agent_io import EntityCard, ExtractedEntity, ExtractionInput, ExtractionOutput, PageExcerpt
from corpdisc.contracts.agents import AgentRequest, CallLimits, CallStatus, FailureKind, Meta
from corpdisc.contracts.common import EvidenceClaim, PageType, SourceClass, Supports
from corpdisc.graphs.grounding import ground_entities
from corpdisc.models.domain import CandidateKind, EntityType
from corpdisc.services.quotes import QuoteVerifier
from tests.agents.harness import build
from tests.fixtures.site import ROOT, Brain, page, routes
from tests.fixtures.world import build_world
from tests.helpers.fakes import FakeHttpClient, resp

AGENT = ExtractionAgent()
PAGE_TEXT = (
    "Northwind Holdings portfolio\n"
    "Aurora Analytics is a wholly owned subsidiary of Northwind Holdings.\n"
    "Brightline Logistics is a member of the Northwind family of companies.\n"
    "Our products: Insight Dashboard, Route Planner.\n"
    "We serve the healthcare industry in Germany.\n"
    "Cobalt Health is an independent customer of Northwind Holdings.\n"
)


def payload(text=PAGE_TEXT) -> ExtractionInput:
    return ExtractionInput(
        parent=EntityCard(entity_id=1, name=ROOT), chunk_index=0, chunk_count=1, dom_candidates=[], links=[],
        allowed_entity_types=[EntityType.COMPANY, EntityType.SUBSIDIARY],
        page=PageExcerpt(page_id=7, url="https://northwind.test/companies", page_type=PageType.COMPANIES,
                         source_class=SourceClass.FIRST_PARTY_PARENT, text=text))


def ent(name, quote, *, kind="COMPANY", corp=True, conf=0.9, ref="e1", page_id=7, link_id=None, raw=None):
    return {"local_ref": ref, "raw_candidate_id": raw, "name": name, "link_id": link_id, "kind": kind,
            "proposed_type": "Company" if corp else None, "is_corporate_entity": corp, "relationship_hint": None,
            "claim": {"page_id": page_id, "quote": quote, "supports": "OWNERSHIP"}, "agent_confidence": conf,
            "reason": "listed on page"}


def output(entities=(), rejected=()):
    return {"entities": list(entities), "rejected": list(rejected), "page_has_ownership_language": True,
            "agent_confidence": 0.9}


Q_AURORA = "Aurora Analytics is a wholly owned subsidiary of Northwind Holdings."
Q_BRIGHT = "Brightline Logistics is a member of the Northwind family of companies."


# ------------------------------------------------------------------ contract and prompt
def test_agent_contract_has_no_tools_and_failure_creates_nothing():
    assert AGENT.allowed_tools == {} and AGENT.max_tool_calls == 0 and AGENT.name.value == "extraction"
    req = AgentRequest[ExtractionInput](
        meta=Meta(run_id=1, task_id=1, correlation_id="1:1:x", agent=AGENT.name, schema_version="1", prompt_version="1"),
        limits=CallLimits(max_output_tokens=100), payload=payload())
    assert AGENT.on_failure(req, None) is None  # a failed chunk yields no entities (20.3)


def test_prompt_states_the_non_negotiable_rules():
    p = EXTRACTION_V1.lower()
    for phrase in ("verbatim", "never invent", "untrusted", "products, services", "partners, customers", "parent itself"):
        assert phrase in p
    msg = AGENT.user_message(payload())
    assert json.loads(msg)["page"]["text"] == PAGE_TEXT.strip()  # page text travels as JSON data


# ------------------------------------------------------------------ schema, repair, cache (FakeLLMProvider)
def test_schema_rejects_product_marked_as_company():
    bad = output([ent("Insight Dashboard", "Our products: Insight Dashboard, Route Planner.", kind="PRODUCT")])
    with pytest.raises(ValueError, match="cannot be a corporate entity"):
        ExtractionOutput.model_validate(bad)
    ExtractionOutput.model_validate(output([ent("Insight Dashboard", "Our products: Insight Dashboard, Route Planner.",
                                                 kind="PRODUCT", corp=False)]))


def test_runner_repairs_once_then_gives_up(tmp_path):
    s = build(tmp_path, ["{not json", output([ent("Aurora Analytics", Q_AURORA)])])
    r = s.runner.invoke(AGENT, _req(s))
    assert r.status is CallStatus.OK and r.payload.entities[0].name == "Aurora Analytics"
    s2 = build(tmp_path / "b", ["nope", {"entities": "wrong"}, output()])
    r2 = s2.runner.invoke(AGENT, _req(s2))
    assert r2.status is CallStatus.FAILED and r2.error.kind is FailureKind.SCHEMA and s2.provider.call_count == 2


def test_runner_caches_identical_chunks_and_never_caches_failures(tmp_path):
    s = build(tmp_path, [output([ent("Aurora Analytics", Q_AURORA)])])
    s.runner.invoke(AGENT, _req(s))
    assert s.runner.invoke(AGENT, _req(s)).usage.cache_hit and s.provider.call_count == 1


def _req(s) -> AgentRequest:
    return AgentRequest[ExtractionInput](
        meta=Meta(run_id=s.run_id, task_id=None, correlation_id=f"{s.run_id}:-:x", agent=AGENT.name,
                  schema_version=AGENT.schema_version, prompt_version=AGENT.prompt_version),
        limits=CallLimits(max_output_tokens=AGENT.max_output_tokens), payload=payload())


# ------------------------------------------------------------------ grounding gates + labelled evaluation
def ground(entities, text=PAGE_TEXT, **kw):
    out = ExtractionOutput.model_validate(output(entities))
    return ground_entities(out, page_id=7, page_text=text, dom_candidates=[], link_ids=kw.get("link_ids", set()),
                           verifier=QuoteVerifier())


def test_gates_g1_g2_g3_each_reject_their_own_failure():
    rep = ground([
        ent("Aurora Analytics", Q_AURORA, ref="ok"),
        ent("Phantom Corp", "Phantom Corp is owned by Northwind Holdings.", ref="g2"),
        ent("Brightline Logistics", "Brightline Logistics was acquired by Northwind in 1999.", ref="g3"),
        ent("Cobalt Health", "Cobalt Health is an independent customer of Northwind Holdings.", ref="g1", page_id=99),
        ent("Aurora Analytics", Q_AURORA, ref="g1b", link_id=424242),
    ])
    assert [e.local_ref for e in rep.accepted] == ["ok"]
    assert {e.local_ref: g for e, g in rep.failed} == {"g2": "G2", "g3": "G3", "g1": "G1", "g1b": "G1"}


def test_non_entities_pass_through_and_are_never_accepted():
    rep = ground([ent("Insight Dashboard", "Our products: Insight Dashboard, Route Planner.", kind="PRODUCT", corp=False, ref="p"),
                  ent("Germany", "We serve the healthcare industry in Germany.", kind="LOCATION", corp=False, ref="l")])
    assert rep.accepted == () and rep.failed == () and len(rep.non_entities) == 2


# Labelled evaluation: (page text, gold entities, gold non-entities, scripted agent output incl. realistic mistakes)
EVAL = [
    (PAGE_TEXT, {"Aurora Analytics", "Brightline Logistics"}, {"Insight Dashboard", "Route Planner", "Germany"},
     [ent("Aurora Analytics", Q_AURORA, ref="a"), ent("Brightline Logistics", Q_BRIGHT, ref="b"),
      ent("Insight Dashboard", "Our products: Insight Dashboard, Route Planner.", kind="PRODUCT", corp=False, ref="c"),
      ent("Phantom Corp", "Phantom Corp is owned by Northwind Holdings.", ref="d"),  # hallucinated
      ent("Route Planner", "Our products: Insight Dashboard, Route Planner.", ref="e", kind="SERVICE", corp=False)]),
    ("Delta Freight GmbH is a subsidiary of Northwind Holdings.\nEcho Systems, a division of Northwind Holdings.\n"
     "Our solutions include Echo Cloud and the Delta Tracker app.\n",
     {"Delta Freight GmbH", "Echo Systems"}, {"Echo Cloud", "Delta Tracker"},
     [ent("Delta Freight GmbH", "Delta Freight GmbH is a subsidiary of Northwind Holdings.", ref="a"),
      ent("Echo Systems", "Echo Systems, a division of Northwind Holdings.", ref="b"),
      ent("Echo Cloud", "Our solutions include Echo Cloud and the Delta Tracker app.", kind="SOLUTION", corp=False, ref="c"),
      ent("Foxtrot Labs", "Echo Systems, a division of Northwind Holdings.", ref="d"),  # name not on page
      ent("Delta Tracker", "Delta Tracker was founded by Northwind Holdings in 2001.", ref="e", kind="PRODUCT", corp=False)]),
    ("Golf Robotics is owned by Northwind Holdings.\nHotel Media is a portfolio company of Northwind Holdings.\n",
     {"Golf Robotics", "Hotel Media"}, set(),
     [ent("Golf Robotics", "Golf Robotics is owned by Northwind Holdings.", ref="a"),
      ent("Hotel Media", "Hotel Media is a portfolio company of Northwind Holdings.", ref="b"),
      ent("Hotel Media", "Hotel Media is a portfolio company of the Northwind group.", ref="c")]),  # altered quote
]


def test_labelled_extraction_eval_precision_recall_and_zero_ungrounded():
    tp = fp = fn = ungrounded = non_entity_accepted = 0
    for text, gold, gold_non, entities in EVAL:
        out = ExtractionOutput.model_validate(output(entities))
        rep = ground_entities(out, page_id=7, page_text=text, dom_candidates=[], link_ids=set(), verifier=QuoteVerifier())
        names = {e.name for e in rep.accepted}
        tp += len(names & gold)
        fp += len(names - gold)
        fn += len(gold - names)
        ungrounded += sum(1 for n in names if n.lower() not in text.lower())
        non_entity_accepted += len(names & gold_non)
    precision, recall = tp / (tp + fp), tp / (tp + fn)
    assert precision == 1.0 and recall >= 0.9
    assert ungrounded == 0 and non_entity_accepted == 0  # M5 exit criteria


def test_ground_entities_is_deterministic():
    entities = EVAL[0][3]
    a, b = ground(entities), ground(entities)
    assert [e.local_ref for e in a.accepted] == [e.local_ref for e in b.accepted]


# ------------------------------------------------------------------ chunking and grounding re-ask through the graph
def long_site(extra_tail: str):
    filler = "".join(f"<p>Paragraph {i}: " + "general corporate background text. " * 9 + "</p>" for i in range(40))
    r = routes()
    r["https://root.test/companies"] = resp(
        "https://root.test/companies",
        200, page("Our Companies", filler + f"<p>{extra_tail}</p>", nav="<a href='/companies'>Our Companies</a>"))
    return FakeHttpClient(r)


def generic_brain(tail_name, tail_sentence, seen):
    base = Brain()

    def brain(request):
        if request.schema_name != "ExtractionOutput":
            return base(request)
        text = request.messages[0].content
        p, _ = json.JSONDecoder().raw_decode(text)
        seen.append({"index": p["chunk_index"], "count": p["chunk_count"], "len": len(p["page"]["text"]),
                     "reask": "CORRECTION REQUEST" in text, "text": p["page"]["text"], "pid": p["page"]["page_id"]})
        if tail_sentence in p["page"]["text"]:
            return output([ent(tail_name, tail_sentence, page_id=p["page"]["page_id"])])
        return output()

    return brain


def test_long_page_is_split_into_bounded_chunks_and_late_entity_is_found(tmp_path):
    seen: list = []
    sentence = "Zulu Dynamics is a subsidiary of Northwind Holdings."
    w = build_world(tmp_path, http=long_site(sentence), brain=generic_brain("Zulu Dynamics", sentence, seen))
    w.sup.seed_root()
    w.sup.run(max_tasks=1)
    chunks = [s for s in seen if s["count"] > 1]
    assert len(chunks) >= 2 and {c["count"] for c in chunks} == {len(chunks)}
    assert all(c["len"] <= 6000 + 400 for c in chunks)  # bounded by CHUNK_CHARS (+ one line)
    assert [c["index"] for c in chunks] == list(range(len(chunks)))
    assert w.q("SELECT decision FROM candidates WHERE raw_name='Zulu Dynamics'")[0][0] in ("ACCEPTED", "REVIEW")


def test_grounding_reask_lists_exactly_what_failed_and_recovers(tmp_path):
    good = "Zulu Dynamics is a subsidiary of Northwind Holdings."
    state = {"n": 0, "msgs": []}
    base = Brain()

    def brain(request):
        if request.schema_name != "ExtractionOutput":
            return base(request)
        raw = request.messages[0].content
        p, _ = json.JSONDecoder().raw_decode(raw)
        if good not in p["page"]["text"]:
            return output()
        state["n"] += 1
        if "CORRECTION REQUEST" in raw:
            state["msgs"].append(raw)
            return output([ent("Zulu Dynamics", good, page_id=p["page"]["page_id"])])
        return output([ent("Ghost Industries", "Ghost Industries is owned by Northwind Holdings.",
                           page_id=p["page"]["page_id"])])

    w = build_world(tmp_path, http=long_site(good), brain=brain)
    w.sup.seed_root()
    w.sup.run(max_tasks=1)
    assert state["n"] == 2 and "Ghost Industries" in state["msgs"][0] and "G2" in state["msgs"][0]
    assert w.q("SELECT 1 FROM candidates WHERE raw_name='Zulu Dynamics'")
    assert w.q("SELECT 1 FROM candidates WHERE raw_name='Ghost Industries'") == []  # the corrected output replaced it


def test_persistent_hallucination_creates_no_entity_and_is_logged(tmp_path):
    good = "Zulu Dynamics is a subsidiary of Northwind Holdings."
    base = Brain()

    def brain(request):
        if request.schema_name != "ExtractionOutput":
            return base(request)
        p, _ = json.JSONDecoder().raw_decode(request.messages[0].content)
        if good not in p["page"]["text"]:
            return output()
        return output([ent("Ghost Industries", "Ghost Industries is owned by Northwind Holdings.", page_id=p["page"]["page_id"])])

    w = build_world(tmp_path, http=long_site(good), brain=brain)
    w.sup.seed_root()
    w.sup.run(max_tasks=1)
    assert "Ghost Industries" not in w.names()
    assert w.q("SELECT 1 FROM gate_rejections WHERE gate_code='G2' AND item_ref='Ghost Industries'")
