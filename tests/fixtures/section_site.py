"""Fixture world for following the pages of a listing's own section. Fictional names only.

Northwind Holdings (root) owns two groups whose portfolio page names no company itself:

* Section Co: /portfolio links to /portfolio/m1 ... /portfolio/m6 (path-section links). Each market page names one company.
  robots.txt disallows /portfolio/m3 and sets Crawl-delay 2.
* Slug Co:    /portfolio shows a list of sibling pages (/auto, /bank, /cargo, /dairy) that redirect to /portfolio/<slug>/.
"""
from __future__ import annotations

from tests.fixtures import site as base
from tests.helpers.fakes import FakeHttpClient, resp

ROOT = base.ROOT
EMPTY = ("m1", "m2")  # market pages that name no company yet; m3 is disallowed by robots.txt
SC = {f"m{i}": name for i, name in enumerate(("", "", "", "Dune Data", "Elm Systems", "Fir Cloud", "Gum Net", "Holly Apps"), 1) if name}
MARKETS = tuple(f"m{i}" for i in range(1, 9))
SLUG = {"auto": "Garnet Autos", "bank": "Hazel Bank Tech", "cargo": "Iris Cargo", "dairy": "Juniper Dairy Soft"}
SENT = {"g_Section Co": f"Section Co is an operating group of {ROOT}.", "g_Slug Co": f"Slug Co is an operating group of {ROOT}."}
for _n in (*SC.values(), *SLUG.values()):
    SENT[f"k_{_n}"] = f"{_n} is a subsidiary of {'Section Co' if _n in SC.values() else 'Slug Co'}."


def host(name: str) -> str:
    return name.lower().replace(" ", "") + ".test"


def routes() -> dict:
    r: dict = {}

    def add(url: str, html: str, final: str | None = None) -> None:
        r[url] = resp(final or url, 200, html)

    add("https://root.test/", base.page(ROOT, "<p>Welcome.</p>", nav=base.NAV))
    add("https://root.test/companies", base.page("Our Operating Groups", (
        base.card("Section Co", "https://sc.test/portfolio") + base.card("Slug Co", "https://slug.test/portfolio")
        + f"<p>{SENT['g_Section Co']}</p><p>{SENT['g_Slug Co']}</p>"), nav=base.NAV))
    links = "".join(f"<li><a href='/portfolio/{k}'>Market {k[1:]}</a></li>" for k in MARKETS)
    add("https://sc.test/portfolio", base.page("Section Co portfolio", f"<p>Our portfolio is organised by market.</p><ul>{links}</ul>",
                                                nav="<a href='/portfolio'>Portfolio</a>"))
    r["https://sc.test/robots.txt"] = resp("https://sc.test/robots.txt", 200,
                                           "User-agent: *\nDisallow: /portfolio/m3\nCrawl-delay: 2\n", ctype="text/plain")
    for k in EMPTY:
        add(f"https://sc.test/portfolio/{k}", base.page(f"Market {k[1:]}", "<p>No companies listed yet.</p>",
                                                         nav="<a href='/portfolio'>Portfolio</a>"))
    for k, name in SC.items():
        add(f"https://sc.test/portfolio/{k}", base.page(f"Market {k[1:]}", base.card(name, f"https://{host(name)}/")
                                                         + f"<p>{SENT[f'k_{name}']}</p>", nav="<a href='/portfolio'>Portfolio</a>"))
    slugs = "".join(f"<li><a href='/{k}'>{k.title()}</a></li>" for k in SLUG)
    add("https://slug.test/portfolio", base.page("Slug Co portfolio", f"<p>Browse by industry.</p><ul>{slugs}</ul>",
                                                  nav="<a href='/portfolio'>Portfolio</a><a href='/team'>Team</a>"))
    for k, name in SLUG.items():  # the short URL redirects to /portfolio/<slug>/
        add(f"https://slug.test/{k}", base.page(k.title(), base.card(name, f"https://{host(name)}/")
                                                 + f"<p>{SENT[f'k_{name}']}</p>"), final=f"https://slug.test/portfolio/{k}/")
    for name in (*SC.values(), *SLUG.values()):
        add(f"https://{host(name)}/", base.page(name, f"<p>{name} home.</p>"))
    return r


def install(monkeypatch) -> FakeHttpClient:
    from corpdisc.services.urls import canonicalize_url

    for k, v in SENT.items():
        monkeypatch.setitem(base.S, k, v)
    monkeypatch.setitem(base.EXTRACT, "https://root.test/companies",
                        [base._ent(g, etype="Operating Group", key=f"g_{g}") for g in ("Section Co", "Slug Co")])
    for g in ("Section Co", "Slug Co"):
        monkeypatch.setitem(base.REL, (ROOT, g), ("SUPPORTED", "Part of", "Operating Group", f"g_{g}", 0.9))
    for k, name in SC.items():
        monkeypatch.setitem(base.EXTRACT, f"https://sc.test/portfolio/{k}", [base._ent(name, etype="Subsidiary", key=f"k_{name}")])
        monkeypatch.setitem(base.REL, ("Section Co", name), ("SUPPORTED", "Subsidiary of", "Subsidiary", f"k_{name}", 0.9))
    for k, name in SLUG.items():
        monkeypatch.setitem(base.EXTRACT, canonicalize_url(f"https://slug.test/{k}").canonical,
                            [base._ent(name, etype="Subsidiary", key=f"k_{name}")])
        monkeypatch.setitem(base.REL, ("Slug Co", name), ("SUPPORTED", "Subsidiary of", "Subsidiary", f"k_{name}", 0.9))
    return FakeHttpClient(routes())
