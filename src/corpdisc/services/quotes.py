"""Deterministic quote verification against stored page text (decision D5).

There is NO fuzzy matching. The only normalization applied to BOTH the quote and the page
text is: Unicode NFKC, typographic quotes/dashes mapped to ASCII, every whitespace run
(including NBSP) collapsed to one space, zero-width characters dropped, ends stripped. The
comparison is a case-sensitive substring test. A hit returns ``char_start``/``char_end`` as
offsets into the ORIGINAL (cleaned, un-normalized) page text, via an index map.

Minimum quote length is a contract rule (``EvidenceClaim.quote``), not the verifier's.
"""

from __future__ import annotations

import hashlib
import unicodedata
from dataclasses import dataclass
from typing import Final, Literal

from corpdisc.contracts.common import EvidenceClaim, SourceClass, Supports, VerifiedEvidence

_ZERO_WIDTH: Final = frozenset("\u200b\u200c\u200d\u2060\ufeff\u00ad")
_QUOTE_MAP: Final = {
    "\u2018": "'", "\u2019": "'", "\u201a": "'", "\u201b": "'", "\u2032": "'", "\u00b4": "'",
    "\u201c": '"', "\u201d": '"', "\u201e": '"', "\u201f": '"', "\u2033": '"',
    "\u2010": "-", "\u2011": "-", "\u2012": "-", "\u2013": "-", "\u2014": "-", "\u2015": "-",
    "\u2212": "-", "\ufe58": "-", "\ufe63": "-", "\uff0d": "-",
}  # fmt: skip
_CACHE_SIZE: Final = 4

Reason = Literal["OK", "EMPTY_QUOTE", "EMPTY_TEXT", "NOT_FOUND"]


@dataclass(frozen=True, slots=True)
class QuoteCheck:
    verified: bool
    char_start: int | None  # offsets into the ORIGINAL page text, end exclusive
    char_end: int | None
    reason: Reason


def _chunks(text: str):
    """Yield (start, end, chunk): a base character with its combining marks (so NFKC composes)."""
    n = len(text)
    i = 0
    while i < n:
        j = i + 1
        while j < n and unicodedata.category(text[j]) in ("Mn", "Mc", "Me"):
            j += 1
        yield i, j, text[i:j]
        i = j


def normalize_with_offsets(text: str) -> tuple[str, list[int], list[int]]:
    """Normalize ``text``; return (normalized, start_offsets, end_offsets) per normalized char."""
    out: list[str] = []
    starts: list[int] = []
    ends: list[int] = []
    pending_space: tuple[int, int] | None = None  # (start, end) of a whitespace run
    for s, e, chunk in _chunks(text):
        if len(chunk) == 1 and chunk in _ZERO_WIDTH:
            continue
        nfkc = unicodedata.normalize("NFKC", chunk)
        for ch in nfkc:
            if ch in _ZERO_WIDTH:
                continue
            ch = _QUOTE_MAP.get(ch, ch)
            if ch.isspace():
                if out:  # leading whitespace is stripped
                    pending_space = (s, e) if pending_space is None else (pending_space[0], e)
                continue
            if pending_space is not None:
                out.append(" ")
                starts.append(pending_space[0])
                ends.append(pending_space[1])
                pending_space = None
            out.append(ch)
            starts.append(s)
            ends.append(e)
    # a trailing whitespace run is dropped (pending_space is simply discarded)
    return "".join(out), starts, ends


def normalize_for_match(text: str) -> str:
    return normalize_with_offsets(text)[0]


class QuoteVerifier:
    """Stateless apart from a tiny cache of normalized page texts (keyed by content hash)."""

    def __init__(self) -> None:
        self._cache: dict[tuple[int, str], tuple[str, list[int], list[int]]] = {}

    def _normalized_page(self, page_text: str) -> tuple[str, list[int], list[int]]:
        key = (len(page_text), hashlib.sha1(page_text.encode("utf-8", "surrogatepass")).hexdigest())  # noqa: S324
        hit = self._cache.get(key)
        if hit is None:
            hit = normalize_with_offsets(page_text)
            if len(self._cache) >= _CACHE_SIZE:
                self._cache.pop(next(iter(self._cache)))
            self._cache[key] = hit
        return hit

    def check(self, quote: str, page_text: str) -> QuoteCheck:
        if not isinstance(quote, str) or not isinstance(page_text, str):
            return QuoteCheck(False, None, None, "EMPTY_QUOTE" if not isinstance(quote, str) else "EMPTY_TEXT")
        nq = normalize_for_match(quote)
        if not nq:
            return QuoteCheck(False, None, None, "EMPTY_QUOTE")
        norm, starts, ends = self._normalized_page(page_text)
        if not norm:
            return QuoteCheck(False, None, None, "EMPTY_TEXT")
        idx = norm.find(nq)
        if idx < 0:
            return QuoteCheck(False, None, None, "NOT_FOUND")
        return QuoteCheck(True, starts[idx], ends[idx + len(nq) - 1], "OK")

    def verify_claim(
        self,
        claim: EvidenceClaim,
        *,
        page_text: str | None,
        url: str,
        source_class: SourceClass,
        invocation_id: int | None = None,
    ) -> VerifiedEvidence:
        """Never raises. A quote that cannot be found yields ``quote_verified=False`` (no offsets)."""
        return self.verify_quote(
            page_id=claim.page_id, quote=claim.quote, supports=claim.supports, page_text=page_text,
            url=url, source_class=source_class, invocation_id=invocation_id,
        )

    def verify_quote(
        self,
        *,
        page_id: int,
        quote: str,
        supports: Supports,
        page_text: str | None,
        url: str,
        source_class: SourceClass,
        invocation_id: int | None = None,
    ) -> VerifiedEvidence:
        result = self.check(quote, page_text or "")
        return VerifiedEvidence(
            page_id=page_id,
            url=url,
            quote=quote.strip() or "(empty)",
            quote_verified=result.verified,
            char_start=result.char_start,
            char_end=result.char_end,
            source_class=source_class,
            supports=supports,
            invocation_id=invocation_id,
        )
