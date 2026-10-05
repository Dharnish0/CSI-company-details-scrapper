"""Throttle, html_to_text, js-shell detection, RequestsHttpClient (local server), renderer import rules."""
from __future__ import annotations

import subprocess
import sys

import pytest

from corpdisc.services.fetch import (
    RequestsHttpClient,
    Throttle,
    html_to_text,
    is_private_host,
    js_shell_reason,
    looks_like_js_shell,
    plain_to_text,
)
from corpdisc.services.renderer import PlaywrightRenderer, RendererUnavailable
from tests.helpers.clock import FakeClock
from tests.helpers.http_server import Route


# ------------------------------------------------------------------ throttle
def test_throttle_first_request_no_wait_then_waits_default():
    clock = FakeClock()
    t = Throttle(clock, clock, 2.0)
    assert t.wait("a.test") == 0.0
    t.mark("a.test")
    assert t.wait("a.test") == pytest.approx(2.0)
    assert clock.sleeps == [pytest.approx(2.0)]
    t.mark("a.test")
    clock.advance(1.5)
    assert t.wait("a.test") == pytest.approx(0.5)


def test_throttle_uses_max_of_default_and_crawl_delay_and_domains_are_independent():
    clock = FakeClock()
    t = Throttle(clock, clock, 2.0)
    t.mark("a.test")
    assert t.wait("a.test", crawl_delay=5.0) == pytest.approx(5.0)
    t.mark("a.test")
    assert t.wait("a.test", crawl_delay=1.0) == pytest.approx(2.0)
    assert t.wait("b.test") == 0.0


def test_throttle_caps_absurd_crawl_delay():
    clock = FakeClock()
    t = Throttle(clock, clock, 0.0, max_delay=10.0)
    t.mark("a.test")
    assert t.wait("a.test", crawl_delay=86400) == pytest.approx(10.0)


# ------------------------------------------------------------------ html_to_text
def test_html_to_text_drops_script_style_noscript_and_keeps_unicode():
    html = ("<html><head><title>T</title><style>p{color:red}</style></head><body>"
            "<script>var x='secret';</script><noscript>enable js</noscript>"
            "<h1>Caf\u00e9  Group</h1><p>Acme   is <b>wholly</b> owned, by <a href='/x'>Northwind</a>, Inc.</p>"
            "<ul><li>One</li><li>Two</li></ul><!-- hidden --></body></html>")
    text = html_to_text(html)
    assert "secret" not in text and "color:red" not in text and "enable js" not in text and "hidden" not in text
    assert "Caf\u00e9 Group" in text
    assert "Acme is wholly owned, by Northwind, Inc." in text
    assert text.split("\n")[-2:] == ["One", "Two"]


def test_html_to_text_is_stable_and_collapses_blank_lines():
    html = "<div>\n\n<p>A</p>\n\n\n<p>  B\u00a0 C </p></div>"
    assert html_to_text(html) == "A\nB C" == html_to_text(html)


def test_plain_text_normalization():
    assert plain_to_text("a  b\r\n\r\n\r\n c ") == "a b\nc"


def test_html_to_text_survives_garbage():
    assert isinstance(html_to_text("<<<div><p>unclosed <b>bold"), str)
    assert html_to_text("") == ""


# ------------------------------------------------------------------ js shell
def test_js_shell_detection():
    assert js_shell_reason('<div id="root"></div><script src="a.js"></script>', "", 200) == "EMPTY_BODY"
    assert js_shell_reason("<p>short</p>", "short text only", 200) == "LITTLE_TEXT"
    long_text = "word " * 100  # 500 chars
    assert js_shell_reason("<p>" + long_text + "</p>", long_text, 200) is None
    assert looks_like_js_shell("<noscript>You need to enable JavaScript to run this app.</noscript>", "x" * 250, 200)
    assert not looks_like_js_shell("<p>" + long_text * 3 + "</p>", long_text * 3, 200)


def test_js_shell_marker_only_counts_when_text_is_modest():
    text = "word " * 100
    assert js_shell_reason('<div id="root"></div>', text, 200) == "JS_SHELL_MARKER"
    assert js_shell_reason('<div id="root"></div>', text * 3, 200) is None


