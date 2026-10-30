"""An exact normalized-name match to an existing entity never creates an entity or an edge, whatever the dedup agent says."""
from __future__ import annotations

from tests.fixtures import exact_name_site
from tests.fixtures.world import seeded_world


def run_world(tmp_path, monkeypatch):
    http = exact_name_site.install(monkeypatch)
    w = seeded_world(tmp_path, http=http, extra_agents=("dedup",))
    w.brain.dedup_verdict = lambda pair: ("DIFFERENT", 0.95, [], [])  # the model would let every pair through
    root_edge = {r["child_id"]: tuple(r) for r in w.q("SELECT * FROM relationships")}  # empty before the run
    summary = w.sup.run()
    return w, summary, root_edge


def count(w, name):
    return w.q("SELECT COUNT(*) FROM entities WHERE name=?", name)[0][0]


def test_sibling_named_on_a_peer_page_is_reviewed_not_stored_even_when_dedup_says_different(tmp_path, monkeypatch):
    w, _, _ = run_world(tmp_path, monkeypatch)
    assert count(w, "Group B") == 1                                   # no second entity
    ga, gb = w.entity("Group A")["entity_id"], w.entity("Group B")["entity_id"]
    assert w.q("SELECT 1 FROM relationships WHERE parent_id=? AND child_id=?", ga, gb) == []   # no second parent edge
    parents = w.q("SELECT p.name FROM relationships r JOIN entities p ON p.entity_id=r.parent_id WHERE r.child_id=?", gb)
    assert [p[0] for p in parents] == ["Northwind Holdings"]            # the existing edge is unchanged
    rv = w.q("SELECT reason_code, reason_text, possible_parent_id FROM review_items WHERE entity_id=? AND possible_parent_id=?", gb, ga)
    assert len(rv) == 1 and rv[0]["possible_parent_id"] == ga and rv[0]["reason_code"] == "MULTI_PARENT"
    assert "'Group B'" in rv[0]["reason_text"] and "'Group A'" in rv[0]["reason_text"]   # names both entities
    cand = w.q("SELECT decision, reason FROM candidates WHERE raw_name='Group B' AND parent_entity_id=?", ga)
    assert [c[0] for c in cand] == ["REVIEW"] and "EXACT_NAME_MATCH" in cand[0][1]


def test_exact_name_of_a_non_sibling_is_an_ambiguous_name_review_with_no_second_parent(tmp_path, monkeypatch):
    w, _, _ = run_world(tmp_path, monkeypatch)
    assert count(w, "Company A1") == 1
    a1, gb = w.entity("Company A1")["entity_id"], w.entity("Group B")["entity_id"]
    assert w.q("SELECT 1 FROM relationships WHERE parent_id=? AND child_id=?", gb, a1) == []
    assert w.edge("Group A", "Company A1")["is_primary"] == 1
    rv = w.q("SELECT reason_code, reason_text FROM review_items WHERE entity_id=? AND possible_parent_id=?", a1, gb)
    assert len(rv) == 1 and rv[0]["reason_code"] == "AMBIGUOUS_NAME"
    assert "entity %d" % a1 in rv[0]["reason_text"] and "entity %d" % gb in rv[0]["reason_text"]
    assert w.q("SELECT 1 FROM research_queue WHERE status='FAILED'") == []


def test_dedup_agent_is_still_used_for_fuzzy_near_matches(tmp_path, monkeypatch):
    w, _, _ = run_world(tmp_path, monkeypatch)
    assert "DuplicateOutput" in w.brain.calls                         # "Company A22" vs "Company A2" is not an exact match
    assert count(w, "Company A22") == 1                              # judged DIFFERENT: stored as its own entity
    assert w.edge("Group B", "Company A22") is not None
    assert w.q("SELECT 1 FROM review_items WHERE reason_text LIKE '%Company A22%EXACT_NAME_MATCH%'") == []


def test_exact_name_claim_is_idempotent_on_rerun(tmp_path, monkeypatch):
    w, _, _ = run_world(tmp_path, monkeypatch)
    tables = ("entities", "relationships", "evidence", "candidates")
    before = {t: w.q(f"SELECT COUNT(*) FROM {t}")[0][0] for t in tables}  # noqa: S608
    w.conn.execute("UPDATE research_queue SET status='PENDING', attempts=0, error=NULL, finished_at=NULL, not_before=NULL")
    w.conn.commit()
    w.sup.run()
    after = {t: w.q(f"SELECT COUNT(*) FROM {t}")[0][0] for t in tables}  # noqa: S608
    assert after == before
    assert len(w.q("SELECT 1 FROM review_items WHERE reason_text LIKE '%EXACT_NAME_MATCH%'")) == 2   # one per claim, not per run
