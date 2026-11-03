import sqlite3

c = sqlite3.connect("file:bench/groq_p5.db?mode=ro", uri=True)
print("per-task provider-reaching calls (budget counter definition) vs cap:")
for r in c.execute("""select task_id, count(*),
   sum(status='SKIPPED'), sum(cache_hit=1)
   from agent_invocations where task_id in (3,5) group by task_id"""):
    print("  task", r[0], "invocations", r[1], "skipped", r[2], "cache hits", r[3])
for t in (3, 5):
    n = c.execute("""select count(*) from agent_invocations where task_id=? and cache_hit=0 and status<>'SKIPPED'
        and (failure_kind is null or failure_kind not in ('BUDGET','CIRCUIT_OPEN'))""", (t,)).fetchone()[0]
    print(f"  task {t}: counted calls = {n} (cap MAX_LLM_CALLS_PER_TASK=12 default)")
print("skip reasons:")
import json
seen = {}
for (resp,) in c.execute("select response_json from agent_invocations where status='SKIPPED'"):
    m = json.loads(resp).get("error", {}).get("message", "?")
    seen[m] = seen.get(m, 0) + 1
print("  ", seen)
