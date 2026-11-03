import json
import requests
from corpdisc.config import load_settings

s = load_settings()
r = requests.post(
    "https://generativelanguage.googleapis.com/v1beta/models/" + s.gemini_model + ":generateContent",
    params={"key": s.gemini_api_key.get_secret_value()},
    json={"contents": [{"parts": [{"text": "Reply with the single word OK."}]}],
          "generationConfig": {"maxOutputTokens": 8}},
    timeout=30,
)
print("HTTP", r.status_code, "retry-after:", r.headers.get("retry-after"))
try:
    e = r.json().get("error", {})
    print("status:", e.get("status"), "| message:", (e.get("message") or "")[:400])
    for d in e.get("details", []):
        t = d.get("@type", "").split(".")[-1]
        if t == "QuotaFailure":
            for v in d.get("violations", []):
                print("quota violation:", v.get("quotaMetric"), v.get("quotaId"))
        elif t == "RetryInfo":
            print("retryDelay:", d.get("retryDelay"))
    if r.ok:
        print("usage:", r.json().get("usageMetadata"))
except Exception as ex:
    print("unparsed:", r.text[:300], ex)
