"""READ-ONLY: show how past Groq 429s were recorded in a DB's agent_invocations (mode=ro). Usage: python slow_crawl_429_scan.py <db> [limit]"""
import sqlite3
import sys
from collections import Counter
from pathlib import Path

db = Path(sys.argv[1]).resolve()
limit = int(sys.argv[2]) if len(sys.argv) > 2 else 5
conn = sqlite3.connect("file:" + db.as_posix().replace(" ", "%20") + "?mode=ro", uri=True)
rows = conn.execute("SELECT invocation_id,agent,failure_kind,started_at,latency_ms,response_json FROM agent_invocations "
                    "WHERE status='FAILED' AND failure_kind='TRANSPORT' ORDER BY invocation_id").fetchall()
print(db.name, "TRANSPORT failures:", len(rows))
c = Counter(r[5][:200] if r[5] else None for r in rows)
for k, v in c.most_common(8):
    print(v, "x", k)
for r in rows[:limit]:
    print(r[0], r[1], r[3], "latency_ms", r[4], "|", (r[5] or "")[:300])
if rows:
    print("first/last started_at:", rows[0][3], rows[-1][3])
    ts = [r[3] for r in rows]
    print("distinct hours (UTC):", sorted({t[:13] for t in ts}))
conn.close()
