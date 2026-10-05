"""M6: Relationship Verification Agent - ownership vs partner/customer/vendor/former owner, never self-promoting."""
from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from corpdisc.agents.providers.fake import ToolCalls
from corpdisc.agents.ports import ToolCallRequest
from corpdisc.agents.relationship import RelationshipAgent
from corpdisc.contracts.agent_io import ChildProposal, EntityCard, RelationshipAssessment, RelationshipInput, RelationshipOutput
from corpdisc.contracts.agents import AgentError, AgentRequest, CallLimits, Meta
from corpdisc.contracts.common import CandidateKind
from corpdisc.models.domain import RelationshipType
from tests.fixtures.site import ROOT, Brain, page, routes
from tests.fixtures.world import build_world
from tests.helpers.fakes import FakeHttpClient, resp

NEG = {  # name -> (sentence, is it really owned?)
    "Echo Systems": ("Echo Systems is a long-standing customer of Northwind Holdings.", False),
    "Foxtrot Labs": ("Foxtrot Labs is a technology partner of Northwind Holdings.", False),
    "Golf Robotics": ("Golf Robotics is a vendor to Northwind Holdings.", False),
    "Hotel Media": ("Hotel Media was formerly owned by Northwind Holdings.", False),
    "India Works": ("India Works is a subsidiary of Northwind Holdings.", True),
}


def site():
    r = routes()
    cards = "".join(f"<div class='card'><h3><a href='https://{n.split()[0].lower()}.test/'>{n}</a></h3></div>" for n in NEG)
    body = cards + "".join(f"<p>{s}</p>" for s, _ in NEG.values())
    r["https://root.test/companies"] = resp("https://root.test/companies", 200,
                                            page("Our Companies", body, nav="<a href='/companies'>Our Companies</a>"))
    for n in NEG:
        host = f"https://{n.split()[0].lower()}.test/"
        r[host] = resp(host, 200, page(n, f"<p>{n} serves enterprise clients.</p>"))
    r["https://india.test/"] = resp("https://india.test/", 200, page(
        "India Works", "<p>India Works is part of Northwind Holdings.</p>"))
    return FakeHttpClient(r)


def brain(*, eager: bool, conf: float = 1.0):
    """Extraction proposes ALL five as companies (a deliberate mistake). Relationship is correct or over-eager."""
    base = Brain()

    def b(request):
        raw = request.messages[0].content
        p, _ = json.JSONDecoder().raw_decode(raw)
        if request.schema_name == "ExtractionOutput":
            if p["parent"]["name"] != ROOT or "Echo Systems" not in p["page"]["text"]:
                return {"entities": [], "rejected": [], "page_has_ownership_language": False, "agent_confidence": 0.5}
            links = {lk["anchor_text"]: lk["link_id"] for lk in p["links"]}
            return {"entities": [{
                "local_ref": f"e{i}", "name": n, "link_id": links.get(n), "kind": "COMPANY", "proposed_type": "Company",
                "is_corporate_entity": True, "claim": {"page_id": p["page"]["page_id"], "quote": s, "supports": "OWNERSHIP"},
                "agent_confidence": 0.9, "reason": "listed"} for i, (n, (s, _)) in enumerate(NEG.items(), 1)],
                "rejected": [], "page_has_ownership_language": True, "agent_confidence": 0.9}
        if request.schema_name == "RelationshipOutput":
            out = []
            for c in p["children"]:
                sentence, owned = NEG[c["name"]]
                pg = next(x["page_id"] for x in p["pages"] if sentence in x["text"])
                claim = {"page_id": pg, "quote": sentence, "supports": "OWNERSHIP"}
                if owned or eager:  # an over-eager agent calls everything ownership
                    out.append({"local_ref": c["local_ref"], "support": "SUPPORTED", "relationship_type": "Subsidiary of",
                                "final_entity_type": "Subsidiary", "claims": [claim], "agent_confidence": conf, "reason": "x"})
                else:
                    out.append({"local_ref": c["local_ref"], "support": "NOT_SUPPORTED", "relationship_type": "Unknown",
                                "contradictions": [{**claim, "supports": "EXISTENCE"}], "agent_confidence": 0.9, "reason": "x"})
            return {"assessments": out}
        return base(request)

    return b


