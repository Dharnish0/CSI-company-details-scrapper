"""Defect C regression: a chunk that outgrows the output cap is split and re-asked, never lost as a SCHEMA failure."""
from __future__ import annotations

import pytest

from tests.fixtures import dense_site
from tests.fixtures.dense_site import ROOT, install, install_provider, names, names_in
from tests.fixtures.world import seeded_world

N = 12
OUTPUT_FITS = 8  # the emulated output cap: a request offering more companies than this is cut off


def world(tmp_path, monkeypatch, n=N, filler=2, **kw):
    http = install(monkeypatch, n, filler)
    w = seeded_world(tmp_path, http=http, settings_kw={"max_links_per_entity": 4})
    sp = install_provider(w, **kw)
    return w, sp


def overfull(all_names):
    return lambda payload: names_in(payload, all_names) > OUTPUT_FITS


def test_truncated_chunk_is_split_and_all_entities_are_extracted(tmp_path, monkeypatch):
    w, sp = world(tmp_path, monkeypatch, truncate=overfull(names(N)))
    w.sup.run(max_tasks=1)  # the root task only
    assert w.names("CONFIRMED") >= set(names(N)) | {ROOT}
    assert len(sp.extractions) == 3  # the whole chunk (truncated) + its two halves
    sizes = sorted(names_in(p, names(N)) for p in sp.extractions)
    assert sizes[-1] == N and all(s <= OUTPUT_FITS for s in sizes[:-1])
    assert w.q("SELECT COUNT(*) FROM review_items WHERE reason_code='EXTRACTION_FAILED'")[0][0] == 0
    row = w.q("SELECT status, error FROM research_queue WHERE task_id=1")[0]
    assert row["status"] == "DONE" and not (row["error"] or "").startswith("PARTIAL")


def test_split_chunks_keep_quotes_verifiable_against_full_page_text(tmp_path, monkeypatch):
    w, sp = world(tmp_path, monkeypatch, truncate=overfull(names(N)))
    w.sup.run(max_tasks=1)
    ev = w.q("SELECT e.name, v.quote_verified, v.quote FROM evidence v JOIN entities e USING(entity_id) "
             "WHERE e.name LIKE '% Labs'")
    assert {r["name"] for r in ev} == set(names(N))  # first AND second half entities
    assert all(r["quote_verified"] == 1 and "is an operating group of" in r["quote"] for r in ev)
    offsets = sorted(p["page"]["char_offset"] for p in sp.extractions)
    assert offsets[0] == 0 and offsets[1] == 0 and offsets[2] > 0  # the second half starts mid-page


def test_split_stops_at_max_depth_and_records_partial(tmp_path, monkeypatch):
    w, sp = world(tmp_path, monkeypatch, filler=6, truncate=lambda payload: True)  # every answer is cut off
    s = w.sup.run(max_tasks=1)
    n = len(sp.extractions)
    assert 3 <= n <= w.deps.settings.max_llm_calls_per_task  # bounded: no infinite re-asking
    assert w.q("SELECT 1 FROM gate_rejections WHERE gate_code='G12' AND detail LIKE '%max split depth%'")
    assert w.names("CONFIRMED") == {ROOT}  # nothing invented from cut-off answers
    row = w.q("SELECT status, error FROM research_queue WHERE task_id=1")[0]
    assert row["status"] == "DONE" and row["error"].startswith("PARTIAL:")
    assert s.processed == 1


def test_earlier_truncation_makes_later_dense_chunks_split_before_their_call(tmp_path, monkeypatch):
    # 30 companies with long filler: the page needs several chunks; only the first full-size chunk may be sent whole
    w, sp = world(tmp_path, monkeypatch, n=30, filler=2, truncate=overfull(names(30)))
    w.sup.run(max_tasks=1)
    whole = [p for p in sp.extractions if names_in(p, names(30)) > OUTPUT_FITS]
    assert len(whole) == 1  # learned from the first truncation; later chunks were split without being sent whole
    assert w.names("CONFIRMED") >= set(names(30))


@pytest.mark.parametrize("n", [3])
def test_small_pages_are_not_split(tmp_path, monkeypatch, n):
    w, sp = world(tmp_path, monkeypatch, n=n, truncate=overfull(names(n)))
    w.sup.run(max_tasks=1)
    assert len(sp.extractions) == 1 and w.names("CONFIRMED") >= set(names(n))
    assert dense_site.ROOT in w.names()
