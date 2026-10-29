"""ExcelExporter: writes the workbook from SQLite and from nothing else (ARCHITECTURE section 11, 24.4 export_excel).

Read-only: it opens no write transaction and never consults crawl objects, so the workbook can always be regenerated from the
database. Sheets: Company Hierarchy (depth-first tree order), Operating Groups, Crawl Log, Review Required, Validation Report,
Summary, plus two audit sheets (Relationships, Evidence) listing every row regardless of status.
"""

from __future__ import annotations

import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from corpdisc.models.domain import run_status_label
from corpdisc.services.calibration import build_report

HIERARCHY_COLS = ["Entity ID", "Tree ID", "Company Name", "Entity Type", "Parent Entity", "Parent ID", "Relationship", "Level",
                  "Hierarchy Path", "Official Website", "Source URL", "Evidence", "Discovery Method", "Country", "Industry",
                  "Status (operating)", "Confidence Score", "Discovery Date"]
GROUP_COLS = ["Group", "Source URL (listing page)", "Official Website", "Description", "Number of Companies", "Status"]
CRAWL_COLS = ["URL", "Domain", "Entity", "Parent Entity", "Page Type", "HTTP Status", "Crawled At", "Pages Found", "Entities Found",
              "Crawl Status", "Error"]
REVIEW_COLS = ["Entity", "Possible Parent", "Website", "Source URL", "Reason for Uncertainty", "Evidence", "Confidence",
               "Recommended Manual Action"]
FINDING_COLS = ["Check", "Severity", "Entity", "Details", "Action Taken"]
REL_COLS = ["Relationship ID", "Parent", "Parent ID", "Child", "Child ID", "Type", "Status", "Confidence", "Primary", "Source URL"]
EVID_COLS = ["Evidence ID", "Entity", "Relationship", "Source URL", "Quote", "Quote verified", "Source class", "Supports", "Recorded At"]
_URL_COLS = {"Official Website", "Source URL", "Website", "URL", "Source URL (listing page)"}
_CLASS_RANK = "CASE source_class WHEN 'FIRST_PARTY_PARENT' THEN 0 WHEN 'FIRST_PARTY_CHILD' THEN 1 WHEN 'OFFICIAL_DOCUMENT' THEN 2 ELSE 3 END"


@dataclass(frozen=True)
class ExportSummary:
    path: Path
    run_id: int
    sheets: dict[str, int]  # sheet name -> data rows
    confirmed: int
    review_required: int


def _fmt_ev(rows) -> str:  # type: ignore[no-untyped-def]
    return "\n".join(f'[{r["source_class"]}] "{r["quote"]}" ({r["url"]})' for r in rows)


def _tree_key(tree_id: str) -> tuple:
    return tuple(int(p) if p.isdigit() else 0 for p in tree_id.split(".")[1:])


