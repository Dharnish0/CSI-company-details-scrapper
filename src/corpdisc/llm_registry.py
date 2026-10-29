"""LLM provider/model registry for runtime selection.

Availability reflects reality: cloud providers are available when their key is configured, and an Ollama model is
listed as available only when the local server answers ``GET /api/tags`` and reports that model as installed.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

DEFAULT_OLLAMA_URL = "http://localhost:11434"
OLLAMA_PROBE_TIMEOUT_S = 2.0

Fetch = Callable[[str, float], tuple[int, Any]]  # (url, timeout) -> (http status, parsed JSON body)


@dataclass(frozen=True)
class OllamaProbe:
    """Result of asking a local Ollama server which models are installed."""

    reachable: bool
    models: tuple[str, ...] = ()
    error: str = ""
    base_url: str = DEFAULT_OLLAMA_URL


def _default_fetch(url: str, timeout: float) -> tuple[int, Any]:
    import requests  # local import: only needed when actually probing

    resp = requests.get(url, timeout=timeout)
    try:
        body = resp.json()
    except ValueError:
        body = None
    return resp.status_code, body


def discover_ollama(base_url: str = DEFAULT_OLLAMA_URL, timeout: float = OLLAMA_PROBE_TIMEOUT_S,
                    fetch: Fetch | None = None) -> OllamaProbe:
    """Query ``<base_url>/api/tags`` with a short timeout. Never raises."""
    base = (base_url or DEFAULT_OLLAMA_URL).rstrip("/")
    try:
        status, body = (fetch or _default_fetch)(f"{base}/api/tags", timeout)
    except Exception as exc:  # noqa: BLE001 - connection refused, timeout, DNS: all mean "not reachable"
        return OllamaProbe(False, (), f"{type(exc).__name__}", base)
    if status != 200 or not isinstance(body, dict):
        return OllamaProbe(False, (), f"unexpected response (HTTP {status})", base)
    names: list[str] = []
    for item in body.get("models") or []:
        name = item.get("name") or item.get("model") if isinstance(item, dict) else None
        if isinstance(name, str) and name and name not in names:
            names.append(name)
    return OllamaProbe(True, tuple(names), "", base)


def ollama_base_url(settings: Any) -> str:
    """LLM_BASE_URL applies to Ollama only when Ollama is the configured provider; otherwise OLLAMA_BASE_URL."""
    if (getattr(settings, "llm_provider", "") or "").strip().lower() == "ollama" and getattr(settings, "llm_base_url", ""):
        return settings.llm_base_url
    return getattr(settings, "ollama_base_url", "") or DEFAULT_OLLAMA_URL


def ollama_model_installed(probe: OllamaProbe, model: str) -> bool:
    """Exact tag match; a bare name ("qwen3") matches its ":latest" tag only."""
    wanted = model.strip()
    if not wanted:
        return False
    if ":" not in wanted:
        wanted += ":latest"
    return wanted in probe.models or model.strip() in probe.models


@dataclass(frozen=True)
class LLMModel:
    """Represents a configured LLM model."""
    provider_id: str
    display_name: str
    model_name: str
    is_local: bool
    is_available: bool
    availability_reason: str = ""
    credential_required: bool = False
    configured: bool = False

    @property
    def selectable(self) -> bool:
        return self.configured and self.is_available

    @property
    def label(self) -> str:
        """Human-readable label for selection."""
        return f"{self.display_name} — {self.model_name}"


@dataclass
class LLMRegistry:
    """Registry of available LLM providers and models."""

    models: list[LLMModel] = field(default_factory=list)
    ollama: OllamaProbe | None = None

    def add(
        self,
        provider_id: str,
        display_name: str,
        model_name: str,
        is_local: bool,
        is_available: bool = True,
        availability_reason: str = "",
        credential_required: bool = False,
        configured: bool = True,
    ) -> LLMModel:
        model = LLMModel(provider_id, display_name, model_name, is_local, is_available, availability_reason,
                         credential_required, configured)
        self.models.append(model)
        return model

    def selectable(self) -> list[LLMModel]:
        """Models a user may pick, in display order (the numbering used by the menu and by ``llms``)."""
        return [m for m in self.models if m.selectable]

    def get_by_index(self, index: int) -> LLMModel | None:
        """Get a SELECTABLE model by its 1-based menu number."""
        items = self.selectable()
        return items[index - 1] if 1 <= index <= len(items) else None

    def get_by_provider_and_model(self, provider_id: str, model_name: str) -> LLMModel | None:
        for m in self.models:
            if m.provider_id == provider_id and m.model_name == model_name:
                return m
        return None

    def filter_by_provider(self, provider_id: str) -> list[LLMModel]:
        return [m for m in self.models if m.provider_id == provider_id]

    @property
    def available_count(self) -> int:
        return len(self.selectable())

    @property
    def configured_count(self) -> int:
        return sum(1 for m in self.models if m.configured)


def build_registry(settings, *, ollama_probe: OllamaProbe | None = None, probe_ollama: bool = True) -> LLMRegistry:
    """Build the registry from settings and (optionally) a live Ollama probe.

    Ollama entries come only from the server's installed-model list. ``LLM_MODEL`` is never turned into an Ollama
    entry unless ``LLM_PROVIDER`` is ``ollama``, in which case a missing model is listed as unavailable.
    """
    registry = LLMRegistry()
    for pid, display, key, model, default in (
        ("gemini", "Gemini", settings.gemini_api_key, settings.gemini_model, "gemini-3.8-flash"),
        ("groq", "Groq", settings.groq_api_key, settings.groq_model, "openai/gpt-oss-120b"),
    ):
        env = f"{pid.upper()}_API_KEY"
        registry.add(pid, display, model or default, False, is_available=bool(key),
                     availability_reason="" if key else f"{env} not configured", credential_required=True,
                     configured=bool(key))

    url = ollama_base_url(settings)
    probe = ollama_probe if ollama_probe is not None else (discover_ollama(url) if probe_ollama else None)
    registry.ollama = probe
    if probe is None:
        return registry
    if not probe.reachable:
        registry.add("ollama", "Ollama", "(server unreachable)", True, is_available=False,
                     availability_reason=f"Ollama server not reachable at {probe.base_url or url} ({probe.error})",
                     configured=True)
        return registry
    for name in probe.models:
        registry.add("ollama", "Ollama", name, True, is_available=True, configured=True)
    wanted = (getattr(settings, "llm_model", "") or "").strip()
    if (settings.llm_provider or "").strip().lower() == "ollama" and wanted and not ollama_model_installed(probe, wanted):
        registry.add("ollama", "Ollama", wanted, True, is_available=False,
                     availability_reason=f"not installed (run: ollama pull {wanted})", configured=True)
    if not probe.models:
        registry.add("ollama", "Ollama", "(no models installed)", True, is_available=False,
                     availability_reason="Ollama is running but has no models installed (run: ollama pull <model>)",
                     configured=True)
    return registry


def format_registry(registry: LLMRegistry, *, show_unavailable: bool = False) -> str:
    """Format the registry. Selectable models carry the menu number [n]; unavailable ones are marked [-] with a reason."""
    lines = ["Available LLMs:"]
    number = 0
    for model in registry.models:
        if model.selectable:
            number += 1
            lines.append(f"  [{number}] {model.display_name}")
        elif show_unavailable:
            lines.append(f"  [-] {model.display_name} (not selectable)")
        else:
            continue
        lines.append(f"      Model: {model.model_name}")
        if not model.is_local:
            lines.append("      Type: Cloud")
        if not model.selectable:
            lines.append(f"      Reason: {model.availability_reason}")
    if number == 0:
        lines.append("  No LLM is configured and available.")
    p = registry.ollama
    if p is not None:
        if p.reachable:
            lines.append(f"Ollama server {p.base_url}: reachable, {len(p.models)} model(s) installed"
                         + (f" ({', '.join(p.models)})" if p.models else ""))
        else:
            lines.append(f"Ollama server {p.base_url}: UNREACHABLE ({p.error})")
    return "\n".join(lines)
