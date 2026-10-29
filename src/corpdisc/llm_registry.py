"""LLM Provider/Model Registry for runtime selection.

This module provides a clean registry of available LLM providers and models
with their configuration status, enabling interactive selection at runtime.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping


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
    def label(self) -> str:
        """Human-readable label for selection."""
        return f"{self.display_name} — {self.model_name}"


@dataclass
class LLMRegistry:
    """Registry of available LLM providers and models."""

    models: list[LLMModel] = field(default_factory=list)

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
        """Add a model to the registry."""
        model = LLMModel(
            provider_id=provider_id,
            display_name=display_name,
            model_name=model_name,
            is_local=is_local,
            is_available=is_available,
            availability_reason=availability_reason,
            credential_required=credential_required,
            configured=configured,
        )
        self.models.append(model)
        return model

    def get_by_index(self, index: int) -> LLMModel | None:
        """Get model by 1-based index (for interactive selection)."""
        if 1 <= index <= len(self.models):
            return self.models[index - 1]
        return None

    def get_by_provider_and_model(self, provider_id: str, model_name: str) -> LLMModel | None:
        """Get model by provider ID and model name."""
        for m in self.models:
            if m.provider_id == provider_id and m.model_name == model_name:
                return m
        return None

    def filter_by_provider(self, provider_id: str) -> list[LLMModel]:
        """Get all models for a specific provider."""
        return [m for m in self.models if m.provider_id == provider_id]

    @property
    def available_count(self) -> int:
        """Count of models that are both configured and available."""
        return sum(1 for m in self.models if m.configured and m.is_available)

    @property
    def configured_count(self) -> int:
        """Count of configured models (regardless of availability)."""
        return sum(1 for m in self.models if m.configured)


def build_registry(settings) -> LLMRegistry:
    """Build the LLM registry from settings.

    Args:
        settings: The application settings object.

    Returns:
        LLMRegistry with all configured models and their availability status.
    """
    registry = LLMRegistry()

    # Check for Gemini
    if settings.gemini_api_key:
        registry.add(
            provider_id="gemini",
            display_name="Gemini",
            model_name=settings.gemini_model or "gemini-3.8-flash",
            is_local=False,
            is_available=True,
            credential_required=True,
            configured=True,
        )
    else:
        registry.add(
            provider_id="gemini",
            display_name="Gemini",
            model_name=settings.gemini_model or "gemini-3.8-flash",
            is_local=False,
            is_available=False,
            availability_reason="GEMINI_API_KEY not configured",
            credential_required=True,
            configured=False,
        )

    # Check for Groq - use working model as default
    if settings.groq_api_key:
        registry.add(
            provider_id="groq",
            display_name="Groq",
            model_name=settings.groq_model or "openai/gpt-oss-120b",
            is_local=False,
            is_available=True,
            credential_required=True,
            configured=True,
        )
    else:
        registry.add(
            provider_id="groq",
            display_name="Groq",
            model_name=settings.groq_model or "openai/gpt-oss-120b",
            is_local=False,
            is_available=False,
            availability_reason="GROQ_API_KEY not configured",
            credential_required=True,
            configured=False,
        )

    # Ollama - always available (local)
    registry.add(
        provider_id="ollama",
        display_name="Ollama",
        model_name=settings.llm_model or "qwen3:4b",
        is_local=True,
        is_available=True,
        credential_required=False,
        configured=True,
    )

    # Add secondary Ollama model for flexibility
    registry.add(
        provider_id="ollama",
        display_name="Ollama",
        model_name="mistral:latest",
        is_local=True,
        is_available=True,
        credential_required=False,
        configured=True,
    )

    return registry


def format_registry(registry: LLMRegistry, *, show_unavailable: bool = False) -> str:
    """Format the registry for display.

    Args:
        registry: The LLM registry to format.
        show_unavailable: Whether to show unavailable models.

    Returns:
        Formatted string suitable for console display.
    """
    lines = ["Available LLMs:"]

    for i, model in enumerate(registry.models, start=1):
        if not show_unavailable and not (model.configured and model.is_available):
            continue

        status_parts = []
        if model.configured and model.is_available:
            status_parts.append("AVAILABLE")
        elif not model.configured:
            status_parts.append("NOT CONFIGURED")
        elif not model.is_available:
            status_parts.append("UNAVAILABLE")

        status = f" [{', '.join(status_parts)}]" if status_parts else ""

        lines.append(f"  [{i}] {model.display_name}")
        lines.append(f"      Model: {model.model_name}")
        if not model.is_local:
            lines.append(f"      Type: Cloud")
        if not model.configured or not model.is_available:
            lines.append(f"      Reason: {model.availability_reason}")

    if not registry.models:
        lines.append("  No LLM providers configured.")

    return "\n".join(lines)
