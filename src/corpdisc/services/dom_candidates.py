"""Deterministic candidate names from page structure (plan 4.M1.7).

Produces ``RawCandidate`` objects only: these are *offers* to the Extraction Agent, never entities.
Nothing on the page is executed or followed; JSON-LD is read with ``json.loads`` only. Items in
``nav``/``header`` and generic boilerplate labels are dropped. Strategies run in a fixed order and
results are deduplicated by normalized name (first occurrence wins), so output is deterministic.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from typing import Final

from bs4 import BeautifulSoup, Tag

from corpdisc.contracts.common import RawCandidate
from corpdisc.services.links import LinkIndex
from corpdisc.services.normalize import normalize_name
from corpdisc.services.urls import canonicalize_url

_WS: Final = re.compile(r"\s+")
_HAS_LETTER: Final = re.compile(r"[^\W\d_]", re.UNICODE)
_MAX_CONTEXT: Final = 400
_MAX_NAME: Final = 200
_MAX_JSONLD_BYTES: Final = 200_000

BOILERPLATE: Final = frozenset({
    "home", "about", "about us", "contact", "contact us", "login", "log in", "sign in", "sign up", "menu",
    "search", "read more", "learn more", "more", "privacy", "privacy policy", "cookies", "cookie policy",
    "terms", "terms of use", "terms and conditions", "careers", "jobs", "news", "blog", "press", "events",
    "subscribe", "back to top", "next", "previous", "view all", "see all", "visit website", "website",
    "skip to content", "accessibility", "sitemap", "english", "language", "follow us", "share",
})  # fmt: skip

_FOOTER_PATTERNS: Final = (
    re.compile(r"\b(?:a|an)\s+([A-Z][\w&.,'\- ]{1,80}?)\s+company\b"),
    re.compile(r"\bpart of\s+(?:the\s+)?([A-Z][\w&.,'\- ]{1,80}?)(?:\s+group)?(?=[.,;]|\s*$)"),
    re.compile(
        r"\b([A-Z][\w&.,'\- ]{1,80}?)\s+is\s+(?:a|an)\s+(?:wholly[- ]owned\s+)?(?:subsidiary|division|member)\s+of\b"
    ),
)


def _clean(text: str | None) -> str:
    return _WS.sub(" ", text or "").strip()


def _in_nav(tag: Tag) -> bool:
    for parent in [tag, *tag.parents]:
        if not isinstance(parent, Tag):
            continue
        role = parent.get("role")
        if parent.name in ("nav", "header") or (isinstance(role, str) and role.lower() == "navigation"):
            return True
    return False


def _in_footer(tag: Tag) -> bool:
    for parent in [tag, *tag.parents]:
        if isinstance(parent, Tag):
            role = parent.get("role")
            if parent.name == "footer" or (isinstance(role, str) and role.lower() == "contentinfo"):
                return True
    return False


def _valid_name(name: str) -> bool:
    return 2 <= len(name) <= _MAX_NAME and bool(_HAS_LETTER.search(name)) and name.lower() not in BOILERPLATE


class _Collector:
    def __init__(self, index: LinkIndex, base_url: str, limit: int) -> None:
        self.index, self.base, self.limit = index, base_url, limit
        self.items: list[RawCandidate] = []
        self._seen: set[str] = set()

    @property
    def full(self) -> bool:
        return len(self.items) >= self.limit

    def link_id_for(self, anchor: Tag | None) -> int | None:
        if anchor is None:
            return None
        href = anchor.get("href")
        if not isinstance(href, str):
            return None
        res = canonicalize_url(href.strip(), self.base)
        if not res.ok or res.canonical is None:
            return None
        hit = self.index.by_url(res.canonical)
        return hit.link_id if hit else None

    def add(self, name: str, context: str, hint: str, anchor: Tag | None = None) -> None:
        if self.full:
            return
        name = _clean(name)[:_MAX_NAME]
        if not _valid_name(name):
            return
        key = normalize_name(name)
        if not key or key in self._seen:
            return
        self._seen.add(key)
        self.items.append(
            RawCandidate(
                raw_candidate_id=len(self.items) + 1, name_text=name, link_id=self.link_id_for(anchor),
                context=_clean(context)[:_MAX_CONTEXT] or name, dom_hint=hint,  # type: ignore[arg-type]
            )
        )


def _cards(soup: BeautifulSoup, c: _Collector) -> None:
    """Repeated sibling blocks (two or more with the same tag+class) that each carry a heading."""
    for parent in soup.find_all(True):
        if c.full or _in_nav(parent):
            continue
        kids = [k for k in parent.find_all(recursive=False) if isinstance(k, Tag)]
        if len(kids) < 2:
            continue
        groups: dict[tuple[str, str], list[Tag]] = {}
        for k in kids:
            groups.setdefault((k.name, " ".join(sorted(k.get("class") or []))), []).append(k)
        for blocks in groups.values():
            if len(blocks) < 2 or blocks[0].name in ("li", "tr", "option"):
                continue
            for b in blocks:
                h = b.find(["h1", "h2", "h3", "h4", "h5", "h6"])
                if isinstance(h, Tag):
                    anchor = h.find("a", href=True) or b.find("a", href=True)
                    c.add(h.get_text(" "), b.get_text(" "), "card", anchor if isinstance(anchor, Tag) else None)


def _list_items(soup: BeautifulSoup, c: _Collector) -> None:
    for li in soup.find_all("li"):
        if c.full or _in_nav(li) or _in_footer(li):
            continue
        text = _clean(li.get_text(" "))
        if 2 <= len(text) <= 80:
            anchor = li.find("a", href=True)
            c.add(text, text, "list_item", anchor if isinstance(anchor, Tag) else None)


def _table_rows(soup: BeautifulSoup, c: _Collector) -> None:
    for tr in soup.find_all("tr"):
        if c.full or _in_nav(tr):
            continue
        cell = tr.find("td")
        if isinstance(cell, Tag):
            anchor = cell.find("a", href=True)
            c.add(cell.get_text(" "), tr.get_text(" "), "table_row", anchor if isinstance(anchor, Tag) else None)


def _logo_alts(soup: BeautifulSoup, c: _Collector) -> None:
    for a in soup.find_all("a", href=True):
        if c.full or _in_nav(a):
            continue
        img = a.find("img")
        if isinstance(img, Tag):
            alt = img.get("alt") or img.get("title")
            if isinstance(alt, str):
                name = re.sub(r"\s+logo$", "", _clean(alt), flags=re.I)
                c.add(name, alt, "logo_alt", a)


def _footer_statements(soup: BeautifulSoup, c: _Collector) -> None:
    for footer in soup.find_all(["footer"]) + soup.find_all(attrs={"role": "contentinfo"}):
        text = _clean(footer.get_text(" "))[:5000]
        for pat in _FOOTER_PATTERNS:
            for m in pat.finditer(text):
                start = max(m.start() - 80, 0)
                c.add(m.group(1).strip(" .,'-"), text[start : m.end() + 80], "footer")


def _walk_jsonld(node: object, depth: int = 0) -> Iterator[tuple[str, str]]:
    if depth > 6:
        return
    if isinstance(node, list):
        for item in node[:200]:
            yield from _walk_jsonld(item, depth + 1)
    elif isinstance(node, dict):
        types = node.get("@type")
        type_list = types if isinstance(types, list) else [types]
        name = node.get("name")
        if isinstance(name, str) and any(t in ("Organization", "Corporation", "Brand") for t in type_list):
            yield name, f"{'/'.join(str(t) for t in type_list)}: {name}"
        for key in ("subOrganization", "brand", "parentOrganization", "member", "@graph"):
            if key in node:
                yield from _walk_jsonld(node[key], depth + 1)


def _jsonld(soup: BeautifulSoup, c: _Collector) -> None:
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = script.string or script.get_text() or ""
        if len(raw) > _MAX_JSONLD_BYTES:
            continue
        try:
            doc = json.loads(raw)
        except (ValueError, RecursionError):
            continue
        for name, ctx in _walk_jsonld(doc):
            c.add(name, ctx, "jsonld")


def extract_dom_candidates(html: str, link_index: LinkIndex, *, max_candidates: int = 300) -> list[RawCandidate]:
    """Candidate names in fixed strategy order: card, list_item, table_row, logo_alt, footer, jsonld."""
    soup = BeautifulSoup(html or "", "html.parser")
    jsonld_soup = BeautifulSoup(html or "", "html.parser")  # keep scripts for JSON-LD only
    for tag in soup.find_all(["script", "style", "noscript", "template"]):
        tag.decompose()
    c = _Collector(link_index, link_index.base_url, max_candidates)
    for strategy in (_cards, _list_items, _table_rows, _logo_alts, _footer_statements):
        strategy(soup, c)
    _jsonld(jsonld_soup, c)
    return c.items
