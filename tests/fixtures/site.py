"""Deterministic fixture world for M4: an in-memory website hierarchy plus a fixture-driven "brain".

Northwind Holdings (root)
 |- Group A  (groupa.test)
 |    |- Company A1 (a1.test) -- A1 Labs (a1labs.test)        <- multi-level
 |    |    '- also referenced as "A1 Analytics" (same site)   <- duplicate reference
 |    |- Company A2 (a2.test)  (Group B also lists A2)         <- duplicate across parents
 |    |- Zephyr Systems: a PARTNER, no website                 <- negative relationship
 |    '- "Aurora Suite", "DataBridge Pro": PRODUCTS            <- must never become entities
 '- Group B  (groupb.test)
      |- Company B1 (b1.test); B1's page lists Group B as its own unit   <- cycle candidate
      '- Dormant Co (dormant.test): connection refused                   <- dead website
"""
from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from tests.helpers.fakes import err, resp

ROOT = "Northwind Holdings"


def page(title: str, body: str, *, nav: str = "") -> str:
    return (f"<html><head><title>{title}</title></head><body><header><nav>{nav}</nav></header>"
            f"<main><h1>{title}</h1>{body}</main><footer><a href='/privacy'>Privacy</a></footer></body></html>")


NAV = "<a href='/companies'>Our Companies</a><a href='/careers'>Careers</a>"


def card(name: str, href: str) -> str:
    return f"<div class='card'><h3><a href='{href}'>{name}</a></h3></div>"


S = {  # the sentences that are quoted as evidence (each is its own <p>, so quotes are verbatim page text)
    "ga": "Group A is an operating group of Northwind Holdings.",
    "gb": "Group B is an operating group of Northwind Holdings.",
    "a1": "Company A1 is a subsidiary of Group A.",
    "a2": "Company A2 is a subsidiary of Group A.",
    "a1b": "A1 Analytics is the analytics brand of Company A1.",
    "zephyr": "Zephyr Systems is a technology partner of Group A.",
    "b1": "Company B1 is a subsidiary of Group B.",
    "dormant": "Dormant Co is a subsidiary of Group B.",
    "a2b": "Company A2 is also a brand of Group B.",
    "labs": "A1 Labs is a business unit of Company A1.",
    "cycle": "Group B is a business unit of Company B1.",
}


def routes() -> dict[str, Any]:
    r: dict[str, Any] = {}

    def add(url: str, html: str) -> None:
        r[url] = resp(url, 200, html)

    add("https://root.test/", page(ROOT, "<p>Welcome.</p>", nav=NAV))
    add("https://root.test/companies", page("Our Operating Groups", (
        card("Group A", "https://groupa.test/") + card("Group B", "https://groupb.test/")
        + f"<p>{S['ga']}</p><p>{S['gb']}</p>"), nav=NAV))
    add("https://root.test/careers", page("Careers", "<p>Join us.</p>"))
    add("https://groupa.test/", page("Group A", f"<p>{S['ga']}</p>", nav=NAV))
    add("https://groupa.test/companies", page("Companies of Group A", (
        card("Company A1", "https://a1.test/") + card("Company A2", "https://a2.test/")
        + f"<p>{S['a1']}</p><p>{S['a2']}</p>"
        + f"<p>{S['a1b']}</p><ul><li><a href='https://a1.test/?ref=analytics'>A1 Analytics</a></li></ul>"
        + f"<h2>Our Products and Services</h2><ul><li>Aurora Suite</li><li>DataBridge Pro</li></ul>"
        + f"<p>{S['zephyr']}</p>"), nav=NAV))
    add("https://groupa.test/careers", page("Careers", "<p>Jobs.</p>"))
    add("https://a1.test/", page("Company A1", f"<p>{S['a1']}</p>", nav=NAV))
    add("https://a1.test/?ref=analytics", page("Company A1", f"<p>{S['a1']}</p>"))
    add("https://a1.test/companies", page("Companies of A1", card("A1 Labs", "https://a1labs.test/")
                                          + f"<p>{S['labs']}</p>", nav=NAV))
    add("https://a1labs.test/", page("A1 Labs", "<p>A1 Labs, a division of Company A1.</p>"))
    add("https://a2.test/", page("Company A2", f"<p>{S['a2']}</p>"))
    add("https://groupb.test/", page("Group B", f"<p>{S['gb']}</p>", nav=NAV))
    add("https://groupb.test/companies", page("Companies of Group B", (
        card("Company B1", "https://b1.test/") + card("Dormant Co", "https://dormant.test/")
        + card("Company A2", "https://a2.test/")
        + f"<p>{S['b1']}</p><p>{S['dormant']}</p><p>{S['a2b']}</p>"), nav=NAV))
    add("https://b1.test/", page("Company B1", f"<p>{S['b1']}</p>", nav=NAV))
    add("https://b1.test/companies", page("Companies of B1", card("Group B", "https://groupb.test/")
                                          + f"<p>{S['cycle']}</p>", nav=NAV))
    r["https://dormant.test/"] = err("https://dormant.test/", "CONNECTION_ERROR", "connection refused")
    return r


