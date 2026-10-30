"""Variant of the M4 fixture world for defect A (duplicate edges and sibling claims). Fictional names only.

Group A's page also names its sibling "Group B" (an invalid ownership claim: both are children of the root).
Group B's page names Company A2 twice, under two spellings that share the dedicated domain a2.test.
"""
from __future__ import annotations

from tests.fixtures import site as base
from tests.helpers.fakes import FakeHttpClient, resp

NEW_S = {
    "gbref": "Group B is a business unit of Group A.",
    "a2g": "Company A2 Group is a subsidiary of Group B.",
}


def routes() -> dict:
    r = base.routes()
    url_a = "https://groupa.test/companies"
    r[url_a] = resp(url_a, 200, base.page("Companies of Group A", (
        base.card("Company A1", "https://a1.test/") + base.card("Company A2", "https://a2.test/")
        + base.card("Group B", "https://groupb.test/")
        + f"<p>{base.S['a1']}</p><p>{base.S['a2']}</p><p>{NEW_S['gbref']}</p>"), nav=base.NAV))
    url_b = "https://groupb.test/companies"
    r[url_b] = resp(url_b, 200, base.page("Companies of Group B", (
        base.card("Company B1", "https://b1.test/") + base.card("Company A2", "https://a2.test/")
        + base.card("Company A2 Group", "https://a2.test/group")
        + f"<p>{base.S['b1']}</p><p>{base.S['a2b']}</p><p>{NEW_S['a2g']}</p>"), nav=base.NAV))
    return r


def install(monkeypatch) -> FakeHttpClient:
    """Patch the shared Brain data so the extraction agent reports what the variant pages say."""
    for k, v in NEW_S.items():
        monkeypatch.setitem(base.S, k, v)
    monkeypatch.setitem(base.EXTRACT, "https://groupa.test/companies", [
        base._ent("Company A1", etype="Subsidiary", key="a1"), base._ent("Company A2", etype="Subsidiary", key="a2"),
        base._ent("Group B", etype="Operating Group", key="gbref")])
    monkeypatch.setitem(base.EXTRACT, "https://groupb.test/companies", [
        base._ent("Company B1", etype="Subsidiary", key="b1"), base._ent("Company A2", etype="Subsidiary", key="a2b"),
        base._ent("Company A2 Group", etype="Subsidiary", key="a2g")])
    return FakeHttpClient(routes())
