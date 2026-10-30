"""Queue recovery and run-status derivation: transient provider failures reschedule work instead of ending discovery."""
from __future__ import annotations

import pytest

from corpdisc.agents.ports import ProviderTransportError
from tests.fixtures.world import seeded_world

WAITING = {"queue_retry_wait_max_seconds": 0}  # pause (resumable) instead of waiting in-process


def failing(world, schema="RelationshipOutput", *, retryable=True, rate_limited=False, times=None):
    """Mutator that raises a transport error for ``schema`` (``times`` calls, or until the mutator is cleared)."""
    state = {"n": 0}

    def m(name, out):
        if name == schema and (times is None or state["n"] < times):
            state["n"] += 1
            raise ProviderTransportError("503 upstream", retryable=retryable, rate_limited=rate_limited,
                                         retry_after_s=None)
        return out

    world.brain.mutators.append(m)
    return state


def snapshot(w):
    return {
        "entities": sorted(tuple(r) for r in w.q("SELECT name, verification_status, entity_type FROM entities")),
        "cands": sorted(tuple(r) for r in w.q("SELECT raw_name, decision FROM candidates")),
        "tasks": sorted(tuple(r) for r in w.q("SELECT e.name, q.status FROM research_queue q JOIN entities e USING(entity_id)")),
    }


def test_transient_relationship_failure_keeps_candidates_and_reschedules(tmp_path):
    w = seeded_world(tmp_path, settings_kw=WAITING)
    failing(w)
    s = w.sup.run()
    assert s.status == "PAUSED" and s.stopped_reason == "RETRY_WAITING" and s.retries == 1
    assert w.names() == {"Northwind Holdings"}  # no half-verified children were created
    cands = w.q("SELECT raw_name, decision, reason FROM candidates")
    assert {c[0] for c in cands} >= {"Group A", "Group B"}
    assert all(c[1] == "REVIEW" and c[2] == "VERIFICATION_RETRY_PENDING" for c in cands)
    task = w.q("SELECT status, attempts, not_before, error FROM research_queue WHERE task_id=1")[0]
    assert task["status"] == "PENDING" and task["attempts"] == 1 and task["not_before"]
    assert "VERIFICATION_RETRY_PENDING" in task["error"]
    assert w.q("SELECT status FROM runs")[0][0] == "PAUSED"


def test_retry_then_success_matches_an_uninterrupted_run(tmp_path):
    base = seeded_world(tmp_path / "base")
    base.sup.run()
    w = seeded_world(tmp_path / "flaky", settings_kw=WAITING)
    failing(w, times=4)  # exactly one task attempt's worth of gateway calls (1 + 3 transport retries)
    first = w.sup.run()
    assert first.status == "PAUSED"
    w.clock.advance(3600)
    final = w.sup.resume()
    assert final.status == "COMPLETED"
    got, want = snapshot(w), snapshot(base)
    assert got["entities"] == want["entities"]  # no loss, no duplication
    assert got["tasks"] == want["tasks"]
    assert {c[0] for c in got["cands"]} == {c[0] for c in want["cands"]}
    assert len(got["cands"]) == len(want["cands"])
    assert "Group A" in w.names("CONFIRMED")


def test_in_process_wait_when_backoff_is_short(tmp_path):
    w = seeded_world(tmp_path)  # default wait ceiling 300 s, first backoff 60 s
    failing(w, times=4)
    s = w.sup.run()
    assert s.status == "COMPLETED" and s.retries == 1 and s.waited_seconds >= 60
    assert "Group A" in w.names("CONFIRMED")


def test_exhausted_retries_degrade_to_review_required_and_run_is_partial(tmp_path):
    w = seeded_world(tmp_path)
    failing(w)  # never recovers
    s = w.sup.run()
    assert s.status == "PARTIAL" and s.stopped_reason == "QUEUE_EMPTY"
    assert s.retries == 2  # attempts 1 and 2 reschedule, attempt 3 (the cap) completes with gaps
    assert not (w.names("CONFIRMED") - {"Northwind Holdings"})
    reasons = {r[0] for r in w.q("SELECT reason_code FROM review_items")}
    assert "VERIFICATION_UNAVAILABLE" in reasons
    row = w.q("SELECT status, attempts, error FROM research_queue WHERE task_id=1")[0]
    assert row["status"] == "DONE" and row["attempts"] == 3 and row["error"].startswith("PARTIAL:")
    # unverified children are never enqueued for crawling (RECURSE_INTO_REVIEW stays off)
    assert w.q("SELECT COUNT(*) FROM research_queue")[0][0] == 1
    assert w.q("SELECT status FROM runs")[0][0] == "PARTIAL"


