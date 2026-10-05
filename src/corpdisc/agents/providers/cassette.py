"""Record/replay providers (plan 4.M2.4). Replay never falls through to a live provider."""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from dataclasses import asdict
from pathlib import Path

from corpdisc.agents.ports import LLMProvider, ProviderRequest, ProviderResponse, ProviderTransportError
from corpdisc.agents.ports import ProviderUsage, ToolCallRequest


def request_key(req: ProviderRequest) -> str:
    doc = {
        "model": req.model, "temperature": req.temperature, "max_output_tokens": req.max_output_tokens,
        "schema_name": req.schema_name, "system": req.system,
        "messages": [[m.role, m.content] for m in req.messages], "tools": [t.name for t in req.tools],
    }  # fmt: skip
    return hashlib.sha256(json.dumps(doc, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _to_doc(resp: ProviderResponse) -> dict:
    return {"content": resp.content, "tool_calls": [asdict(t) for t in resp.tool_calls],
            "usage": asdict(resp.usage), "model": resp.model}


def _from_doc(doc: dict) -> ProviderResponse:
    return ProviderResponse(content=doc.get("content"),
                            tool_calls=tuple(ToolCallRequest(**t) for t in doc.get("tool_calls", [])),
                            usage=ProviderUsage(**doc.get("usage", {})), model=doc.get("model", ""))


class RecordingProvider:
    def __init__(self, inner: LLMProvider, cassette_path: Path) -> None:
        self._inner = inner
        self._path = Path(cassette_path)
        self.name = f"record({inner.name})"

    def complete(self, request: ProviderRequest) -> ProviderResponse:
        resp = self._inner.complete(request)  # errors propagate and are not recorded
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"key": request_key(request), "response": _to_doc(resp)}, ensure_ascii=False) + "\n")
        return resp


class ReplayProvider:
    name = "replay"

    def __init__(self, cassette_path: Path) -> None:
        self._entries: dict[str, list[dict]] = defaultdict(list)
        path = Path(cassette_path)
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1) if path.exists() else []:
            if not line.strip():
                continue
            try:
                doc = json.loads(line)
                self._entries[doc["key"]].append(doc["response"])
            except (ValueError, KeyError) as exc:
                raise ValueError(f"corrupt cassette line {n} in {path.name}") from exc

    def complete(self, request: ProviderRequest) -> ProviderResponse:
        queue = self._entries.get(request_key(request))
        if not queue:
            raise ProviderTransportError("cassette miss", retryable=False)
        doc = queue.pop(0) if len(queue) > 1 else queue[0]
        return _from_doc(doc)
