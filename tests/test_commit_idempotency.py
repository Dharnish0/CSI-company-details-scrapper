"""Defect A regression: duplicate (parent, child) edges, sibling ownership claims, deterministic commit rejection."""
from __future__ import annotations

from corpdisc.contracts.postgate import CommitRejected
from tests.fixtures import sibling_site
from tests.fixtures.world import seeded_world


def run_world(tmp_path, monkeypatch):
    http = sibling_site.install(monkeypatch)
    w = seeded_world(tmp_path, http=http)
    summary = w.sup.run()
    return w, summary


def edges(w, parent, child):
    return w.q("SELECT r.* FROM relationships r JOIN entities p ON p.entity_id=r.parent_id "
               "JOIN entities c ON c.entity_id=r.child_id WHERE p.name=? AND c.name=?", parent, child)


def test_two_name_variants_of_one_known_entity_produce_one_edge_and_children_are_stored(tmp_path, monkeypatch):
    w, summary = run_world(tmp_path, monkeypatch)
    assert w.q("SELECT COUNT(*) FROM research_queue WHERE status='FAILED'")[0][0] == 0
    assert not w.q("SELECT 1 FROM crawl_errors WHERE stage IN ('REGISTRY','TASK')")
    assert w.names("CONFIRMED") >= {"Group A", "Group B", "Company A1", "Company A2", "Company B1"}
    assert len(edges(w, "Group B", "Company A2")) == 1
    aliases = {r[0] for r in w.q("SELECT a.normalized_alias FROM entity_aliases a JOIN entities e USING(entity_id) "
                                 "WHERE e.name='Company A2'")}
    assert "company a2 group" in aliases
    assert w.q("SELECT COUNT(*) FROM entities WHERE name LIKE 'Company A2%'")[0][0] == 1
    assert summary.status in ("COMPLETED", "PARTIAL")


def test_sibling_group_named_on_a_peer_page_is_never_stored_as_its_child(tmp_path, monkeypatch):
    w, _ = run_world(tmp_path, monkeypatch)
    assert edges(w, "Group A", "Group B") == []
    gb = w.entity("Group B")
    parents = w.q("SELECT p.name FROM relationships r JOIN entities p ON p.entity_id=r.parent_id WHERE r.child_id=?",
                  gb["entity_id"])
    assert [p[0] for p in parents] == ["Northwind Holdings"]
    rv = w.q("SELECT * FROM review_items WHERE entity_id=? AND reason_code='MULTI_PARENT'", gb["entity_id"])
    assert len(rv) == 1 and rv[0]["possible_parent_id"] == w.entity("Group A")["entity_id"]
    assert "SIBLING_REFERENCE" in rv[0]["reason_text"]
    cand = w.q("SELECT decision, reason FROM candidates WHERE raw_name='Group B' AND parent_entity_id=?",
               w.entity("Group A")["entity_id"])
    assert [c[0] for c in cand] == ["REVIEW"] and "SIBLING_REFERENCE" in cand[0][1]
    assert gb["verification_status"] == "CONFIRMED"


def test_genuine_second_parent_reference_is_review_edge_once(tmp_path, monkeypatch):
    w, _ = run_world(tmp_path, monkeypatch)
    rows = edges(w, "Group B", "Company A2")
    assert len(rows) == 1
    assert rows[0]["verification_status"] == "REVIEW_REQUIRED" and rows[0]["is_primary"] == 0
    a2 = w.entity("Company A2")["entity_id"]
    assert w.q("SELECT COUNT(*) FROM review_items WHERE entity_id=? AND reason_code='MULTI_PARENT'", a2)[0][0] == 1
    assert len(edges(w, "Group A", "Company A2")) == 1  # the primary edge is untouched
    assert edges(w, "Group A", "Company A2")[0]["is_primary"] == 1


def test_rerun_of_a_completed_task_is_idempotent(tmp_path, monkeypatch):
    w, _ = run_world(tmp_path, monkeypatch)
    tables = ("entities", "relationships", "evidence", "candidates", "entity_aliases", "websites")
    before = {t: w.q(f"SELECT COUNT(*) FROM {t}")[0][0] for t in tables}  # noqa: S608
    w.conn.execute("UPDATE research_queue SET status='PENDING', attempts=0, error=NULL, finished_at=NULL, "
                   "not_before=NULL")
    w.conn.commit()
    w.sup.run()
    after = {t: w.q(f"SELECT COUNT(*) FROM {t}")[0][0] for t in tables}  # noqa: S608
    assert after == before
    assert w.q("SELECT COUNT(*) FROM research_queue WHERE status='FAILED'")[0][0] == 0


def test_deterministic_commit_rejection_is_failed_once_without_retries(tmp_path, monkeypatch):
    http = sibling_site.install(monkeypatch)
    w = seeded_world(tmp_path, http=http)
    real = w.deps.committer.commit

    def reject_for_group_a(plan):
        if plan.task_id == 2:  # the first child task (Group A)
            return CommitRejected(failed_invariant="INV_TEST", message="forced rejection")
        return real(plan)
    monkeypatch.setattr(w.deps.committer, "commit", reject_for_group_a)
    summary = w.sup.run()
    row = w.q("SELECT status, attempts, error FROM research_queue WHERE task_id=2")[0]
    assert row["status"] == "FAILED" and row["attempts"] == 1 and "INV_TEST" in row["error"]
    assert w.q("SELECT status FROM research_queue WHERE task_id=1")[0][0] == "DONE"
    assert w.q("SELECT status FROM research_queue WHERE task_id=3")[0][0] == "DONE"  # other tasks still ran
    assert summary.status == "PARTIAL"
