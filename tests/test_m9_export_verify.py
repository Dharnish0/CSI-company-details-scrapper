"""Tree IDs, Excel workbook (read from SQLite only) and the data-quality verifier, on the full fixture hierarchy."""
from __future__ import annotations

import hashlib
import json

import pytest
from openpyxl import load_workbook

from corpdisc.cli import main
from corpdisc.graphs.finalize import FinalizeGraph
from corpdisc.services.cycle_guard import CycleGuard
from corpdisc.services.data_quality import check_run, format_checks
from corpdisc.services.excel import EVID_COLS, GROUP_COLS, HIERARCHY_COLS, REVIEW_COLS, export_workbook
from corpdisc.services.tree import assign_tree_ids, compute_tree
from tests.fixtures.site import ROOT
from tests.fixtures.world import seeded_world

SHEETS = ["Summary", "Company Hierarchy", "Operating Groups", "Crawl Log", "Review Required", "Validation Report", "Relationships", "Evidence"]


@pytest.fixture
def done(tmp_path):
    w = seeded_world(tmp_path)
    s = w.sup.run()
    assert s.status == "COMPLETED"
    return w


def table_hash(conn):
    h = hashlib.sha256()
    for t in ("entities", "relationships", "evidence", "websites", "candidates", "review_items", "crawl_pages", "validation_findings"):
        for r in conn.execute(f"SELECT * FROM {t} ORDER BY 1"):  # noqa: S608
            h.update(repr(tuple(r)).encode())
    return h.hexdigest()


def sheet_rows(wb, name):
    ws = wb[name]
    rows = list(ws.iter_rows(values_only=True))
    return rows[0], rows[1:]


# ------------------------------------------------------------------ tree ids
def test_tree_ids_levels_and_paths(done):
    ids = {r["name"]: (r["tree_id"], r["level"], r["hierarchy_path"]) for r in done.q("SELECT name, tree_id, level, hierarchy_path FROM entities WHERE tree_id IS NOT NULL")}
    assert ids[ROOT] == ("NWH", 0, ROOT)
    assert ids["Group A"] == ("NWH.01", 1, f"{ROOT} > Group A") and ids["Group B"][0] == "NWH.02"
    assert ids["Company A1"] == ("NWH.01.01", 2, f"{ROOT} > Group A > Company A1")
    assert ids["A1 Labs"] == ("NWH.01.01.01", 3, f"{ROOT} > Group A > Company A1 > A1 Labs") and ids["Company B1"][0] == "NWH.02.01"
    assert len({v[0] for v in ids.values()}) == len(ids)                                   # unique
    numbered = set(ids)
    assert numbered == done.names("CONFIRMED")                                             # exactly the CONFIRMED entities
    assert done.entity("Zephyr Systems")["tree_id"] is None and done.entity("Dormant Co")["tree_id"] is None


def test_tree_ids_are_recomputed_without_gaps_after_a_demotion(done):
    from corpdisc.contracts.postgate import CommitPlan, DemotionWrite
    from corpdisc.models.domain import ReviewReason
    before = done.entity("Company A2")["tree_id"]
    assert before == "NWH.01.02"
    done.deps.committer.commit(CommitPlan(run_id=done.run_id, demotions=(DemotionWrite(entity_id=done.entity("Company A1")["entity_id"],
                               reason_code=ReviewReason.LOW_CONFIDENCE, reason_text="t", recommended_action="a"),)))
    assign_tree_ids(done.conn, done.deps.committer, done.run_id, "NWH")
    assert done.entity("Company A1")["tree_id"] is None and done.entity("Company A2")["tree_id"] == "NWH.01.01"   # renumbered, no gap


def test_more_than_99_children_widen_the_numbers(tmp_path):
    from tests.agents.test_dedup_validation import add, root_id, world
    w = world(tmp_path, agents=())
    for i in range(105):
        add(w, f"Child Company {i:03d}", root_id(w), domain=f"cc{i}")
    rows = compute_tree(w.conn, w.run_id, "NWH")
    assert rows[1].tree_id == "NWH.001" and rows[-1].tree_id == "NWH.105"


