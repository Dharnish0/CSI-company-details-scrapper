"""M7: Website Resolution Agent + SearchService - official site from supplied options only, gated by G1/G5/G6/G7."""
from __future__ import annotations

import json

import pytest

from corpdisc.agents.website import WebsiteAgent
from corpdisc.contracts.agent_io import WebsiteOutput
from corpdisc.services.search import RawHit, SearchService
from tests.fixtures.site import ROOT, S, Brain, page, routes
from tests.fixtures.world import build_world
from tests.helpers.fakes import FakeHttpClient, resp

SENT = "Orchid Works is a subsidiary of Northwind Holdings."


def site():
    r = routes()
    r["https://root.test/companies"] = resp("https://root.test/companies", 200, page(
        "Our Companies", f"<p>{SENT}</p>", nav="<a href='/companies'>Our Companies</a>"))  # no link to Orchid's site
    r["https://orchid.test/"] = resp("https://orchid.test/", 200, page("Orchid Works", "<p>Orchid Works is part of Northwind Holdings.</p>"))
    r["https://wrong.test/"] = resp("https://wrong.test/", 200, page("Totally Different Inc", "<p>Something else.</p>"))
    return FakeHttpClient(r)


class Provider:
    def __init__(self, hits):
        self.hits, self.queries = hits, []

    def search(self, query, *, limit):
        self.queries.append(query)
        if isinstance(self.hits, Exception):
            raise self.hits
        return self.hits


HITS = [RawHit("https://www.linkedin.com/company/orchid-works", "Orchid Works | LinkedIn", "profile"),
        RawHit("https://orchid.test/?utm_source=x", "Orchid Works", "official"),
        RawHit("https://orchid.test/", "dup", "dup"),
        RawHit("https://wrong.test/", "Wrong", "other")]


def brain(select, conf=0.9, decision="SELECTED"):
    base = Brain()

    def b(request):
        p, _ = json.JSONDecoder().raw_decode(request.messages[0].content)
        if request.schema_name == "ExtractionOutput":
            if SENT not in p["page"]["text"]:
                return {"entities": [], "rejected": [], "page_has_ownership_language": False, "agent_confidence": 0.5}
            return {"entities": [{"local_ref": "e1", "name": "Orchid Works", "kind": "COMPANY", "proposed_type": "Subsidiary",
                                  "is_corporate_entity": True, "claim": {"page_id": p["page"]["page_id"], "quote": SENT,
                                                                         "supports": "OWNERSHIP"},
                                  "agent_confidence": 0.9, "reason": "x"}], "rejected": [],
                    "page_has_ownership_language": True, "agent_confidence": 0.9}
        if request.schema_name == "RelationshipOutput":
            pg = p["pages"][0]["page_id"]
            return {"assessments": [{"local_ref": c["local_ref"], "support": "SUPPORTED", "relationship_type": "Subsidiary of",
                                     "final_entity_type": "Subsidiary", "claims": [{"page_id": pg, "quote": SENT, "supports": "OWNERSHIP"}],
                                     "agent_confidence": 0.95, "reason": "x"} for c in p["children"]]}
        if request.schema_name == "WebsiteOutput":
            hit = select(p)
            if hit is None:
                return {"decision": "NONE_FOUND", "basis": [], "agent_confidence": conf, "reason": "none"}
            return {"decision": decision, "selected_hit_id": hit, "basis": ["NAME_MATCH"], "agent_confidence": conf, "reason": "x"}
        return base(request)

    return b


def by_domain(domain):
    return lambda p: next((h["hit_id"] for h in p["search_hits"] if h["domain"].endswith(domain)), None)


def run(tmp_path, select, hits=HITS, **kw):
    prov = Provider(hits)
    w = build_world(tmp_path, http=site(), brain=brain(select, **kw), search=SearchService(prov))
    w.sup.seed_root()
    w.sup.run(max_tasks=1)
    return w, prov


def test_search_service_canonicalises_dedupes_and_drops_shared_platforms():
    svc = SearchService(Provider(HITS))
    hits = svc.hits_for("Orchid Works", ROOT)
    assert [h.url for h in hits] == ["https://orchid.test/", "https://wrong.test/"]  # linkedin dropped, tracking param + dup merged
    assert [h.hit_id for h in hits] == [1, 2] and "Orchid Works" in svc.queries[0]
    assert SearchService(Provider(RuntimeError("down"))).hits_for("X", "Y") == []  # never raises
    assert len(SearchService(Provider(HITS), max_hits=1).hits_for("X", "Y")) == 1


def test_agent_selects_supplied_hit_and_entity_is_confirmed_with_search_website(tmp_path):
    w, prov = run(tmp_path, by_domain("orchid.test"))
    assert prov.queries and w.entity("Orchid Works")["verification_status"] == "CONFIRMED"
    assert tuple(w.q("SELECT resolution_method, kind FROM websites WHERE resolution_method<>'CONFIG'")[0]) == ("SEARCH_AGENT", "OFFICIAL")
    assert w.entity("Orchid Works")["primary_website_id"] is not None


def test_unknown_hit_id_is_rejected_by_g1(tmp_path):
    w, _ = run(tmp_path, lambda p: 77)
    assert w.q("SELECT 1 FROM gate_rejections WHERE gate_code='G1' AND item_ref LIKE 'hit_id=%'")
    assert w.entity("Orchid Works")["verification_status"] == "REVIEW_REQUIRED" and w.entity("Orchid Works")["crawl_state"] == "NO_WEBSITE"


def test_wrong_site_fails_identity_check_g7_and_is_not_primary(tmp_path):
    w, _ = run(tmp_path, by_domain("wrong.test"))
    assert w.q("SELECT 1 FROM gate_rejections WHERE gate_code='G7'")
    assert w.entity("Orchid Works")["verification_status"] != "CONFIRMED" and w.entity("Orchid Works")["primary_website_id"] is None


def test_low_confidence_selection_is_discarded(tmp_path):
    w, _ = run(tmp_path, by_domain("orchid.test"), conf=0.5)
    assert w.entity("Orchid Works")["verification_status"] == "REVIEW_REQUIRED" and w.q("SELECT 1 FROM websites WHERE resolution_method<>'CONFIG'") == []


def test_without_a_search_service_only_parent_links_are_used(tmp_path):
    w = build_world(tmp_path, http=site(), brain=brain(by_domain("orchid.test")))
    w.sup.seed_root()
    w.sup.run(max_tasks=1)
    assert w.entity("Orchid Works")["verification_status"] == "REVIEW_REQUIRED"  # nothing supplied to select


def test_agent_failure_means_no_website_never_a_guess(tmp_path):
    base = brain(by_domain("orchid.test"))
    w = build_world(tmp_path, http=site(), search=SearchService(Provider(HITS)),
                    brain=lambda r: "garbage" if r.schema_name == "WebsiteOutput" else base(r))
    w.sup.seed_root()
    w.sup.run(max_tasks=1)
    assert w.q("SELECT 1 FROM websites WHERE resolution_method<>'CONFIG'") == []
    assert w.entity("Orchid Works")["verification_status"] == "REVIEW_REQUIRED"


def test_output_schema_and_contract():
    with pytest.raises(ValueError):
        WebsiteOutput(decision="SELECTED", agent_confidence=0.9, reason="x")  # needs exactly one id
    with pytest.raises(ValueError):
        WebsiteOutput(decision="SELECTED", selected_hit_id=1, selected_link_id=2, agent_confidence=0.9, reason="x")
    assert WebsiteAgent.allowed_tools == {}  # runtime pre-fetches the hits; the agent cannot search or fetch
