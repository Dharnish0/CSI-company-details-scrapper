"""Import boundaries (ARCHITECTURE 28.1, plan section 2), enforced by AST over the real tree.

services/ and gates/ never import agents/; agents/ never imports db/ or services/ (it sees them only
through ports); agents/ outside providers/ has no DB, network or filesystem modules; only graphs/ may
import LangGraph; no LLM SDK is imported anywhere.
"""
from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src" / "corpdisc"

ALLOWED: dict[str, set[str]] = {
    "models": set(), "clock": set(), "config": set(), "logging_setup": {"config"},
    "contracts": {"models", "clock", "contracts"},
    "db": {"models", "contracts", "clock", "db"},
    "services": {"models", "contracts", "clock", "config", "logging_setup", "db", "services"},
    "gates": {"models", "contracts", "clock", "config", "logging_setup", "services", "gates"},
    "agents": {"models", "contracts", "clock", "config", "logging_setup", "agents"},
}
AGENT_BANNED = {"sqlite3", "requests", "urllib", "urllib3", "http", "httpx", "socket", "ssl", "playwright", "bs4",
                "os", "pathlib", "io", "shutil", "tempfile", "glob"}
PROVIDER_BANNED = {"sqlite3", "requests", "urllib3", "httpx", "socket", "ssl", "playwright", "bs4"}
SDKS = {"openai", "anthropic", "langchain", "google.generativeai", "google.genai"}
HANDLE_PARAMS = {"conn", "connection", "cursor", "db", "session_db", "http_client", "fetch_service", "fetcher"}


def _imports(tree: ast.AST) -> list[str]:
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            out.append(node.module)
    return out


def check_source(module: str, source: str) -> list[str]:
    """``module`` is the path under corpdisc, e.g. 'agents/runner.py'. Returns violations."""
    tree = ast.parse(source)
    top = module.split("/")[0].removesuffix(".py")
    in_providers = module.startswith("agents/providers/")
    bad: list[str] = []
    for name in _imports(tree):
        root = name.split(".")[0]
        if any(name == s or name.startswith(s + ".") for s in SDKS):
            bad.append(f"{module}: LLM SDK import {name}")
        if root == "langgraph" and top != "graphs":
            bad.append(f"{module}: langgraph outside graphs/")
        if root == "corpdisc":
            target = name.split(".")[1] if "." in name else ""
            if top in ALLOWED and target not in ALLOWED[top]:
                bad.append(f"{module}: may not import corpdisc.{target}")
        elif top == "agents":
            if root in (PROVIDER_BANNED if in_providers else AGENT_BANNED):
                bad.append(f"{module}: banned import {name}")
    if top == "agents" and not in_providers:
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in (
                    "open", "eval", "exec", "compile", "__import__"):
                bad.append(f"{module}: banned builtin {node.func.id}")
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for a in node.args.args + node.args.kwonlyargs:
                    if a.arg in HANDLE_PARAMS:
                        bad.append(f"{module}: handle-like parameter {a.arg!r}")
    return bad


def test_real_tree_respects_boundaries():
    bad = []
    for path in sorted(SRC.rglob("*.py")):
        bad += check_source(path.relative_to(SRC).as_posix(), path.read_text(encoding="utf-8"))
    assert bad == []


def test_linter_can_fail():
    assert check_source("services/x.py", "from corpdisc.agents.runner import AgentRunner")
    assert check_source("gates/g99.py", "import corpdisc.agents")
    assert check_source("agents/x.py", "from corpdisc.db.connection import connect_writer")
    assert check_source("agents/x.py", "from corpdisc.services.fetch import FetchService")
    assert check_source("agents/x.py", "import sqlite3")
    assert check_source("agents/x.py", "def f(conn):\n    return open('x')")
    assert check_source("agents/providers/p.py", "import socket")
    assert not check_source("agents/providers/p.py", "import pathlib, os, json")
    assert check_source("services/y.py", "import anthropic")
    assert check_source("services/y.py", "from langgraph.graph import StateGraph")
    assert not check_source("graphs/research.py", "from langgraph.graph import StateGraph\nimport corpdisc.agents")
