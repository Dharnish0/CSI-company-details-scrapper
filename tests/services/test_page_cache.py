from __future__ import annotations

import os
from datetime import UTC, datetime

from corpdisc.services.pagecache import PageCache

URL = "https://a.test/about"
T = datetime(2025, 1, 1, tzinfo=UTC)


def put(cache, url=URL, text="Hello world text", html="<p>Hello world text</p>"):
    return cache.put(url, html=html, text=text, final_url=url, status_code=200, content_type="text/html",
                     headers={"content-type": "text/html"}, retrieved_at=T, origin_method="requests")


def test_round_trip_and_relative_posix_paths(tmp_path):
    cache = PageCache(tmp_path)
    stored = put(cache)
    key = cache.key(URL)
    assert stored.text_path == f"pages/{key}.txt" and stored.html_path == f"pages/{key}.html"
    assert "\\" not in stored.text_path
    got = cache.get(URL)
    assert got is not None and got.meta.status_code == 200 and got.meta.retrieved_at == T
    assert cache.read_text(got.text_path) == "Hello world text"
    assert cache.read_html(got.html_path) == "<p>Hello world text</p>"
    assert sorted(p.suffix for p in (tmp_path / "pages").iterdir()) == [".html", ".json", ".txt"]


def test_key_is_sha256_of_canonical_url_and_variants_share_it(tmp_path):
    import hashlib

    from corpdisc.services.urls import canonicalize_url

    assert PageCache.key(URL) == hashlib.sha256(URL.encode()).hexdigest()
    a = canonicalize_url("HTTPS://A.test:443/about/?utm_source=x#top").canonical
    b = canonicalize_url("https://a.test//about").canonical
    assert a == b and PageCache.key(a) == PageCache.key(b)


def test_unicode_text_round_trips(tmp_path):
    cache = PageCache(tmp_path)
    s = put(cache, text="Soci\u00e9t\u00e9 \u2014 caf\u00e9", html=None)
    assert s.html_path is None
    assert cache.read_text(s.text_path) == "Soci\u00e9t\u00e9 \u2014 caf\u00e9"


def test_atomic_writes_leave_no_temp_files(tmp_path):
    cache = PageCache(tmp_path)
    for i in range(5):
        put(cache, url=f"https://a.test/{i}")
    assert not [p for p in (tmp_path / "pages").iterdir() if p.name.startswith(".tmp-") or p.suffix == ".part"]


def test_failed_write_cleans_temp_file(tmp_path, monkeypatch):
    cache = PageCache(tmp_path)

    def boom(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", boom)
    try:
        put(cache)
    except OSError:
        pass
    assert [p.name for p in (tmp_path / "pages").iterdir()] == []
    monkeypatch.undo()
    assert cache.get(URL) is None


def test_corrupted_text_is_a_miss(tmp_path, caplog):
    cache = PageCache(tmp_path)
    stored = put(cache)
    (tmp_path / stored.text_path).write_text("tampered", encoding="utf-8")
    assert cache.get(URL) is None
    assert cache.read_text(stored.text_path) is None


def test_corrupted_html_or_meta_is_a_miss(tmp_path):
    cache = PageCache(tmp_path)
    stored = put(cache)
    (tmp_path / stored.html_path).write_text("<p>evil</p>", encoding="utf-8")
    assert cache.get(URL) is None
    stored = put(cache)
    assert cache.get(URL) is not None
    (tmp_path / "pages" / f"{cache.key(URL)}.json").write_text("{not json", encoding="utf-8")
    assert cache.get(URL) is None


def test_missing_meta_means_uncommitted_entry(tmp_path):
    cache = PageCache(tmp_path)
    put(cache)
    (tmp_path / "pages" / f"{cache.key(URL)}.json").unlink()
    assert cache.get(URL) is None


def test_missing_text_file_is_a_miss(tmp_path):
    cache = PageCache(tmp_path)
    stored = put(cache)
    (tmp_path / stored.text_path).unlink()
    assert cache.get(URL) is None


def test_read_refuses_paths_outside_pages_dir(tmp_path):
    cache = PageCache(tmp_path)
    put(cache)
    outside = tmp_path / "secret.txt"
    outside.write_text("x", encoding="utf-8")
    assert cache.read_text("secret.txt") is None
    assert cache.read_text("pages/../secret.txt") is None
    assert cache.read_text("../" + outside.name) is None


def test_unknown_url_is_a_miss(tmp_path):
    assert PageCache(tmp_path).get("https://nope.test/") is None