def test_non_retryable_failure_is_not_rescheduled(tmp_path):
    w = seeded_world(tmp_path)
    failing(w, retryable=False)
    s = w.sup.run()
    assert s.retries == 0 and s.status == "PARTIAL"
    assert w.q("SELECT attempts FROM research_queue WHERE task_id=1")[0][0] == 1


def test_rate_limited_failure_reschedules_without_opening_the_breaker(tmp_path):
    w = seeded_world(tmp_path, settings_kw=WAITING)
    failing(w, rate_limited=True)
    s = w.sup.run()
    assert s.stopped_reason == "RETRY_WAITING"
    assert w.deps.runner.gateway.breaker.state.value == "CLOSED"


def test_extraction_transient_failure_reschedules(tmp_path):
    w = seeded_world(tmp_path, settings_kw=WAITING)
    failing(w, schema="ExtractionOutput")
    s = w.sup.run()
    assert s.stopped_reason == "RETRY_WAITING" and w.names() == {"Northwind Holdings"}
    w.brain.mutators.clear()
    w.clock.advance(3600)
    assert w.sup.resume().status == "COMPLETED"
    assert "Group A" in w.names("CONFIRMED")


def test_verified_children_are_enqueued_and_review_children_are_not(tmp_path):
    w = seeded_world(tmp_path)
    s = w.sup.run()
    assert s.status == "COMPLETED"
    confirmed = w.names("CONFIRMED") - {"Northwind Holdings"}
    assert {"Group A", "Group B"} <= confirmed
    queued = {r[0] for r in w.q("SELECT e.name FROM research_queue q JOIN entities e USING(entity_id)")}
    assert {"Group A", "Group B"} <= queued
    review = {r[0] for r in w.q("SELECT name FROM entities WHERE verification_status='REVIEW_REQUIRED'")}
    assert review.isdisjoint(queued)  # only verified children are researched further


def test_every_task_failing_is_FAILED_not_completed(tmp_path):
    w = seeded_world(tmp_path)

    def boom(url, **kw):
        raise RuntimeError("boom")

    w.deps.fetch.fetch = boom
    s = w.sup.run()
    assert s.status == "FAILED" and s.failed_tasks == 1
    assert w.q("SELECT status FROM runs")[0][0] == "FAILED"


def test_max_tasks_leaves_the_run_running_and_resumable(tmp_path):
    w = seeded_world(tmp_path)
    s = w.sup.run(max_tasks=1)
    assert s.status == "RUNNING" and s.stopped_reason == "MAX_TASKS"
    assert w.sup.resume().status == "COMPLETED"


@pytest.mark.parametrize("status", ["COMPLETED", "PARTIAL", "FAILED", "BUDGET_EXHAUSTED", "PAUSED"])
def test_runs_status_check_accepts_all_states(tmp_path, status):
    w = seeded_world(tmp_path)
    w.conn.execute("UPDATE runs SET status=?", (status,))
    w.conn.commit()


# ------------------------------------------------------------------ Phase 4, defect C: rate limits and resumable partial extraction
def dense_world(tmp_path, monkeypatch, n, *, settings_kw=None, **provider_kw):
    from tests.fixtures import dense_site
    http = dense_site.install(monkeypatch, n, 2)
    w = seeded_world(tmp_path, http=http, settings_kw={"max_links_per_entity": 4, **(settings_kw or {})})
    return w, dense_site.install_provider(w, **provider_kw)


def calls_down(first_n):
    """An extraction provider that answers 429 for its first ``first_n`` requests."""
    box = {"n": 0}

    def down(payload):
        box["n"] += 1
        return box["n"] <= first_n
    return down


def test_rate_limit_deferrals_do_not_consume_attempts(tmp_path, monkeypatch):
    from tests.fixtures.dense_site import names
    per_round = 5 + 1  # llm_rate_limit_retries + the first call
    w, sp = dense_world(tmp_path, monkeypatch, 3, down=calls_down(3 * per_round))  # three consecutive 429 rounds
    s = w.sup.run()
    assert s.status == "COMPLETED" and s.retries == 3
    row = w.q("SELECT status, attempts, error FROM research_queue WHERE task_id=1")[0]
    assert row["status"] == "DONE" and row["attempts"] == 1 and row["error"] is None
    assert w.names("CONFIRMED") >= set(names(3))


