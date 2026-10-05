from __future__ import annotations

import pytest
from pydantic import ValidationError

from corpdisc.contracts import agents as agents_contracts
from corpdisc.contracts import common
from corpdisc.contracts.common import (
    MIN_QUOTE_CHARS,
    BudgetDecision,
    EvidenceClaim,
    LinkRef,
    RawCandidate,
    Signal,
    SourceClass,
    Supports,
    ToolCall,
    VerifiedEvidence,
)
from corpdisc.models import domain

GOOD_QUOTE = "Northwind Analytics is a wholly owned subsidiary of Northwind Holdings."


def _roundtrip(model):
    return type(model).model_validate_json(model.model_dump_json())


def test_enums_are_reexports_of_models_domain():
    for name in ("EntityType", "RelationshipType", "VerificationStatus", "SourceClass", "Supports",
                 "CandidateKind", "ReviewReason", "PageType", "WebsiteKind", "QueueStatus",
                 "CrawlStatus", "CrawlState", "RunStatus", "OperatingStatus"):
        assert getattr(common, name) is getattr(domain, name)


def test_strict_base_forbids_extra_and_is_frozen():
    link = LinkRef(link_id=1, url="https://a.test/x", anchor_text="x", location="nav",
                   same_domain=True, det_score=0.5)
    with pytest.raises(ValidationError):
        LinkRef(link_id=1, url="u", anchor_text="x", location="nav", same_domain=True,
                det_score=0.5, surprise=1)
    with pytest.raises(ValidationError):
        link.link_id = 2  # type: ignore[misc]
    assert _roundtrip(link) == link


def test_link_ref_bounds():
    with pytest.raises(ValidationError):
        LinkRef(link_id=1, url="u", anchor_text="x" * 201, location="body", same_domain=True, det_score=0.1)
    with pytest.raises(ValidationError):
        LinkRef(link_id=1, url="u", anchor_text="x", location="sidebar", same_domain=True, det_score=0.1)
    with pytest.raises(ValidationError):
        LinkRef(link_id=1, url="u", anchor_text="x", location="body", same_domain=True, det_score=1.2)


def test_raw_candidate_hints_and_lengths():
    rc = RawCandidate(raw_candidate_id=1, name_text="Acme", link_id=None, context="ctx", dom_hint="card")
    assert _roundtrip(rc) == rc
    with pytest.raises(ValidationError):
        RawCandidate(raw_candidate_id=1, name_text="Acme", link_id=None, context="c", dom_hint="div")
    with pytest.raises(ValidationError):
        RawCandidate(raw_candidate_id=1, name_text="A" * 201, link_id=None, context="c", dom_hint="card")
    with pytest.raises(ValidationError):
        RawCandidate(raw_candidate_id=1, name_text="A", link_id=None, context="c" * 401, dom_hint="card")


def test_evidence_claim_quote_length_rule():
    assert MIN_QUOTE_CHARS == 15
    EvidenceClaim(page_id=1, quote="x" * 15, supports=Supports.OWNERSHIP)
    EvidenceClaim(page_id=1, quote="x" * 400, supports=Supports.OWNERSHIP)
    with pytest.raises(ValidationError):
        EvidenceClaim(page_id=1, quote="x" * 14, supports=Supports.OWNERSHIP)
    with pytest.raises(ValidationError):
        EvidenceClaim(page_id=1, quote="x" * 401, supports=Supports.OWNERSHIP)


def _ev(**kw):
    base = dict(page_id=1, url="https://a.test/", quote=GOOD_QUOTE, quote_verified=True, char_start=0,
                char_end=10, source_class=SourceClass.FIRST_PARTY_PARENT, supports=Supports.OWNERSHIP)
    base.update(kw)
    return VerifiedEvidence(**base)


def test_verified_evidence_offset_validator():
    assert _roundtrip(_ev()) == _ev()
    with pytest.raises(ValidationError):
        _ev(char_start=None, char_end=None)
    with pytest.raises(ValidationError):
        _ev(char_start=5, char_end=5)
    with pytest.raises(ValidationError):
        _ev(char_start=-1, char_end=5)
    unverified = _ev(quote_verified=False, char_start=None, char_end=None)
    assert unverified.invocation_id is None
    with pytest.raises(ValidationError):
        _ev(quote_verified=False)  # offsets present on an unverified quote


def test_signal_names_and_values():
    Signal(name="NAME_EXACT", value=True)
    Signal(name="NAME_FUZZY", value=0.95)
    with pytest.raises(ValidationError):
        Signal(name="NAME_MAGIC", value=1)


def test_tool_call_and_budget_decision():
    assert _roundtrip(ToolCall(tool="lookup", args={"a": 1})).args == {"a": 1}
    with pytest.raises(ValidationError):
        ToolCall(tool="")
    d = BudgetDecision(allow=False, reason="DOMAIN_PAGE_LIMIT", detail="40 of 40")
    assert not d.allow and _roundtrip(d) == d


def test_agents_contract_is_generic_only():
    spec = agents_contracts.AgentSpec(
        name=agents_contracts.AgentName.DISCOVERY, schema_version="1", prompt_version="1",
        tool_allowlist=("registry_lookup",), max_tool_calls=2,
    )
    assert _roundtrip(spec) == spec
    assert spec.failure_policy.max_schema_repairs == 1
    public = {n for n in vars(agents_contracts) if not n.startswith("_")}
    # M2 adds the generic envelope here; per-agent payload models live in contracts/agent_io.py.
    for forbidden in ("DiscoveryInput", "DiscoveryOutput", "ExtractionInput", "ExtractionOutput",
                      "RelationshipInput", "RelationshipOutput"):
        assert forbidden not in public
    assert {"AgentRequest", "AgentResponse", "Meta", "CallLimits", "Usage", "AgentError"} <= public
    assert {m.value for m in agents_contracts.AgentName} == {
        "supervisor_advisor", "discovery", "extraction", "relationship_verification",
        "website_resolution", "deduplication", "validation"}


def test_contracts_import_nothing_from_db_services_agents():
    import ast
    import pathlib

    root = pathlib.Path(common.__file__).parent
    for path in root.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            elif isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            for n in names:
                assert not n.startswith(("corpdisc.db", "corpdisc.services", "corpdisc.agents")), (path, n)
