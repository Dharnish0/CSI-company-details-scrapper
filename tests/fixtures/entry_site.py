"""Fixture world for defect B (child tasks must process their OWN start page). Fictional names only.

Northwind Holdings (root) lists five operating groups; each group's website resolves to a different shape:

* Jonas Like  : start URL is the listing itself (/portfolio) and the page links to itself.
* Vela Like   : same, but the server redirects /portfolio to /portfolio/ (trailing slash), so final_url != canonical_url.
* Harris Like : the home page itself shows the companies with ownership wording, but has no listing links.
* Plain Co    : a home page with nothing to extract (legitimately terminal, with a reason).
* Locked Co   : home page links to /portfolio, which answers 403 (unreachable, with a G6 rejection).
"""
from __future__ import annotations

from tests.fixtures import site as base
from tests.helpers.fakes import FakeHttpClient, resp

ROOT = base.ROOT
GROUPS = ("Jonas Like", "Vela Like", "Harris Like", "Plain Co", "Locked Co")
KIDS = {  # group -> {child: (domain)}
    "Jonas Like": {"Alpha Labs": "alpha.test", "Beta Works": "beta.test"},
    "Vela Like": {"Gamma Soft": "gamma.test", "Delta Soft": "delta.test"},
    "Harris Like": {"Epsilon Corp": "epsilon.test", "Zeta Corp": "zeta.test", "Eta Corp": "eta.test"},
}
START = {"Jonas Like": "https://jl.test/portfolio", "Vela Like": "https://vl.test/portfolio",
         "Harris Like": "https://hl.test/", "Plain Co": "https://plain.test/", "Locked Co": "https://lk.test/"}
SENT = {f"g_{g}": f"{g} is an operating group of {ROOT}." for g in GROUPS}
for _g, _kids in KIDS.items():
    for _k in _kids:
        SENT[f"k_{_k}"] = f"{_k} is a subsidiary of {_g}."


def _listing(group: str, *, nav: str) -> str:
    cards = "".join(base.card(k, f"https://{d}/") for k, d in KIDS[group].items())
    sents = "".join(f"<p>{SENT[f'k_{k}']}</p>" for k in KIDS[group])
    return base.page(f"{group} companies", cards + sents, nav=nav)


def routes() -> dict:
    r: dict = {}

    def add(url: str, html: str, final: str | None = None) -> None:
        r[url] = resp(final or url, 200, html)

    add("https://root.test/", base.page(ROOT, "<p>Welcome.</p>", nav=base.NAV))
    cards = "".join(base.card(g, START[g]) for g in GROUPS)
    add("https://root.test/companies", base.page("Our Operating Groups", cards + "".join(
        f"<p>{SENT[f'g_{g}']}</p>" for g in GROUPS), nav=base.NAV))
    # the listing is its own start page and links to itself (and back to the parent's listing)
    own = "<a href='/portfolio'>Portfolio</a><a href='https://root.test/companies'>Group companies</a>"
    add("https://jl.test/portfolio", _listing("Jonas Like", nav=own))
    add("https://vl.test/portfolio", _listing("Vela Like", nav="<a href='/portfolio'>Portfolio</a>"),
        final="https://vl.test/portfolio/")
    add("https://hl.test/", _listing("Harris Like", nav="<a href='/contact'>Contact</a><a href='/login'>Login</a>"))
    add("https://plain.test/", base.page("Plain Co", "<p>Welcome to our home.</p>"))
    add("https://lk.test/", base.page("Locked Co", "<p>Welcome.</p>", nav="<a href='/portfolio'>Portfolio</a>"))
    r["https://lk.test/portfolio"] = resp("https://lk.test/portfolio", 403, "forbidden", ctype="text/plain")
    for kids in KIDS.values():
        for k, d in kids.items():
            add(f"https://{d}/", base.page(k, f"<p>{k} home.</p>"))
    return r


def install(monkeypatch) -> FakeHttpClient:
    """Patch the shared Brain data so extraction/verification report exactly what these pages say."""
    from corpdisc.services.urls import canonicalize_url

    for k, v in SENT.items():
        monkeypatch.setitem(base.S, k, v)
    monkeypatch.setitem(base.EXTRACT, "https://root.test/companies",
                        [base._ent(g, etype="Operating Group", key=f"g_{g}") for g in GROUPS])
    for g in GROUPS:
        monkeypatch.setitem(base.REL, (ROOT, g), ("SUPPORTED", "Part of", "Operating Group", f"g_{g}", 0.9))
    for g, kids in KIDS.items():
        url = canonicalize_url(START[g]).canonical
        monkeypatch.setitem(base.EXTRACT, url, [base._ent(k, etype="Subsidiary", key=f"k_{k}") for k in kids])
        for k in kids:
            monkeypatch.setitem(base.REL, (g, k), ("SUPPORTED", "Subsidiary of", "Subsidiary", f"k_{k}", 0.9))
    return FakeHttpClient(routes())