def export_workbook(conn: sqlite3.Connection, run_id: int, path: str | Path) -> ExportSummary:
    conn.row_factory = sqlite3.Row
    q = lambda sql, *p: conn.execute(sql, p).fetchall()  # noqa: E731
    run = q("SELECT * FROM runs WHERE run_id=?", run_id)
    if not run:
        raise LookupError(f"unknown run {run_id}")
    run = run[0]
    page_url = {r["page_id"]: (r["final_url"] or r["canonical_url"]) for r in q("SELECT page_id, final_url, canonical_url FROM crawl_pages WHERE run_id=?", run_id)}
    name = {r["entity_id"]: r["name"] for r in q("SELECT entity_id, name FROM entities WHERE run_id=?", run_id)}
    site = {r["entity_id"]: r["url"] for r in q("SELECT e.entity_id, w.url FROM entities e JOIN websites w ON w.website_id=e.primary_website_id WHERE e.run_id=?", run_id)}

    # ---------------------------------------------------------------- Company Hierarchy (+ Operating Groups source)
    tree = sorted((r for r in q("SELECT * FROM entities WHERE run_id=? AND tree_id IS NOT NULL AND merged_into_id IS NULL", run_id)),
                  key=lambda r: _tree_key(r["tree_id"]))
    hierarchy: list[list[Any]] = []
    parent_of: dict[int, int] = {}
    for e in tree:
        rel = q("SELECT * FROM relationships WHERE child_id=? AND is_primary=1 AND verification_status='CONFIRMED' LIMIT 1", e["entity_id"])
        rel = rel[0] if rel else None
        if rel is not None:
            parent_of[e["entity_id"]] = rel["parent_id"]
            ev = q(f"SELECT quote, source_class, url FROM evidence WHERE relationship_id=? AND quote_verified=1 ORDER BY {_CLASS_RANK}, evidence_id LIMIT 2", rel["relationship_id"])
            src = page_url.get(rel["source_page_id"]) or (ev[0]["url"] if ev else None)
            row = [e["entity_id"], e["tree_id"], e["name"], e["entity_type"], name.get(rel["parent_id"], "—"), rel["parent_id"], rel["relationship_type"],
                   e["level"], e["hierarchy_path"], site.get(e["entity_id"]), src, _fmt_ev(ev), e["discovery_method"], e["country"], e["industry"],
                   e["operating_status"], float(e["confidence"] if e["confidence"] is not None else rel["confidence"]), (e["discovered_at"] or "")[:10]]
        else:  # the root
            row = [e["entity_id"], e["tree_id"], e["name"], e["entity_type"], "—", None, "—", e["level"], e["hierarchy_path"], site.get(e["entity_id"]),
                   site.get(e["entity_id"]), "Root company (from configuration)", e["discovery_method"], e["country"], e["industry"], e["operating_status"],
                   1.0, (e["discovered_at"] or "")[:10]]
        hierarchy.append(row)

    descendants: Counter = Counter()
    for eid in parent_of:  # all confirmed descendants per ancestor
        cur, guard = parent_of.get(eid), 0
        while cur is not None and guard < 100:
            descendants[cur] += 1
            cur, guard = parent_of.get(cur), guard + 1
    groups = []
    for e in tree:
        if e["level"] == 1:
            rel = q("SELECT source_page_id FROM relationships WHERE child_id=? AND is_primary=1 AND verification_status='CONFIRMED' LIMIT 1", e["entity_id"])
            groups.append([e["name"], page_url.get(rel[0]["source_page_id"]) if rel else None, site.get(e["entity_id"]), e["description"],
                           descendants[e["entity_id"]], e["operating_status"]])

    # ---------------------------------------------------------------- Crawl Log
    crawl = [[r["url"], r["domain"], r["en"], r["pn"], r["page_type"], r["http_status"], r["crawled_at"], r["pages_found"], r["entities_found"], r["crawl_status"], r["error"]]
             for r in q("SELECT COALESCE(p.final_url, p.canonical_url) url, p.domain, e.name en, pe.name pn, p.page_type, p.http_status, p.crawled_at, p.pages_found, "
                        "p.entities_found, p.crawl_status, p.error FROM crawl_pages p LEFT JOIN entities e ON e.entity_id=p.entity_id "
                        "LEFT JOIN entities pe ON pe.entity_id=p.parent_entity_id WHERE p.run_id=? ORDER BY p.page_id", run_id)]

    # ---------------------------------------------------------------- Review Required: one row per non-confirmed entity (+ extra items)
    def ev_for(eid):  # type: ignore[no-untyped-def]
        return _fmt_ev(q(f"SELECT quote, source_class, url FROM evidence WHERE entity_id=? AND quote_verified=1 ORDER BY {_CLASS_RANK}, evidence_id LIMIT 2", eid))

    review, has_item = [], set()
    for r in q("SELECT * FROM review_items WHERE run_id=? ORDER BY review_id", run_id):
        has_item.add(r["entity_id"])
        ev = q("SELECT url FROM evidence WHERE entity_id=? ORDER BY evidence_id LIMIT 1", r["entity_id"])
        review.append([name.get(r["entity_id"]), name.get(r["possible_parent_id"], "—"), site.get(r["entity_id"]), ev[0]["url"] if ev else None,
                       f"{r['reason_code']}: {r['reason_text']}", ev_for(r["entity_id"]), r["confidence"], r["recommended_action"]])
    for e in q("SELECT entity_id, name, confidence FROM entities WHERE run_id=? AND verification_status='REVIEW_REQUIRED' AND merged_into_id IS NULL ORDER BY entity_id", run_id):
        if e["entity_id"] not in has_item:
            review.append([e["name"], "—", site.get(e["entity_id"]), None, "REVIEW_REQUIRED (no detail recorded)", ev_for(e["entity_id"]), e["confidence"], "Review manually."])

    findings = [[r["check_name"], r["severity"], name.get(r["entity_id"], "—"), r["details"], r["action_taken"]]
                for r in q("SELECT * FROM validation_findings WHERE run_id=? ORDER BY finding_id", run_id)]
    rels = [[r["relationship_id"], name.get(r["parent_id"]), r["parent_id"], name.get(r["child_id"]), r["child_id"], r["relationship_type"], r["verification_status"],
             r["confidence"], "yes" if r["is_primary"] else "no", page_url.get(r["source_page_id"])]
            for r in q("SELECT x.* FROM relationships x JOIN entities e ON e.entity_id=x.child_id WHERE e.run_id=? ORDER BY x.relationship_id", run_id)]
    evid = []
    for r in q("SELECT ev.*, x.parent_id, x.child_id FROM evidence ev JOIN entities e ON e.entity_id=ev.entity_id LEFT JOIN relationships x "
               "ON x.relationship_id=ev.relationship_id WHERE e.run_id=? ORDER BY ev.evidence_id", run_id):
        evid.append([r["evidence_id"], name.get(r["entity_id"]), f"{name.get(r['parent_id'], '?')} -> {name.get(r['child_id'], '?')}" if r["relationship_id"] else "—",
                     r["url"], r["quote"], "yes" if r["quote_verified"] else "no", r["source_class"], r["supports"], r["created_at"]])

    # ---------------------------------------------------------------- Summary (aggregates + cost, from the same database)
    rep = build_report(conn, run_id)
    per_level = Counter(e["level"] for e in tree)
    errors = sum(rep["errors"].values())
    summary: list[list[Any]] = [
        ["Run ID", run_id], ["Run status", run_status_label(run["status"])], ["Budget limited", "YES" if run["status"] == "BUDGET_EXHAUSTED" else "no"],
        ["Failed tasks", q("SELECT COUNT(*) FROM research_queue WHERE run_id=? AND status='FAILED'", run_id)[0][0]],
        ["Tasks finished with gaps (partial)", q("SELECT COUNT(*) FROM research_queue WHERE run_id=? AND status='DONE' AND error LIKE 'PARTIAL:%'", run_id)[0][0]],
        ["Root company", run["root_name"]], ["Total confirmed (incl. root)", len(tree)], ["Deepest level", max(per_level) if per_level else 0],
        *[[f"Confirmed at level {lv}", per_level[lv]] for lv in sorted(per_level)],
        *[[f"Companies under group: {g[0]}", g[4]] for g in groups],
        ["Review required (rows)", len(review)], ["Pages crawled", sum(rep["pages"].values())], ["Crawl errors", errors],
        ["LLM provider calls", rep["llm"]["provider_calls"]], ["LLM cache hits", rep["llm"]["cache_hits"]], ["Cache hit rate", rep["llm"]["cache_hit_rate"]],
        ["LLM calls per task", rep["llm"]["calls_per_task"]], ["Tokens used", rep["run"]["tokens_used"]], ["Cost (USD)", rep["run"]["cost_usd"]],
        *[[f"Agent {a}: calls / cache hits / tokens in / out", f"{v['provider_calls']} / {v['cache_hits']} / {v['tokens_in']} / {v['tokens_out']}"] for a, v in sorted(rep["llm"]["per_agent"].items())],
    ]

    # ---------------------------------------------------------------- write workbook
    wb = Workbook()
    wb.remove(wb.active)
    sheets = {"Company Hierarchy": (HIERARCHY_COLS, hierarchy), "Operating Groups": (GROUP_COLS, groups), "Crawl Log": (CRAWL_COLS, crawl),
              "Review Required": (REVIEW_COLS, review), "Validation Report": (FINDING_COLS, findings), "Relationships": (REL_COLS, rels),
              "Evidence": (EVID_COLS, evid)}
    head_fill = PatternFill("solid", fgColor="1F3864")
    for title, (cols, rows) in sheets.items():
        ws = wb.create_sheet(title)
        ws.append(cols)
        for c in ws[1]:
            c.font, c.fill, c.alignment = Font(bold=True, color="FFFFFF"), head_fill, Alignment(vertical="center", wrap_text=True)
        for row in rows:
            ws.append(row)
        ws.freeze_panes = "A2"
        ws.auto_filter.ref = ws.dimensions if rows else f"A1:{get_column_letter(len(cols))}1"
        for i, col in enumerate(cols, 1):
            width = min(max([len(col)] + [len(str(r[i - 1] or "").split("\n")[0]) for r in rows[:300]]) + 2, 70)
            ws.column_dimensions[get_column_letter(i)].width = width
            for r in range(2, len(rows) + 2):
                cell = ws.cell(row=r, column=i)
                if col in _URL_COLS and isinstance(cell.value, str) and cell.value.startswith("http"):
                    cell.hyperlink, cell.font = cell.value, Font(color="0563C1", underline="single")
                if col in ("Evidence", "Quote", "Reason for Uncertainty", "Details", "Recommended Manual Action", "Hierarchy Path", "Description"):
                    cell.alignment = Alignment(wrap_text=True, vertical="top")
                if col in ("Confidence Score", "Confidence") and isinstance(cell.value, (int, float)):
                    cell.number_format = "0.00"
    ws = wb.create_sheet("Summary", 0)
    ws.append(["Metric", "Value"])
    for c in ws[1]:
        c.font, c.fill = Font(bold=True, color="FFFFFF"), head_fill
    for row in summary:
        ws.append(row)
    ws.column_dimensions["A"].width, ws.column_dimensions["B"].width = 58, 40
    ws.freeze_panes = "A2"
    wb.move_sheet("Company Hierarchy", offset=-(wb.sheetnames.index("Company Hierarchy") - 1))
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    wb.save(out)
    counts = {t: len(r) for t, (_, r) in sheets.items()} | {"Summary": len(summary)}
    return ExportSummary(out, run_id, counts, len(tree), len(review))
