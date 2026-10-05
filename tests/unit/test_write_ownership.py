"""Write ownership (ARCHITECTURE 26.2): each table is mutated only by its owning module.

Scans every SQL string literal under src/corpdisc for INSERT/UPDATE/DELETE and checks the owner.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src" / "corpdisc"
WRITE = re.compile(r"\b(?:INSERT\s+INTO|UPDATE|DELETE\s+FROM)\s+([a-z_]+)", re.I)

BUSINESS = {"entities", "entity_aliases", "websites", "relationships", "evidence", "candidates", "review_items",
            "validation_findings"}
OWNERS: dict[str, set[str]] = {
    **{t: {"services/committer.py"} for t in BUSINESS},
    # M3 gates log rejections through this repo; the Committer also writes them inside a plan.
    "gate_rejections": {"services/committer.py", "db/repos/gate_rejections.py"},
    "research_queue": {"services/queue.py"},
    "crawl_pages": {"db/repos/pages.py"}, "domain_stats": {"db/repos/pages.py"}, "crawl_errors": {"db/repos/pages.py"},
    "agent_invocations": {"db/repos/audit.py"}, "agent_tool_calls": {"db/repos/audit.py"},
    "runs": {"services/budget.py", "db/repos/runs.py"},
    "llm_calls": set(),  # D1: never written
}
# QueueService.complete/skip set entities.crawl_state with the task in one transaction (plan 4.M1.8).
EXTRA = {("entities", "services/queue.py")}


def _writes() -> list[tuple[str, str]]:
    out = []
    for path in SRC.rglob("*.py"):
        rel = path.relative_to(SRC).as_posix()
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                for table in WRITE.findall(node.value):
                    out.append((table.lower(), rel))
            elif isinstance(node, ast.JoinedStr):
                text = "".join(v.value for v in node.values if isinstance(v, ast.Constant) and isinstance(v.value, str))
                for table in WRITE.findall(text):
                    out.append((table.lower(), rel))
    return out


def test_every_table_write_is_by_its_owner():
    bad = [(t, f) for t, f in _writes() if t in OWNERS and f not in OWNERS[t] and (t, f) not in EXTRA]
    assert bad == [], f"tables written outside their owner: {bad}"


def test_scanner_finds_known_writes():
    found = set(_writes())
    assert ("entities", "services/committer.py") in found and ("research_queue", "services/queue.py") in found