# ------------------------------------------------------------------ workbook
def test_workbook_has_the_specified_sheets_columns_and_database_rows(done, tmp_path):
    out = tmp_path / "CSI_Company_Hierarchy.xlsx"
    summ = export_workbook(done.conn, done.run_id, out)
    wb = load_workbook(out)
    assert wb.sheetnames == SHEETS and out.exists()
    head, rows = sheet_rows(wb, "Company Hierarchy")
    assert list(head) == HIERARCHY_COLS and len(rows) == len(done.names("CONFIRMED")) == summ.confirmed
    assert {r[2] for r in rows} == done.names("CONFIRMED")
    keys = [tuple(int(x) for x in r[1].split(".")[1:]) for r in rows]
    assert keys == sorted(keys)                                                          # depth-first tree order
    root, a1 = rows[0], next(r for r in rows if r[2] == "Company A1")
    assert (root[4], root[5], root[6], root[7], root[16]) == ("—", None, "—", 0, 1.0)
    assert (a1[1], a1[4], a1[6], a1[7]) == ("NWH.01.01", "Group A", "Subsidiary of", 2) and isinstance(a1[16], float)
    assert "[FIRST_PARTY_PARENT]" in a1[11] and "Company A1 is a subsidiary of Group A." in a1[11]   # evidence text is a stored verified quote
    assert a1[9].startswith("https://a1.test") and wb["Company Hierarchy"].cell(row=rows.index(a1) + 2, column=10).hyperlink is not None


def test_every_evidence_cell_in_the_workbook_exists_in_the_database(done, tmp_path):
    wb = load_workbook(export_workbook(done.conn, done.run_id, tmp_path / "x.xlsx").path)
    quotes = {r[0] for r in done.q("SELECT quote FROM evidence WHERE quote_verified=1")}
    for r in list(wb["Company Hierarchy"].iter_rows(min_row=3, values_only=True)):
        for line in (r[11] or "").split("\n"):
            q = line.split('"')[1]
            assert q in quotes
    ev_head, ev_rows = sheet_rows(wb, "Evidence")
    assert list(ev_head) == EVID_COLS and len(ev_rows) == done.q("SELECT COUNT(*) FROM evidence")[0][0]


def test_operating_groups_review_crawl_and_validation_sheets(done, tmp_path):
    wb = load_workbook(export_workbook(done.conn, done.run_id, tmp_path / "x.xlsx").path)
    gh, groups = sheet_rows(wb, "Operating Groups")
    assert list(gh) == GROUP_COLS and {g[0]: g[4] for g in groups} == {"Group A": 3, "Group B": 1}   # confirmed descendants
    rh, review = sheet_rows(wb, "Review Required")
    assert list(rh) == REVIEW_COLS
    non_confirmed = {r[0] for r in done.q("SELECT name FROM entities WHERE verification_status='REVIEW_REQUIRED' AND merged_into_id IS NULL")}
    assert non_confirmed <= {r[0] for r in review}                                          # one row per non-confirmed entity
    assert any(r[0] == "Dormant Co" and "BROKEN_SITE" in r[4] for r in review)
    _, crawl = sheet_rows(wb, "Crawl Log")
    assert len(crawl) == done.q("SELECT COUNT(*) FROM crawl_pages")[0][0]
    _, findings = sheet_rows(wb, "Validation Report")
    assert len(findings) == done.q("SELECT COUNT(*) FROM validation_findings")[0][0]
    _, rels = sheet_rows(wb, "Relationships")
    assert len(rels) == done.q("SELECT COUNT(*) FROM relationships")[0][0] and any(r[6] == "REVIEW_REQUIRED" for r in rels)
    summary = dict(list(wb["Summary"].iter_rows(min_row=2, values_only=True)))
    assert summary["Run status"] == "COMPLETED" and summary["Total confirmed (incl. root)"] == len(done.names("CONFIRMED"))
    assert summary["Deepest level"] == 3 and summary["Budget limited"] == "no"


def test_the_workbook_is_a_pure_function_of_the_database(done, tmp_path):
    before = table_hash(done.conn)
    a = export_workbook(done.conn, done.run_id, tmp_path / "a.xlsx")
    assert table_hash(done.conn) == before                                                 # export never writes
    done.conn.execute("UPDATE entities SET confidence=0.123 WHERE name='Company B1'")      # change the source of truth ...
    done.conn.commit()
    wb = load_workbook(export_workbook(done.conn, done.run_id, tmp_path / "b.xlsx").path)
    row = next(r for r in wb["Company Hierarchy"].iter_rows(min_row=2, values_only=True) if r[2] == "Company B1")
    assert row[16] == pytest.approx(0.123) and a.confirmed == len(done.names("CONFIRMED"))  # ... and the workbook follows it


def test_finalize_writes_the_workbook_only_when_an_export_path_is_configured(tmp_path):
    w = seeded_world(tmp_path)
    w.sup.run()
    assert not list(tmp_path.glob("*.xlsx"))                       # default deps: no export path -> no file
    w.deps.export_path = tmp_path / "out" / "CSI_Company_Hierarchy.xlsx"
    FinalizeGraph(w.deps).run()
    assert w.deps.export_path.exists() and load_workbook(w.deps.export_path).sheetnames == SHEETS


