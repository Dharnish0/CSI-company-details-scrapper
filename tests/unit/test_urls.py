from __future__ import annotations

import pytest

from corpdisc.services.urls import MAX_URL_LENGTH, UrlRejectReason, canonicalize_url, domain_of

BASE = "https://www.example.test/group/index.html"


def canon(url, base=None, **kw):
    r = canonicalize_url(url, base, **kw)
    assert r.ok, (url, r.reason, r.detail)
    return r.canonical


# ------------------------------------------------------------ relative / absolute
@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("/companies", "https://www.example.test/companies"),
        ("companies/", "https://www.example.test/group/companies"),
        ("../about", "https://www.example.test/about"),
        ("./x.html", "https://www.example.test/group/x.html"),
        ("//cdn.other.test/a", "https://cdn.other.test/a"),
        ("?page=2", "https://www.example.test/group/index.html?page=2"),
        ("https://other.test/a", "https://other.test/a"),
    ],
)
def test_relative_and_absolute_resolution(url, expected):
    assert canon(url, BASE) == expected


def test_relative_without_base_is_rejected_not_raised():
    r = canonicalize_url("/companies")
    assert not r.ok and r.reason is UrlRejectReason.RELATIVE_WITHOUT_BASE
    assert canonicalize_url("example.test/path").reason is UrlRejectReason.RELATIVE_WITHOUT_BASE


def test_absolute_url_lowercases_scheme_and_host_keeps_path_case():
    assert canon("HTTPS://WWW.Example.TEST/About/Us") == "https://www.example.test/About/Us"


# ------------------------------------------------------------ fragment / slashes / ports
def test_fragment_removed():
    assert canon("https://a.test/page#section-2") == "https://a.test/page"
    assert canon("https://a.test/#top") == "https://a.test/"
    assert canon("#frag", "https://a.test/x") == "https://a.test/x"


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://a.test//about///team", "https://a.test/about/team"),
        ("https://a.test/about/", "https://a.test/about"),
        ("https://a.test/about//", "https://a.test/about"),
        ("https://a.test", "https://a.test/"),
        ("https://a.test//", "https://a.test/"),
        ("https://a.test/a/./b/../c", "https://a.test/a/c"),
    ],
)
def test_duplicate_slashes_and_trailing_slash(url, expected):
    assert canon(url) == expected


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("http://a.test:80/x", "http://a.test/x"),
        ("https://a.test:443/x", "https://a.test/x"),
        ("https://a.test:8443/x", "https://a.test:8443/x"),
        ("http://a.test:443/x", "http://a.test:443/x"),
        ("http://user:pw@a.test/x", "http://a.test/x"),
    ],
)
def test_default_port_and_userinfo(url, expected):
    assert canon(url) == expected


def test_result_exposes_host_port_registrable_domain():
    r = canonicalize_url("https://sub.Example.co.uk:8443/x")
    assert (r.scheme, r.host, r.port) == ("https", "sub.example.co.uk", 8443)
    assert r.registrable_domain == "example.co.uk"
    assert r.domain == "sub.example.co.uk:8443"
    assert canonicalize_url("https://a.test:443/").domain == "a.test"
    assert domain_of("http://127.0.0.1:5000/x") == "127.0.0.1:5000"
    assert domain_of("javascript:void(0)") is None


# ------------------------------------------------------------ query parameters
def test_meaningful_query_params_kept_and_sorted():
    assert canon("https://a.test/list?page=2&b=1&a=") == "https://a.test/list?a=&b=1&page=2"
    assert canon("https://a.test/list?page=2") == "https://a.test/list?page=2"
    assert canon("https://a.test/?q=a%20b&flag") == "https://a.test/?flag&q=a%20b"


def test_tracking_params_removed_but_not_blind_stripping():
    url = "https://a.test/p?utm_source=x&page=3&fbclid=abc&UTM_Medium=m&gclid=1&id=7"
    assert canon(url) == "https://a.test/p?id=7&page=3"
    assert canon("https://a.test/p?utm_source=x") == "https://a.test/p"


def test_tracking_params_are_configurable():
    url = "https://a.test/p?ref=home&utm_source=x&page=1"
    assert canon(url, tracking_params=("ref",)) == "https://a.test/p?page=1&utm_source=x"
    assert canon(url, tracking_params=("ut*",)) == "https://a.test/p?page=1&ref=home"
    assert canon(url, tracking_params=()) == "https://a.test/p?page=1&ref=home&utm_source=x"


