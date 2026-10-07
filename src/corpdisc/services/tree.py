"""Tree IDs, levels and hierarchy paths (ARCHITECTURE section 11). Read-only computation + one Committer write.

Only CONFIRMED entities whose primary CONFIRMED edge comes from a CONFIRMED parent that is itself in the tree are numbered.
Children are ordered by (sibling_order, normalized_name, entity_id) and numbered zero-padded to at least two digits (wider when a
parent has more than 99 children). Tree IDs are a presentation key; entity_id stays the stable identity.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from dataclasses import dataclass

from corpdisc.contracts.postgate import CommitPlan, CommitRejected, TreeAssignWrite
from corpdisc.services.committer import Committer


@dataclass(frozen=True)
class TreeRow:
    entity_id: int
    parent_id: int | None
    tree_id: str
    level: int
    path: str


def compute_tree(conn: sqlite3.Connection, run_id: int, root_code: str) -> list[TreeRow]:
    root = conn.execute("SELECT entity_id, name FROM entities WHERE run_id=? ORDER BY discovery_seq LIMIT 1", (run_id,)).fetchone()
    if root is None:
        return []
    kids: dict[int, list[tuple]] = defaultdict(list)
    for r in conn.execute(
        "SELECT x.parent_id, e.entity_id, e.name, e.normalized_name, COALESCE(e.sibling_order, 0) so FROM relationships x "
        "JOIN entities e ON e.entity_id = x.child_id JOIN entities p ON p.entity_id = x.parent_id "
        "WHERE e.run_id=? AND e.verification_status='CONFIRMED' AND e.merged_into_id IS NULL AND x.verification_status='CONFIRMED' "
        "AND x.is_primary=1 AND p.verification_status='CONFIRMED' AND p.merged_into_id IS NULL", (run_id,)):
        kids[r["parent_id"]].append((r["so"], r["normalized_name"], r["entity_id"], r["name"]))
    rows = [TreeRow(root["entity_id"], None, root_code, 0, root["name"])]
    seen = {root["entity_id"]}

    def walk(parent: TreeRow) -> None:
        children = sorted(kids.get(parent.entity_id, []))
        width = max(2, len(str(len(children))))
        for n, (_, _, eid, name) in enumerate(children, 1):
            if eid in seen:  # a cycle can never be numbered
                continue
            seen.add(eid)
            row = TreeRow(eid, parent.entity_id, f"{parent.tree_id}.{n:0{width}d}", parent.level + 1, f"{parent.path} > {name}")
            rows.append(row)
            walk(row)

    walk(rows[0])
    return rows


def assign_tree_ids(conn: sqlite3.Connection, committer: Committer, run_id: int, root_code: str) -> list[TreeRow]:
    rows = compute_tree(conn, run_id, root_code)
    res = committer.commit(CommitPlan(run_id=run_id, tree_assignments=tuple(
        TreeAssignWrite(entity_id=r.entity_id, tree_id=r.tree_id, level=r.level, hierarchy_path=r.path[:2000]) for r in rows)))
    if isinstance(res, CommitRejected):
        raise RuntimeError(f"tree assignment rejected: {res.failed_invariant}: {res.message}")
    return rows
