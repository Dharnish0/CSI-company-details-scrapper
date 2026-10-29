#!/usr/bin/env python
"""Debug Groq API request to see exact response."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import httpx
import json

from corpdisc.config import Settings


def main():
    print("=" * 80)
    print("Groq API Debug Test")
    print("=" * 80)
    print()

    settings = Settings(_env_file=".env")
    api_key = settings.groq_api_key.get_secret_value() if settings.groq_api_key else None
    if not api_key:
        print("ERROR: GROQ_API_KEY not set")
        return 1

    print(f"API Key (last 4): {api_key[-4:]}")
    print()

    # Try with the deprecated llama-3.3-70b-versatile to see if it gives different error
    models_to_try = [
        "llama-3.3-70b-versatile",
        "llama-3-groq-70b-tool-use",
        "qwen/qwen-2.5-32b",
    ]

    for model in models_to_try:
        print(f"Testing model: {model}")
        print("-" * 40)

        url = "https://api.groq.com/openai/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        body = {
            "model": model,
            "messages": [
                {"role": "system", "content": "You are a helpful assistant."},
                {"role": "user", "content": "Say hello"}
            ],
            "max_completion_tokens": 100,
            "temperature": 0.0,
        }

        try:
            response = httpx.post(url, headers=headers, json=body, timeout=30)
            print(f"Status: {response.status_code}")
            print(f"Response: {response.text[:500]}")
            print()
        except Exception as e:
            print(f"Error: {type(e).__name__}: {e}")
            print()

    return 0


if __name__ == "__main__":
    sys.exit(main())
