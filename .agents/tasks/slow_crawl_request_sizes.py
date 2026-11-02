"""READ-ONLY (mode=ro): per-agent live (non-cache) request token sizes in a DB, to size pacing vs TPM 8000 / TPD 200K."""
import sqlite3
import sys
from pathlib import Path

db = Path(sys.argv[1]).resolve()
conn = sqlite3.connect("file:" + db.as_posix().replace(" ", "%20") + "?mode=ro", uri=True)
print("agent, status, n_live, avg_in, max_in, avg_out, max_out, avg_total, avg_latency_ms")
for r in conn.execute("SELECT agent,status,COUNT(*),AVG(tokens_in),MAX(tokens_in),AVG(tokens_out),MAX(tokens_out),"
                      "AVG(COALESCE(tokens_in,0)+COALESCE(tokens_out,0)),AVG(latency_ms) FROM agent_invocations "
                      "WHERE cache_hit=0 AND COALESCE(tokens_in,0)>0 GROUP BY 1,2"):
    print(tuple(round(x) if isinstance(x, float) else x for x in r))
live = conn.execute("SELECT COUNT(*),SUM(COALESCE(tokens_in,0)+COALESCE(tokens_out,0)) FROM agent_invocations "
                    "WHERE cache_hit=0 AND COALESCE(tokens_in,0)>0").fetchone()
print("live token-bearing requests:", live[0], "tokens:", live[1], "avg/request:", round(live[1] / live[0]))
print("requests per 200K TPD at that average:", round(200000 / (live[1] / live[0]), 1))
print("seconds between requests to spread 200K over 24h:", round(86400 / (200000 / (live[1] / live[0]))))
print("hours until 200K TPD is spent at a 90 s interval:", round(200000 / (live[1] / live[0]) * 90 / 3600, 2))
conn.close()
