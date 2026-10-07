"""Redirect handling and the SSRF guard: correctness (slash, loops, chains) and security (private hosts, parser tricks).

The decisive assertion everywhere is that NO request is ever issued to a private/loopback host, not merely that an error
was returned."""
from __future__ import annotations

import pytest

from corpdisc.models.domain import CrawlStatus
from corpdisc.services.fetch import _numeric_ipv4, is_private_host, unsafe_redirect_target
from tests.services.test_fetch_service import BODY, U, make_env, resp  # noqa: F401  (make_env is a fixture)


def redirect(src, location, status=302):
    return {f"{U}{src}": resp(f"{U}{src}", status, "", headers={"Location": location})}


def hosts_requested(e):
    from urllib.parse import urlsplit
    return [(urlsplit(c).hostname or "") for c in e.http.calls if "robots.txt" not in c]


def assert_never_contacted_private(e):
    bad = [h for h in hosts_requested(e) if is_private_host(h)]
    assert bad == [], f"a request was issued to a private host: {bad}"


# ------------------------------------------------------------------ correctness
def test_trailing_slash_redirect_is_followed_and_is_not_a_loop(make_env):
    e = make_env({**redirect("/page", "/page/", 301), f"{U}/page/": resp(f"{U}/page/", 200, BODY)})
    r = e.fetch("/page")
    assert r.ok and r.crawl_status is CrawlStatus.OK and r.final_url == f"{U}/page/"


def test_normal_external_redirect_is_followed(make_env):
    e = make_env({**redirect("/go", "https://b.test/landing"), "https://b.test/landing": resp("https://b.test/landing", 200, BODY)})
    r = e.fetch("/go")
    assert r.ok and r.final_url == "https://b.test/landing"


def test_redirect_chain_within_the_limit_succeeds_and_beyond_it_fails(make_env):
    ok = make_env({**redirect("/a", "/b"), **redirect("/b", "/c"), f"{U}/c": resp(f"{U}/c", 200, BODY)})
    assert ok.fetch("/a").ok
    long = {}
    for i in range(12):
        long.update(redirect(f"/h{i}", f"/h{i + 1}"))
    r = make_env(long).fetch("/h0")
    assert r.error_code == "TOO_MANY_REDIRECTS"


@pytest.mark.parametrize("routes_, start", [
    (redirect("/self", "/self"), "/self"),                                                   # A -> A
    ({**redirect("/ab", "/ba"), **redirect("/ba", "/ab")}, "/ab"),                           # A -> B -> A
    ({**redirect("/p", "/p/"), **redirect("/p/", "/p")}, "/p"),                              # slash alternation
])
def test_real_loops_are_detected_by_exact_request_url(make_env, routes_, start):
    e = make_env(routes_)
    r = e.fetch(start)
    assert r.error_code == "REDIRECT_ERROR" and not r.ok
    assert len([c for c in e.http.calls if "robots" not in c]) <= 3  # bounded: no wandering until max_redirects


# ------------------------------------------------------------------ SSRF: private / local targets
@pytest.mark.parametrize("target", [
    "http://127.0.0.1/admin", "http://127.0.0.1:11434/api/tags", "http://localhost/x", "http://LOCALHOST./x", "http://[::1]/",
    "http://[::ffff:127.0.0.1]/", "http://10.0.0.5/", "http://192.168.1.1/", "http://169.254.169.254/latest/meta-data/",
    "http://0.0.0.0/", "http://foo.localhost/", "http://svc.internal/",
])
def test_redirects_to_private_or_local_hosts_are_refused_without_a_request(make_env, target):
    e = make_env(redirect("/r", target))
    r = e.fetch("/r")
    assert r.error_code == "PRIVATE_HOST_BLOCKED" and r.crawl_status is CrawlStatus.SKIPPED_FILTER
    assert_never_contacted_private(e)


@pytest.mark.parametrize("target", ["http://127.1/", "http://0x7f.0.0.1/", "http://2130706433/", "http://0177.0.0.1/",
                                    "http://0x7f000001/", "http://127.0.1/", "http://017700000001/"])
def test_numeric_ip_spellings_are_treated_as_private(make_env, target):
    e = make_env(redirect("/r", target))
    r = e.fetch("/r")
    assert r.error_code == "PRIVATE_HOST_BLOCKED"
    assert_never_contacted_private(e)


