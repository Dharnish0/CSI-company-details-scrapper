import requests
from corpdisc.config import load_settings

s = load_settings()
r = requests.get(
    "https://generativelanguage.googleapis.com/v1beta/models/" + s.gemini_model,
    params={"key": s.gemini_api_key.get_secret_value()},
    timeout=20,
)
print("gemini model", s.gemini_model, "HTTP", r.status_code)
if r.ok:
    d = r.json()
    print("outputTokenLimit", d.get("outputTokenLimit"), "thinking", d.get("thinking"))
