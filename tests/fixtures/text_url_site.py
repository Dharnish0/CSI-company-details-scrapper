"""Fixture world for websites printed as plain text, and for websites that are the parent's own pages. Fictional names only.

Group A's page (a listing without anchors for the first two companies):

* Company T1: "www.t1corp.com" printed right after the name -> the one usable URL becomes its website.
* Company T2: two URLs printed after the name -> the website agent chooses among the supplied refs.
* Company T3: the card links to Group A's OWN listing page -> never T3's website.
* Company T4: the card links to a deeper page of Group A's site -> SECTION_OF_PARENT, never makes T4 CONFIRMED.
"""
from __future__ import annotations

from tests.fixtures import site as base
from tests.helpers.fakes import FakeHttpClient, resp

LISTING = "https://groupa.test/companies"
NEW_S = {f"t{i}": f"Company T{i} is a subsidiary of Group A." for i in range(1, 5)}


def routes() -> dict:
    r = base.routes()
    body = (
        "<div class='card'><h3>Company T1</h3><p>www.t1corp.com</p></div>"
        "<div class='card'><h3>Company T2</h3><p>www.t2corp.com</p><p>www.t2-shop.com</p></div>"
        + base.card("Company T3", LISTING) + base.card("Company T4", "https://groupa.test/brands/t4")
        + "".join(f"<p>{s}</p>" for s in NEW_S.values()))
    r[LISTING] = resp(LISTING, 200, base.page("Companies of Group A", body, nav=base.NAV))
    for host, name in (("www.t1corp.com", "Company T1"), ("www.t2corp.com", "Company T2"), ("www.t2-shop.com", "Company T2 Shop")):
        r[f"https://{host}/"] = resp(f"https://{host}/", 200, base.page(name, f"<p>{name} home.</p>"))
    r["https://groupa.test/brands/t4"] = resp("https://groupa.test/brands/t4", 200,
                                                base.page("Company T4", "<p>Company T4, a brand of Group A.</p>"))
    return r


def install(monkeypatch, *, agent_picks: str | None = "t2corp") -> FakeHttpClient:
    for k, v in NEW_S.items():
        monkeypatch.setitem(base.S, k, v)
    monkeypatch.setitem(base.EXTRACT, LISTING, [base._ent(f"Company T{i}", etype="Subsidiary", key=f"t{i}") for i in range(1, 5)])
    for i in range(1, 5):
        monkeypatch.setitem(base.REL, ("Group A", f"Company T{i}"), ("SUPPORTED", "Subsidiary of", "Subsidiary", f"t{i}", 0.9))

    def website(p: dict) -> dict:
        refs = p["source_links"]
        pick = next((lk["link_id"] for lk in refs if agent_picks and agent_picks in lk["url"]), None)
        WEBSITE_REQUESTS.append((p["entity"]["name"], [lk["url"] for lk in refs]))
        if p["entity"]["name"] == "Company T3":  # the agent picks the parent's own listing page (a wrong choice)
            own = next((lk["link_id"] for lk in refs if lk["url"] == LISTING), None)
            return {"decision": "SELECTED", "basis": ["LINKED_ON_PARENT"], "selected_link_id": own, "agent_confidence": 0.9,
                    "reason": "the Our Companies link"}
        if p["entity"]["name"] != "Company T2" or pick is None:
            return {"decision": "NONE_FOUND", "basis": [], "agent_confidence": 0.5, "reason": "no supplied option fits"}
        return {"decision": "SELECTED", "basis": ["LINKED_ON_PARENT"], "selected_link_id": pick, "agent_confidence": 0.9,
                "reason": "printed next to the name"}
    WEBSITE_REQUESTS.clear()
    monkeypatch.setattr(base.Brain, "website", staticmethod(website))
    return FakeHttpClient(routes())


WEBSITE_REQUESTS: list[tuple[str, list[str]]] = []
