#!/usr/bin/env python
"""Ollama preflight + controlled extraction tests A-C through the REAL application stack (Steps 3 and 11).

    python scripts/ollama_preflight.py                  # measure the environment, then run tests A, B, C
    python scripts/ollama_preflight.py --env-only       # only measure the environment

It reads your .env (LLM_MODEL, OLLAMA_NUM_CTX, LLM_TIMEOUT_SECONDS ...) but uses throw-away databases under ./preflight_out and an
in-memory synthetic site, so it never touches data/ and never fetches a real website. Test D (the exact application request on the
real CSI pages) is `python main.py run --yes --max-tasks 1` followed by `python main.py report`.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import shutil
import sqlite3
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import requests  # noqa: E402


def environment(base: str, model: str, timeout: float = 10.0) -> dict:
    s = requests.Session()
    s.trust_env = False
    env: dict = {"base_url": base, "model_requested": model}
    try:
        env["ollama_version"] = s.get(base + "/api/version", timeout=timeout).json().get("version")
        tags = s.get(base + "/api/tags", timeout=timeout).json().get("models", [])
        hit = next((m for m in tags if m.get("name") == model or m.get("model") == model), None)
        env["model_installed"] = hit is not None
        if hit:
            d = hit.get("details", {})
            env.update(parameter_size=d.get("parameter_size"), quantization=d.get("quantization_level"), family=d.get("family"), size_bytes=hit.get("size"))
        else:
            env["installed_models"] = [m.get("name") for m in tags]
        info = s.post(base + "/api/show", json={"model": model}, timeout=timeout).json() if hit else {}
        env["capabilities"] = info.get("capabilities")
        env["model_context_length"] = next((v for k, v in (info.get("model_info") or {}).items() if k.endswith(".context_length")), None)
    except requests.RequestException as exc:
        env["error"] = f"cannot reach Ollama: {type(exc).__name__}"
    env["ram_gb"] = _ram_gb()
    env["nvidia_smi_present"] = shutil.which("nvidia-smi") is not None
    env["OLLAMA_CONTEXT_LENGTH(this process)"] = os.environ.get("OLLAMA_CONTEXT_LENGTH")
    return env


def _ram_gb() -> float | None:
    try:
        if sys.platform == "win32":
            import ctypes

            class MS(ctypes.Structure):
                _fields_ = [("l", ctypes.c_ulong), ("m", ctypes.c_ulong), ("total", ctypes.c_ulonglong), ("a", ctypes.c_ulonglong), ("pf", ctypes.c_ulonglong),
                            ("pa", ctypes.c_ulonglong), ("v", ctypes.c_ulonglong), ("va", ctypes.c_ulonglong), ("e", ctypes.c_ulonglong)]
            ms = MS()
            ms.l = ctypes.sizeof(MS)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(ms))  # type: ignore[attr-defined]
            return round(ms.total / 2**30, 1)
        for line in open("/proc/meminfo", encoding="utf-8"):
            if line.startswith("MemTotal"):
                return round(int(line.split()[1]) / 2**20, 1)
    except Exception:  # noqa: BLE001
        return None
    return None


def loaded_placement(base: str) -> list[dict]:
    try:
        ps = requests.Session()
        ps.trust_env = False
        return [{"name": m.get("name"), "size": m.get("size"), "size_vram": m.get("size_vram"), "context_length": m.get("context_length"),
                 "processor": "CPU only" if not m.get("size_vram") else "GPU (partly or fully)"} for m in ps.get(base + "/api/ps", timeout=10).json().get("models", [])]
    except requests.RequestException:
        return []


def run_case(label: str, n_cards: int, *, transport, workdir: pathlib.Path, model: str = "qwen3:4b", base_url: str = "", env_file: str | None = ".env",
             **overrides) -> dict:
    """One real-stack extraction task on a synthetic listing of ``n_cards`` companies. ``transport`` is the real RequestsJsonTransport
    in normal use (the self-test injects a fake). Returns measured facts read back from SQLite."""
    from corpdisc.app import build_runtime
    from corpdisc.config import Settings
    from tests.fixtures.listing_site import ROOT, site

    workdir.mkdir(parents=True, exist_ok=True)
    st = Settings(_env_file=env_file, start_url="https://root.test/", root_company=ROOT, root_code="CSI", request_delay_seconds=0.0, max_retries=0,
                  db_path=workdir / f"{label}.db", cache_dir=workdir / f".cache_{label}", log_dir=workdir / "logs", use_playwright="false",
                  llm_provider="ollama", llm_model=model, llm_base_url=base_url, output_xlsx=workdir / f"{label}.xlsx", **overrides)
    rt = build_runtime(st, http=site(n_cards), transport=transport)
    t0 = time.perf_counter()
    try:
        rt.supervisor.seed_root()
        rt.supervisor.run(max_tasks=1)
        wall = time.perf_counter() - t0
        c = rt.conn
        rows = [dict(r) for r in c.execute("SELECT attempt, status, failure_kind, latency_ms, tokens_in, tokens_out, response_json FROM agent_invocations WHERE agent='extraction' ORDER BY invocation_id")]
        out = []
        for r in rows:
            j = json.loads(r.pop("response_json") or "{}")
            pm = j.get("provider_metrics") or {}
            out.append({**r, "finish_reason": j.get("finish_reason"), "request_bytes": pm.get("request_bytes"), "prompt_eval_ms": pm.get("prompt_eval_ms"),
                        "eval_ms": pm.get("eval_ms"), "load_ms": pm.get("load_ms"), "reasoning_chars": pm.get("reasoning_chars"), "context_used_pct": pm.get("context_used_pct")})
        gate = dict(c.execute("SELECT gate_code, COUNT(*) FROM gate_rejections GROUP BY 1").fetchall())
        cand = dict(c.execute("SELECT COALESCE(decision,'NONE'), COUNT(*) FROM candidates GROUP BY 1").fetchall())
        return {"label": label, "cards": n_cards, "wall_s": round(wall, 1), "attempts": out, "candidates": cand, "gate_rejections": gate,
                "entities": c.execute("SELECT COUNT(*) FROM entities").fetchone()[0],
                "schema_valid": all(a["status"] == "OK" for a in out) and bool(out), "model_context_used_max_pct": max([a["context_used_pct"] or 0 for a in out] or [0])}
    finally:
        rt.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--env-only", action="store_true")
    ap.add_argument("--model", default=os.environ.get("LLM_MODEL", "qwen3:4b"))
    ap.add_argument("--base", default=os.environ.get("LLM_BASE_URL") or "http://localhost:11434")
    ap.add_argument("--cases", default="A:2,B:8,C:60", help="label:cards,... (A small, B realistic-size listing, C large listing)")
    args = ap.parse_args()
    print("ENVIRONMENT (measured, not assumed)")
    for k, v in environment(args.base, args.model).items():
        print(f"  {k:<36} {v}")
    if args.env_only:
        return 0
    from corpdisc.config import Settings
    from corpdisc.services.llm_http import RequestsJsonTransport
    st = Settings(_env_file=".env")
    print(f"\nAPPLICATION SETTINGS  timeout={st.llm_timeout_seconds}s temperature={st.llm_temperature} num_ctx={st.ollama_num_ctx} "
          f"extraction_reasoning={st.llm_extraction_reasoning} keep_alive={st.ollama_keep_alive} max_output_tokens=3000 transport_retries={st.llm_transport_retries}")
    out = pathlib.Path("preflight_out")
    shutil.rmtree(out, ignore_errors=True)
    for spec in args.cases.split(","):
        label, n = spec.split(":")
        r = run_case(label, int(n), transport=RequestsJsonTransport(), workdir=out, model=args.model, base_url=args.base if args.base != "http://localhost:11434" else "")
        print(f"\nTEST {label}: {n}-company listing  wall={r['wall_s']}s  schema_valid={r['schema_valid']}  candidates={r['candidates']}  gate_rejections={r['gate_rejections']}")
        for a in r["attempts"]:
            print("   ", {k: v for k, v in a.items() if v is not None})
    print("\nMODEL PLACEMENT AFTER THE TESTS (context actually used, CPU vs GPU)")
    for m in loaded_placement(args.base):
        print("  ", m)
    return 0


if __name__ == "__main__":
    sys.exit(main())
