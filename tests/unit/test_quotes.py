from __future__ import annotations

import random

import pytest

from corpdisc.contracts.common import EvidenceClaim, SourceClass, Supports
from corpdisc.services.quotes import QuoteVerifier, normalize_with_offsets

PAGE = (
    "Our Companies\n\n"
    "Northwind Analytics is a wholly owned subsidiary of   Northwind Holdings.\n"
    "Aurora Labs \u2014 a \u201cportfolio\u201d company of Northwind Holdings\u2019 Group.\n"
    "Contact us at the head office."
)
OTHER_PAGE = "Completely unrelated page about careers and benefits for employees."
V = QuoteVerifier()


def test_exact_quote_verified_with_original_offsets():
    quote = "Northwind Analytics is a wholly owned subsidiary"
    r = V.check(quote, PAGE)
    assert r.verified and r.reason == "OK"
    assert PAGE[r.char_start : r.char_end] == quote


def test_whitespace_differences_are_collapsed_and_span_maps_to_original():
    quote = "wholly owned subsidiary of Northwind Holdings."  # page has 3 spaces before 'Northwind'
    r = V.check(quote, PAGE)
    assert r.verified
    assert PAGE[r.char_start : r.char_end] == "wholly owned subsidiary of   Northwind Holdings."
    # newline / NBSP in the quote
    assert V.check("Our\nCompanies\u00a0 Northwind Analytics", PAGE).verified


def test_altered_quote_is_rejected_no_fuzzy():
    assert not V.check("Northwind Analytics is a partially owned subsidiary", PAGE).verified
    assert not V.check("Northwind Analytics is a wholly owned subsidairy of", PAGE).verified
    assert V.check("Northwind Analytics is a partially owned subsidiary", PAGE).reason == "NOT_FOUND"


def test_case_sensitive():
    assert not V.check("northwind analytics is a wholly owned subsidiary", PAGE).verified


def test_quote_from_another_page_is_rejected():
    quote = "Northwind Analytics is a wholly owned subsidiary"
    assert V.check(quote, PAGE).verified
    assert not V.check(quote, OTHER_PAGE).verified


def test_partial_true_substring_is_valid():
    r = V.check("owned subsidiary of", PAGE)
    assert r.verified and PAGE[r.char_start : r.char_end] == "owned subsidiary of"
    assert V.check("Analytics", PAGE).verified  # length is a contract rule, not the verifier's


@pytest.mark.parametrize("quote", ["", "   ", "\n\t ", "\u200b\u200b", "\u00a0\u00a0"])
def test_empty_or_whitespace_quote_is_invalid(quote):
    r = V.check(quote, PAGE)
    assert not r.verified and r.reason == "EMPTY_QUOTE"
    assert r.char_start is None and r.char_end is None


def test_empty_page_text():
    r = V.check("Northwind Analytics", "")
    assert not r.verified and r.reason == "EMPTY_TEXT"
    assert not V.check("Northwind Analytics", "   \n ").verified


def test_reordered_quote_is_invalid():
    assert not V.check("Northwind Holdings subsidiary wholly owned", PAGE).verified
    assert not V.check("subsidiary of wholly owned", PAGE).verified


def test_smart_quotes_dashes_and_apostrophes_match_both_ways():
    assert V.check('Aurora Labs - a "portfolio" company of Northwind Holdings\' Group.', PAGE).verified
    assert V.check("Aurora Labs \u2013 a \u201cportfolio\u201d company", PAGE).verified
    ascii_page = 'He said "hello" - it\'s fine, truly fine.'
    assert V.check("He said \u201chello\u201d \u2014 it\u2019s fine", ascii_page).verified


def test_zero_width_soft_hyphen_and_ligature_normalization():
    page = "The o\u00adffice of \ufb01nance is at Northwind\u200b Holdings headquarters"
    assert V.check("office of finance is at Northwind Holdings", page).verified
    r = V.check("Holdings headquarters", page)
    assert page[r.char_start : r.char_end] == "Holdings headquarters"


def test_composed_vs_decomposed_accents():
    page = "Soci\u00e9t\u00e9 Fran\u00e7aise d'Analyse is a subsidiary of Northwind"
    decomposed = "Socie\u0301te\u0301 Franc\u0327aise d'Analyse is a subsidiary"
    r = V.check(decomposed, page)
    assert r.verified and r.char_start == 0


def test_offsets_handle_leading_trailing_whitespace_in_page():
    page = "   \n  Alpha   Beta  Gamma  \n"
    r = V.check("Alpha Beta Gamma", page)
    assert r.verified and page[r.char_start : r.char_end] == "Alpha   Beta  Gamma"


def test_normalize_with_offsets_shapes():
    norm, starts, ends = normalize_with_offsets("  a \n b\u00a0 ")
    assert norm == "a b"
    assert len(starts) == len(ends) == len(norm)
    assert all(s < e for s, e in zip(starts, ends, strict=True))


def test_random_slices_verify_and_map_back():
    rng = random.Random(1234)
    words = PAGE.replace("\n", " \n ").split(" ")
    text = "  ".join(words)
    for _ in range(60):
        a = rng.randrange(0, len(text) - 20)
        b = a + rng.randrange(15, 60)
        piece = text[a:b].strip()
        if len(piece) < 15:
            continue
        r = V.check(piece, text)
        assert r.verified, piece
        mapped = text[r.char_start : r.char_end]
        assert mapped.split() == piece.split()


def test_verify_claim_verified():
    claim = EvidenceClaim(page_id=7, quote="wholly owned subsidiary of Northwind Holdings",
                          supports=Supports.OWNERSHIP)
    ev = V.verify_claim(claim, page_text=PAGE, url="https://example.test/companies",
                        source_class=SourceClass.FIRST_PARTY_PARENT, invocation_id=3)
    assert ev.quote_verified and ev.page_id == 7 and ev.invocation_id == 3
    assert PAGE[ev.char_start : ev.char_end].split() == claim.quote.split()
    assert ev.source_class is SourceClass.FIRST_PARTY_PARENT and ev.supports is Supports.OWNERSHIP


def test_verify_claim_missing_quote_is_unverified_without_offsets():
    claim = EvidenceClaim(page_id=7, quote="Northwind owns everything on earth", supports=Supports.OWNERSHIP)
    ev = V.verify_claim(claim, page_text=PAGE, url="https://example.test/x",
                        source_class=SourceClass.THIRD_PARTY)
    assert not ev.quote_verified and ev.char_start is None and ev.char_end is None
    ev2 = V.verify_claim(claim, page_text=None, url="https://example.test/x",
                         source_class=SourceClass.THIRD_PARTY)
    assert not ev2.quote_verified


def test_normalized_page_cache_is_bounded_and_correct():
    v = QuoteVerifier()
    for i in range(10):
        page = f"page number {i} with some words in it for the cache test"
        assert v.check(f"number {i} with some", page).verified
    assert len(v._cache) <= 4
