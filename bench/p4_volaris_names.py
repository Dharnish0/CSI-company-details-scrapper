import json
import sqlite3
import sys

c = sqlite3.connect("file:" + sys.argv[1] + "?mode=ro", uri=True)
names = set()
n_resp = 0
for (resp,) in c.execute("select response_json from agent_invocations where task_id=3 and agent='extraction' and status='OK' and invocation_id>37"):
    try:
        ents = json.loads(resp)["payload"]["entities"]
    except Exception:
        continue
    n_resp += 1
    for e in ents:
        names.add(e["name"].strip().lower())
print("extraction responses (id>37, task 3):", n_resp, "distinct entity names:", len(names))
print("entities stored under Volaris:", c.execute("select count(*) from relationships where parent_id=3").fetchone()[0],
      "candidates rows:", c.execute("select count(*) from candidates where parent_entity_id=3").fetchone()[0])
print("task 3 attempts / provider calls this attempt window:", c.execute(
    "select count(*) from agent_invocations where task_id=3 and invocation_id>37 and cache_hit=0 and status<>'SKIPPED'").fetchone()[0])