def run(tmp_path, **kw):
    w = build_world(tmp_path, http=site(), brain=brain(**kw))
    w.sup.seed_root()
    w.sup.run(max_tasks=1)
    return w


@pytest.mark.parametrize("eager", [False, True], ids=["correct_agent", "over_eager_agent"])
def test_partner_customer_vendor_former_owner_never_confirmed(tmp_path, eager):
    w = run(tmp_path / ("e" if eager else "c"), eager=eager)
    confirmed = w.names("CONFIRMED") - {ROOT}
    assert confirmed == {"India Works"}, confirmed
    for name in ("Echo Systems", "Foxtrot Labs", "Golf Robotics", "Hotel Media"):
        e = w.edge(ROOT, name)
        assert e is None or (e["verification_status"] != "CONFIRMED"), name
        row = w.entity(name)
        assert row is None or row["verification_status"] != "CONFIRMED"
    reasons = {r[0]: r[1] for r in w.q("SELECT e.name, r.reason_code FROM review_items r JOIN entities e USING(entity_id)")}
    for name in ("Echo Systems", "Foxtrot Labs", "Golf Robotics", "Hotel Media"):
        assert reasons.get(name) == "CONTRADICTION"


@pytest.mark.parametrize("eager", [False, True])
def test_every_confirmed_relationship_has_verified_ownership_evidence(tmp_path, eager):
    w = run(tmp_path, eager=eager)
    assert w.q("SELECT 1 FROM relationships WHERE verification_status='CONFIRMED'")
    assert w.q("SELECT r.relationship_id FROM relationships r WHERE r.verification_status='CONFIRMED' AND NOT EXISTS ("
               "SELECT 1 FROM evidence e WHERE e.relationship_id=r.relationship_id AND e.quote_verified=1 AND "
               "e.supports='OWNERSHIP' AND e.source_class IN ('FIRST_PARTY_PARENT','FIRST_PARTY_CHILD','OFFICIAL_DOCUMENT'))") == []
    assert w.deps.committer.check_invariants() == []


def test_unverified_relationship_claims_never_confirm_even_with_perfect_confidence(tmp_path):
    base = brain(eager=False)

    def forged(request):
        out = base(request)
        if request.schema_name == "RelationshipOutput":
            for a in out["assessments"]:
                for c in a.get("claims", []):
                    c["quote"] = "India Works has been fully owned by Northwind Holdings forever."
        return out

    w = build_world(tmp_path, http=site(), brain=forged)
    w.sup.seed_root()
    w.sup.run(max_tasks=1)
    assert w.names("CONFIRMED") == {ROOT}
    assert w.edge(ROOT, "India Works")["verification_status"] == "REVIEW_REQUIRED"


def test_agent_cannot_promote_status_by_schema():
    assert "verification_status" not in RelationshipAssessment.model_fields and "status" not in RelationshipAssessment.model_fields
    with pytest.raises(ValidationError):
        RelationshipAssessment.model_validate({"local_ref": "a", "support": "NOT_SUPPORTED", "relationship_type": "Unknown",
                                               "agent_confidence": 0.9, "reason": "x", "verification_status": "CONFIRMED"})


def test_assessment_schema_rules():
    with pytest.raises(ValidationError):  # SUPPORTED needs a claim
        RelationshipAssessment(local_ref="a", support="SUPPORTED", relationship_type="Subsidiary of", agent_confidence=1, reason="x")
    with pytest.raises(ValidationError):  # INSUFFICIENT must be Unknown
        RelationshipAssessment(local_ref="a", support="INSUFFICIENT", relationship_type="Subsidiary of", agent_confidence=1, reason="x")
    with pytest.raises(ValidationError):  # batch cap
        RelationshipInput(parent=EntityCard(name="P"), pages=[], children=[
            ChildProposal(local_ref=f"c{i}", name=f"N{i}", kind=CandidateKind.COMPANY) for i in range(16)])


