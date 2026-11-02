"""READ-ONLY (mode=ro): cumulative tokens vs 429 events over time for one DB run, bucketed per 10 minutes (UTC)."""
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

db = Path(sys.argv[1]).resolve()
conn = sqlite3.connect("file:" + db.as_posix().replace(" ", "%20") + "?mode=ro", uri=True)
rows = conn.execute("SELECT started_at, status, failure_kind, COALESCE(tokens_in,0), COALESCE(tokens_out,0), cache_hit, "
                    "COALESCE(latency_ms,0), response_json FROM agent_invocations ORDER BY invocation_id").fetchall()
b = defaultdict(lambda: [0, 0, 0, 0, 0])  # calls_ok, tokens, rate_limited_invocations, schema_fail, cache
cum = 0
first_rl = None
for ts, st, fk, ti, to, hit, lat, resp in rows:
    k = ts[:15] + "0"
    if st in ("OK", "PARTIAL") and not hit:
        b[k][0] += 1
    b[k][1] += ti + to
    cum += ti + to
    if fk == "TRANSPORT" and resp and "429" in resp:
        b[k][2] += 1
        if first_rl is None:
            first_rl = (ts, cum)
    if fk == "SCHEMA":
        b[k][3] += 1
    b[k][4] += 1 if hit else 0
print("bucket(UTC,10min)  ok_calls  tokens  rl_invocations  schema_fail  cache_hits")
for k in sorted(b):
    print(k, *b[k])
print("total tokens (sum of invocation tokens):", cum)
print("first 429 invocation (ts, cumulative tokens in this DB before it):", first_rl)
conn.close()