# ------------------------------------------------------------------ malformed, userinfo, encoded, hostname confusion
@pytest.mark.parametrize("location", [
    "http://[a.test/x", "http://[::1\\@a.test/", "http://a.test/ x", "http://a.test/\tx", "http://a.test/\x00x", "http://a.test/\nx",
    "http://127.0.0.1\\@a.test/", "http://a.test\\@127.0.0.1/", "http://a.test\\.127.0.0.1/", "http://127.0.0.1\\a.test/",
    "http://user:pw@a.test/x", "http://127.0.0.1@a.test/", "http://a.test@127.0.0.1/", "http://127.0.0.1%2f@a.test/",
    "http://a.test%2f@127.0.0.1/",
])
def test_malformed_userinfo_and_confusable_targets_are_rejected_before_any_request(make_env, location):
    e = make_env(redirect("/r", location))
    r = e.fetch("/r")
    assert r.error_code == "REDIRECT_ERROR", (location, r.error_code)  # never INTERNAL_ERROR
    assert [c for c in e.http.calls if "robots" not in c] == [f"{U}/r"]  # nothing beyond the original request
    assert_never_contacted_private(e)


@pytest.mark.parametrize("location", ["http://127%2e0%2e0%2e1/", "http://%31%32%37.0.0.1/", "http://127.0.0.1%00.a.test/",
                                      "http://127.0.0.1#@a.test/", "http://127.0.0.1?@a.test/", "//127.0.0.1/admin", "///127.0.0.1/x"])
def test_encoded_and_fragment_confusions_never_reach_a_private_host(make_env, location):
    e = make_env(redirect("/r", location))
    r = e.fetch("/r")
    assert not r.ok
    assert_never_contacted_private(e)


def test_fragment_trick_to_a_public_host_follows_only_the_public_host(make_env):
    e = make_env({**redirect("/r", "http://b.test#@127.0.0.1/"), "http://b.test/": resp("http://b.test/", 200, BODY)})
    r = e.fetch("/r")
    assert set(hosts_requested(e)) <= {"a.test", "b.test"}  # '#' ends the authority: the host is b.test, never 127.0.0.1


@pytest.mark.parametrize("location", ["ftp://a.test/x", "file:///etc/passwd", "javascript:alert(1)", "data:text/html,x", "gopher://a.test/"])
def test_non_http_schemes_are_never_followed(make_env, location):
    e = make_env(redirect("/r", location))
    assert not e.fetch("/r").ok
    assert [c for c in e.http.calls if "robots" not in c] == [f"{U}/r"]


def test_private_redirect_is_followed_only_when_private_hosts_are_explicitly_allowed(make_env):
    """The guard is configuration-driven, not removed: the same redirect succeeds only with ALLOW_PRIVATE_HOSTS."""
    from dataclasses import replace

    from tests.services.test_fetch_service import CFG
    routes_ = {**redirect("/r", "http://127.0.0.1:9/x"), "http://127.0.0.1:9/x": resp("http://127.0.0.1:9/x", 200, BODY)}
    assert make_env(routes_).fetch("/r").error_code == "PRIVATE_HOST_BLOCKED"  # default: refused
    allowed = make_env(routes_, cfg=replace(CFG, allow_private_hosts=True))
    r = allowed.fetch("/r")
    assert r.ok and r.final_url.startswith("http://127.0.0.1:9/")  # explicitly allowed: followed


# ------------------------------------------------------------------ unit: the guard itself
@pytest.mark.parametrize("host", ["127.0.0.1", "127.1", "0x7f.0.0.1", "2130706433", "0177.0.0.1", "0x7f000001", "10.1.2.3", "172.16.0.1",
                                  "192.168.0.1", "169.254.169.254", "0.0.0.0", "localhost", "localhost.", "LOCALHOST", "a.localhost",
                                  "x.internal", "x.local", "[::1]", "::1", "[::ffff:127.0.0.1]", "fe80::1", "fc00::1", "", None,
                                  "100.64.0.1"])
def test_is_private_host_true(host):
    assert is_private_host(host) is True


@pytest.mark.parametrize("host", ["a.test", "example.com", "8.8.8.8", "1.1.1.1", "93.184.216.34", "123.test", "1e3.example", "256.1.1.1",
                                  "99999999999", "constellationsoftware.com", "[2606:4700:4700::1111]"])
def test_is_private_host_false_for_public_names_and_addresses(host):
    assert is_private_host(host) is False


def test_numeric_ipv4_semantics_match_inet_aton():
    import socket
    for h in ("127.1", "0x7f.0.0.1", "2130706433", "0177.0.0.1", "10.1", "192.168.257", "1.2.3.4"):
        mine = _numeric_ipv4(h)
        assert mine is not None and str(mine) == socket.inet_ntoa(socket.inet_aton(h)), h
    for h in ("256.1.1.1", "1.2.3.4.5", "a.b", "1..2", "99999999999", "0xzz", ""):
        assert _numeric_ipv4(h) is None


def test_unsafe_redirect_target_shapes():
    for bad in ("http://a\\b", "http://u@h/", "http://[x/", "a b", "a\tb"):
        assert unsafe_redirect_target(bad) is not None
    for ok in ("/page/", "https://b.test/x?y=1", "//b.test/x", "../up", "http://b.test:8080/p#frag"):
        assert unsafe_redirect_target(ok) is None
