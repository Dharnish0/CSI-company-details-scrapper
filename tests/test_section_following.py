"""A listing that names no company is followed into its own section, within every budget, with nothing dropped silently."""
from __future__ import annotations

from tests.fixtures import section_site
from tests.fixtures.world import seeded_world


def run_world(tmp_path, monkeypatch, **settings_kw):
    http = section_site.install(monkeypatch)
    w = seeded_world(tmp_path, http=http, settings_kw=settings_kw or None)
    return w, w.sup.run()


def stored_children(w, parent):
    return {r[0] for r in w.q("SELECT c.name FROM relationships r JOIN entities p ON p.entity_id=r.parent_id "
                              "JOIN entities c ON c.entity_id=r.child_id WHERE p.name=?", parent)}


def unprocessed(w):
    return [r[0] for r in w.q("SELECT item_ref FROM gate_rejections WHERE gate_code='G9' AND detail LIKE 'UNPROCESSED_BRANCH%'")]


def test_section_pages_of_a_listing_that_named_no_company_are_followed_and_their_companies_stored(tmp_path, monkeypatch):
    # first round (4 approved links): the portfolio page itself, m1 and m2 are empty, m3 is blocked by robots.txt;
    # the remaining section pages m4.. are then followed (cap: MAX_LINKS_PER_ENTITY = 4 in the test world)
    w, _ = run_world(tmp_path, monkeypatch)
    kids = stored_children(w, "Section Co")
    assert kids == {"Dune Data", "Elm Systems", "Fir Cloud", "Gum Net"}
    for name in kids:
        assert w.entity(name)["verification_status"] == "CONFIRMED"
        assert w.edge("Section Co", name)["is_primary"] == 1
        assert w.q("SELECT depth FROM research_queue WHERE entity_id=?", w.entity(name)["entity_id"])[0][0] == 2
    assert w.q("SELECT COUNT(*) FROM research_queue WHERE status='FAILED'")[0][0] == 0


def test_listed_sibling_pages_that_redirect_into_the_section_are_followed_too(tmp_path, monkeypatch):
    w, _ = run_world(tmp_path, monkeypatch)
    assert stored_children(w, "Slug Co") == set(section_site.SLUG.values())
    assert w.q("SELECT 1 FROM research_queue WHERE entity_id=? AND depth=2", w.entity("Garnet Autos")["entity_id"])


def test_robots_crawl_delay_and_the_cap_are_honoured_and_unfollowed_links_are_recorded(tmp_path, monkeypatch):
    w, _ = run_world(tmp_path, monkeypatch)
    pages = w.page_requests()
    assert "https://sc.test/portfolio/m3" not in pages                     # robots.txt disallows it
    assert len(w.q("SELECT 1 FROM gate_rejections WHERE gate_code='G6' AND item_ref LIKE '%/portfolio/m3'")) == 1
    assert "https://sc.test/portfolio/m8" not in pages                     # beyond the cap of 4 section links
    assert unprocessed(w) == ["https://sc.test/portfolio/m8"]              # ... and recorded, not dropped
    assert any(sl >= 2.0 for sl in w.clock.sleeps)                         # Crawl-delay: 2 was waited out


def test_page_budget_caps_the_section_and_the_rest_is_recorded_not_dropped(tmp_path, monkeypatch):
    w, _ = run_world(tmp_path, monkeypatch, max_pages_per_domain=3)
    sc_pages = [u for u in w.page_requests() if u.startswith("https://sc.test/")]
    assert len(sc_pages) <= 3                                              # the per-domain page budget binds
    left = [u for u in unprocessed(w) if u.startswith("https://sc.test/")]   # (slug.test has its own budget)
    assert len(left) >= 4 and all(u.startswith("https://sc.test/portfolio/") for u in left)
    assert not set(left) & set(sc_pages)                                   # nothing both fetched and "unprocessed"
    assert stored_children(w, "Section Co") == set()


def test_a_listing_that_already_has_companies_does_not_follow_its_section(tmp_path, monkeypatch):
    w, _ = run_world(tmp_path, monkeypatch)
    root_pages = [u for u in w.page_requests() if u.startswith("https://root.test/")]
    assert all(u in ("https://root.test/", "https://root.test/companies") for u in root_pages)
    assert not any(r[0].startswith("https://root.test") for r in w.q("SELECT item_ref FROM gate_rejections WHERE gate_code='G9'"))


def test_each_page_is_extracted_exactly_once(tmp_path, monkeypatch):
    w, _ = run_world(tmp_path, monkeypatch)
    # entry page + m1 + m2 (first round) + m4..m7 (second round); the first round is not re-sent in the second
    n = w.q("SELECT COUNT(*) FROM agent_invocations WHERE agent='extraction' AND status='OK' AND task_id=2")[0][0]
    assert n == 7, n
