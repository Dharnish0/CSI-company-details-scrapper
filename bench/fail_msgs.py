import json
import sqlite3
import sys

c = sqlite3.connect("file:" + sys.argv[1] + "?mode=ro", uri=True)
seen = {}
for inv, task, agent, status, kind, tin, resp in c.execute(
    "select invocation_id,task_id,agent,status,failure_kind,tokens_in,response_json from agent_invocations where status<>'OK' order by 1"
):
    try:
        m = json.loads(resp).get("error", {}).get("message", "")[:160]
    except Exception:
        m = "?"
    seen.setdefault((agent, status, m), []).append(inv)
for k, v in seen.items():
    print(len(v), "x", k, "e.g. inv", v[:4])
print("queue:", c.execute("select e.name,q.depth,q.status,q.attempts,q.error from research_queue q join entities e using(entity_id) order by q.task_id").fetchall())
print("tokens used:", c.execute("select llm_tokens_used from runs").fetchone())
