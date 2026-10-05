from __future__ import annotations

import socket

import pytest

from corpdisc.services.normalize import (
    DEFAULT_LEGAL_SUFFIXES,
    make_domain_extractor,
    name_similarity,
    normalize_alias,
    normalize_domain,
    normalize_name,
    registrable_domain,
)


def test_required_equivalences():
    a = normalize_name("Example Software Inc.")
    assert a == normalize_name("EXAMPLE SOFTWARE, INC") == normalize_name("Example Software, Inc.")
    assert a == "example software"


def test_example_differs_from_example_technologies():
    assert normalize_name("Example") != normalize_name("Example Technologies")
    assert normalize_name("Example Technologies") == "example technologies"


@pytest.mark.parametrize("word", ["software", "technologies", "group", "systems", "holdings", "solutions"])
def test_meaningful_words_are_never_stripped(word):
    assert normalize_name(f"Acme {word}") == f"acme {word}"


def test_accents_ampersand_punctuation():
    assert normalize_name("Caf\u00e9 \u00c9toile S.A.") == "cafe etoile"
    assert normalize_name("Smith & Sons Ltd") == "smith and sons"
    assert normalize_name("Smith and Sons") == normalize_name("Smith & Sons")
    assert normalize_name("O'Neil-Jones (Pty) Ltd.") == "oneil jones"
    assert normalize_name("  Multiple   Spaces\tHere ") == "multiple spaces here"
    assert normalize_name("\uff21\uff22\uff23 Corp") == "abc"  # NFKC full-width


def test_suffix_only_trailing_and_repeated():
    assert normalize_name("Acme Pty Ltd") == "acme"
    assert normalize_name("Acme Corporation Limited") == "acme"
    assert normalize_name("Ltd Acme") == "ltd acme"  # leading suffix word is kept
    assert normalize_name("Inc Holdings Inc") == "inc holdings"


def test_never_reduces_to_empty():
    assert normalize_name("Inc") == "inc"
    assert normalize_name("Ltd.") == "ltd"
    assert normalize_name("Co Ltd") == "co"  # keeps the first token rather than nothing
    assert normalize_name("") == ""
    assert normalize_name("!!!") == ""


def test_suffix_set_is_configurable():
    assert normalize_name("Acme Software", legal_suffixes={"software"}) == "acme"
    assert normalize_name("Acme Inc", legal_suffixes=set()) == "acme inc"
    assert "inc" in DEFAULT_LEGAL_SUFFIXES and "group" not in DEFAULT_LEGAL_SUFFIXES
    assert normalize_alias("Acme Inc.") == "acme"


def test_name_similarity_bounds_and_symmetry():
    assert name_similarity("Acme Software Inc", "ACME SOFTWARE") == 1.0
    assert name_similarity("", "x") == 0.0
    s1 = name_similarity("Northwind Analytics", "Northwynd Analytics")
    assert 0.9 < s1 < 1.0
    assert name_similarity("Northwind Analytics", "Northwynd Analytics") == name_similarity(
        "Northwynd Analytics", "Northwind Analytics")
    assert name_similarity("Alpha", "Zzzzzz") < 0.3


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("www.example.com", "example.com"),
        ("a.b.example.co.uk", "example.co.uk"),
        ("shop.foo.com.au", "foo.com.au"),
        ("https://Sub.Example.COM:8443/path?q=1", "example.com"),
        ("example.test", "example.test"),  # unknown TLD: last two labels
        ("a.b.example.test", "example.test"),
        ("127.0.0.1", "127.0.0.1"),
        ("http://[::1]:8000/x", "::1"),
        ("localhost", "localhost"),
        ("co.uk", "co.uk"),
    ],
)
def test_registrable_domain(host, expected):
    assert registrable_domain(host) == expected


def test_registrable_domain_empty():
    assert registrable_domain("") is None
    assert registrable_domain("   ") is None


def test_normalize_domain():
    assert normalize_domain("https://WWW.Example.com:8080/x") == "example.com"
    assert normalize_domain("www.example.com") == "example.com"
    assert normalize_domain("Sub.Example.com.") == "sub.example.com"


def test_extractor_is_offline_and_uses_cache_dir(tmp_path, monkeypatch):
    import requests

    def boom(*a, **k):
        raise AssertionError("tldextract attempted network access")

    monkeypatch.setattr(requests.Session, "request", boom)
    monkeypatch.setattr(requests.Session, "get", boom)

    def refuse(*a, **k):
        raise AssertionError("socket used")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    ext = make_domain_extractor(tmp_path)
    assert ext.suffix_list_urls == ()
    assert registrable_domain("deep.sub.example.co.uk", ext) == "example.co.uk"
