import json
import sqlite3

c = sqlite3.connect("file:bench/ollama_qwen.db?mode=ro", uri=True)
resp = c.execute("select response_json from agent_invocations where agent='extraction' and status='OK'").fetchone()[0]
p = json.loads(resp).get("payload", {})
ents = p.get("entities", [])
print("qwen extraction entities:", len(ents))
for e in ents:
    print("  ", {k: e.get(k) for k in ("local_ref", "name", "kind", "is_company", "proposed_type") if k in e})
print("other payload keys:", [k for k in p if k != "entities"])