# ------------------------------------------------------------------ verifier
def verify(w):
    from corpdisc.services.fetch import make_page_text_loader
    loader = lambda pid: w.deps.fetch.load_text(pid)  # noqa: E731
    return check_run(w.conn, w.run_id, committer=w.deps.committer, cycle=CycleGuard(w.conn), page_text=loader,
                     shared_domains=frozenset(d.lower() for d in w.deps.settings.shared_platform_domains))


def statuses(checks):
    return {c.name.split(" (")[0][:60]: c.status for c in checks}


def test_verifier_passes_on_a_clean_run(done):
    checks = verify(done)
    assert [c.name for c in checks if c.status == "FAIL"] == []
    text = format_checks(checks)
    assert "0 FAILED" in text and len(checks) >= 14


@pytest.mark.parametrize("defect, expected", [
    ("UPDATE evidence SET quote='Group A was founded on the moon in 1492 by nobody.' WHERE evidence_id=(SELECT MIN(evidence_id) FROM evidence WHERE quote_verified=1)", "STORED page text"),
    ("UPDATE evidence SET quote_verified=0 WHERE relationship_id IN (SELECT relationship_id FROM relationships WHERE verification_status='CONFIRMED')", "without verified"),
    ("INSERT INTO websites(entity_id,url,domain,registrable_domain,kind,resolution_method) SELECT entity_id,'https://www.linkedin.com/company/x','www.linkedin.com','linkedin.com','OFFICIAL','TEST' FROM entities WHERE name='Group B'", "social/network"),
    ("INSERT INTO websites(entity_id,url,domain,registrable_domain,kind,resolution_method) SELECT entity_id,'https://a1.test/dup','a1.test','a1.test','OFFICIAL','TEST' FROM entities WHERE name='Company B1'", "duplicate websites"),
    ("UPDATE entities SET name='Totally Invented Corp', normalized_name='totally invented corp' WHERE name='Company A2'", "fabricated"),
    ("INSERT INTO relationships(parent_id,child_id,relationship_type,verification_status,confidence,is_primary,created_at) "
     "SELECT c.entity_id, p.entity_id, 'Part of','REVIEW_REQUIRED',0.5,0,'t' FROM entities c, entities p WHERE c.name='Company A1' AND p.name='Group A'", "cycles"),
])
def test_verifier_detects_injected_defects(done, defect, expected):
    """The checker must be able to fail: each defect is injected into a TEST database and must be reported."""
    done.conn.execute(defect)
    done.conn.commit()
    failed = [c for c in verify(done) if c.status == "FAIL"]
    assert any(expected in c.name for c in failed), (expected, [c.name for c in failed])


def test_verifier_flags_partner_wording_behind_a_confirmed_relationship(done):
    done.conn.execute("UPDATE evidence SET quote='Company A1 is a subsidiary of Group A.' WHERE 0")  # no-op guard
    done.conn.execute("UPDATE evidence SET quote_verified=1, supports='OWNERSHIP' WHERE entity_id=(SELECT entity_id FROM entities WHERE name='Zephyr Systems')")
    zid = done.entity("Zephyr Systems")["entity_id"]
    done.conn.execute("UPDATE relationships SET verification_status='CONFIRMED' WHERE child_id=?", (zid,))
    done.conn.commit()
    failed = [c.name for c in verify(done) if c.status == "FAIL"]
    assert any("partner/customer" in n for n in failed)


# ------------------------------------------------------------------ CLI
def cli_env(monkeypatch, w):
    path = [r[2] for r in w.conn.execute("PRAGMA database_list")][0]
    for k, v in {"DB_PATH": path, "CACHE_DIR": str(w.deps.settings.cache_dir), "LOG_DIR": str(w.deps.settings.log_dir), "ROOT_CODE": "NWH"}.items():
        monkeypatch.setenv(k, v)


def test_cli_export_and_verify(done, tmp_path, monkeypatch, capsys):
    cli_env(monkeypatch, done)
    out = tmp_path / "cli.xlsx"
    assert main(["export", "--output", str(out)]) == 0
    printed = capsys.readouterr().out
    assert "wrote" in printed and "Company Hierarchy" in printed and load_workbook(out).sheetnames == SHEETS
    assert main(["verify"]) == 0
    assert "0 FAILED" in capsys.readouterr().out


def test_cli_verify_exits_nonzero_on_a_defect(done, monkeypatch, capsys):
    cli_env(monkeypatch, done)
    done.conn.execute("UPDATE evidence SET quote='This sentence appears on no page at all.' WHERE evidence_id=(SELECT MIN(evidence_id) FROM evidence WHERE quote_verified=1)")
    done.conn.commit()
    assert main(["verify"]) == 1
    assert "FAIL" in capsys.readouterr().out
