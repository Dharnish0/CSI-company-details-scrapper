"""Scripted fakes for fetch tests: no sockets, no sleeping."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from corpdisc.services.fetch import RawResponse
from corpdisc.services.renderer import RenderedPage, RendererError, RendererUnavailable


def resp(url: str, status: int = 200, body: str | bytes = "", *, ctype: str | None = "text/html; charset=utf-8",
         headers: dict[str, str] | None = None, truncated: bool = False) -> RawResponse:
    data = body.encode("utf-8") if isinstance(body, str) else body
    h = {k.lower(): v for k, v in (headers or {}).items()}
    if ctype:
        h["content-type"] = ctype
    return RawResponse(url=url, status_code=status, headers=h, body=data, truncated=truncated)


def err(url: str, code: str, message: str = "boom") -> RawResponse:
    return RawResponse(url=url, status_code=None, headers={}, error_code=code, error=message)


class FakeHttpClient:
    """routes[url] is one RawResponse or a list served in order (the last one repeats)."""

    def __init__(self, routes: Mapping[str, RawResponse | list[RawResponse]] | None = None) -> None:
        self.routes: dict[str, list[RawResponse]] = {}
        for url, r in (routes or {}).items():
            self.set(url, r)
        self.calls: list[str] = []

    def set(self, url: str, r: RawResponse | list[RawResponse]) -> None:
        self.routes[url] = list(r) if isinstance(r, list) else [r]

    def get(self, url: str, *, headers: Mapping[str, str], timeout: float, max_bytes: int) -> RawResponse:
        self.calls.append(url)
        seq = self.routes.get(url)
        if not seq:
            return resp(url, 404, "not found", ctype="text/plain")
        return seq.pop(0) if len(seq) > 1 else seq[0]

    def count(self, url: str) -> int:
        return self.calls.count(url)


@dataclass
class FakeRenderer:
    html: str = ""
    error: Exception | None = None
    calls: list[str] = field(default_factory=list)
    closed: bool = False
    final_url: str | None = None

    def render(self, url: str, *, timeout: float, user_agent: str, max_bytes: int) -> RenderedPage:
        self.calls.append(url)
        if self.error is not None:
            raise self.error
        return RenderedPage(final_url=self.final_url or url, status_code=200, html=self.html)

    def close(self) -> None:
        self.closed = True


__all__ = ["FakeHttpClient", "FakeRenderer", "RendererError", "RendererUnavailable", "err", "resp"]