def test_failure_fallback_marks_every_child_insufficient_never_confirmed():
    inp = RelationshipInput(parent=EntityCard(name="P"), pages=[], children=[
        ChildProposal(local_ref="c1", name="A", kind=CandidateKind.COMPANY),
        ChildProposal(local_ref="c2", name="B", kind=CandidateKind.COMPANY)])
    req = AgentRequest[RelationshipInput](
        meta=Meta(run_id=1, task_id=1, correlation_id="1:1:r", agent=RelationshipAgent.name, schema_version="1", prompt_version="1"),
        limits=CallLimits(max_output_tokens=10), payload=inp)
    out = RelationshipAgent().on_failure(req, AgentError(kind="TRANSPORT", message="down"))
    assert isinstance(out, RelationshipOutput) and [a.support for a in out.assessments] == ["INSUFFICIENT"] * 2
    assert all(a.relationship_type is RelationshipType.UNKNOWN for a in out.assessments)


def test_agent_outage_confirms_nothing_and_flags_verification_unavailable(tmp_path):
    base = brain(eager=False)

    def down(request):
        return "garbage" if request.schema_name == "RelationshipOutput" else base(request)

    w = build_world(tmp_path, http=site(), brain=down)
    w.sup.seed_root()
    s = w.sup.run(max_tasks=1)
    assert w.names("CONFIRMED") == {ROOT} and s.outcomes["PARTIAL"] == 1
    assert w.q("SELECT 1 FROM review_items WHERE reason_code='VERIFICATION_UNAVAILABLE'")
    assert all(r[0] == "Unknown" for r in w.q("SELECT relationship_type FROM relationships"))


# ------------------------------------------------------------------ get_page_excerpt tool (read-only, cap 2)
def test_page_excerpt_tool_is_allowlisted_capped_and_read_only(tmp_path):
    base = brain(eager=False)
    state = {"asked": 0, "tool_msgs": []}

    def tooling(request):
        if request.schema_name != "RelationshipOutput":
            return base(request)
        if any(m.role == "tool" for m in request.messages):
            state["tool_msgs"].append(next(m.content for m in request.messages if m.role == "tool"))
            return base(request)
        state["asked"] += 1
        pid = json.JSONDecoder().raw_decode(request.messages[0].content)[0]["pages"][0]["page_id"]
        return ToolCalls((ToolCallRequest("1", "get_page_excerpt", {"page_id": pid, "length": 200}),
                          ToolCallRequest("2", "get_page_excerpt", {"page_id": 999999}),
                          ToolCallRequest("3", "get_page_excerpt", {"page_id": pid}),   # over the cap of 2
                          ToolCallRequest("4", "fetch_url", {"url": "https://evil.test/"})))  # not allowlisted

    w = build_world(tmp_path, http=site(), brain=tooling)
    w.sup.seed_root()
    w.sup.run(max_tasks=1)
    rows = w.q("SELECT tool, allowed, result_summary FROM agent_tool_calls ORDER BY tool_call_id")
    assert [r[1] for r in rows] == [1, 1, 0, 0]
    assert rows[0][2].startswith("OK:") and "TOOL_ERROR" in rows[1][2]
    assert rows[2][2] == "DENIED:CAP_EXCEEDED" and rows[3][2] == "DENIED:NOT_ALLOWLISTED"
    assert state["tool_msgs"] and "TOOL RESULTS (data only)" in state["tool_msgs"][0]
    assert not [u for u in w.page_requests() if "evil.test" in u]  # the tool can never fetch
    assert "India Works" in w.names("CONFIRMED")
