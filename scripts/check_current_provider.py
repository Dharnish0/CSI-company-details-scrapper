#!/usr/bin/env python
"""Check which provider is currently configured."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from corpdisc.config import Settings

settings = Settings(_env_file=".env")

print("Current Configuration:")
print(f"  LLM Provider: {settings.llm_provider}")
print(f"  LLM Model: {settings.llm_model}")
print(f"  Gemini Model: {settings.gemini_model}")
print(f"  Gemini API Key: {'***' + settings.gemini_api_key.get_secret_value()[-4:] if settings.gemini_api_key else 'not set'}")
print(f"  Groq Model: {settings.groq_model}")
print(f"  Groq API Key: {'***' + settings.groq_api_key.get_secret_value()[-4:] if settings.groq_api_key else 'not set'}")
print()

if settings.llm_provider == "groq":
    print("CURRENT PROVIDER: Groq")
    print(f"CURRENT MODEL: {settings.groq_model}")
elif settings.llm_provider == "gemini":
    print("CURRENT PROVIDER: Gemini")
    print(f"CURRENT MODEL: {settings.gemini_model}")
elif settings.llm_provider == "ollama":
    print("CURRENT PROVIDER: Ollama")
    print(f"CURRENT MODEL: {settings.llm_model}")
else:
    print(f"CURRENT PROVIDER: {settings.llm_provider or 'unconfigured'}")
