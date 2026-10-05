"""Link index (link ids, dedupe, scoring, locations) and DOM candidate strategies."""
from __future__ import annotations

from corpdisc.contracts.common import LinkRef
from corpdisc.models.domain import PageType
from corpdisc.services.dom_candidates import extract_dom_candidates
from corpdisc.services.links import build_link_index, guess_page_type

BASE = "https://northwind.test/"

PAGE = """<html><head><title>Northwind Holdings</title></head><body>
<header><nav><a href="/companies">Our Companies</a><a href="/careers">Careers</a><a href="/login">Login</a></nav></header>
<main>
  <a href="#top">Top</a><a href="javascript:void(0)">JS</a><a href="mailto:x@northwind.test">Mail</a>
  <a href="/logo.png">Logo file</a><a href="/companies#list">Companies again</a>
  <div class="grid">
    <div class="card"><h3><a href="https://aurora-analytics.test/">Aurora Analytics Ltd.</a></h3><p>Data tools.</p></div>
    <div class="card"><h3><a href="https://brightline.test/">Brightline Logistics</a></h3><p>Freight.</p></div>
  </div>
  <ul><li>Learn more</li><li><a href="/cobalt">Cobalt Health</a></li></ul>
  <table><tr><td>Delta Freight GmbH</td><td>Germany</td></tr></table>
  <a href="https://echo.test/"><img src="e.png" alt="Echo Systems logo"></a>
  <a href="https://partners.example.test/partners">Partners</a>
</main>
<footer>Northwind Holdings is a subsidiary of Example Group. <a href="/privacy">Privacy</a></footer>
<script>var name = "Hidden Corp";</script>
<script type="application/ld+json">{"@type":"Organization","name":"Northwind Holdings",
 "subOrganization":[{"@type":"Organization","name":"Foxtrot Labs"}]}</script>
</body></html>"""


def test_link_ids_are_document_order_deduplicated_and_filtered():
    idx = build_link_index(PAGE, BASE)
    urls = [lk.url for lk in idx.links]
    assert urls[0] == "https://northwind.test/companies" and idx.get(1).anchor_text == "Our Companies"
    assert urls.count("https://northwind.test/companies") == 1  # fragment variant deduplicated
    assert not any(u.startswith(("javascript", "mailto")) or u.endswith(".png") for u in urls)
    assert [lk.link_id for lk in idx.links] == list(range(1, len(idx.links) + 1))
    assert idx.by_url("https://brightline.test/").link_id == idx.get(idx.by_url("https://brightline.test/").link_id).link_id


def test_locations_scores_and_external_factor():
    idx = build_link_index(PAGE, BASE)
    companies = idx.by_url("https://northwind.test/companies")
    careers = idx.by_url("https://northwind.test/careers")
    privacy = idx.by_url("https://northwind.test/privacy")
    assert companies.location == "nav" and companies.det_score >= 0.8
    assert careers.det_score <= 0.1 and privacy.location == "footer" and privacy.det_score <= 0.1
    external = idx.by_url("https://partners.example.test/partners")
    assert not external.same_registrable_domain and external.det_score <= 0.1
    assert idx.top(1)[0].url == companies.url


def test_logo_link_uses_alt_text_and_refs_are_valid_contracts():
    idx = build_link_index(PAGE, BASE)
    echo = idx.by_url("https://echo.test/")
    assert echo.anchor_text == "Echo Systems logo"
    refs = idx.to_refs(limit=3)
    assert len(refs) == 3 and all(isinstance(r, LinkRef) and len(r.anchor_text) <= 200 for r in refs)


def test_deterministic_and_max_links():
    assert build_link_index(PAGE, BASE).links == build_link_index(PAGE, BASE).links
    assert len(build_link_index(PAGE, BASE, max_links=2).links) == 2


def test_guess_page_type_includes_excluded_types():
    assert guess_page_type("https://x.test/") is PageType.HOME
    assert guess_page_type("https://x.test/our-companies") is PageType.COMPANIES
    assert guess_page_type("https://x.test/partners") is PageType.PARTNERS
    assert guess_page_type("https://x.test/careers/jobs") is PageType.CAREERS
    assert guess_page_type("https://x.test/news/acquired-x") is PageType.NEWS
    assert guess_page_type("https://x.test/products/widget") is None


def test_dom_candidates_each_strategy_and_exclusions():
    idx = build_link_index(PAGE, BASE)
    cands = extract_dom_candidates(PAGE, idx)
    by_name = {c.name_text: c for c in cands}
    assert by_name["Aurora Analytics Ltd."].dom_hint == "card"
    assert by_name["Aurora Analytics Ltd."].link_id == idx.by_url("https://aurora-analytics.test/").link_id
    assert by_name["Cobalt Health"].dom_hint == "list_item"
    assert by_name["Delta Freight GmbH"].dom_hint == "table_row" and by_name["Delta Freight GmbH"].link_id is None
    assert by_name["Echo Systems"].dom_hint == "logo_alt"
    assert by_name["Northwind Holdings"].dom_hint == "footer"
    assert by_name["Foxtrot Labs"].dom_hint == "jsonld"
    names = set(by_name)
    assert "Our Companies" not in names and "Careers" not in names  # nav excluded
    assert "Learn more" not in names and "Hidden Corp" not in names  # boilerplate / script text
    assert [c.raw_candidate_id for c in cands] == list(range(1, len(cands) + 1))
    assert all(len(c.context) <= 400 for c in cands)


def test_dom_candidates_hostile_page_is_bounded():
    hostile = ("<html><body>" + "<div class='c'><h3>N%d Corp</h3></div>" * 1 % 1
               + "".join(f"<li>Item Company {i}</li>" for i in range(2000))
               + '<script type="application/ld+json">{"@type": ' + "[" * 5000 + "</script>"
               + "<a href='javascript:alert(1)' title='" + "x" * 100000 + "'>bad</a></body></html>")
    idx = build_link_index(hostile, BASE)
    cands = extract_dom_candidates(hostile, idx, max_candidates=50)
    assert len(cands) == 50 and not idx.links
