"""Only the gateway may call a provider's ``complete``: that is what makes the slow-crawl interval global."""
from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src" / "corpdisc"
PROVIDER_WORDS = ("provider", "_inner")


def provider_calls() -> list[tuple[str, str]]:
    found = []
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "complete":
                receiver = ast.unparse(node.func.value)
                if any(w in receiver for w in PROVIDER_WORDS):
                    found.append((path.relative_to(SRC).as_posix(), receiver))
    return found


def test_only_the_gateway_calls_a_provider():
    calls = provider_calls()
    gateway = [c for c in calls if c[0] == "agents/gateway.py"]
    assert gateway == [("agents/gateway.py", "self._provider")]
    # the cassette recorder wraps a provider; it is a provider itself and is only reached through the gateway
    others = {c for c in calls if c[0] != "agents/gateway.py"}
    assert others <= {("agents/providers/cassette.py", "self._inner")}


def test_the_provider_call_is_preceded_by_the_limiter_acquire():
    src = (SRC / "agents" / "gateway.py").read_text(encoding="utf-8")
    assert src.count("self._provider.complete(") == 1
    assert src.index("self.limiter.acquire(") < src.index("self._provider.complete(")
