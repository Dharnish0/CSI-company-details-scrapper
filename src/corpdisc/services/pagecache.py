"""On-disk page cache (decision D15). SQLite keeps only metadata and relative paths.

Layout: ``<cache_dir>/pages/<sha256(canonical_url)>.{json,html,txt}``. The ``.json`` meta file
is written LAST (its presence is the commit marker). Every file is written to a temp name in
the same directory and moved with ``os.replace``. Reads verify the content hash; a mismatch is
a cache miss (logged), never an error.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Final

from corpdisc.clock import iso_utc, parse_iso_utc
from corpdisc.logging_setup import get_logger

log = get_logger("services.pagecache")

PAGES_DIR: Final = "pages"


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@dataclass(frozen=True)
class CacheMeta:
    canonical_url: str
    final_url: str | None
    status_code: int
    content_type: str | None
    headers: dict[str, str]
    retrieved_at: datetime
    origin_method: str  # 'requests' or 'playwright'
    content_hash: str  # sha256 of the cleaned-text file
    html_hash: str | None


@dataclass(frozen=True)
class StoredPage:
    text_path: str  # relative to cache_dir, posix separators
    html_path: str | None
    meta: CacheMeta


class PageCache:
    def __init__(self, cache_dir: Path | str) -> None:
        self._root = Path(cache_dir)
        self._dir = self._root / PAGES_DIR

    @property
    def directory(self) -> Path:
        return self._dir

    @staticmethod
    def key(canonical_url: str) -> str:
        return sha256_hex(canonical_url.encode("utf-8"))

    def _rel(self, key: str, suffix: str) -> str:
        return PurePosixPath(PAGES_DIR, f"{key}.{suffix}").as_posix()

    def _atomic_write(self, target: Path, data: bytes) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=self._dir, prefix=".tmp-", suffix=".part")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_name, target)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

    # ------------------------------------------------------------------ write
    def put(
        self,
        canonical_url: str,
        *,
        html: str | None,
        text: str,
        final_url: str | None,
        status_code: int,
        content_type: str | None,
        headers: Mapping[str, str],
        retrieved_at: datetime,
        origin_method: str,
    ) -> StoredPage:
        key = self.key(canonical_url)
        html_bytes = html.encode("utf-8") if html is not None else None
        text_bytes = text.encode("utf-8")
        html_rel = self._rel(key, "html") if html_bytes is not None else None
        text_rel = self._rel(key, "txt")
        meta = CacheMeta(
            canonical_url=canonical_url, final_url=final_url, status_code=status_code,
            content_type=content_type, headers=dict(headers), retrieved_at=retrieved_at,
            origin_method=origin_method, content_hash=sha256_hex(text_bytes),
            html_hash=sha256_hex(html_bytes) if html_bytes is not None else None,
        )  # fmt: skip
        if html_bytes is not None:
            self._atomic_write(self._dir / f"{key}.html", html_bytes)
        self._atomic_write(self._dir / f"{key}.txt", text_bytes)
        meta_doc = {
            "canonical_url": meta.canonical_url, "final_url": meta.final_url, "status_code": meta.status_code,
            "content_type": meta.content_type, "headers": meta.headers,
            "retrieved_at": iso_utc(meta.retrieved_at), "origin_method": meta.origin_method,
            "content_hash": meta.content_hash, "html_hash": meta.html_hash,
        }  # fmt: skip
        self._atomic_write(self._dir / f"{key}.json", json.dumps(meta_doc, sort_keys=True).encode("utf-8"))
        return StoredPage(text_path=text_rel, html_path=html_rel, meta=meta)

    # ------------------------------------------------------------------- read
    def _read_meta(self, key: str) -> CacheMeta | None:
        path = self._dir / f"{key}.json"
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
            return CacheMeta(
                canonical_url=doc["canonical_url"], final_url=doc.get("final_url"),
                status_code=int(doc["status_code"]), content_type=doc.get("content_type"),
                headers=dict(doc.get("headers") or {}), retrieved_at=parse_iso_utc(doc["retrieved_at"]),
                origin_method=doc["origin_method"], content_hash=doc["content_hash"],
                html_hash=doc.get("html_hash"),
            )  # fmt: skip
        except FileNotFoundError:
            return None
        except (OSError, ValueError, KeyError, TypeError) as exc:
            log.warning("page cache meta unreadable for %s: %s", key[:12], type(exc).__name__)
            return None

    def get(self, canonical_url: str) -> StoredPage | None:
        """Return the stored page, or None when missing or corrupted (corruption is logged)."""
        key = self.key(canonical_url)
        meta = self._read_meta(key)
        if meta is None or meta.canonical_url != canonical_url:
            return None
        text_rel = self._rel(key, "txt")
        if self._read_verified(text_rel, meta.content_hash) is None:
            return None
        html_rel: str | None = None
        if meta.html_hash is not None:
            html_rel = self._rel(key, "html")
            if self._read_verified(html_rel, meta.html_hash) is None:
                return None
        return StoredPage(text_path=text_rel, html_path=html_rel, meta=meta)

    def _resolve(self, rel_path: str) -> Path | None:
        """Resolve a cache-relative path, refusing anything outside <cache_dir>/pages."""
        candidate = (self._root / rel_path).resolve()
        if candidate.parent != self._dir.resolve():
            log.warning("refusing cache path outside the page cache")
            return None
        return candidate

    def _read_verified(self, rel_path: str, expected_hash: str) -> str | None:
        path = self._resolve(rel_path)
        if path is None:
            return None
        try:
            data = path.read_bytes()
        except OSError:
            log.warning("page cache file missing: %s", PurePosixPath(rel_path).name)
            return None
        if sha256_hex(data) != expected_hash:
            log.warning("page cache corruption (hash mismatch): %s", PurePosixPath(rel_path).name)
            return None
        return data.decode("utf-8", errors="replace")

    def _meta_for_path(self, rel_path: str) -> CacheMeta | None:
        return self._read_meta(PurePosixPath(rel_path).stem)

    def read_text(self, text_path: str) -> str | None:
        """Cleaned text, verified against the stored hash. None on missing or mismatch."""
        meta = self._meta_for_path(text_path)
        return None if meta is None else self._read_verified(text_path, meta.content_hash)

    def read_html(self, html_path: str) -> str | None:
        meta = self._meta_for_path(html_path)
        if meta is None or meta.html_hash is None:
            return None
        return self._read_verified(html_path, meta.html_hash)