# ------------------------------------------------------------------ ssrf
@pytest.mark.parametrize("host", ["127.0.0.1", "10.0.0.5", "192.168.1.1", "169.254.169.254", "::1", "localhost",
                                  "db.internal", "printer.local", "x.localhost", "0.0.0.0", None, ""])
def test_private_hosts(host):
    assert is_private_host(host)


@pytest.mark.parametrize("host", ["example.com", "93.184.216.34", "sub.example.co.uk"])
def test_public_hosts(host):
    assert not is_private_host(host)


# ------------------------------------------------------------------ RequestsHttpClient
def client():
    return RequestsHttpClient(trust_env=False)


def test_requests_client_200_body_and_headers(local_site):
    local_site.add("/ok", "<p>hi</p>")
    r = client().get(local_site.url("/ok"), headers={"User-Agent": "T/1"}, timeout=5, max_bytes=10_000)
    assert r.status_code == 200 and r.body == b"<p>hi</p>" and not r.truncated and r.error_code is None
    assert r.headers["content-type"].startswith("text/html")
    assert local_site.request_log[-1][1]["User-Agent"] == "T/1"


def test_requests_client_404_and_redirect_not_followed(local_site):
    local_site.add("/gone", "nope", status=404, content_type="text/plain")
    local_site.redirect("/old", "/new")
    local_site.add("/new", "new")
    c = client()
    assert c.get(local_site.url("/gone"), headers={}, timeout=5, max_bytes=1000).status_code == 404
    r = c.get(local_site.url("/old"), headers={}, timeout=5, max_bytes=1000)
    assert r.status_code == 302 and r.headers["location"] == "/new" and local_site.hits("/new") == 0


def test_requests_client_truncates_oversize_body(local_site):
    local_site.add("/big", b"x" * 5000, content_type="text/plain")
    r = client().get(local_site.url("/big"), headers={}, timeout=5, max_bytes=1000)
    assert r.truncated and r.status_code == 200 and len(r.body) <= 1000


def test_requests_client_bounds_decoded_gzip(local_site):
    local_site.add("/bomb", b"a" * 200_000, content_type="text/plain", gzip_body=True)
    r = client().get(local_site.url("/bomb"), headers={}, timeout=5, max_bytes=50_000)
    assert r.truncated and len(r.body) <= 50_000


def test_requests_client_small_gzip_is_decoded(local_site):
    local_site.add("/gz", b"hello gzip", content_type="text/plain", gzip_body=True)
    assert client().get(local_site.url("/gz"), headers={}, timeout=5, max_bytes=1000).body == b"hello gzip"


def test_requests_client_timeout(local_site):
    local_site.add("/slow", "late", delay=1.5)
    r = client().get(local_site.url("/slow"), headers={}, timeout=0.3, max_bytes=1000)
    assert r.error_code == "TIMEOUT" and r.status_code is None


def test_requests_client_connection_refused(local_site):
    url = local_site.url("/x")
    local_site.stop()
    r = client().get(url, headers={}, timeout=5, max_bytes=1000)
    # Windows retries a refused loopback connect for ~2s, which can surface as a connect timeout.
    assert r.error_code in {"CONNECTION_ERROR", "TIMEOUT"} and r.status_code is None
    local_site.start()  # fixture teardown calls stop()


def test_requests_client_never_raises_on_blocked_external_host():
    r = client().get("http://203.0.113.9/", headers={}, timeout=1, max_bytes=100)
    assert r.error_code == "CONNECTION_ERROR" and r.status_code is None


def test_route_dataclass_defaults():
    assert Route().status == 200


# ------------------------------------------------------------------ renderer import rules
def test_constructing_renderer_does_not_import_playwright():
    code = ("import sys; import corpdisc.services.fetch, corpdisc.services.renderer as r; r.PlaywrightRenderer(); "
            "assert 'playwright' not in sys.modules, 'playwright imported'")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr


def test_renderer_unavailable_without_playwright(monkeypatch):
    monkeypatch.setitem(sys.modules, "playwright", None)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", None)
    with pytest.raises(RendererUnavailable):
        PlaywrightRenderer().render("http://127.0.0.1/", timeout=1, user_agent="t", max_bytes=100)