# name -> (kind, entity type, relationship type, sentence key, agent confidence)
def _ent(name, kind="COMPANY", etype="Company", key="", conf=0.9):
    return {"name": name, "kind": kind, "type": etype, "key": key, "conf": conf}


EXTRACT: dict[str, list[dict]] = {
    "https://root.test/companies": [_ent("Group A", etype="Operating Group", key="ga"),
                                    _ent("Group B", etype="Operating Group", key="gb")],
    "https://groupa.test/companies": [
        _ent("Company A1", etype="Subsidiary", key="a1"), _ent("Company A2", etype="Subsidiary", key="a2"),
        _ent("A1 Analytics", key="a1b"), _ent("Zephyr Systems", key="zephyr", conf=0.8),
        _ent("Aurora Suite", kind="PRODUCT", etype=None), _ent("DataBridge Pro", kind="PRODUCT", etype=None)],
    "https://a1.test/companies": [_ent("A1 Labs", etype="Business Unit", key="labs")],
    "https://groupb.test/companies": [_ent("Company B1", etype="Subsidiary", key="b1"),
                                      _ent("Dormant Co", etype="Subsidiary", key="dormant"),
                                      _ent("Company A2", etype="Subsidiary", key="a2b")],
    "https://b1.test/companies": [_ent("Group B", etype="Operating Group", key="cycle")],
}
# (parent, child) -> (support, relationship type, final entity type, sentence key, confidence)
REL: dict[tuple[str, str], tuple] = {
    (ROOT, "Group A"): ("SUPPORTED", "Part of", "Operating Group", "ga", 0.9),
    (ROOT, "Group B"): ("SUPPORTED", "Part of", "Operating Group", "gb", 0.9),
    ("Group A", "Company A1"): ("SUPPORTED", "Subsidiary of", "Subsidiary", "a1", 0.9),
    ("Group A", "Company A2"): ("SUPPORTED", "Subsidiary of", "Subsidiary", "a2", 0.9),
    ("Group A", "A1 Analytics"): ("SUPPORTED", "Part of", "Brand", "a1b", 0.9),
    ("Group A", "Zephyr Systems"): ("NOT_SUPPORTED", "Unknown", None, "zephyr", 0.9),
    ("Company A1", "A1 Labs"): ("SUPPORTED", "Part of", "Business Unit", "labs", 0.9),
    ("Group B", "Company B1"): ("SUPPORTED", "Subsidiary of", "Subsidiary", "b1", 0.9),
    ("Group B", "Dormant Co"): ("SUPPORTED", "Subsidiary of", "Subsidiary", "dormant", 0.9),
}