def test_repeated_keys_sorted_by_value():
    assert canon("https://a.test/?t=b&t=a") == "https://a.test/?t=a&t=b"


# ------------------------------------------------------------ percent encoding
@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://a.test/%7euser", "https://a.test/~user"),
        ("https://a.test/a%2fb", "https://a.test/a%2Fb"),
        ("https://a.test/a b", "https://a.test/a%20b"),
        ("https://a.test/caf\u00e9", "https://a.test/caf%C3%A9"),
        ("https://a.test/caf%c3%a9", "https://a.test/caf%C3%A9"),
        ("https://a.test/100%", "https://a.test/100%25"),
        ("https://a.test/%41bc", "https://a.test/Abc"),
    ],
)
def test_percent_encoding_normalization(url, expected):
    assert canon(url) == expected


def test_idn_host_becomes_punycode():
    assert canon("https://B\u00fccher.Example/x") == "https://xn--bcher-kva.example/x"


def test_ipv6_literal():
    assert canon("http://[::1]:8080/a") == "http://[::1]:8080/a"


# ------------------------------------------------------------ unsupported schemes
@pytest.mark.parametrize(
    "url",
    ["javascript:void(0)", "JavaScript:alert(1)", "mailto:a@b.test", "tel:+1555", "data:text/html,hi",
     "ftp://a.test/f", "file:///etc/passwd", "sms:123", "  java\nscript:alert(1)"],
)
def test_unsupported_schemes_rejected(url):
    r = canonicalize_url(url, BASE)
    assert not r.ok and r.canonical is None
    assert r.reason is UrlRejectReason.UNSUPPORTED_SCHEME


def test_only_http_and_https_pass():
    assert canonicalize_url("http://a.test/").ok and canonicalize_url("https://a.test/").ok


# ------------------------------------------------------------ malformed (never raise)
@pytest.mark.parametrize(
    ("url", "reason"),
    [
        ("", UrlRejectReason.EMPTY),
        ("   ", UrlRejectReason.EMPTY),
        (None, UrlRejectReason.EMPTY),
        ("http://", UrlRejectReason.MISSING_HOST),
        ("https:///path", UrlRejectReason.MISSING_HOST),
        ("http://[::1", UrlRejectReason.MALFORMED),
        ("http://host:99999/", UrlRejectReason.INVALID_PORT),
        ("http://host:abc/", UrlRejectReason.INVALID_PORT),
        ("http://exa mple.test/", UrlRejectReason.INVALID_HOST),
        ("http://a..b.test/", UrlRejectReason.INVALID_HOST),
        ("http://-bad-.test/", UrlRejectReason.INVALID_HOST),
        ("https://a.test/" + "x" * MAX_URL_LENGTH, UrlRejectReason.TOO_LONG),
    ],
)
def test_malformed_urls_return_structured_rejection(url, reason):
    r = canonicalize_url(url, BASE)
    assert not r.ok
    assert r.reason is reason, (url, r.reason)


@pytest.mark.parametrize("junk", ["http://\x00", "https://a.test/\x07x", "\ud800", "http://%zz.test", "::::", "http://[", 12345])
def test_garbage_never_raises(junk):
    r = canonicalize_url(junk, BASE)  # type: ignore[arg-type]
    assert r.ok or r.reason is not None


def test_control_chars_inside_are_stripped():
    assert canon("https://a.test/pa\tth\n") == "https://a.test/path"


# ------------------------------------------------------------ idempotency
CORPUS = [
    "https://Example.TEST:443//a//b/?utm_source=x&b=2&a=1#frag",
    "http://a.test/%7euser/caf\u00e9?q=a b&flag",
    "https://a.test/a%2Fb/../c/",
    "https://B\u00fccher.Example/x?z=1&z=0",
    "http://[::1]:80/",
    "/relative/../path?x=1",
    "https://a.test/100%?x=%",
]


@pytest.mark.parametrize("url", CORPUS)
def test_canonicalization_is_idempotent(url):
    first = canonicalize_url(url, BASE)
    assert first.ok
    again = canonicalize_url(first.canonical)
    assert again.canonical == first.canonical


def test_equivalent_variants_share_one_canonical_form():
    variants = ["HTTP://A.test:80/x/?utm_x=1#f", "http://a.test//x", "http://a.test/x/", "http://a.test/x#y"]
    assert len({canon(v) for v in variants}) == 1
