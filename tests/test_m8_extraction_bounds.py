"""Bounded, economical extraction on the real app wiring (Supervisor -> graph -> runner -> native Ollama provider)."""
from __future__ import annotations

import json

import pytest

from corpdisc.app import build_runtime
from corpdisc.config import Settings
from corpdisc.contracts.agent_io import ExtractionInput
from tests.fixtures.listing_site import ROOT, OllamaRecorder, site


def runtime(tmp_path, n_cards, mode="ok", **kw):
    st = Settings(_env_file=None, start_url="https://root.test/", root_company=ROOT, root_code="CSI", request_delay_seconds=0.0, max_retries=0,
                  db_path=tmp_path / "x.db", cache_dir=tmp_path / ".cache", log_dir=tmp_path / "l", use_playwright="false",
                  llm_provider="ollama", llm_model="qwen3:4b", llm_backoff_base_seconds=0.0, llm_backoff_max_seconds=0.0, **kw)
    rec = OllamaRecorder(mode)
    rt = build_runtime(st, http=site(n_cards), transport=rec)
    rt.supervisor.seed_root()
    return rt, rec


def extraction_calls(rec):
    return [b for b in rec.bodies if "format" in b]


# ------------------------------------------------------------------ prompt size: remove duplication, keep information
@pytest.mark.parametrize("cards, user_budget", [(8, 6_000), (30, 15_500), (60, 16_500)])
def test_prompt_size_budget_for_a_6000_char_chunk(tmp_path, cards, user_budget):
    rt, rec = runtime(tmp_path, cards)
    rt.supervisor.run(max_tasks=1)
    rt.close()
    first = extraction_calls(rec)[0]
    user = first["messages"][1]["content"]
    assert len(user) <= user_budget, f"user message {len(user)} chars exceeds the budget {user_budget}"
    assert len(first["messages"][0]["content"]) < 4_200          # system prompt + compact schema
    payload = ExtractionInput.model_validate_json(user)
    assert len(payload.page.text) <= 6_200                       # chunk size is respected (CHUNK_CHARS=6000, whole lines)
    dom_ctx = sum(len(c.context) for c in payload.dom_candidates)
    assert dom_ctx <= 0.15 * len(payload.page.text) + 800       # was ~98% of the text duplicated; now only non-redundant context
    side = len(json.dumps([c.model_dump() for c in payload.dom_candidates])) + len(json.dumps([lk.model_dump() for lk in payload.links]))
    assert side <= 4_000 + 4_500 + 2_500                         # hard side-data budget (JSON overhead allowance)


def test_dedup_is_lossless_for_what_the_gates_and_the_model_need(tmp_path):
    rt, rec = runtime(tmp_path, 30)
    rt.supervisor.run(max_tasks=1)
    first = ExtractionInput.model_validate_json(extraction_calls(rec)[0]["messages"][1]["content"])
    assert len(first.dom_candidates) == 30 and all(c.name_text for c in first.dom_candidates)        # every candidate name kept (G2)
    assert all(c.link_id is not None and c.dom_hint == "card" for c in first.dom_candidates[:5])      # structure kept
    text = first.page.text
    for c in first.dom_candidates:
        assert c.context == c.name_text or c.context not in text                                       # only redundant context was removed
    assert len(first.links) >= 30 and all(lk.url.startswith("https://") for lk in first.links)         # links untouched
    assert "is an operating group of" in text                                                          # ownership sentences stay in the chunk (G3)
    rt.close()


# ------------------------------------------------------------------ bounded execution
def test_the_request_asks_for_a_real_cap_a_context_and_no_thinking(tmp_path):
    rt, rec = runtime(tmp_path, 8)
    rt.supervisor.run(max_tasks=1)
    rt.close()
    b = extraction_calls(rec)[0]
    assert b["options"] == {"temperature": 0.0, "num_predict": 3000, "num_ctx": 12288} and b["think"] is False and b["stream"] is False
    assert not {"max_tokens", "max_completion_tokens", "response_format"} & set(b)


def test_prompt_plus_output_cap_fits_the_configured_context(tmp_path):
    rt, rec = runtime(tmp_path, 60)
    rt.supervisor.run(max_tasks=1)
    rt.close()
    for b in extraction_calls(rec):
        chars = sum(len(m["content"]) for m in b["messages"])
        assert chars // 3 + b["options"]["num_predict"] <= b["options"]["num_ctx"], "worst-case estimate (chars/3) must fit num_ctx"


def test_a_timeout_stops_the_chunk_loop_after_one_request_and_is_recorded(tmp_path):
    rt, rec = runtime(tmp_path, 60, mode="timeout")             # 3 chunks on this page
    s = rt.supervisor.run(max_tasks=1)
    assert len(extraction_calls(rec)) == 1                       # not 4 attempts x 3 chunks = 12 x timeout
    assert s.status == "RUNNING" and s.processed == 1
    rows = rt.conn.execute("SELECT status, failure_kind, attempt FROM agent_invocations WHERE agent='extraction'").fetchall()
    assert [tuple(r) for r in rows] == [("FAILED", "TRANSPORT", 1)]
    assert rt.conn.execute("SELECT 1 FROM review_items WHERE reason_code='EXTRACTION_FAILED'").fetchone()   # failure visible, not silent
    assert rt.conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0] == 1                               # nothing invented
    rt.close()


def test_schema_failures_do_not_stop_other_chunks_but_stay_bounded(tmp_path):
    rt, rec = runtime(tmp_path, 60, mode="garbage")
    rt.supervisor.run(max_tasks=1)
    n = len(extraction_calls(rec))
    assert n == 6                                                # 3 chunks x (1 initial + exactly 1 repair)
    rt.close()


def test_per_task_llm_call_budget_bounds_a_task(tmp_path):
    rt, rec = runtime(tmp_path, 60, mode="garbage", max_llm_calls_per_task=4)
    rt.supervisor.run(max_tasks=1)
    assert len(extraction_calls(rec)) <= 4
    assert rt.conn.execute("SELECT 1 FROM agent_invocations WHERE status='SKIPPED'").fetchone()
    rt.close()
