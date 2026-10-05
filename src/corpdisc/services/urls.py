"""Deterministic URL canonicalization (never raises on untrusted input).

Rules: http/https only; lowercase scheme and host; IDNA host; no userinfo; default
ports dropped; fragments removed; dot segments and duplicate slashes collapsed; trailing
slash removed from non-root paths; percent-encoding normalized (unreserved decoded, the
rest uppercased); configured tracking parameters dropped and every other query parameter
kept, sorted by (key, value). Relative URLs resolve against ``base``.
"""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Final
from urllib.parse import urljoin, urlsplit

from corpdisc.config import DEFAULT_TRACKING_QUERY_PARAMS

MAX_URL_LENGTH: Final = 2048
_DEFAULT_PORTS: Final = {"http": 80, "https": 443}
_SCHEME_RE = re.compile(r"^([A-Za-z][A-Za-z0-9+.\-]*):")
_HEX = "0123456789abcdefABCDEF"
_UNRESERVED = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")
_SUB_DELIMS = frozenset("!$&'()*+,;=")
_PATH_SAFE = _UNRESERVED | _SUB_DELIMS | frozenset(":@/")
# In a query, '&' and '=' are structural, so they are handled by the splitter, not here.
_QUERY_SAFE = _UNRESERVED | (_SUB_DELIMS - frozenset("&=")) | frozenset(":@/?")
_LABEL_RE = re.compile(r"^[a-z0-9_]([a-z0-9_\-]*[a-z0-9_])?$")


class UrlRejectReason(StrEnum):
    EMPTY = "EMPTY"
    TOO_LONG = "TOO_LONG"
    UNSUPPORTED_SCHEME = "UNSUPPORTED_SCHEME"
    MALFORMED = "MALFORMED"
    MISSING_HOST = "MISSING_HOST"
    INVALID_HOST = "INVALID_HOST"
    INVALID_PORT = "INVALID_PORT"
    RELATIVE_WITHOUT_BASE = "RELATIVE_WITHOUT_BASE"


@dataclass(frozen=True, slots=True)
class UrlResult:
    original: str
    canonical: str | None
    reason: UrlRejectReason | None
    detail: str | None = None
    scheme: str | None = None
    host: str | None = None
    port: int | None = None  # None when the default port for the scheme
    registrable_domain: str | None = None

    @property
    def ok(self) -> bool:
        return self.canonical is not None

    @property
    def url(self) -> str | None:
        return self.canonical

    @property
    def domain(self) -> str | None:
        """Lowercase host, plus ``:port`` only when the port is non-default."""
        if self.host is None:
            return None
        return f"{self.host}:{self.port}" if self.port is not None else self.host


def _reject(original: str, reason: UrlRejectReason, detail: str | None = None) -> UrlResult:
    return UrlResult(original=original, canonical=None, reason=reason, detail=detail)


def _encode_component(text: str, safe: frozenset[str]) -> str:
    """Normalize percent-encoding: decode unreserved, uppercase other escapes, encode the rest."""
    out: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "%":
            if i + 2 < n and text[i + 1] in _HEX and text[i + 2] in _HEX:
                decoded = chr(int(text[i + 1 : i + 3], 16))
                out.append(decoded if decoded in _UNRESERVED else "%" + text[i + 1 : i + 3].upper())
                i += 3
                continue
            out.append("%25")  # a lone '%' is data
        elif ch in safe:
            out.append(ch)
        else:
            out.extend(f"%{b:02X}" for b in ch.encode("utf-8", errors="replace"))
        i += 1
    return "".join(out)


def _collapse_path(path: str) -> str:
    segments: list[str] = []
    for seg in path.split("/"):
        if seg in ("", "."):
            continue
        if seg == "..":
            if segments:
                segments.pop()
            continue
        segments.append(seg)
    return "/" + "/".join(segments)


def _tracking_matcher(patterns: Iterable[str]) -> tuple[frozenset[str], tuple[str, ...]]:
    exact: set[str] = set()
    prefixes: list[str] = []
    for raw in patterns:
        p = raw.strip().lower()
        if not p:
            continue
        if p.endswith("*"):
            prefixes.append(p[:-1])
        else:
            exact.add(p)
    return frozenset(exact), tuple(prefixes)


def _is_tracking(key: str, matcher: tuple[frozenset[str], tuple[str, ...]]) -> bool:
    k = key.lower()
    exact, prefixes = matcher
    return k in exact or any(k.startswith(p) for p in prefixes)


