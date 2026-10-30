"""Defect B regression: a child task extracts its OWN start page; TERMINAL/UNREACHABLE always carry a reason."""
from __future__ import annotations

import pytest

from corpdisc.contracts.common import PageType
from corpdisc.services.links import build_link_index
from corpdisc.services.urls import canonicalize_url
from tests.fixtures import entry_site
from tests.fixtures.world import seeded_world


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    mp = pytest.MonkeyPatch()
    try:
        http = entry_site.install(mp)
        w = seeded_world(tmp_path_factory.mktemp("entry"), http=http, settings_kw={"max_links_per_entity": 4})
        w.summary = w.sup.run()
        yield w
    finally:
        mp.undo()


def state(w, name):
    return w.entity(name)["crawl_state"]


def note(w, name):
    return w.q("SELECT q.status, q.error FROM research_queue q JOIN entities e USING(entity_id) WHERE e.name=?", name)[0]


def test_trailing_slash_self_link_is_recognised_by_g8(world):
    base = "https://vl.test/portfolio/"
    idx = build_link_index("<a href='/portfolio'>Portfolio</a>", base)
    link = idx.links[0]
    assert canonicalize_url(base).canonical == canonicalize_url(link.url).canonical
    rej = world.q("SELECT gate_code, detail FROM gate_rejections WHERE item_ref LIKE '%vl.test/portfolio%'")
    assert rej and all(r["gate_code"] == "G8" for r in rej)
    assert "DUPLICATE_URL" in rej[0]["detail"] or "already visited" in rej[0]["detail"]


def test_entry_page_is_requested_exactly_once(world):
    for g in ("Jonas Like", "Vela Like", "Harris Like"):
        url = entry_site.START[g]
        hits = [u for u in world.page_requests() if canonicalize_url(u).canonical == canonicalize_url(url).canonical]
        assert len(hits) == 1, (g, hits)


def test_child_whose_start_page_is_the_listing_extracts_its_children(world):  # Jonas-like
    assert state(world, "Jonas Like") == "EXPANDED"
    assert {"Alpha Labs", "Beta Works"} <= world.names("CONFIRMED")
    assert world.edge("Jonas Like", "Alpha Labs") is not None
    assert world.brain.calls.count("ExtractionOutput") >= 4  # root + the three listing-like start pages


def test_trailing_slash_redirect_listing_is_not_marked_unreachable(world):  # Vela-like
    assert state(world, "Vela Like") == "EXPANDED"
    assert {"Gamma Soft", "Delta Soft"} <= world.names("CONFIRMED")


def test_home_with_ownership_wording_and_no_listing_links_is_extracted_once(world):  # Harris-like
    assert state(world, "Harris Like") == "EXPANDED"
    assert {"Epsilon Corp", "Zeta Corp", "Eta Corp"} <= world.names("CONFIRMED")
    assert world.q("SELECT COUNT(*) FROM entities WHERE name='Epsilon Corp'")[0][0] == 1


def test_home_without_listing_signals_is_terminal_with_reason(world):
    assert state(world, "Plain Co") == "TERMINAL"
    status, error = note(world, "Plain Co")
    assert status == "DONE" and error == "TERMINAL:NO_LISTING_LINKS"
    assert world.q("SELECT COUNT(*) FROM candidates c JOIN entities e ON e.entity_id=c.parent_entity_id "
                   "WHERE e.name='Plain Co'")[0][0] == 0


def test_unreachable_only_when_a_fetch_actually_failed(world):
    assert state(world, "Locked Co") == "UNREACHABLE"
    assert note(world, "Locked Co")["error"] == "UNREACHABLE:ALL_APPROVED_FETCHES_FAILED"
    assert world.q("SELECT 1 FROM gate_rejections WHERE gate_code='G6' AND item_ref LIKE '%lk.test/portfolio%'")


def test_cycle_back_to_parent_site_is_still_blocked(world):
    assert [u for u in world.page_requests() if u.rstrip("/").endswith("root.test/companies")] == [
        "https://root.test/companies"]
    assert world.q("SELECT COUNT(*) FROM research_queue q JOIN entities e USING(entity_id) "
                   "WHERE e.name='Jonas Like'")[0][0] == 1


def test_entry_page_attributed_to_child(world):
    for g in ("Jonas Like", "Vela Like", "Harris Like"):
        row = world.q("SELECT entity_id FROM crawl_pages WHERE canonical_url=?",
                      canonicalize_url(entry_site.START[g]).canonical)[0]
        assert row["entity_id"] == world.entity(g)["entity_id"]


def test_every_terminal_or_unreachable_task_has_a_reason(world):
    rows = world.q("SELECT e.name, e.crawl_state, q.error FROM research_queue q JOIN entities e USING(entity_id) "
                   "WHERE q.status='DONE' AND e.crawl_state IN ('TERMINAL','UNREACHABLE','NO_WEBSITE')")
    assert len(rows) >= 5  # plain, locked and the leaf companies
    for r in rows:
        assert r["error"] and r["error"].startswith(r["crawl_state"] + ":"), tuple(r)
