"""Time a simple prompt on each Ollama model; report load time, tokens/s, context length."""
import json
import sys
import time

import requests

BASE = "http://localhost:11434"
for model in sys.argv[1:]:
    info = requests.post(BASE + "/api/show", json={"model": model}, timeout=30).json()
    ctx = {k: v for k, v in info.get("model_info", {}).items() if "context_length" in k}
    print(f"== {model} | context_length(model max)={ctx} | params={info.get('details', {}).get('parameter_size')}")
    t = time.time()
    r = requests.post(
        BASE + "/api/chat",
        json={"model": model, "stream": False, "think": False, "keep_alive": "30m",
              "messages": [{"role": "user", "content": 'Reply with only this JSON: {"ok": true}'}],
              "options": {"num_ctx": 4096, "num_predict": 40, "temperature": 0}},
        timeout=280,
    ).json()
    wall = time.time() - t
    ns = 1e9
    ev = r.get("eval_count", 0)
    print(f"   wall={wall:.1f}s load={r.get('load_duration', 0)/ns:.1f}s prompt_eval={r.get('prompt_eval_count')}tok/{r.get('prompt_eval_duration', 0)/ns:.1f}s "
          f"gen={ev}tok/{r.get('eval_duration', 0)/ns:.1f}s -> {ev / max(r.get('eval_duration', 1) / ns, 1e-9):.1f} tok/s")
    print("   reply:", (r.get("message", {}).get("content", "") or "").strip()[:80].replace("\n", " "))
