"""Cycle protection (ARCHITECTURE.md 6.3). No depth cap is involved: hierarchy depth is unbounded.

Before adding ``parent -> child`` the guard walks UP from ``parent`` over CONFIRMED edges with a
recursive CTE. If ``child`` is among the ancestors the edge would close a cycle. The CTE uses
UNION, so it terminates even if the stored graph already contains a cycle.
"""

from __future__ import annotations

import sqlite3
from collections import deque
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

_CONFIRMED = ("CONFIRMED",)
_WITH_REVIEW = ("CONFIRMED", "REVIEW_REQUIRED")


@dataclass(frozen=True)
class CycleCheck:
    creates_cycle: bool
    reason: str | None = None
    path: tuple[int, ...] = ()  # child -> ... -> parent through existing edges, when a cycle is found


def find_cycle(edges: Iterable[tuple[int, int]]) -> list[int] | None:
    """Pure helper: return the nodes of one directed cycle in (parent, child) edges, or None."""
    graph: dict[int, list[int]] = {}
    for parent, child in edges:
        graph.setdefault(parent, []).append(child)
        graph.setdefault(child, [])
    state: dict[int, int] = {}  # 1 = on stack, 2 = done
    for root in graph:
        if state.get(root):
            continue
        stack = [(root, iter(graph[root]))]
        state[root] = 1
        trail = [root]
        while stack:
            node, it = stack[-1]
            for nxt in it:
                if state.get(nxt) == 1:
                    return [*trail[trail.index(nxt):], nxt][:-1] or [nxt]
                if not state.get(nxt):
                    state[nxt] = 1
                    trail.append(nxt)
                    stack.append((nxt, iter(graph[nxt])))
                    break
            else:
                state[node] = 2
                trail.pop()
                stack.pop()
    return None


class CycleGuard:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def _edges_up(self, start: int, include_review: bool) -> list[tuple[int, int]]:
        statuses = _WITH_REVIEW if include_review else _CONFIRMED
        marks = ",".join("?" for _ in statuses)
        rows = self._conn.execute(
            f"""WITH RECURSIVE up(child_id, parent_id) AS (
                    SELECT child_id, parent_id FROM relationships
                     WHERE child_id = ? AND verification_status IN ({marks})
                    UNION
                    SELECT r.child_id, r.parent_id FROM relationships r
                      JOIN up ON r.child_id = up.parent_id
                     WHERE r.verification_status IN ({marks})
                )
                SELECT child_id, parent_id FROM up""",  # noqa: S608 - only placeholders are interpolated
            (start, *statuses, *statuses),
        ).fetchall()
        return [(r[0], r[1]) for r in rows]

    def ancestors(self, entity_id: int, *, include_review: bool = False) -> list[int]:
        """All ancestors of ``entity_id`` (nearest first), following confirmed edges."""
        parents: dict[int, list[int]] = {}
        for child, parent in self._edges_up(entity_id, include_review):
            parents.setdefault(child, []).append(parent)
        seen: set[int] = set()
        order: list[int] = []
        queue = deque([entity_id])
        while queue:
            node = queue.popleft()
            for p in sorted(parents.get(node, [])):
                if p not in seen:
                    seen.add(p)
                    order.append(p)
                    queue.append(p)
        return order

    def would_create_cycle(
        self,
        parent_id: int,
        child_id: int,
        *,
        ancestry: Sequence[int] = (),
        include_review: bool = False,
    ) -> CycleCheck:
        if parent_id == child_id:
            return CycleCheck(True, "self-loop: an entity cannot be its own parent", (child_id,))
        if child_id in ancestry:
            return CycleCheck(True, "child is in the task ancestry", (child_id, parent_id))
        if child_id in self.ancestors(parent_id, include_review=include_review):
            return CycleCheck(True, "child is an ancestor of parent", self._path(child_id, parent_id, include_review))
        return CycleCheck(False)

    def _path(self, child_id: int, parent_id: int, include_review: bool) -> tuple[int, ...]:
        """Shortest existing chain parent -> ... -> child, reported as child-first ancestry."""
        up: dict[int, list[int]] = {}
        for c, p in self._edges_up(parent_id, include_review):
            up.setdefault(c, []).append(p)
        prev: dict[int, int | None] = {parent_id: None}
        queue = deque([parent_id])
        while queue:
            node = queue.popleft()
            if node == child_id:
                break
            for p in sorted(up.get(node, [])):
                if p not in prev:
                    prev[p] = node
                    queue.append(p)
        chain: list[int] = []
        cur: int | None = child_id if child_id in prev else None
        while cur is not None:
            chain.append(cur)
            cur = prev[cur]
        return tuple(chain) if chain else (child_id, parent_id)

    def has_cycle_through(self, entity_ids: Iterable[int], *, include_review: bool = False) -> list[int]:
        """Entities (from ``entity_ids``) that are their own ancestor."""
        return [e for e in entity_ids if any(p == e for _, p in self._edges_up(e, include_review))]
