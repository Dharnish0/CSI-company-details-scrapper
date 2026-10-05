"""Deterministic link index for one fetched page (ARCHITECTURE Part I 10.1, plan 4.M1.7).

Every link gets a runtime ``link_id`` (1-based, document order, deduplicated by canonical URL).
Agents only ever see ``LinkRef`` objects built from this index, so any link an agent names can
be checked against it (gate G1). Scores are keyword-based, generic (no company names) and in
[0, 1]. Band thresholds for routing are applied by the research graph, not here.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final, Literal

from bs4 import BeautifulSoup, Tag

from corpdisc.contracts.common import LinkRef
from corpdisc.models.domain import PageType
from corpdisc.services.urls import canonicalize_url

if TYPE_CHECKING:
    from tldextract import TLDExtract

Location = Literal["nav", "footer", "body"]

_STATIC_EXT: Final = re.compile(
    r"\.(?:png|jpe?g|gif|webp|svg|ico|bmp|tiff?|css|js|mjs|map|woff2?|ttf|otf|eot|zip|gz|tgz|rar|7z|"
    r"mp3|mp4|webm|avi|mov|wav|exe|dmg|msi)$",
    re.I,
)
_WS: Final = re.compile(r"\s+")
_MAX_ANCHOR: Final = 200


@dataclass(frozen=True, slots=True)
class IndexedLink:
    link_id: int
    url: str  # canonical
    raw_href: str
    anchor_text: str
    title_attr: str | None
    rel: tuple[str, ...]
    location: Location
    same_domain: bool
    same_registrable_domain: bool
    det_score: float
    score_reasons: tuple[str, ...]
    page_type_hint: PageType | None
    position: int  # 0-based position of the first occurrence among <a> tags


@dataclass(frozen=True)
class LinkScoringConfig:
    anchor_keywords: Mapping[str, float]
    path_keywords: Mapping[str, float]
    negative_keywords: frozenset[str]
    nav_bonus: float = 0.10
    footer_bonus: float = 0.0
    external_factor: float = 0.5
    negative_cap: float = 0.1


DEFAULT_SCORING: Final = LinkScoringConfig(
    anchor_keywords={
        "our companies": 0.9, "companies": 0.8, "portfolio": 0.8, "our brands": 0.8, "brands": 0.6,
        "subsidiaries": 0.9, "operating groups": 0.9, "groups": 0.6, "group": 0.4, "businesses": 0.7,
        "divisions": 0.7, "business units": 0.7, "acquisitions": 0.7, "family of companies": 0.9,
        "about": 0.3, "investors": 0.3, "who we are": 0.3,
    },
    path_keywords={
        "companies": 0.7, "portfolio": 0.7, "brands": 0.5, "subsidiaries": 0.8, "operating-groups": 0.8,
        "groups": 0.5, "businesses": 0.6, "divisions": 0.6, "acquisitions": 0.6, "about": 0.25,
        "investors": 0.25,
    },
    negative_keywords=frozenset({
        "careers", "jobs", "privacy", "cookie", "cookies", "login", "log in", "sign in", "signin", "contact",
        "blog", "news", "press", "partners", "partner", "customers", "customer", "terms", "legal",
        "case studies", "case-studies", "support", "events", "webinar", "subscribe", "sitemap",
    }),
)  # fmt: skip

# Page-type hints, checked in order; the first match wins. Excluded types are included so that
# gate G4 can drop them (partners, customers, case studies, careers, blog, news).
_PAGE_TYPE_RULES: Final[tuple[tuple[PageType, tuple[str, ...]], ...]] = (
    (PageType.CAREERS, ("careers", "jobs", "join-us", "join us")),
    (PageType.PARTNERS, ("partners", "partner")),
    (PageType.CUSTOMERS, ("customers", "clients", "customer")),
    (PageType.CASE_STUDIES, ("case-studies", "case studies", "case-study", "success-stories")),
    (PageType.BLOG, ("blog",)),
    (PageType.NEWS, ("news", "press", "media")),
    (PageType.SUBSIDIARIES, ("subsidiaries",)),
    (PageType.OPERATING_GROUPS, ("operating-groups", "operating groups")),
    (PageType.PORTFOLIO, ("portfolio",)),
    (PageType.COMPANIES, ("companies", "our-businesses", "businesses", "family of companies")),
    (PageType.BRANDS, ("brands",)),
    (PageType.ACQUISITIONS, ("acquisitions", "acquired")),
    (PageType.LEADERSHIP, ("leadership", "management", "board")),
    (PageType.LOCATIONS, ("locations", "offices")),
    (PageType.INVESTOR, ("investors", "investor-relations", "investor")),
    (PageType.ABOUT, ("about", "who-we-are", "who we are")),
)


def _clean(text: str | None) -> str:
    return _WS.sub(" ", text or "").strip()


def guess_page_type(url: str, anchor_text: str = "") -> PageType | None:
    """Deterministic page-type hint from the URL path and the anchor text."""
    res = canonicalize_url(url)
    path = ""
    if res.ok and res.canonical:
        path = res.canonical.split("://", 1)[-1].split("/", 1)[-1].split("?", 1)[0].lower()
        if path == "":
            return PageType.HOME
    hay = f" {path.replace('/', ' ')} {anchor_text.lower()} "
    for page_type, words in _PAGE_TYPE_RULES:
        for w in words:
            if (w in path) or re.search(rf"\b{re.escape(w)}\b", hay):
                return page_type
    return None


def _location(tag: Tag) -> Location:
    for parent in tag.parents:
        if not isinstance(parent, Tag):
            continue
        role = (parent.get("role") or "").lower() if isinstance(parent.get("role"), str) else ""
        if parent.name in ("nav", "header") or role == "navigation":
            return "nav"
        if parent.name == "footer" or role == "contentinfo":
            return "footer"
    return "body"


def _score(
    anchor: str, url: str, location: Location, same_reg: bool, cfg: LinkScoringConfig
) -> tuple[float, tuple[str, ...]]:
    a = anchor.lower()
    path = url.split("://", 1)[-1].partition("/")[2].lower()
    reasons: list[str] = []
    best = 0.0
    for kw, w in cfg.anchor_keywords.items():
        if re.search(rf"\b{re.escape(kw)}\b", a) and w > best:
            best, reasons = w, [f"anchor:{kw}"]
    for kw, w in cfg.path_keywords.items():
        if kw in path:
            if w > best:
                best = w
            reasons.append(f"path:{kw}")
    score = best
    if location == "nav" and score > 0:
        score += cfg.nav_bonus
        reasons.append("nav_bonus")
    elif location == "footer" and score > 0 and cfg.footer_bonus:
        score += cfg.footer_bonus
        reasons.append("footer_bonus")
    negative = [kw for kw in cfg.negative_keywords if re.search(rf"\b{re.escape(kw)}\b", a) or kw in path]
    if negative:
        score = min(score, cfg.negative_cap)
        reasons.append(f"negative:{sorted(negative)[0]}")
    if not same_reg:
        score *= cfg.external_factor
        reasons.append("external")
    return round(max(0.0, min(1.0, score)), 4), tuple(reasons)


@dataclass(frozen=True)
class LinkIndex:
    base_url: str
    links: tuple[IndexedLink, ...]
    _by_id: dict[int, IndexedLink] = field(default_factory=dict, repr=False, compare=False)
    _by_url: dict[str, IndexedLink] = field(default_factory=dict, repr=False, compare=False)

    def __post_init__(self) -> None:
        self._by_id.update({lk.link_id: lk for lk in self.links})
        self._by_url.update({lk.url: lk for lk in self.links})

    def get(self, link_id: int) -> IndexedLink | None:
        return self._by_id.get(link_id)

    def by_url(self, canonical_url: str) -> IndexedLink | None:
        return self._by_url.get(canonical_url)

    @property
    def ids(self) -> frozenset[int]:
        return frozenset(self._by_id)

    def top(self, n: int, *, min_score: float = 0.0, same_domain_only: bool = False) -> list[IndexedLink]:
        pool = [lk for lk in self.links if lk.det_score >= min_score and (lk.same_registrable_domain
                                                                             or not same_domain_only)]
        return sorted(pool, key=lambda lk: (-lk.det_score, lk.link_id))[: max(n, 0)]

    def to_refs(self, link_ids: Iterable[int] | None = None, *, limit: int = 60) -> list[LinkRef]:
        chosen = self.links if link_ids is None else [lk for i in link_ids if (lk := self.get(i)) is not None]
        return [
            LinkRef(link_id=lk.link_id, url=lk.url, anchor_text=lk.anchor_text[:_MAX_ANCHOR], location=lk.location,
                    same_domain=lk.same_registrable_domain, det_score=lk.det_score)
            for lk in list(chosen)[:limit]
        ]  # fmt: skip


def build_link_index(
    html: str,
    base_url: str,
    *,
    config: LinkScoringConfig = DEFAULT_SCORING,
    extractor: TLDExtract | None = None,
    max_links: int = 500,
) -> LinkIndex:
    """Parse ``html`` (never executed) and return the deduplicated, scored link index."""
    base = canonicalize_url(base_url, extractor=extractor)
    base_canon = base.canonical or base_url
    soup = BeautifulSoup(html or "", "html.parser")
    seen: dict[str, int] = {}
    out: list[IndexedLink] = []
    for position, a in enumerate(soup.find_all("a", href=True)):
        if len(out) >= max_links:
            break
        href = a.get("href")
        if not isinstance(href, str):
            continue
        raw = href.strip()[:2048]
        if not raw or raw.startswith("#"):
            continue
        res = canonicalize_url(raw, base_canon, extractor=extractor)
        if not res.ok or res.canonical is None or res.scheme not in ("http", "https"):
            continue
        path = res.canonical.split("?", 1)[0]
        if _STATIC_EXT.search(path):
            continue
        if res.canonical == base_canon and "#" in raw:
            continue
        anchor = _clean(a.get_text(" "))[:_MAX_ANCHOR]
        if not anchor:
            img = a.find("img")
            if isinstance(img, Tag):
                alt = img.get("alt") or img.get("title")
                anchor = _clean(alt if isinstance(alt, str) else "")[:_MAX_ANCHOR]
        if res.canonical in seen:
            idx = seen[res.canonical]
            if not out[idx].anchor_text and anchor:  # keep the first non-empty anchor
                prev = out[idx]
                score, reasons = _score(anchor, prev.url, prev.location, prev.same_registrable_domain, config)
                out[idx] = IndexedLink(
                    prev.link_id, prev.url, prev.raw_href, anchor, prev.title_attr, prev.rel, prev.location,
                    prev.same_domain, prev.same_registrable_domain, score, reasons,
                    guess_page_type(prev.url, anchor), prev.position,
                )  # fmt: skip
            continue
        title = a.get("title")
        rel_attr = a.get("rel") or ()
        rel = tuple(str(r).lower() for r in (rel_attr if isinstance(rel_attr, list) else str(rel_attr).split()))
        location = _location(a)
        same_domain = res.domain == base.domain
        same_reg = res.registrable_domain is not None and res.registrable_domain == base.registrable_domain
        score, reasons = _score(anchor, res.canonical, location, same_reg, config)
        seen[res.canonical] = len(out)
        out.append(
            IndexedLink(
                link_id=len(out) + 1, url=res.canonical, raw_href=raw[:500], anchor_text=anchor,
                title_attr=_clean(title)[:200] if isinstance(title, str) else None, rel=rel, location=location,
                same_domain=same_domain, same_registrable_domain=same_reg, det_score=score,
                score_reasons=reasons, page_type_hint=guess_page_type(res.canonical, anchor), position=position,
            )
        )  # fmt: skip
    return LinkIndex(base_url=base_canon, links=tuple(out))
