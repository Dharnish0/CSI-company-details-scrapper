"""Synthetic CSI-style listing site (in-memory) + a recording transport that speaks Ollama's native /api/chat."""
from __future__ import annotations

import json
import random

from corpdisc.transport import HttpReply, TransportFailure
from tests.helpers.fakes import FakeHttpClient, resp

ROOT = "Constellation Software Inc."
_A = ["Aurora", "Brightline", "Cobalt", "Delta", "Echo", "Foxtrot", "Golf", "Harbor", "Indigo", "Juniper", "Kestrel", "Lumen", "Meridian", "Nimbus", "Orchid"]
_B = ["Health", "Logistics", "Analytics", "Software", "Systems", "Media", "Robotics", "Finance", "Energy", "Education", "Retail", "Transit"]
NAV = "<a href='/companies'>Our Companies</a><a href='/careers'>Careers</a>"


def names(n: int) -> list[str]:
    rnd, out = random.Random(7), []
    while len(out) < n:
        nm = f"{rnd.choice(_A)} {rnd.choice(_B)}"
        if nm not in out:
            out.append(nm)
    return out


def page(title: str, body: str, nav: str = "") -> str:
    return (f"<html><head><title>{title}</title></head><body><header><nav>{nav}</nav></header><main><h1>{title}</h1>{body}</main>"
            f"<footer><a href='/privacy'>Privacy</a></footer></body></html>")


def site(n_cards: int) -> FakeHttpClient:
    cards = "".join(f"<div class='card'><h3><a href='https://og{i}.test/'>{nm}</a></h3><p>{nm} provides vertical market software and services "
                    f"to customers across several industries, with offices in multiple countries and a long history of serving its clients.</p></div>"
                    for i, nm in enumerate(names(n_cards)))
    sents = "".join(f"<p>{nm} is an operating group of {ROOT}.</p>" for nm in names(n_cards))
    return FakeHttpClient({"https://root.test/": resp("https://root.test/", 200, page(ROOT, "<p>Welcome.</p>", NAV)),
                           "https://root.test/companies": resp("https://root.test/companies", 200, page("Our Operating Groups", cards + sents, NAV))})


class OllamaRecorder:
    """Transport that records native /api/chat bodies and answers with ``mode``: ok | garbage | timeout."""

    def __init__(self, mode: str = "ok"):
        self.mode, self.bodies = mode, []

    def post(self, url, *, headers, body, timeout):
        self.bodies.append(json.loads(body))
        if self.mode == "timeout":
            raise TransportFailure("TIMEOUT")
        text = "not json at all" if self.mode == "garbage" else json.dumps(
            {"entities": [], "rejected": [], "page_has_ownership_language": False, "agent_confidence": 0.5})
        return HttpReply(200, {}, json.dumps({"model": "qwen3:4b", "message": {"role": "assistant", "content": text}, "done": True, "done_reason": "stop",
                                              "prompt_eval_count": 100, "eval_count": 20, "total_duration": 10**9, "load_duration": 1, "prompt_eval_duration": 1, "eval_duration": 1}))