def test_rate_limit_deferral_cap_falls_back_to_attempts(tmp_path, monkeypatch):
    w, sp = dense_world(tmp_path, monkeypatch, 3, settings_kw={"queue_max_rate_limit_deferrals": 2}, down=lambda p: True)
    s = w.sup.run()
    assert s.status == "PARTIAL" and s.retries == 4  # 2 free deferrals, then attempts 1 and 2 are consumed
    row = w.q("SELECT status, attempts, error FROM research_queue WHERE task_id=1")[0]
    assert row["status"] == "DONE" and row["attempts"] == 3 and row["error"].startswith("PARTIAL:RETRYABLE:")


def three_chunk_world(tmp_path, monkeypatch):
    box = {"down": True}
    w, sp = dense_world(tmp_path, monkeypatch, 36, settings_kw={"queue_max_rate_limit_deferrals": 0},
                        down=lambda p: box["down"] and p["chunk_index"] == 2)
    return w, sp, box


def chunk_names(sp, index):
    from tests.fixtures.dense_site import names
    seen = {}
    for p in sp.extractions:
        seen.setdefault(p["chunk_index"], p["page"]["text"])
    return {nm for nm in names(36) if nm in seen[index]}


def test_partial_extraction_keeps_accepted_candidates(tmp_path, monkeypatch):
    w, sp, box = three_chunk_world(tmp_path, monkeypatch)
    s = w.sup.run()
    first, last = chunk_names(sp, 0) | chunk_names(sp, 1), chunk_names(sp, 2)
    assert first and (last - first)  # a company on a chunk boundary shows up in both chunks
    stored = {n for n in w.names("CONFIRMED") if n.endswith(" Labs")}
    assert stored == first  # chunks 0-1 were kept, chunk 2 was not invented
    assert not ((last - first) & stored)
    assert w.q("SELECT COUNT(*) FROM review_items WHERE reason_code='EXTRACTION_FAILED'")[0][0] >= 1
    row = w.q("SELECT status, attempts, error FROM research_queue WHERE task_id=1")[0]
    assert row["status"] == "DONE" and row["attempts"] == 3 and row["error"].startswith("PARTIAL:RETRYABLE:")
    assert s.status == "PARTIAL"


def test_resume_after_cooldown_completes_remaining_chunks(tmp_path, monkeypatch):
    from tests.fixtures.dense_site import names
    w, sp, box = three_chunk_world(tmp_path, monkeypatch)
    w.sup.run()
    before = {i: sum(1 for p in sp.extractions if p["chunk_index"] == i) for i in (0, 1)}
    assert before == {0: 1, 1: 1}  # the retries re-asked only chunk 2; chunks 0-1 came from the response cache
    box["down"] = False
    w.clock.advance(3600)
    s = w.sup.resume()
    assert s.status == "COMPLETED"
    after = {i: sum(1 for p in sp.extractions if p["chunk_index"] == i) for i in (0, 1)}
    assert after == before  # completed chunks are not paid for again
    assert {n for n in w.names("CONFIRMED") if n.endswith(" Labs")} == set(names(36))
    assert w.q("SELECT COUNT(*) FROM entities WHERE name LIKE '% Labs'")[0][0] == 36  # no duplicates
    assert w.q("SELECT COUNT(*) FROM relationships WHERE parent_id=1")[0][0] == 36
    row = w.q("SELECT status, error FROM research_queue WHERE task_id=1")[0]
    assert row["status"] == "DONE" and row["error"] is None


def test_requeue_retryable_partials_ignores_other_done_tasks(tmp_path):
    w = seeded_world(tmp_path)
    w.sup.run()
    tasks = [r[0] for r in w.q("SELECT task_id FROM research_queue WHERE status='DONE' ORDER BY task_id")][:3]
    assert len(tasks) == 3
    w.conn.execute("UPDATE research_queue SET error='PARTIAL:RETRYABLE:gap' WHERE task_id=?", (tasks[0],))
    w.conn.execute("UPDATE research_queue SET error='PARTIAL:VERIFICATION_UNAVAILABLE' WHERE task_id=?", (tasks[1],))
    w.conn.commit()
    assert w.deps.queue.counts_detail(w.run_id)["retryable_partial"] == 1
    assert w.deps.queue.requeue_retryable_partials(w.run_id) == 1
    rows = {r["task_id"]: r for r in w.q("SELECT task_id, status, attempts, error FROM research_queue")}
    assert rows[tasks[0]]["status"] == "PENDING" and rows[tasks[0]]["attempts"] == 0
    assert rows[tasks[0]]["error"].startswith("REQUEUED:")
    assert rows[tasks[1]]["status"] == "DONE" and rows[tasks[1]]["error"] == "PARTIAL:VERIFICATION_UNAVAILABLE"
    assert rows[tasks[2]]["status"] == "DONE"