def _canonical_query(query: str, matcher: tuple[frozenset[str], tuple[str, ...]]) -> str:
    pairs: list[tuple[str, str | None]] = []
    for part in query.split("&"):
        if part == "":
            continue
        key, sep, value = part.partition("=")
        nkey = _encode_component(key, _QUERY_SAFE)
        if _is_tracking(nkey, matcher):
            continue
        pairs.append((nkey, _encode_component(value, _QUERY_SAFE) if sep else None))
    pairs.sort(key=lambda kv: (kv[0], "" if kv[1] is None else kv[1], kv[1] is not None))
    return "&".join(k if v is None else f"{k}={v}" for k, v in pairs)


def _canonical_host(raw_host: str) -> str | None:
    host = raw_host.strip().rstrip(".").lower()
    if not host:
        return None
    if ":" in host:  # IPv6 literal (urlsplit strips the brackets)
        try:
            return f"[{ipaddress.IPv6Address(host.split('%')[0])}]"
        except ValueError:
            return None
    if not host.isascii():
        try:
            host = host.encode("idna").decode("ascii").lower()
        except UnicodeError:
            return None
    if len(host) > 253:
        return None
    labels = host.split(".")
    if any(not lbl or len(lbl) > 63 or not _LABEL_RE.match(lbl) for lbl in labels):
        return None
    return host


def _registrable(host: str, extractor: object | None) -> str | None:
    # Imported lazily: normalize.py owns the offline tldextract setup.
    from corpdisc.services.normalize import registrable_domain

    return registrable_domain(host, extractor)  # type: ignore[arg-type]


def canonicalize_url(
    url: str | None,
    base: str | None = None,
    *,
    tracking_params: Sequence[str] = DEFAULT_TRACKING_QUERY_PARAMS,
    extractor: object | None = None,
) -> UrlResult:
    """Canonicalize ``url`` (resolving against ``base``). Never raises."""
    original = url if isinstance(url, str) else ""
    try:
        return _canonicalize(original, base, tracking_params, extractor)
    except Exception as exc:  # defensive: untrusted input must never crash a crawl
        return _reject(original, UrlRejectReason.MALFORMED, f"{type(exc).__name__}")


def _canonicalize(original: str, base: str | None, tracking_params: Sequence[str],
                  extractor: object | None) -> UrlResult:
    text = original.strip()
    text = "".join(ch for ch in text if ord(ch) >= 32 and ord(ch) != 127)
    if not text:
        return _reject(original, UrlRejectReason.EMPTY)
    if len(text) > MAX_URL_LENGTH:
        return _reject(original, UrlRejectReason.TOO_LONG, f"{len(text)} chars")

    m = _SCHEME_RE.match(text)
    if m:
        scheme = m.group(1).lower()
        if scheme not in _DEFAULT_PORTS:
            return _reject(original, UrlRejectReason.UNSUPPORTED_SCHEME, scheme)
    else:
        if base is None or not base.strip():
            return _reject(original, UrlRejectReason.RELATIVE_WITHOUT_BASE)
        base_text = "".join(ch for ch in base.strip() if ord(ch) >= 32 and ord(ch) != 127)
        bm = _SCHEME_RE.match(base_text)
        if not bm or bm.group(1).lower() not in _DEFAULT_PORTS:
            return _reject(original, UrlRejectReason.UNSUPPORTED_SCHEME, "base")
        try:
            text = urljoin(base_text, text)
        except ValueError:
            return _reject(original, UrlRejectReason.MALFORMED, "cannot resolve against base")

    try:
        parts = urlsplit(text)
        scheme = parts.scheme.lower()
        hostname = parts.hostname
        port = parts.port
    except ValueError as exc:
        reason = UrlRejectReason.INVALID_PORT if "Port" in str(exc) else UrlRejectReason.MALFORMED
        return _reject(original, reason, "unparsable URL")
    if scheme not in _DEFAULT_PORTS:
        return _reject(original, UrlRejectReason.UNSUPPORTED_SCHEME, scheme)
    if not hostname:
        return _reject(original, UrlRejectReason.MISSING_HOST)
    host = _canonical_host(hostname)
    if host is None:
        return _reject(original, UrlRejectReason.INVALID_HOST)
    if port is not None and not 0 < port < 65536:
        return _reject(original, UrlRejectReason.INVALID_PORT, str(port))
    if port == _DEFAULT_PORTS[scheme]:
        port = None

    path = _collapse_path(_encode_component(parts.path, _PATH_SAFE))
    if len(path) > 1:
        path = path.rstrip("/") or "/"
    query = _canonical_query(parts.query, _tracking_matcher(tracking_params))

    netloc = host if port is None else f"{host}:{port}"
    canonical = f"{scheme}://{netloc}{path}" + (f"?{query}" if query else "")
    return UrlResult(
        original=original,
        canonical=canonical,
        reason=None,
        scheme=scheme,
        host=host,
        port=port,
        registrable_domain=_registrable(host, extractor),
    )


def domain_of(url: str) -> str | None:
    """``host[:port]`` of a canonicalizable URL, else None."""
    return canonicalize_url(url).domain