class Brain:
    """Fixture-driven stand-in for the model. ``mutators`` let a test corrupt an output to prove the gates work."""

    def __init__(self) -> None:
        self.mutators: list[Callable[[str, dict], dict]] = []
        self.calls: list[str] = []

    def __call__(self, request) -> str:  # ProviderRequest -> JSON text
        text = request.messages[0].content
        payload, _ = json.JSONDecoder().raw_decode(text)
        self.calls.append(request.schema_name)
        fn = {"ExtractionOutput": self.extract, "DiscoveryOutput": self.discover,
              "RelationshipOutput": self.relationship, "WebsiteOutput": self.website}[request.schema_name]
        out = fn(payload)
        for m in self.mutators:
            out = m(request.schema_name, out)
        return json.dumps(out)

    @staticmethod
    def extract(p: dict) -> dict:
        page_ = p["page"]
        entities, rejected = [], []
        links = {lk["anchor_text"]: lk["link_id"] for lk in p["links"]}
        dom = {c["name_text"]: c["raw_candidate_id"] for c in p["dom_candidates"]}
        for i, e in enumerate(EXTRACT.get(page_["url"], []), 1):
            if e["kind"] != "COMPANY":
                rejected.append({"raw_candidate_id": dom.get(e["name"]), "name": e["name"], "kind": e["kind"],
                                 "reason": "product or service name, not a company"})
                continue
            if e["name"] not in page_["text"]:
                continue  # fixture only reports what is on this chunk
            entities.append({
                "local_ref": f"e{i}", "raw_candidate_id": dom.get(e["name"]), "name": e["name"],
                "link_id": links.get(e["name"]), "kind": "COMPANY", "proposed_type": e["type"],
                "is_corporate_entity": True, "relationship_hint": None, "description": None, "country": None,
                "industry": None, "claim": {"page_id": page_["page_id"], "quote": S[e["key"]], "supports": "OWNERSHIP"},
                "agent_confidence": e["conf"], "reason": "listed with ownership wording"})
        return {"entities": entities, "rejected": rejected, "page_has_ownership_language": bool(entities),
                "agent_confidence": 0.9}

    @staticmethod
    def discover(p: dict) -> dict:
        sel = [{"link_id": lk["link_id"], "page_type": "COMPANIES", "relevance": 0.8, "reason": "likely listing"}
               for lk in p["links"] if lk["det_score"] >= 0.3][: p["max_selections"]]
        return {"selections": sel, "no_relevant_pages": not sel, "agent_confidence": 0.8}

    @staticmethod
    def relationship(p: dict) -> dict:
        parent = p["parent"]["name"]
        out = []
        for c in p["children"]:
            spec = REL.get((parent, c["name"]))
            if spec is None:
                out.append({"local_ref": c["local_ref"], "support": "INSUFFICIENT", "relationship_type": "Unknown",
                            "agent_confidence": 0.3, "reason": "no ownership statement found"})
                continue
            support, rtype, etype, key, conf = spec
            quote = S[key]
            page_id = next((pg["page_id"] for pg in p["pages"] if quote in pg["text"]), p["pages"][0]["page_id"])
            claim = {"page_id": page_id, "quote": quote, "supports": "OWNERSHIP"}
            if support == "SUPPORTED":
                out.append({"local_ref": c["local_ref"], "support": support, "relationship_type": rtype,
                            "final_entity_type": etype, "claims": [claim], "agent_confidence": conf,
                            "reason": "page states ownership"})
            else:
                out.append({"local_ref": c["local_ref"], "support": support, "relationship_type": rtype,
                            "contradictions": [{**claim, "supports": "EXISTENCE"}], "agent_confidence": conf,
                            "reason": "wording is partner, not ownership"})
        return {"assessments": out}

    @staticmethod
    def website(p: dict) -> dict:
        return {"decision": "NONE_FOUND", "basis": [], "agent_confidence": 0.5, "reason": "no supplied option fits"}
