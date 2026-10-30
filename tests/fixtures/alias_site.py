"""Fixture world for the n_commit alias crash. Fictional names only.

Group A's page lists Company A1 plus "Zephyr Systems" and "Zephyr Systems Group", two spellings of one company that share
zephyr.test. The relationship check does not support the claim (agent confidence 0.0, no ownership wording), so the
surviving candidate "Zephyr Systems" is REJECTED and never stored; its same-site variant is a duplicate_in_batch that
used to alias a ref that was not in the plan.
"""
from __future__ import annotations

from tests.fixtures import site as base
from tests.helpers.fakes import FakeHttpClient, resp

ZEPHYR_URL = "https://zephyr.test/"


def routes() -> dict:
    r = base.routes()
    url_a = "https://groupa.test/companies"
    r[url_a] = resp(url_a, 200, base.page("Companies of Group A", (
        base.card("Company A1", "https://a1.test/") + base.card("Zephyr Systems", ZEPHYR_URL)
        + base.card("Zephyr Systems Group", ZEPHYR_URL + "group")
        + f"<p>{base.S['a1']}</p><p>{base.S['zephyr']}</p>"), nav=base.NAV))
    r[ZEPHYR_URL] = resp(ZEPHYR_URL, 200, base.page("Zephyr Systems", "<p>Zephyr Systems home.</p>"))
    r[ZEPHYR_URL + "group"] = resp(ZEPHYR_URL + "group", 200, base.page("Zephyr Systems Group", "<p>Zephyr Systems Group.</p>"))
    return r


def install(monkeypatch) -> FakeHttpClient:
    monkeypatch.setitem(base.EXTRACT, "https://groupa.test/companies", [
        base._ent("Company A1", etype="Subsidiary", key="a1"),
        base._ent("Zephyr Systems", key="zephyr", conf=0.8),
        base._ent("Zephyr Systems Group", key="zephyr", conf=0.8)])
    monkeypatch.setitem(base.REL, ("Group A", "Zephyr Systems"), ("NOT_SUPPORTED", "Unknown", None, "zephyr", 0.0))
    return FakeHttpClient(routes())
