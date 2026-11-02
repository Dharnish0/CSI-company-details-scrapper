"""A scripted stand-in for api.groq.com behind the real ``GroqProvider``.

``FakeGroq`` is a ``JsonTransport``: it answers chat completions from the fixture ``Brain`` (so the crawl still produces the
fixture hierarchy), adds ``x-ratelimit-*`` headers, records the monotonic start time of every request and lets a test
inject HTTP 429 (and other) replies. Nothing here touches the network.
"""
from __future__ import annotations

import json
from collections.abc import Callable
from types import SimpleNamespace

from corpdisc.agents.providers.groq import GroqProvider
from corpdisc.transport import HttpReply

_SCHEMA_MARK = "validates against this JSON Schema:\n"
_BY_PROPERTY = {"assessments": "RelationshipOutput", "entities": "ExtractionOutput", "selections": "DiscoveryOutput",
                "verdicts": "DuplicateOutput", "judgements": "ValidationOutput", "basis": "WebsiteOutput"}

Fault = Callable[[int, str], "HttpReply | None"]  # (request index from 1, schema name) -> reply to send instead, or None


def rate_limited(retry_after: str | float, message: str, *, remaining_tokens: int = 0) -> HttpReply:
    """An HTTP 429 as Groq sends it: error body plus ``retry-after`` (seconds, or '9m35s') and rate-limit headers."""
    return HttpReply(429, {"retry-after": str(retry_after), "x-ratelimit-limit-tokens": "8000",
                           "x-ratelimit-remaining-tokens": str(remaining_tokens), "x-ratelimit-limit-requests": "1000",
                           "x-ratelimit-remaining-requests": "900"},
                     json.dumps({"error": {"message": message, "type": "tokens", "code": "rate_limit_exceeded"}}))


def minute_429(retry_after: str | float = 30) -> HttpReply:
    return rate_limited(retry_after, "Rate limit reached for model `m` in organization `org` service tier `on_demand` on "
                                     "tokens per minute (TPM): Limit 8000, Used 7900, Requested 1200.")


def day_429(retry_after: str | float = "9m35s") -> HttpReply:
    return rate_limited(retry_after, "Rate limit reached for model `m` in organization `org` service tier `on_demand` on "
                                     "tokens per day (TPD): Limit 200000, Used 199500, Requested 1800.")


def on_request(*indexes: int, reply: HttpReply) -> Fault:
    """Fault for the given request numbers (1-based, counting every request that reaches the provider)."""
    wanted = set(indexes)
    return lambda n, schema: reply if n in wanted else None


def on_schema(schema: str, reply: HttpReply, *, times: int | None = None, after: int = 0) -> Fault:
    """Fault for requests of one schema, ``times`` of them (None = all), starting after request number ``after``."""
    box = {"n": 0}

    def fault(n: int, name: str) -> HttpReply | None:
        if name != schema or n <= after or (times is not None and box["n"] >= times):
            return None
        box["n"] += 1
        return reply
    return fault


class FakeGroq:
    def __init__(self, brain, clock, *, limit_tokens: int = 8000, faults: list[Fault] | None = None,
                 on_post: Callable[[int, str], None] | None = None) -> None:
        self.brain, self.clock = brain, clock
        self.limit_tokens = limit_tokens
        self.faults: list[Fault] = list(faults or [])
        self.on_post = on_post  # called after the reply is built, before it is returned (e.g. to raise a shutdown request)
        self.starts: list[float] = []  # clock.monotonic() when each request reached the provider
        self.schemas: list[str] = []
        self.statuses: list[int] = []
        self.tokens: list[int] = []  # total tokens of every 200 reply, 0 for failures
        self.in_flight = 0
        self.peak_in_flight = 0

    # ------------------------------------------------------------------ JsonTransport
    def post(self, url: str, *, headers, body: str, timeout: float) -> HttpReply:  # noqa: ARG002
        self.in_flight += 1
        self.peak_in_flight = max(self.peak_in_flight, self.in_flight)
        try:
            data = json.loads(body)
            system, user = data["messages"][0]["content"], data["messages"][1]["content"]
            schema = self._schema_name(data, system)
            self.starts.append(self.clock.monotonic())
            self.schemas.append(schema)
            n = len(self.starts)
            for fault in self.faults:
                reply = fault(n, schema)
                if reply is not None:
                    self.statuses.append(reply.status)
                    self.tokens.append(0)
                    if self.on_post:
                        self.on_post(n, schema)
                    return reply
            out = self.brain(SimpleNamespace(schema_name=schema, messages=[SimpleNamespace(content=user)]))
            t_in, t_out = max(1, (len(system) + len(user)) // 4), max(1, len(out) // 4)
            reply = HttpReply(200, {"x-ratelimit-limit-tokens": str(self.limit_tokens),
                                    "x-ratelimit-remaining-tokens": str(max(self.limit_tokens - t_in - t_out, 0)),
                                    "x-ratelimit-limit-requests": "1000", "x-ratelimit-remaining-requests": "999",
                                    "x-ratelimit-reset-tokens": "2.5s", "x-ratelimit-reset-requests": "86.4s"},
                              json.dumps({"model": "m", "choices": [{"message": {"role": "assistant", "content": out},
                                                                      "finish_reason": "stop"}],
                                          "usage": {"prompt_tokens": t_in, "completion_tokens": t_out}}))
            self.statuses.append(200)
            self.tokens.append(t_in + t_out)
            if self.on_post:
                self.on_post(n, schema)
            return reply
        finally:
            self.in_flight -= 1

    @staticmethod
    def _schema_name(data: dict, system: str) -> str:
        fmt = data.get("response_format")
        if fmt:
            return fmt["json_schema"]["name"]
        schema = json.loads(system.split(_SCHEMA_MARK, 1)[1])  # tool turns carry no response_format: read the prompt's schema
        for prop, name in _BY_PROPERTY.items():
            if prop in schema.get("properties", {}):
                return name
        return "RecursionOutput"

    # ------------------------------------------------------------------ helpers for assertions
    def gaps(self, start: int = 0) -> list[float]:
        """Seconds between consecutive request starts, from request ``start`` (0-based) on."""
        s = self.starts[start:]
        return [b - a for a, b in zip(s, s[1:])]

    def provider(self, **kw) -> GroqProvider:
        return GroqProvider(api_key="test-key-not-real", transport=self, model_name="m", **kw)
