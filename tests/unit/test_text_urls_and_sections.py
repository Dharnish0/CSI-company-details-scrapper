"""with_text_urls (URLs printed in page text become link refs) and section_links (same-section pages of a listing)."""
from __future__ import annotations

from corpdisc.services.links import build_link_index, section_links, with_text_urls

BASE = "https://hub.example.com/portfolio"


def idx(html, base=BASE):
    return build_link_index(html, base)


def test_text_urls_become_refs_with_ids_after_the_anchors_and_exact_offsets():
    html = "<a href='/a'>A</a><a href='https://known.example.org/x'>Known</a>"
    i0 = idx(html)
    text = "CORA Group\nwww.thecoragroup.com\nsee https://known.example.org/x and https://new-co.com/a/b."
    i1 = with_text_urls(i0, text)
    assert [lk.link_id for lk in i1.links] == list(range(1, len(i1.links) + 1))
    assert [lk.url for lk in i1.links[: len(i0.links)]] == [lk.url for lk in i0.links]       # existing ids never change
    added = [lk for lk in i1.links if lk.origin == "text"]
    assert [lk.url for lk in added] == ["https://www.thecoragroup.com/", "https://new-co.com/a/b"]   # known anchor not re-added
    assert all(text[lk.text_offset:].startswith(lk.raw_href) for lk in added)                  # literally in the page text
    assert all(lk.det_score == 0.0 and lk.location == "body" for lk in added)


def test_text_urls_ignore_emails_filenames_and_unknown_suffixes():
    text = "mail info@vendor-co.com or read index.html, Node.js and v2.0.1 or e.g. this; also vendor-co.com/shop."
    urls = [lk.url for lk in with_text_urls(idx(""), text).links]
    assert urls == ["https://vendor-co.com/shop"]


def test_text_urls_are_capped():
    text = " ".join(f"site{i}.com" for i in range(300))
    assert len(with_text_urls(idx(""), text, max_urls=7).links) == 7


def page(links, base=BASE):
    return idx(links, base)


def test_links_under_the_pages_own_path_are_the_section():
    html = ("<nav><a href='/about'>About</a></nav><a href='/portfolio/alpha'>Alpha</a><a href='/portfolio/beta/'>Beta</a>"
            "<a href='/portfolio'>Portfolio</a><a href='/portfolio/x?page=2'>Next</a><a href='https://other.org/portfolio/z'>Z</a>")
    got = section_links(page(html), BASE, listing_page=False)
    assert [lk.anchor_text for lk in got] == ["Alpha", "Beta"]        # not the page itself, not a query, not off-site


def test_a_list_of_sibling_body_links_is_the_section_of_a_listing_page_only():
    items = "".join(f"<li><a href='/{s}'>{s.title()}</a></li>" for s in ("auto", "retail", "travel"))
    html = f"<nav><a href='/people'>People</a><a href='/jobs'>Jobs</a><a href='/team'>Team</a></nav><ul>{items}</ul>" \
           "<footer><a href='/legal'>Legal</a><a href='/cookies'>Cookies</a><a href='/terms'>Terms</a></footer>"
    got = section_links(page(html), BASE, listing_page=True)
    assert [lk.anchor_text for lk in got] == ["Auto", "Retail", "Travel"]       # nav and footer links never count
    assert section_links(page(html), BASE, listing_page=False) == []


def test_fewer_than_three_siblings_and_root_pages_have_no_section():
    html = "<ul><li><a href='/one'>One</a></li><li><a href='/two'>Two</a></li></ul>"
    assert section_links(page(html), BASE, listing_page=True) == []
    root = "<a href='/a'>A</a><a href='/b'>B</a><a href='/c'>C</a>"
    assert section_links(page(root, "https://hub.example.com/"), "https://hub.example.com/", listing_page=False) == []


def test_exclude_removes_already_visited_urls():
    html = "<a href='/portfolio/alpha'>Alpha</a><a href='/portfolio/beta'>Beta</a>"
    got = section_links(page(html), BASE, listing_page=False, exclude=["https://hub.example.com/portfolio/alpha"])
    assert [lk.anchor_text for lk in got] == ["Beta"]
