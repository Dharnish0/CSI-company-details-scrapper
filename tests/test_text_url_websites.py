"""Websites printed as plain text become grounded link refs; the parent's own pages are never a child's website."""
from __future__ import annotations

from tests.fixtures import text_url_site
from tests.fixtures.world import seeded_world


def run_world(tmp_path, monkeypatch, **kw):
    http = text_url_site.install(monkeypatch, **kw)
    w = seeded_world(tmp_path, http=http)
    return w, w.sup.run()


def site_of(w, name):
    return w.q("SELECT w.url, w.kind, w.resolution_method, w.reachable FROM websites w JOIN entities e "
               "USING(entity_id) WHERE e.name=?", name)


def test_single_url_printed_after_the_name_is_its_website_and_the_child_can_be_confirmed_and_queued(tmp_path, monkeypatch):
    w, _ = run_world(tmp_path, monkeypatch)
    t1 = w.entity("Company T1")
    assert t1["verification_status"] == "CONFIRMED"
    (row,) = site_of(w, "Company T1")
    assert row["url"].startswith("https://t1corp.com") or "t1corp.com" in row["url"]
    assert row["kind"] == "OFFICIAL" and row["resolution_method"] == "TEXT_URL_NEAR_NAME" and row["reachable"] == 1
    # the website was really fetched (robots.txt is checked by the fetch service) and the child is queued for crawling
    assert any("t1corp.com" in u for u in w.page_requests())
    assert w.q("SELECT depth FROM research_queue WHERE entity_id=?", t1["entity_id"])[0][0] == 2


def test_several_urls_go_to_the_website_agent_which_can_only_pick_a_supplied_ref(tmp_path, monkeypatch):
    w, _ = run_world(tmp_path, monkeypatch)
    asked = dict(text_url_site.WEBSITE_REQUESTS)
    urls = asked["Company T2"]
    assert any("t2corp.com" in u for u in urls[:2]) and any("t2-shop.com" in u for u in urls[:2])   # near URLs come first
    (row,) = site_of(w, "Company T2")
    assert "t2corp.com" in row["url"] and row["resolution_method"] == "LINKED_ON_PARENT" and row["kind"] == "OFFICIAL"
    assert "Company T1" not in asked                                      # T1 needed no agent call


def test_a_url_that_is_not_in_the_page_text_cannot_be_chosen(tmp_path, monkeypatch):
    w, _ = run_world(tmp_path, monkeypatch, agent_picks=None)           # the agent returns NONE_FOUND for T2
    assert site_of(w, "Company T2") == []
    assert w.entity("Company T2")["verification_status"] == "REVIEW_REQUIRED"
    assert w.q("SELECT reason_code FROM review_items WHERE entity_id=?", w.entity("Company T2")["entity_id"])[0][0] == "NO_WEBSITE"


def test_parents_own_listing_page_is_rejected_as_a_childs_website(tmp_path, monkeypatch):
    w, _ = run_world(tmp_path, monkeypatch)
    assert site_of(w, "Company T3") == []
    rej = w.q("SELECT detail FROM gate_rejections WHERE gate_code='G14' AND detail LIKE '%Company T3%'")
    assert len(rej) >= 1 and "own listing page" in rej[0][0]
    t3 = w.entity("Company T3")
    assert t3["verification_status"] != "CONFIRMED"
    assert w.q("SELECT 1 FROM research_queue WHERE entity_id=?", t3["entity_id"]) == []


def test_a_deeper_page_of_the_parents_site_is_section_of_parent_and_never_confirms(tmp_path, monkeypatch):
    w, _ = run_world(tmp_path, monkeypatch)
    t4 = w.entity("Company T4")
    (row,) = site_of(w, "Company T4")
    assert row["kind"] == "SECTION_OF_PARENT"
    assert t4["verification_status"] == "REVIEW_REQUIRED"
    assert w.q("SELECT reason_code FROM review_items WHERE entity_id=?", t4["entity_id"])[0][0] == "NO_WEBSITE"
    assert w.q("SELECT 1 FROM research_queue WHERE entity_id=?", t4["entity_id"]) == []
