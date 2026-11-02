"""Tiny Groq quota probe: GET /models once, then at most ONE minimal chat completion per model.

Prints ONLY whitelisted rate-limit headers, usage and (for errors) the error message. Never prints the API key.
TLS verification stays on. Honors Retry-After by reporting it (this script never retries or loops).
Usage: python slow_crawl_quota_probe.py model1 [model2 ...]
"""
import json
import re
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[2]
key = None
for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
    if line.startswith("GROQ_API_KEY="):
        key = line.split("=", 1)[1].strip().strip('"').strip("'")
if not key:
    sys.exit("GROQ_API_KEY missing")

BASE = "https://api.groq.com/openai/v1"
H = {"authorization": f"Bearer {key}", "content-type": "application/json"}
WANT = ("x-ratelimit-limit-requests", "x-ratelimit-limit-tokens", "x-ratelimit-remaining-requests",
        "x-ratelimit-remaining-tokens", "x-ratelimit-reset-requests", "x-ratelimit-reset-tokens", "retry-after")


def clean(s: str) -> str:
    return s.replace(key, "[REDACTED]") if key else s


def show(tag, r):
    hdr = {k: r.headers.get(k) for k in WANT if r.headers.get(k) is not None}
    print(f"[{tag}] HTTP {r.status_code} headers={json.dumps(hdr)}")


# 1) availability
r = requests.get(f"{BASE}/models", headers=H, timeout=30)
show("GET /models", r)
avail = {}
if r.ok:
    for m in r.json().get("data", []):
        avail[m["id"]] = {k: m.get(k) for k in ("active", "context_window", "max_completion_tokens")}
for model in sys.argv[1:]:
    print(f"availability {model}: {avail.get(model, 'NOT LISTED')}")
print("listed models (count):", len(avail))

# 2) one tiny completion per model
for model in sys.argv[1:]:
    body = {"model": model, "messages": [{"role": "user", "content": "Reply with: ok"}],
            "max_completion_tokens": 5, "temperature": 0}
    if "qwen3" in model.lower():
        body["reasoning_effort"] = "none"
    t0 = time.time()
    r = requests.post(f"{BASE}/chat/completions", headers=H, data=json.dumps(body), timeout=60, allow_redirects=False)
    show(f"chat {model} ({time.time() - t0:.1f}s)", r)
    try:
        j = r.json()
    except ValueError:
        j = {}
    if r.ok:
        print("  usage:", json.dumps(j.get("usage", {}) and {k: v for k, v in j["usage"].items() if k.endswith("tokens")}))
    else:
        err = j.get("error", {})
        print("  error.type:", err.get("type"), "| code:", err.get("code"))
        print("  error.message:", clean(str(err.get("message", r.text[:300])))[:500])
        m = re.search(r"(per (minute|day)|TPM|RPM|TPD|RPD)", str(err.get("message", "")), re.I)
        print("  scope hint:", m.group(0) if m else None)
    time.sleep(2)
