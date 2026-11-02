#!/usr/bin/env python
"""Slow crawl benchmark harness: ``micro`` (pacing and 429 resume proof) and ``models`` (qwen vs gpt-oss comparison).

    python scripts/slow_crawl_benchmark.py micro  [--dry-run]
    python scripts/slow_crawl_benchmark.py models [--dry-run]

Hard rules enforced here (not by convention):
  * Writes only ``bench/slow_crawl_*`` files. The protected databases (``csi_coverage_test*``, ``csi_hierarchy*`` and
    anything under ``data/backups``) are refused as targets. The coverage DB is only ever read through a ``mode=ro``
    sqlite backup copy.
  * A persistent token ledger (``bench/slow_crawl_token_ledger.json``, starting at 178) refuses any request that could
    take the total past 120,000 tokens (hard cap 150,000), plus a per-phase cap (micro 20,000, models 25,000 each).
  * The API key is never printed or written; only its presence is reported. TLS verification is not touched.
  * ``--dry-run`` uses a fake transport: no network, no ledger write. The configured model is never changed.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import math
import os
import re
import sqlite3
import sys
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from corpdisc.agents.extraction import ExtractionAgent  # noqa: E402
from corpdisc.agents.gateway import CircuitBreaker, GatewayPolicy, LLMGateway  # noqa: E402
from corpdisc.agents.providers import build_provider_from_settings  # noqa: E402
from corpdisc.agents.runner import AgentRunner, RunnerLimits  # noqa: E402
from corpdisc.agents.ports import Message, ProviderRequest  # noqa: E402
from corpdisc.app import make_limiter, provider_settings_for_run  # noqa: E402
from corpdisc.clock import SystemClock, iso_utc  # noqa: E402
from corpdisc.config import Settings, load_settings  # noqa: E402
from corpdisc.contracts.agent_io import EntityCard, ExtractionInput  # noqa: E402
from corpdisc.contracts.agent_io import PageExcerpt  # noqa: E402
from corpdisc.contracts.agents import AgentName, AgentRequest, CallLimits, Meta  # noqa: E402
from corpdisc.db.connection import connect_writer, init_db  # noqa: E402
from corpdisc.db.repos.audit import SqliteAuditSink  # noqa: E402
from corpdisc.models.domain import EntityType, PageType, SourceClass  # noqa: E402
from corpdisc.services.budget import BudgetGuard, BudgetLimits  # noqa: E402
from corpdisc.services.llm_http import RequestsJsonTransport  # noqa: E402
from corpdisc.services.quotes import QuoteVerifier, normalize_for_match  # noqa: E402
from corpdisc.transport import HttpReply  # noqa: E402

START_TOTAL = 178  # tokens already spent by the quota probe
SOFT_CAP = 120_000  # this harness refuses to go past it
HARD_CAP = 150_000  # absolute project limit
MICRO_CAP = 20_000
MODEL_CAP = 25_000
MICRO_INTERVAL_S = 20.0  # benchmark only; production default is 90 s
MODELS_INTERVAL_S = 65.0  # 8K TPM
MICRO_REQUESTS = 6
MICRO_MAX_OUT = 20
GAP_TOLERANCE_S = 0.5
DEFAULT_MICRO_MODEL = "qwen/qwen3.8-27b"
DEFAULT_MODELS = ("qwen/qwen3.8-27b", "openai/gpt-oss-120b", "openai/gpt-oss-20b")
EXTRACTION_MAX_OUT = 3000
MAX_SAMPLE_TOKENS_IN = 4500
LEDGER_NAME = "slow_crawl_token_ledger.json"
PROTECTED_MARKERS = ("csi_coverage_test", "csi_hierarchy")
EXIT_OK, EXIT_USAGE, EXIT_STOPPED = 0, 2, 3


# ----------------------------------------------------------------------------------------------- path guards
class ProtectedPathError(RuntimeError):
    """A path outside the benchmark sandbox, or one of the protected databases, was used as a target."""


def is_protected(path: Path) -> bool:
    p = Path(path).resolve()
    low = [x.lower() for x in p.parts]
    if any(m in p.name.lower() for m in PROTECTED_MARKERS):
        return True
    return any(low[i] == "data" and low[i + 1] == "backups" for i in range(len(low) - 1))


def assert_benchmark_target(path: str | Path, bench_dir: Path) -> Path:
    """Only ``<bench_dir>/slow_crawl_*`` may be written. Protected databases are always refused."""
    p = Path(path).resolve()
    if is_protected(p):
        raise ProtectedPathError(f"refusing protected path: {p.name}")
    if p.parent != Path(bench_dir).resolve() or not p.name.lower().startswith("slow_crawl_"):
        raise ProtectedPathError(f"benchmark files must be {Path(bench_dir).name}/slow_crawl_*: {p.name}")
    return p


def assert_source_readable(path: str | Path) -> Path:
    p = Path(path).resolve()
    low = [x.lower() for x in p.parts]
    if "csi_hierarchy" in p.name.lower() or any(low[i] == "data" and low[i + 1] == "backups" for i in range(len(low) - 1)):
        raise ProtectedPathError(f"refusing to read protected path: {p.name}")
    if not p.is_file():
        raise FileNotFoundError(f"source database not found: {p}")
    return p


# ----------------------------------------------------------------------------------------------- token ledger
class LedgerError(RuntimeError):
    pass


class LedgerRefused(LedgerError):
    """A request could push the total past the cap; it was not sent."""


@dataclass
class Reservation:
    tokens: int
    label: str


class TokenLedger:
    """Persistent total of Groq tokens spent by all benchmarks. ``persist=False`` (dry run) never writes."""

    def __init__(self, path: Path, *, persist: bool = True, soft_cap: int = SOFT_CAP, hard_cap: int = HARD_CAP) -> None:
        self.path, self.persist = Path(path), persist
        self.soft_cap = min(soft_cap, hard_cap)
        self.hard_cap = hard_cap
        self.entries: list[dict[str, Any]] = []
        self.total = START_TOTAL
        if self.path.is_file():
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self.total = int(data["total_tokens"])
            self.entries = list(data.get("entries", []))
            if self.total < 0 or self.total > self.hard_cap:
                raise LedgerError(f"ledger total {self.total} is outside 0..{self.hard_cap}")
        self.start_total = self.total
        self._reserved = 0
        self._scope: tuple[str, int, int] | None = None  # name, cap, spent
        self._lock = threading.Lock()

    def remaining(self) -> int:
        left = self.soft_cap - self.total - self._reserved
        if self._scope is not None:
            _, cap, spent = self._scope
            left = min(left, cap - spent - self._reserved)
        return left

    def can_afford(self, tokens: int) -> bool:
        return tokens <= self.remaining()

    def reserve(self, tokens: int, label: str) -> Reservation:
        with self._lock:
            tokens = max(int(tokens), 0)
            if tokens > self.remaining():
                raise LedgerRefused(f"token ledger: {tokens} more tokens would pass the cap (remaining {self.remaining()})")
            self._reserved += tokens
            return Reservation(tokens, label)

    def settle(self, res: Reservation, actual: int) -> None:
        with self._lock:
            self._reserved -= res.tokens
            actual = max(int(actual), 0)
            self.total += actual
            if self._scope is not None:
                n, cap, spent = self._scope
                self._scope = (n, cap, spent + actual)
            self.entries.append({"at": iso_utc(datetime.now(UTC)), "label": res.label, "tokens": actual, "total": self.total})
            self._save()

    @contextlib.contextmanager
    def scope(self, name: str, cap: int):
        self._scope = (name, cap, 0)
        try:
            yield self
        finally:
            self._scope = None

    def scope_spent(self) -> int:
        return self._scope[2] if self._scope else 0

    def _save(self) -> None:
        if not self.persist:
            return
        tmp = self.path.with_name(self.path.name + ".tmp")
        payload = {"total_tokens": self.total, "start_total": START_TOTAL, "soft_cap": self.soft_cap, "hard_cap": self.hard_cap,
                   "entries": self.entries[-500:]}
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)

    def summary(self) -> dict[str, Any]:
        return {"path": self.path.name, "persisted": self.persist, "start_total": self.start_total, "end_total": self.total,
                "spent_this_run": self.total - self.start_total, "soft_cap": self.soft_cap, "hard_cap": self.hard_cap}


# ----------------------------------------------------------------------------------------------- clocks
class VirtualClock:
    """Dry-run clock: sleeping advances virtual time instantly."""

    def __init__(self) -> None:
        self._t0 = datetime(2026, 1, 1, tzinfo=UTC)
        self._mono = 1000.0

    def now(self) -> datetime:
        return self._t0 + timedelta(seconds=self._mono - 1000.0)

    def monotonic(self) -> float:
        return self._mono

    def sleep(self, seconds: float) -> None:
        if seconds > 0:
            self._mono += seconds


# ----------------------------------------------------------------------------------------------- transports
def estimate_body_tokens(body: str) -> int:
    """Worst-case tokens of one request: prompt size / 4 plus the completion cap."""
    try:
        data = json.loads(body)
        chars = sum(len(str(m.get("content", ""))) for m in data.get("messages", []))
        return math.ceil(chars / 4) + int(data.get("max_completion_tokens") or 0) + 50
    except (ValueError, AttributeError):
        return math.ceil(len(body) / 4) + 4000


def reply_tokens(reply: HttpReply) -> int | None:
    try:
        u = json.loads(reply.body).get("usage") or {}
        return int(u.get("prompt_tokens", 0) or 0) + int(u.get("completion_tokens", 0) or 0)
    except (ValueError, AttributeError, TypeError):
        return None


class LedgerTransport:
    """Innermost guard: reserves tokens before forwarding and settles actual usage after the reply."""

    def __init__(self, inner: Any, ledger: TokenLedger, label: str) -> None:
        self._inner, self._ledger, self._label = inner, ledger, label

    def post(self, url: str, *, headers: Any, body: str, timeout: float) -> HttpReply:
        est = estimate_body_tokens(body)
        res = self._ledger.reserve(est, self._label)  # LedgerRefused: nothing is sent
        try:
            reply = self._inner.post(url, headers=headers, body=body, timeout=timeout)
        except BaseException:
            self._ledger.settle(res, est)  # unknown outcome: charge the worst case
            raise
        if 200 <= reply.status < 300:
            used = reply_tokens(reply)
            self._ledger.settle(res, est if used is None else used)
        elif reply.status >= 500 or reply.status == 408:
            self._ledger.settle(res, est)
        else:
            self._ledger.settle(res, 0)  # 4xx incl. 429: refused before any model work
        return reply


class FaultInjectingTransport:
    """Returns a synthetic minute-scope 429 for one numbered call (counted from ``arm``); forwards everything else."""

    def __init__(self, inner: Any, retry_after_s: float = 20.0) -> None:
        self._inner, self._retry_after = inner, retry_after_s
        self._fail_on: int | None = None
        self._calls = 0
        self.injected = 0

    def arm(self, fail_on_call: int | None) -> None:
        self._fail_on, self._calls = fail_on_call, 0

    def post(self, url: str, *, headers: Any, body: str, timeout: float) -> HttpReply:
        self._calls += 1
        if self._fail_on is not None and self._calls == self._fail_on:
            self.injected += 1
            msg = ("Rate limit reached for model in organization on tokens per minute (TPM): Limit 8000, Used 7900, "
                   f"Requested 200. Please try again in {self._retry_after:.0f}s. (synthetic benchmark fault)")
            return HttpReply(429, {"retry-after": f"{self._retry_after:.0f}", "content-type": "application/json"},
                             json.dumps({"error": {"message": msg, "type": "tokens", "code": "rate_limit_exceeded"}}))
        return self._inner.post(url, headers=headers, body=body, timeout=timeout)


class RecordingTransport:
    """Outermost wrapper: start times, status codes and concurrency, measured on the injected clock."""

    def __init__(self, inner: Any, clock: Any) -> None:
        self._inner, self._clock = inner, clock
        self.starts: list[float] = []
        self.statuses: list[int] = []
        self.in_flight = self.max_in_flight = 0
        self._lock = threading.Lock()

    def post(self, url: str, *, headers: Any, body: str, timeout: float) -> HttpReply:
        with self._lock:
            self.in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)
            self.starts.append(self._clock.monotonic())
        try:
            reply = self._inner.post(url, headers=headers, body=body, timeout=timeout)
            self.statuses.append(reply.status)
            return reply
        finally:
            with self._lock:
                self.in_flight -= 1

    @property
    def http_429(self) -> int:
        return sum(1 for s in self.statuses if s == 429)


# ----------------------------------------------------------------------------------------------- dry-run fakes
SYNTH_PAGES: tuple[dict[str, Any], ...] = (
    {"page_id": 1, "text": "Acme Software Inc. is a wholly owned subsidiary of Example Holdings. "
                           "Birch Analytics Ltd. operates as a portfolio company of Example Holdings.",
     "entities": [("Acme Software Inc.", "Acme Software Inc. is a wholly owned subsidiary of Example Holdings."),
                  ("Birch Analytics Ltd.", "Birch Analytics Ltd. operates as a portfolio company of Example Holdings.")]},
    {"page_id": 2, "text": "Cedar Systems GmbH joined the Example Holdings group in 2019 as an operating business. "
                           "Delta Health Corp. is a business of Example Holdings.",
     "entities": [("Cedar Systems GmbH", "Cedar Systems GmbH joined the Example Holdings group in 2019 as an operating business."),
                  ("Delta Health Corp.", "Delta Health Corp. is a business of Example Holdings.")]},
    {"page_id": 3, "text": "Elm Logistics Pty is part of Example Holdings. Fir Payments LLC is a subsidiary of Example Holdings.",
     "entities": [("Elm Logistics Pty", "Elm Logistics Pty is part of Example Holdings."),
                  ("Fir Payments LLC", "Fir Payments LLC is a subsidiary of Example Holdings.")]},
)


def synthetic_inputs() -> list[ExtractionInput]:
    return [ExtractionInput(
        parent=EntityCard(name="Example Holdings"),
        page=PageExcerpt(page_id=p["page_id"], url=f"https://example.test/p{p['page_id']}", page_type=PageType.PORTFOLIO,
                         source_class=SourceClass.FIRST_PARTY_PARENT, text=p["text"]),
        chunk_index=0, chunk_count=1, dom_candidates=[], links=[], allowed_entity_types=[EntityType.COMPANY],
    ) for p in SYNTH_PAGES]


def _entity_json(i: int, name: str, quote: str, page_id: int) -> dict[str, Any]:
    return {"local_ref": f"e{i}", "name": name, "kind": "COMPANY", "is_corporate_entity": True,
            "claim": {"page_id": page_id, "quote": quote, "supports": "EXISTENCE"}, "agent_confidence": 0.9, "reason": "listed"}


class FakeGroqTransport:
    """Deterministic stand-in for the Groq endpoint. No network. ``gpt-oss-20b`` fails the schema on page 2;
    ``gpt-oss-120b`` adds one ungrounded company on page 1."""

    def __init__(self) -> None:
        self.calls = 0

    def post(self, url: str, *, headers: Any, body: str, timeout: float) -> HttpReply:
        self.calls += 1
        data = json.loads(body)
        model = data.get("model", "")
        user = next(m["content"] for m in data["messages"] if m["role"] == "user")
        if "ping" in user and "page" not in user:
            content, p_tok, c_tok = '{"ok": true}', 70, 6
        else:
            page_id = json.loads(user)["page"]["page_id"] if user.lstrip().startswith("{") else 1
            spec = next(p for p in SYNTH_PAGES if p["page_id"] == page_id)
            if model.endswith("gpt-oss-20b") and page_id == 2:
                content = '{"entities": "not a list"}'
            else:
                ents = [_entity_json(i, n, q, page_id) for i, (n, q) in enumerate(spec["entities"])]
                if model.endswith("gpt-oss-120b") and page_id == 1:
                    ents.append(_entity_json(9, "Ghost Industries", "Ghost Industries is owned by Example Holdings entirely.", page_id))
                content = json.dumps({"entities": ents, "rejected": [], "page_has_ownership_language": True, "agent_confidence": 0.8})
            p_tok, c_tok = 900, max(len(content) // 4, 1)
        headers_out = {"x-ratelimit-limit-tokens": "8000", "x-ratelimit-remaining-tokens": "7000",
                       "x-ratelimit-limit-requests": "1000", "x-ratelimit-remaining-requests": "990"}
        reply = {"choices": [{"message": {"content": content}, "finish_reason": "stop"}],
                 "usage": {"prompt_tokens": p_tok, "completion_tokens": c_tok}, "model": model}
        return HttpReply(200, headers_out, json.dumps(reply))


# ----------------------------------------------------------------------------------------------- context
@dataclass
class Context:
    mode: str
    dry_run: bool
    bench_dir: Path
    settings: Settings
    clock: Any
    ledger: TokenLedger
    stamp: str
    inner_transport: Any
    api_key: str
    secrets: list[str] = field(default_factory=list)
    close: list[Any] = field(default_factory=list)


def new_db(ctx: Context, name: str) -> tuple[Path, sqlite3.Connection, int]:
    path = assert_benchmark_target(ctx.bench_dir / f"slow_crawl_{name}_{ctx.stamp}.db", ctx.bench_dir)
    if path.exists():
        raise ProtectedPathError(f"refusing to reuse an existing database: {path.name}")
    init_db(path)
    conn = connect_writer(path)
    cur = conn.execute(
        "INSERT INTO runs(root_name, root_url, root_code, config_json, status, started_at, llm_token_budget) "
        "VALUES (?,?,?,?,?,?,?)",
        ("Slow crawl benchmark", "https://example.test/", "SCB", "{}", "RUNNING", iso_utc(ctx.clock.now()), None))
    conn.commit()
    return path, conn, int(cur.lastrowid)


def benchmark_settings(ctx: Context, db: Path, interval: float, model: str) -> Settings:
    return ctx.settings.model_copy(update={
        "slow_crawl": True, "slow_crawl_interval_seconds": interval, "db_path": db, "llm_provider": "groq",
        "groq_model": model, "allow_insecure_ssl": False})


def build_gateway(ctx: Context, settings: Settings, conn: sqlite3.Connection, model: str, limiter: Any, transport: Any,
                  budget: Any) -> LLMGateway:
    provider = build_provider_from_settings(provider_settings_for_run(settings, run_provider="groq", run_model=model),
                                            transport=transport)
    return LLMGateway(provider, model=model, policy=GatewayPolicy.from_settings(settings), breaker=CircuitBreaker(5),
                      budget=budget, clock=ctx.clock, sleeper=ctx.clock, limiter=limiter)


def transport_chain(ctx: Context, label: str, *, fault: bool = False) -> tuple[RecordingTransport, FaultInjectingTransport | None]:
    inner: Any = LedgerTransport(ctx.inner_transport, ctx.ledger, label)
    fi = None
    if fault:
        fi = FaultInjectingTransport(inner)
        inner = fi
    return RecordingTransport(inner, ctx.clock), fi


# ----------------------------------------------------------------------------------------------- micro mode
PING_SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}


def _gaps(starts: list[float]) -> list[float]:
    return [round(b - a, 3) for a, b in zip(starts, starts[1:])]


def run_micro(ctx: Context, model: str, interval: float) -> dict[str, Any]:
    db, conn, _run = new_db(ctx, "micro")
    try:
        settings = benchmark_settings(ctx, db, interval, model)
        limiter = make_limiter(settings, conn, ctx.clock, ctx.clock)
        rec, fault = transport_chain(ctx, "micro", fault=True)
        gateway = build_gateway(ctx, settings, conn, model, limiter, rec, None)
        preq = ProviderRequest(model=model, system='Reply with the JSON object {"ok": true} and nothing else.',
                               messages=(Message("user", "ping"),), response_schema=PING_SCHEMA, schema_name="Ping",
                               temperature=0.0, max_output_tokens=MICRO_MAX_OUT, reasoning="none")
        items: list[dict[str, Any]] = []
        stopped: str | None = None
        t0 = ctx.clock.monotonic()
        with ctx.ledger.scope("micro", MICRO_CAP):
            fault.arm(None)
            for n in range(1, MICRO_REQUESTS + 4):
                if n == MICRO_REQUESTS + 1:
                    fault.arm(3)  # the third request of the fault phase gets the synthetic 429
                if not ctx.ledger.can_afford(estimate_body_tokens(json.dumps({"messages": [{"content": preq.system + "ping"}],
                                                                              "max_completion_tokens": MICRO_MAX_OUT})) + 250):
                    stopped = "token ledger cap reached before request %d" % n
                    break
                before = len(rec.starts)
                try:
                    res = gateway.complete(preq, max_requests=4)
                except LedgerRefused as exc:
                    stopped = str(exc)
                    break
                r = res.response
                items.append({"n": n, "phase": "plain" if n <= MICRO_REQUESTS else "fault", "ok": r is not None,
                              "provider_requests": len(rec.starts) - before, "requests_used": res.requests_used,
                              "tokens_in": r.usage.tokens_in if r else 0, "tokens_out": r.usage.tokens_out if r else 0,
                              "latency_ms": res.latency_ms, "transport_errors": list(res.transport_errors),
                              "failure": res.failure.message if res.failure else None,
                              "started_at_s": round(rec.starts[before] - t0, 3) if len(rec.starts) > before else None})
        gaps = _gaps(rec.starts)
        fault_item = next((i for i in items if i["phase"] == "fault" and i["provider_requests"] > 1), None)
        plain_tokens = [i["tokens_in"] + i["tokens_out"] for i in items]
        resume_wait = None
        if 429 in rec.statuses:
            k = rec.statuses.index(429)
            if k + 1 < len(rec.starts):
                resume_wait = round(rec.starts[k + 1] - rec.starts[k], 3)
        results = {
            "interval_s": interval, "requests": items, "start_gaps_s": gaps,
            "min_gap_s": min(gaps) if gaps else None,
            "gaps_respect_interval": bool(gaps) and min(gaps) >= interval - GAP_TOLERANCE_S, "gap_tolerance_s": GAP_TOLERANCE_S,
            "max_in_flight": rec.max_in_flight, "one_in_flight": rec.max_in_flight <= 1,
            "each_request_within_120_tokens": all(t <= 120 for t in plain_tokens) if plain_tokens else None,
            "fault_injection": {"synthetic_429_injected": fault.injected, "retry_after_s": 20, "scope": "minute",
                                "request_recovered": bool(fault_item and fault_item["ok"]),
                                "wait_before_retry_s": resume_wait,
                                "wait_respects_retry_after": resume_wait is not None and resume_wait >= 20 - GAP_TOLERANCE_S,
                                "real_http_429": rec.http_429 - fault.injected},
            "limiter": limiter.snapshot(),
        }
        return {"results": results, "stopped_reason": stopped, "db": db.name, "model": model}
    finally:
        conn.close()


# ----------------------------------------------------------------------------------------------- models mode
def make_readonly_copy(src: Path, dest: Path) -> None:
    """sqlite backup of ``src`` taken through a ``mode=ro`` connection; the original is never opened for write."""
    s = sqlite3.connect(src.resolve().as_uri() + "?mode=ro", uri=True)
    d = sqlite3.connect(dest)
    try:
        s.backup(d)
    finally:
        d.close()
        s.close()


def load_samples(copy: Path, n: int) -> list[dict[str, Any]]:
    """First ``n`` distinct pages with a stored successful INITIAL extraction that found at least one entity."""
    conn = sqlite3.connect(copy.resolve().as_uri() + "?mode=ro", uri=True)
    out: list[dict[str, Any]] = []
    seen: set[int] = set()
    try:
        rows = conn.execute("SELECT request_json, response_json, COALESCE(tokens_in,0) FROM agent_invocations "
                            "WHERE agent='extraction' AND status='OK' AND cache_hit=0 AND response_json IS NOT NULL "
                            "ORDER BY invocation_id").fetchall()
    finally:
        conn.close()
    for rq_json, rs_json, tok_in in rows:
        try:
            rq, rs = json.loads(rq_json), json.loads(rs_json)
            if rq.get("attempt_kind") != "INITIAL" or "reask_feedback" in rq.get("input", {}):
                continue
            inp = ExtractionInput.model_validate(rq["input"])
            stored = (rs.get("payload") or {}).get("entities") or []
        except (ValueError, KeyError, TypeError, AttributeError):
            continue
        if not stored or inp.page.page_id in seen or int(tok_in) > MAX_SAMPLE_TOKENS_IN:
            continue
        seen.add(inp.page.page_id)
        out.append({"input": inp, "stored": [(str(e.get("name", "")), str((e.get("claim") or {}).get("quote", ""))) for e in stored]})
        if len(out) >= n:
            break
    return out


def norm_name(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", normalize_for_match(name).lower()).strip()


def grounded_names(pairs: list[tuple[str, str]], page_text: str, verifier: QuoteVerifier) -> set[str]:
    return {norm_name(n) for n, q in pairs if n.strip() and verifier.check(q, page_text).verified}


def run_models(ctx: Context, models: list[str], interval: float, samples: list[dict[str, Any]],
               prices: dict[str, tuple[float, float]]) -> dict[str, Any]:
    db, conn, run_id = new_db(ctx, "models")
    verifier = QuoteVerifier()
    agent = ExtractionAgent()
    per_model: dict[str, Any] = {}
    emitted_grounded: dict[str, list[set[str]]] = {}
    stopped: str | None = None
    try:
        settings = benchmark_settings(ctx, db, interval, models[0])
        limiter = make_limiter(settings, conn, ctx.clock, ctx.clock)  # one limiter: the gap holds across models too
        budget = BudgetGuard(conn, run_id=run_id, limits=BudgetLimits())
        for model in models:
            rec, _ = transport_chain(ctx, model)
            gw = build_gateway(ctx, settings, conn, model, limiter, rec, budget)
            runner = AgentRunner(gateway=gw, audit=SqliteAuditSink(conn), cache=None, budget=budget, broker=None,
                                 limits=RunnerLimits.from_settings(settings), clock=ctx.clock,
                                 reasoning_overrides={AgentName.EXTRACTION: "none"})
            rows: list[dict[str, Any]] = []
            names_by_sample: list[set[str]] = []
            with ctx.ledger.scope(model, MODEL_CAP):
                for idx, s in enumerate(samples):
                    inp: ExtractionInput = s["input"]
                    est = math.ceil((len(agent.system_prompt()) + len(inp.model_dump_json())) / 4) + 1500 + EXTRACTION_MAX_OUT
                    if stopped or not ctx.ledger.can_afford(est):
                        stopped = stopped or f"token ledger cap reached before {model} sample {idx + 1}"
                        names_by_sample.append(set())
                        rows.append({"sample": idx + 1, "page_id": inp.page.page_id, "skipped": "ledger cap"})
                        continue
                    meta = Meta(run_id=run_id, task_id=None, correlation_id=f"{run_id}:-:bench{idx}", agent=agent.name,
                                schema_version=agent.schema_version, prompt_version=agent.prompt_version)
                    resp = runner.invoke(agent, AgentRequest[ExtractionInput](
                        meta=meta, limits=CallLimits(max_output_tokens=EXTRACTION_MAX_OUT), payload=inp))
                    valid = resp.payload is not None
                    ents = resp.payload.entities if valid else []
                    pairs = [(e.name, e.claim.quote) for e in ents]
                    g = grounded_names(pairs, inp.page.text, verifier)
                    corp = {norm_name(e.name) for e in ents if e.is_corporate_entity} & g
                    if resp.error and "LedgerRefused" in resp.error.message:
                        stopped = f"token ledger cap reached during {model} sample {idx + 1}"
                    names_by_sample.append(g)
                    rows.append({"sample": idx + 1, "page_id": inp.page.page_id, "status": resp.status.value, "schema_valid": valid,
                                 "error": resp.error.message[:160] if resp.error else None, "emitted": len(ents),
                                 "grounded": len(g), "grounded_corporate_entities": len(corp),
                                 "tokens_in": resp.usage.tokens_in, "tokens_out": resp.usage.tokens_out,
                                 "latency_ms": resp.usage.latency_ms})
                spent = ctx.ledger.scope_spent()
            emitted_grounded[model] = names_by_sample
            tin = sum(r.get("tokens_in", 0) for r in rows)
            tout = sum(r.get("tokens_out", 0) for r in rows)
            p_in, p_out = prices.get(model, (0.0, 0.0))
            run_rows = [r for r in rows if "skipped" not in r]
            per_model[model] = {
                "samples": rows, "samples_run": len(run_rows),
                "schema_valid_rate": (sum(1 for r in run_rows if r["schema_valid"]) / len(run_rows)) if run_rows else None,
                "emitted": sum(r["emitted"] for r in run_rows), "grounded": sum(r["grounded"] for r in run_rows),
                "grounded_corporate_entities": sum(r["grounded_corporate_entities"] for r in run_rows),
                "tokens_in": tin, "tokens_out": tout, "ledger_tokens": spent,
                "latency_ms": sum(r["latency_ms"] for r in run_rows), "http_429": rec.http_429,
                "cost_usd": round((tin * p_in + tout * p_out) / 1_000_000, 6),
                "cost_note": "prices set" if (p_in or p_out) else "no price configured (--price MODEL=IN,OUT per 1M tokens)"}
        # reference set per sample: grounded names of every model plus the stored qwen output
        stored_sets = [grounded_names(s["stored"], s["input"].page.text, verifier) for s in samples]
        for model, m in per_model.items():
            hits = total_ref = 0
            for i, g in enumerate(emitted_grounded[model]):
                ref = set(stored_sets[i]).union(*(emitted_grounded[o][i] for o in emitted_grounded))
                hits += len(g & ref)
                total_ref += len(ref)
            m["precision"] = (m["grounded"] / m["emitted"]) if m["emitted"] else None
            m["recall"] = (hits / total_ref) if total_ref else None
        return {"results": {"interval_s": interval, "sample_count": len(samples), "models": per_model,
                            "stored_qwen_grounded": sum(len(x) for x in stored_sets),
                            "definitions": {
                                "precision": "grounded emitted companies / emitted companies (quote found verbatim in the page)",
                                "recall": "grounded names / union of grounded names across models and the stored qwen output",
                                "grounded_corporate_entities": "grounded and is_corporate_entity; extraction only proposes, the "
                                                               "pipeline confirms ownership separately"}},
                "stopped_reason": stopped, "db": db.name, "models": models}
    finally:
        conn.close()


# ----------------------------------------------------------------------------------------------- driver
def scrub(text: str, secrets: list[str]) -> str:
    for s in secrets:
        if s:
            text = text.replace(s, "[REDACTED]")
    return text


def write_report(ctx: Context, body: dict[str, Any]) -> Path:
    path = assert_benchmark_target(ctx.bench_dir / f"slow_crawl_benchmark_{ctx.stamp}.json", ctx.bench_dir)
    report = {"schema_version": 1, "mode": ctx.mode, "dry_run": ctx.dry_run, "stamp": ctx.stamp,
              "groq_api_key_present": bool(ctx.api_key and not ctx.dry_run), "tls_verification": "default (unchanged)",
              "configured_model_changed": False, "ledger": ctx.ledger.summary(), **body}
    text = scrub(json.dumps(report, indent=2, default=str), ctx.secrets)
    path.write_text(text, encoding="utf-8")
    return path


def parse_prices(items: list[str]) -> dict[str, tuple[float, float]]:
    out: dict[str, tuple[float, float]] = {}
    for it in items:
        try:
            model, rest = it.split("=", 1)
            a, b = rest.split(",", 1)
            out[model.strip()] = (float(a), float(b))
        except ValueError:
            raise SystemExit(f"--price expects MODEL=IN,OUT (USD per 1M tokens): {it!r}") from None
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Slow crawl benchmark harness (see module docstring).")
    ap.add_argument("mode", choices=("micro", "models"))
    ap.add_argument("--dry-run", action="store_true", help="fake transport: no network, no ledger write")
    ap.add_argument("--bench-dir", type=Path, default=ROOT / "bench")
    ap.add_argument("--model", default=DEFAULT_MICRO_MODEL, help="micro mode model")
    ap.add_argument("--models", nargs="+", default=list(DEFAULT_MODELS), help="models mode model list")
    ap.add_argument("--interval", type=float, default=None, help="seconds between request starts (benchmark only)")
    ap.add_argument("--source-db", type=Path, default=ROOT / "bench" / "csi_coverage_test.db")
    ap.add_argument("--samples", type=int, default=3)
    ap.add_argument("--price", action="append", default=[], metavar="MODEL=IN,OUT")
    args = ap.parse_args(argv)
    bench = Path(args.bench_dir).resolve()
    try:
        bench.mkdir(parents=True, exist_ok=True)
        ledger_path = assert_benchmark_target(bench / LEDGER_NAME, bench)
        settings = load_settings(None if args.dry_run else ROOT / ".env")
        key = settings.groq_api_key.get_secret_value() if settings.groq_api_key else ""
        if args.dry_run:
            clock: Any = VirtualClock()
            inner: Any = FakeGroqTransport()
            key = key or "dry-run-key"
            settings = settings.model_copy(update={"groq_api_key": _secret(key)})
        else:
            if not key:
                print("GROQ_API_KEY is not set (environment or .env); nothing was sent.", file=sys.stderr)
                return EXIT_USAGE
            clock = SystemClock()
            inner = RequestsJsonTransport()  # verify_ssl defaults to True
        ledger = TokenLedger(ledger_path, persist=not args.dry_run)
        ctx = Context(mode=args.mode, dry_run=args.dry_run, bench_dir=bench, settings=settings, clock=clock, ledger=ledger,
                      stamp=datetime.now(UTC).strftime("%Y%m%d-%H%M%S"), inner_transport=inner, api_key=key, secrets=[key])
        print(f"slow-crawl benchmark: mode={args.mode} dry_run={args.dry_run} groq_api_key_present={bool(key and not args.dry_run)} "
              f"ledger_total={ledger.total}/{ledger.soft_cap}")
        if args.mode == "micro":
            body = run_micro(ctx, args.model, args.interval or MICRO_INTERVAL_S)
        else:
            body = run_models_mode(ctx, args)
        path = write_report(ctx, body)
        r = body["results"]
        if args.mode == "micro":
            print(f"micro: min gap {r['min_gap_s']} s (interval {r['interval_s']} s), max in flight {r['max_in_flight']}, "
                  f"429 recovered={r['fault_injection']['request_recovered']}")
        else:
            for m, v in r["models"].items():
                print(f"{m}: valid={v['schema_valid_rate']} emitted={v['emitted']} grounded={v['grounded']} "
                      f"tokens={v['tokens_in'] + v['tokens_out']} 429={v['http_429']}")
        print(f"ledger: {ledger.total} tokens in total{" (dry run, not saved)" if args.dry_run else ""}; report: {path.name}")
        if body.get("stopped_reason"):
            print(f"stopped: {body['stopped_reason']}", file=sys.stderr)
            return EXIT_STOPPED
        return EXIT_OK
    except (ProtectedPathError, LedgerError, FileNotFoundError) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return EXIT_USAGE


def _secret(value: str) -> Any:
    from pydantic import SecretStr

    return SecretStr(value)


def run_models_mode(ctx: Context, args: argparse.Namespace) -> dict[str, Any]:
    interval = args.interval or MODELS_INTERVAL_S
    prices = parse_prices(args.price)
    if ctx.dry_run:
        inputs = synthetic_inputs()
        samples = [{"input": i, "stored": list(p["entities"])} for i, p in zip(inputs, SYNTH_PAGES)]
    else:
        src = assert_source_readable(args.source_db)
        copy = assert_benchmark_target(ctx.bench_dir / f"slow_crawl_models_source_{ctx.stamp}.db", ctx.bench_dir)
        make_readonly_copy(src, copy)
        samples = load_samples(copy, args.samples)
        if not samples:
            raise FileNotFoundError("no stored extraction inputs with entities were found in the copy")
    return run_models(ctx, list(args.models), interval, samples, prices)


if __name__ == "__main__":
    raise SystemExit(main())
