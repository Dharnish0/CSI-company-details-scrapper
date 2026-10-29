#!/usr/bin/env python
"""List available Groq models."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import httpx

from corpdisc.config import Settings


def main():
    print("=" * 80)
    print("Groq Available Models")
    print("=" * 80)
    print()

    settings = Settings(_env_file=".env")
    api_key = settings.groq_api_key.get_secret_value() if settings.groq_api_key else None
    if not api_key:
        print("ERROR: GROQ_API_KEY not set")
        return 1

    print(f"API Key (last 4): {api_key[-4:]}")
    print()

    # Try the models list endpoint
    url = "https://api.groq.com/openai/v1/models"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    try:
        response = httpx.get(url, headers=headers, timeout=30)
        print(f"Status: {response.status_code}")
        print()

        if response.status_code == 200:
            data = response.json()
            print("Available models:")
            for model in data.get("data", []):
                model_id = model.get("id", "unknown")
                print(f"  - {model_id}")
            print()
            print(f"Total models: {len(data.get('data', []))}")
        else:
            print(f"Response: {response.text[:1000]}")

    except Exception as e:
        print(f"Error: {type(e).__name__}: {e}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
