"""Fixture world for exact-name claims (a name that already belongs to an existing entity). Fictional names only.

Group A's page also lists a sibling group by plain name ("Group B": no link, so no dedicated domain decides it).
Group B's page lists "Company A1" (an existing entity of Group A) and "Company A22" (a near-miss spelling of Company A2).
The dedup agent is configured to say DIFFERENT for everything: exact names must still never become new entities.
"""
from __future__ import annotations

from tests.fixtures import site as base
from tests.helpers.fakes import FakeHttpClient, resp

NEW_S = {
    "gbref": "Group B is a business unit of Group A.",
    "a1ref": "Company A1 is a brand of Group B.",
    "a22": "Company A22 is a subsidiary of Group B.",
}


def routes() -> dict:
    r = base.routes()
    url_a = "https://groupa.test/companies"
    r[url_a] = resp(url_a, 200, base.page("Companies of Group A", (
        base.card("Company A1", "https://a1.test/") + base.card("Company A2", "https://a2.test/")
        + f"<ul><li>Group B</li></ul><p>{base.S['a1']}</p><p>{base.S['a2']}</p><p>{NEW_S['gbref']}</p>"), nav=base.NAV))
    url_b = "https://groupb.test/companies"
    r[url_b] = resp(url_b, 200, base.page("Companies of Group B", (
        base.card("Company B1", "https://b1.test/") + f"<ul><li>Company A1</li><li>Company A22</li></ul>"
        + f"<p>{base.S['b1']}</p><p>{NEW_S['a1ref']}</p><p>{NEW_S['a22']}</p>"), nav=base.NAV))
    return r


def install(monkeypatch) -> FakeHttpClient:
    for k, v in NEW_S.items():
        monkeypatch.setitem(base.S, k, v)
    monkeypatch.setitem(base.EXTRACT, "https://groupa.test/companies", [
        base._ent("Company A1", etype="Subsidiary", key="a1"), base._ent("Company A2", etype="Subsidiary", key="a2"),
        base._ent("Group B", etype="Operating Group", key="gbref")])
    monkeypatch.setitem(base.EXTRACT, "https://groupb.test/companies", [
        base._ent("Company B1", etype="Subsidiary", key="b1"), base._ent("Company A1", etype="Subsidiary", key="a1ref"),
        base._ent("Company A22", etype="Subsidiary", key="a22")])
    monkeypatch.setitem(base.REL, ("Group B", "Company A22"), ("SUPPORTED", "Subsidiary of", "Subsidiary", "a22", 0.9))
    return FakeHttpClient(routes())
