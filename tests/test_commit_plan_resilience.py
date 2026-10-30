"""The n_commit alias crash ("unknown entity local_ref"): an alias of a candidate that was not stored is dropped with a
recorded reason, and an invalid commit plan never loses the extracted candidates."""
from __future__ import annotations

from corpdisc.graphs import research
from tests.fixtures import alias_site
from tests.fixtures import site as base
from tests.fixtures.world import seeded_world


def run_world(tmp_path, monkeypatch):
    http = alias_site.install(monkeypatch)
    w = seeded_world(tmp_path, http=http)
    return w, w.sup.run()


def test_alias_of_a_candidate_that_was_not_stored_is_dropped_with_a_reason_not_a_crash(tmp_path, monkeypatch):
    w, _ = run_world(tmp_path, monkeypatch)
    assert not w.q("SELECT 1 FROM crawl_errors WHERE stage IN ('REGISTRY','TASK')")
    assert w.q("SELECT status, attempts FROM research_queue WHERE task_id=2")[0][:] == ("DONE", 1)
    assert w.entity("Company A1") is not None                      # the rest of the batch was committed
    assert w.entity("Zephyr Systems") is None and w.entity("Zephyr Systems Group") is None
    assert not w.q("SELECT 1 FROM entity_aliases WHERE normalized_alias LIKE 'zephyr%'")
    rej = w.q("SELECT detail FROM gate_rejections WHERE detail LIKE 'ALIAS_TARGET_NOT_STORED%'")
    assert len(rej) == 1 and "Zephyr Systems Group" in rej[0][0]
    names = {r[0] for r in w.q("SELECT raw_name FROM candidates WHERE parent_entity_id=?", w.entity("Group A")["entity_id"])}
    assert {"Zephyr Systems", "Zephyr Systems Group", "Company A1"} <= names   # both spellings stay on record


def test_invalid_commit_plan_keeps_candidates_and_the_task_is_retried_with_the_error_recorded(tmp_path, monkeypatch):
    http = alias_site.install(monkeypatch)
    w = seeded_world(tmp_path, http=http)
    real_alias = research.AliasWrite

    def ghost_alias(**kw):  # an alias pointing at a local_ref that is not in the plan => CommitPlan validation error
        return real_alias(**{**kw, "entity": research.EntityRef(local_ref="ghost")})
    monkeypatch.setattr(research, "AliasWrite", ghost_alias)
    # Zephyr is supported here, so it IS stored and its same-site spelling becomes an alias (which the patch breaks)
    monkeypatch.setitem(base.REL, ("Group A", "Zephyr Systems"), ("SUPPORTED", "Subsidiary of", "Subsidiary", "zephyr", 0.9))
    summary = w.sup.run()
    row = w.q("SELECT status, attempts, error FROM research_queue WHERE task_id=2")[0]
    assert row["attempts"] > 1, "a validation error is retried, not failed once like a deterministic rejection"
    assert "unknown entity local_ref" in (row["error"] or "")
    assert w.q("SELECT 1 FROM crawl_errors WHERE error_type='COMMIT_PLAN_INVALID'")
    assert w.q("SELECT 1 FROM gate_rejections WHERE detail LIKE 'COMMIT_PLAN_INVALID%'")
    cands = w.q("SELECT raw_name, decision, resolved_entity_id FROM candidates WHERE parent_entity_id=?",
                w.entity("Group A")["entity_id"])
    assert {"Company A1", "Zephyr Systems"} <= {c[0] for c in cands}      # extracted candidates survive the failure
    assert all(c["resolved_entity_id"] is None and c["decision"] != "ACCEPTED" for c in cands)
    assert w.entity("Company A1") is None                                  # nothing half-committed
    assert summary.status in ("PARTIAL", "FAILED")
