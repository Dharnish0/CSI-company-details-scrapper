"""Queue helpers used by the slow-crawl loop: release a running task, bulk-defer pending tasks."""
from __future__ import annotations

from datetime import timedelta

from corpdisc.clock import iso_utc
from corpdisc.services.queue import QueueService
from tests.helpers.clock import FakeClock
from tests.helpers.db import count, make_db, seed_entity, seed_run


def _setup(tmp_path, n=4):
    conn = make_db(tmp_path)
    run = seed_run(conn)
    clock = FakeClock()
    q = QueueService(conn, clock)
    ids = []
    for i in range(n):
        e = seed_entity(conn, run, f"Co {i}")
        ids.append(q.enqueue(run_id=run, entity_id=e, start_url=f"https://co{i}.test/", depth=1))
    return conn, run, clock, q, ids


def _row(conn, task_id):
    return conn.execute("SELECT status, attempts, not_before, error FROM research_queue WHERE task_id=?",
                        (task_id,)).fetchone()


def test_release_running_restores_attempt_and_keeps_schedule(tmp_path):
    conn, run, clock, q, ids = _setup(tmp_path, 1)
    future = clock.now() + timedelta(hours=1)
    q.reschedule(q.claim_next(run).task_id, future, "RETRY:429")  # attempts 1, PENDING, not_before set
    clock.advance(7200)
    claimed = q.claim_next(run)
    assert claimed.attempts == 2
    assert q.release_running(claimed.task_id) is True
    row = _row(conn, claimed.task_id)
    assert row["status"] == "PENDING" and row["attempts"] == 1
    assert row["not_before"] == iso_utc(future) and row["error"] == "RETRY:429"
    assert count(conn, "research_queue") == 1  # no duplicate rows


def test_release_running_floor_is_zero_and_only_running(tmp_path):
    conn, run, clock, q, ids = _setup(tmp_path, 3)
    t = q.claim_next(run)
    assert q.release_running(t.task_id) is True
    assert _row(conn, t.task_id)["attempts"] == 0
    assert q.release_running(t.task_id) is False  # now PENDING: untouched
    assert _row(conn, t.task_id)["attempts"] == 0
    done = q.claim_next(run)
    q.complete(done.task_id, note="PARTIAL:RETRYABLE:X")
    failed = q.claim_next(run)
    q.fail(failed.task_id, "boom", retry=False)
    before = [tuple(_row(conn, i)) for i in ids]
    for i in ids:
        if _row(conn, i)["status"] in ("DONE", "FAILED"):
            assert q.release_running(i) is False
    assert [tuple(_row(conn, i)) for i in ids] == before
    assert q.release_running(99999) is False


def test_defer_all_pending_only_moves_forward_and_only_pending(tmp_path):
    conn, run, clock, q, ids = _setup(tmp_path, 5)
    now = clock.now()
    soon, late = now + timedelta(minutes=5), now + timedelta(hours=10)
    conn.execute("UPDATE research_queue SET not_before=? WHERE task_id=?", (iso_utc(soon), ids[0]))
    conn.execute("UPDATE research_queue SET not_before=? WHERE task_id=?", (iso_utc(late), ids[1]))
    conn.commit()
    running = q.claim_next(run)  # ids[0] has a future not_before -> claims the lowest ready one
    q.complete(running.task_id)
    done_before = tuple(_row(conn, running.task_id))
    wake = now + timedelta(hours=1)
    changed = q.defer_all_pending(run, wake)
    pending = [i for i in ids if _row(conn, i)["status"] == "PENDING"]
    assert changed == len(pending) - 1  # the row already at +10 h is left alone
    for i in pending:
        nb = _row(conn, i)["not_before"]
        assert nb == (iso_utc(late) if i == ids[1] else iso_utc(wake))
    assert tuple(_row(conn, running.task_id)) == done_before
    assert q.defer_all_pending(run, wake) == 0  # idempotent
    assert count(conn, "research_queue") == 5


def test_defer_all_pending_leaves_failed_and_skipped_and_other_runs(tmp_path):
    conn, run, clock, q, ids = _setup(tmp_path, 3)
    other = seed_run(conn, root_name="Other", root_code="OT")
    oe = seed_entity(conn, other, "Other Co")
    otask = q.enqueue(run_id=other, entity_id=oe, start_url=None, depth=1)
    t = q.claim_next(run)
    q.fail(t.task_id, "x", retry=False)
    t2 = q.claim_next(run)
    q.skip(t2.task_id, "why")
    snap = {i: tuple(_row(conn, i)) for i in (t.task_id, t2.task_id, otask)}
    assert q.defer_all_pending(run, clock.now() + timedelta(hours=2)) == 1
    assert {i: tuple(_row(conn, i)) for i in snap} == snap
