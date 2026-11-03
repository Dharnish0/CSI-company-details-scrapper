import pathlib
import subprocess
import sys


def run(*args):
    p = subprocess.run([sys.executable, *args], capture_output=True, text=True, encoding="utf-8")
    return p.stdout + p.stderr


verbose = run("-m", "pytest", "tests/test_commit_idempotency.py", "tests/services/test_committer.py",
              "tests/test_child_entry_page.py", "tests/test_extraction_truncation.py", "tests/test_queue_recovery.py",
              "tests/unit/test_chunk_split.py", "tests/agents/test_extraction_agent.py", "-v", "-p", "no:cacheprovider")
lines = [ln for ln in verbose.splitlines() if "PASSED" in ln or "FAILED" in ln or "ERROR" in ln or " passed" in ln or " failed" in ln]
db = "bench/p4_scratch/data/test.db"
doc = [
    "# CSI Phase 4 offline evidence", "",
    "Scripted/fake providers only. No network, no billable calls. Branch claude/m8-ollama-e2e-fix, commits "
    "6ba5e81 (A), cf5c9bf (B), 4749523 (C).", "",
    "## Regression tests per defect (pytest -v)", "",
    "- A: tests/test_commit_idempotency.py, tests/services/test_committer.py",
    "- B: tests/test_child_entry_page.py (+ tests/test_m9_export_verify.py::test_verify_flags_terminal_without_reason)",
    "- C: tests/test_extraction_truncation.py, tests/test_queue_recovery.py, tests/unit/test_chunk_split.py, "
    "tests/agents/test_extraction_agent.py", "", "```", *lines, "```", "",
    "## Scratch DB (bench/p4_scratch, entry-page fixture with Jonas/Vela/Harris-like children)", "", "```",
    run("bench/summarize.py", db).strip(), "", run("bench/trace_children.py", db).strip(), "```", "",
]
pathlib.Path(".agents/tasks/csi-p4-evidence.md").write_text("\n".join(doc), encoding="utf-8")
print(len(lines), "test lines written")
