"""Optional headless-browser rendering behind a ``Renderer`` protocol (decision D14).

Playwright is an OPTIONAL dependency, imported lazily inside ``PlaywrightRenderer.render``.
Crawled pages are untrusted, so the browser is tightly constrained: one navigation, no
interactions, no injected scripts, images/fonts/media blocked, cross-origin document
navigations aborted, downloads and service workers off, bounded timeout and output size, and
the browser context is ALWAYS closed. Page JavaScript only ever runs inside that context.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlsplit

from corpdisc.logging_setup import get_logger

log = get_logger("services.renderer")

BLOCKED_RESOURCE_TYPES = frozenset({"image", "font", "media"})
NETWORK_IDLE_WAIT_MS = 3000


class RendererError(Exception):
    """Rendering failed (navigation error, timeout, oversize, cross-origin redirect)."""


class RendererUnavailable(RendererError):
    """Playwright or its browser is not installed."""


@dataclass(frozen=True)
class RenderedPage:
    final_url: str
    status_code: int | None
    html: str


class Renderer(Protocol):
    def render(self, url: str, *, timeout: float, user_agent: str, max_bytes: int) -> RenderedPage: ...
    def close(self) -> None: ...


def _origin(url: str) -> tuple[str, str, int | None]:
    parts = urlsplit(url)
    return parts.scheme.lower(), (parts.hostname or "").lower(), parts.port


def same_origin(a: str, b: str) -> bool:
    return _origin(a) == _origin(b)


class PlaywrightRenderer:
    """Sync Playwright/Chromium renderer. Constructing it imports nothing."""

    def __init__(self) -> None:
        self._playwright: Any = None
        self._browser: Any = None

    def _ensure_browser(self) -> Any:
        if self._browser is not None:
            return self._browser
        try:
            from playwright.sync_api import sync_playwright  # lazy, optional dependency
        except ImportError as exc:
            raise RendererUnavailable("playwright package is not installed") from exc
        try:
            self._playwright = sync_playwright().start()
            self._browser = self._playwright.chromium.launch(headless=True)
        except Exception as exc:  # browser binary missing or cannot start
            self.close()
            raise RendererUnavailable(f"chromium could not be launched: {type(exc).__name__}") from exc
        return self._browser

    def render(self, url: str, *, timeout: float, user_agent: str, max_bytes: int) -> RenderedPage:
        browser = self._ensure_browser()
        timeout_ms = max(1, int(timeout * 1000))
        context = browser.new_context(
            user_agent=user_agent,
            accept_downloads=False,
            service_workers="block",
            java_script_enabled=True,
            permissions=[],
        )
        try:
            context.set_default_timeout(timeout_ms)
            context.set_default_navigation_timeout(timeout_ms)
            page = context.new_page()

            def guard(route: Any) -> None:
                request = route.request
                if request.resource_type in BLOCKED_RESOURCE_TYPES:
                    route.abort()
                elif request.resource_type == "document" and not same_origin(request.url, url):
                    route.abort()  # no cross-origin document navigations (frames or redirects)
                else:
                    route.continue_()

            page.route("**/*", guard)
            try:
                response = page.goto(url, wait_until="load", timeout=timeout_ms)
                try:
                    page.wait_for_load_state("networkidle", timeout=min(timeout_ms, NETWORK_IDLE_WAIT_MS))
                except Exception:  # noqa: BLE001 - idle is best effort
                    pass
                final_url = page.url
                if not same_origin(final_url, url):
                    raise RendererError("navigation ended on a different origin")
                html = page.content()
            except RendererError:
                raise
            except Exception as exc:
                raise RendererError(f"render failed: {type(exc).__name__}") from exc
            if len(html.encode("utf-8", errors="replace")) > max_bytes:
                raise RendererError("rendered document exceeds the size limit")
            return RenderedPage(final_url=final_url, status_code=response.status if response else None, html=html)
        finally:
            try:
                context.close()
            except Exception:  # noqa: BLE001
                log.debug("context close failed", exc_info=False)

    def close(self) -> None:
        for name, closer in (("browser", "close"), ("playwright", "stop")):
            obj = getattr(self, f"_{name}")
            if obj is not None:
                try:
                    getattr(obj, closer)()
                except Exception:  # noqa: BLE001
                    pass
        self._browser = None
        self._playwright = None
