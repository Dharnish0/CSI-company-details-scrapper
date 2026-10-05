"""Search port for Website Resolution (ARCHITECTURE 10.2). The runtime pre-fetches hits; agents never search.

``SearchProvider`` is a protocol so tests inject fixtures and a real provider (M8) needs only an adapter. The service
canonicalises and de-duplicates URLs, drops shared platforms (social, code hosts, directories, site builders) and
never raises: a provider failure means "no hits".
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Protocol

from corpdisc.config import DEFAULT_SHARED_PLATFORM_DOMAINS
from corpdisc.contracts.agent_io import SearchHit
from corpdisc.logging_setup import get_logger
from corpdisc.services.normalize import registrable_domain
from corpdisc.services.urls import canonicalize_url

log = get_logger("services.search")


@dataclass(frozen=True)
class RawHit:
    url: str
    title: str
    snippet: str = ""


class SearchProvider(Protocol):
    def search(self, query: str, *, limit: int) -> Sequence[RawHit]: ...


class SearchService:
    def __init__(self, provider: SearchProvider, *, max_hits: int = 5,
                 blocklist: Iterable[str] = DEFAULT_SHARED_PLATFORM_DOMAINS) -> None:
        self._provider = provider
        self._max = max_hits
        self._blocked = frozenset(d.lower() for d in blocklist)
        self.queries: list[str] = []

    def hits_for(self, entity_name: str, parent_name: str, *, country: str | None = None) -> list[SearchHit]:
        query = " ".join(x for x in (entity_name, "official website", parent_name, country) if x)[:200]
        self.queries.append(query)
        try:
            raw = list(self._provider.search(query, limit=self._max * 2))
        except Exception as exc:  # noqa: BLE001 - search is best effort
            log.warning("search failed: %s", type(exc).__name__)
            return []
        out: list[SearchHit] = []
        seen: set[str] = set()
        for h in raw:
            res = canonicalize_url(h.url)
            reg = registrable_domain(h.url) if res.ok else None
            if not res.ok or not reg or reg in self._blocked or res.canonical in seen:
                continue
            seen.add(res.canonical)  # type: ignore[arg-type]
            out.append(SearchHit(hit_id=len(out) + 1, url=res.canonical, domain=res.domain or reg,  # type: ignore[arg-type]
                                 title=h.title[:200], snippet=h.snippet[:400], rank=len(out) + 1))
            if len(out) >= self._max:
                break
        return out
