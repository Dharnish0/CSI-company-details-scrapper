"""Regression: GateRejectionRepo.write returned a non-existent attribute (lastrow_id) on first real use."""
from corpdisc.db.repos.gate_rejections import GateRejectionRepo
from tests.helpers.clock import FakeClock
from tests.helpers.db import count, make_db, seed_run


def test_write_returns_the_new_rejection_id(tmp_path):
    conn = make_db(tmp_path)
    run = seed_run(conn)
    repo = GateRejectionRepo(conn, FakeClock())
    first = repo.write(run_id=run, gate_code="G2", item_ref="x", detail="NOT_IN_TEXT")
    second = repo.write(run_id=run, gate_code="G3", item_ref="y", detail="UNVERIFIED_QUOTE")
    assert (first, second) == (1, 2) and count(conn, "gate_rejections") == 2
