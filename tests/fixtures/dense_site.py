"""Fixture world for defect C: a root listing page long enough to need splitting / several chunks. Fictional names only.

Each company is a card, filler lines and its ownership sentence, kept together so a split page keeps every company whole.
``ScriptedProvider`` wraps the fixture brain to emulate output-cap truncation (finish_reason=length) and 429 outages.
"""
from __future__ import annotations

import json

from corpdisc.agents.ports import ProviderResponse, ProviderTransportError
from tests.fixtures import site as base
from tests.helpers.fakes import FakeHttpClient, resp

ROOT = base.ROOT
FILLER = ("This business unit serves customers with specialised vertical market software, support and long term "
          "maintenance contracts across many regions and industries. ")


# distinct fictional words: names that differ only by a digit would be (correctly) flagged as ambiguous by the name matcher
_WORDS = ("Aldebaran Bellatrix Canopus Deneb Elnath Fomalhaut Gacrux Hadar Izar Jabbah Kochab Larawag Mirfak Naos Okab "
          "Peacock Quasar Rigel Saiph Tarazed Unukalhai Vindemiatrix Wezen Xamidimura Yildun Zosma Acrux Biham Capella "
          "Diphda Enif Furud Girtab Heze Imai Jishui Kraz Lesath Menkar Nunki").split()


def names(n: int) -> list[str]:
    assert n <= len(_WORDS)
    return [f"{w} Labs" for w in _WORDS[:n]]


def sentence(name: str) -> str:
    return f"{name} is an operating group of {ROOT}."


def routes(n: int, filler_lines: int) -> dict:
    r: dict = {}
    r["https://root.test/"] = resp("https://root.test/", 200, base.page(ROOT, "<p>Welcome.</p>", nav=base.NAV))
    body = ""
    for i, nm in enumerate(names(n)):
        body += base.card(nm, f"https://dense{i:02d}.test/")
        body += "".join(f"<p>{FILLER}{nm} line {k}.</p>" for k in range(filler_lines))
        body += f"<p>{sentence(nm)}</p>"
    r["https://root.test/companies"] = resp("https://root.test/companies", 200,
                                           base.page("Our Operating Groups", body, nav=base.NAV))
    for i, nm in enumerate(names(n)):
        u = f"https://dense{i:02d}.test/"
        r[u] = resp(u, 200, base.page(nm, f"<p>{nm} home.</p>"))
    return r


def install(monkeypatch, n: int, filler_lines: int) -> FakeHttpClient:
    for nm in names(n):
        monkeypatch.setitem(base.S, f"o_{nm}", sentence(nm))
        monkeypatch.setitem(base.REL, (ROOT, nm), ("SUPPORTED", "Part of", "Operating Group", f"o_{nm}", 0.9))
    monkeypatch.setitem(base.EXTRACT, "https://root.test/companies",
                        [base._ent(nm, etype="Operating Group", key=f"o_{nm}") for nm in names(n)])
    return FakeHttpClient(routes(n, filler_lines))


class ScriptedProvider:
    """Delegates to the fixture brain, except extraction calls selected by ``truncate`` / ``down`` (both take the payload dict)."""

    name = "scripted"

    def __init__(self, inner, *, truncate=None, down=None) -> None:
        self.inner, self.truncate, self.down = inner, truncate, down
        self.extractions: list[dict] = []  # payloads of every extraction request that reached the provider

    def complete(self, request):
        if request.schema_name == "ExtractionOutput":
            payload, _ = json.JSONDecoder().raw_decode(request.messages[0].content)
            self.extractions.append(payload)
            if self.down is not None and self.down(payload):
                raise ProviderTransportError("429 rate limited", retryable=True, rate_limited=True, retry_after_s=None)
            if self.truncate is not None and self.truncate(payload):
                return ProviderResponse(content='{"entities": [{"local_ref": "e1", "name": "Ald', model="stub",
                                        finish_reason="length")
        return self.inner.complete(request)


def install_provider(world, **kw) -> ScriptedProvider:
    sp = ScriptedProvider(world.provider, **kw)
    world.deps.runner.gateway._provider = sp
    return sp


def names_in(payload: dict, all_names) -> int:
    text = payload["page"]["text"]
    return sum(1 for nm in all_names if nm in text)
