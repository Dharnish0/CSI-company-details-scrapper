"""Name and domain normalization (ARCHITECTURE.md 6.1).

Pure functions. Registrable domains use tldextract in OFFLINE mode (bundled public
suffix snapshot, no HTTP). Fuzzy similarity uses stdlib ``difflib`` only.
"""

from __future__ import annotations

import ipaddress
import re
import unicodedata
from collections.abc import Iterable
from difflib import SequenceMatcher
from pathlib import Path
from typing import TYPE_CHECKING, Final
from urllib.parse import urlsplit

if TYPE_CHECKING:  # pragma: no cover
    from tldextract import TLDExtract

# Generic legal-form suffixes only. Words such as software/technologies/group/systems are
# meaningful parts of a name and are never stripped.
DEFAULT_LEGAL_SUFFIXES: Final[frozenset[str]] = frozenset(
    {
        "inc", "incorporated", "ltd", "limited", "llc", "gmbh", "bv", "nv", "ab", "as", "oy", "sa",
        "srl", "spa", "pty", "pte", "corp", "corporation", "co", "plc", "lp", "llp", "ag",
    }
)  # fmt: skip

_DROP_JOINED = re.compile(r"[.'\u2019\u2018`\u00b4]")  # removed without leaving a gap: S.A. -> sa
_NON_WORD = re.compile(r"[^0-9a-z\s]+")
_WS = re.compile(r"\s+")


def normalize_text(text: str) -> str:
    """NFKC, strip accents, casefold, collapse whitespace."""
    text = unicodedata.normalize("NFKC", text)
    text = "".join(ch for ch in unicodedata.normalize("NFD", text) if unicodedata.category(ch) != "Mn")
    return _WS.sub(" ", text.casefold()).strip()


def normalize_name(name: str, *, legal_suffixes: Iterable[str] | None = None) -> str:
    """Canonical comparison form of an entity name.

    NFKC, lowercase, strip accents, ``&`` -> ``and``, drop punctuation, strip ONLY trailing
    legal-form suffixes (configurable, repeatedly), collapse whitespace. Never reduces a
    name to nothing: if every token is a suffix the unstripped form is returned. The result
    is empty only when the input has no letters or digits at all.
    """
    suffixes = DEFAULT_LEGAL_SUFFIXES if legal_suffixes is None else frozenset(s.lower() for s in legal_suffixes)
    text = normalize_text(name or "").replace("&", " and ")
    text = _DROP_JOINED.sub("", text)
    text = _NON_WORD.sub(" ", text)
    tokens = _WS.sub(" ", text).strip().split(" ")
    tokens = [t for t in tokens if t]
    stripped = list(tokens)
    while len(stripped) > 1 and stripped[-1] in suffixes:
        stripped.pop()
    return " ".join(stripped)


def normalize_alias(alias: str, *, legal_suffixes: Iterable[str] | None = None) -> str:
    return normalize_name(alias, legal_suffixes=legal_suffixes)


def name_similarity(a: str, b: str) -> float:
    """difflib ratio (0..1) of the normalized names. Symmetric for identical strings."""
    na, nb = normalize_name(a), normalize_name(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    return SequenceMatcher(None, na, nb, autojunk=False).ratio()


# ------------------------------------------------------------------ domains
def make_domain_extractor(cache_dir: Path | str | None = None) -> TLDExtract:
    """Offline tldextract: no suffix-list URLs, bundled snapshot, cache under ``cache_dir``."""
    from tldextract import TLDExtract

    cache = str(Path(cache_dir) / "tldextract") if cache_dir is not None else None
    return TLDExtract(cache_dir=cache, suffix_list_urls=(), fallback_to_snapshot=True)


_DEFAULT_EXTRACTOR: TLDExtract | None = None


def _extractor(extractor: TLDExtract | None) -> TLDExtract:
    global _DEFAULT_EXTRACTOR
    if extractor is not None:
        return extractor
    if _DEFAULT_EXTRACTOR is None:
        _DEFAULT_EXTRACTOR = make_domain_extractor(None)
    return _DEFAULT_EXTRACTOR


def _host_of(host_or_url: str) -> str:
    text = (host_or_url or "").strip()
    if not text:
        return ""
    try:
        if "://" in text or text.startswith("//"):
            host = urlsplit(text if "://" in text else "http:" + text).hostname or ""
        else:
            host = urlsplit("//" + text).hostname or ""
    except ValueError:
        return ""
    return host.strip(".").lower()


def normalize_domain(host_or_url: str) -> str:
    """Lowercase host without port, path, trailing dot or leading ``www.``."""
    host = _host_of(host_or_url)
    return host[4:] if host.startswith("www.") and len(host) > 4 else host


def registrable_domain(host_or_url: str, extractor: TLDExtract | None = None) -> str | None:
    """Registered domain (public-suffix aware, offline). IP literals and single-label hosts map to
    themselves; unknown suffixes fall back to the last two labels. Empty input -> None."""
    host = _host_of(host_or_url)
    if not host:
        return None
    try:
        ipaddress.ip_address(host)
        return host
    except ValueError:
        pass
    if "." not in host:
        return host
    ext = _extractor(extractor)(host)
    reg = getattr(ext, "top_domain_under_public_suffix", "") or ""
    if reg:
        return reg.lower()
    if not ext.suffix:  # unknown TLD (e.g. .test): last two labels
        return ".".join(host.split(".")[-2:])
    return host  # the host IS a public suffix
