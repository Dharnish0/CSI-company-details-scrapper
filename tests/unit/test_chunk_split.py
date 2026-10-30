"""_split_text: the pure helper behind output-limit-aware re-chunking (defect C)."""
from __future__ import annotations

from corpdisc.graphs.research import MIN_SPLIT_CHARS, _split_text


def lines(n: int, width: int = 99) -> str:
    return "\n".join(f"{i:03d} " + "x" * (width - 4) for i in range(n))


def test_split_is_at_a_line_break_near_the_middle_and_offsets_add_up():
    text = lines(30)
    (a, a_off), (b, b_off) = _split_text(text, 1000)
    assert a_off == 1000 and b_off == 1000 + len(a) + 1
    assert text[len(a)] == "\n" and a + "\n" + b == text
    assert abs(len(a) - len(b)) <= 100  # one line of slack around the middle
    assert all(len(ln) == 99 for ln in a.split("\n") + b.split("\n"))  # no line is cut


def test_split_never_goes_below_the_minimum_half_size():
    assert MIN_SPLIT_CHARS == 800
    short = lines(10)  # 999 chars: no break leaves two halves of 800
    assert _split_text(short, 5) == [(short, 5)]
    edge = lines(18)  # just big enough for two halves of at least 800 chars
    parts = _split_text(edge, 0)
    assert len(parts) == 2 and all(len(p) >= MIN_SPLIT_CHARS for p, _ in parts)


def test_text_without_line_breaks_is_not_split():
    blob = "y" * 5000
    assert _split_text(blob, 0) == [(blob, 0)]


def test_repeated_splitting_terminates_and_covers_the_text():
    text = lines(60)
    pending, leaves = [(text, 0)], []
    while pending:
        t, off = pending.pop()
        halves = _split_text(t, off)
        if len(halves) == 1:
            leaves.append((t, off))
        else:
            pending += halves
    assert all(len(t) < 2 * MIN_SPLIT_CHARS + 200 for t, _ in leaves)
    assert "\n".join(t for t, _ in sorted(leaves, key=lambda x: x[1])) == text
