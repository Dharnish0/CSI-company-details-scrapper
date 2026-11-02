"""READ-ONLY inspection of a CSI sqlite DB (opened with mode=ro). Never writes. Usage: python slow_crawl_db_inspect.py <db>"""
import json
import re
import sqlite3
import sys
from collections import Counter
from pathlib import Path

db = Path(sys.argv[1]).resolve()
uri = "file:" + db.as_posix().replace(" ", "%20") + "?mode=ro"
conn = sqlite3.connect(uri, uri=True)
conn.row_factory = sqlite3.Row


def q(sql, *a):
    return [dict(r) for r in conn.execute(sql, a).fetchall()]


print("DB:", db)
print("runs:")
for r in q("SELECT run_id,status,started_at,finished_at,llm_token_budget,llm_tokens_used,llm_cost_usd,root_code FROM runs"):
    print("  ", r)
rid = conn.execute("SELECT run_id FROM runs ORDER BY run_id DESC LIMIT 1").fetchone()[0]
print("active/latest run id:", rid)
cfg = json.loads(conn.execute("SELECT config_json FROM runs WHERE run_id=?", (rid,)).fetchone()[0])
print("run config:", {k: cfg.get(k) for k in ("llm_provider", "llm_model", "groq_model", "llm_rpm_limit", "llm_tpm_limit",
      "llm_token_budget", "max_llm_calls_per_run", "max_depth", "max_total_pages", "queue_retry_wait_max_seconds",
      "queue_max_rate_limit_deferrals", "llm_rate_limit_retries", "llm_retry_after_ceiling_seconds")})
print("entities by verification_status / type:")
for r in q("SELECT verification_status,COUNT(*) n FROM entities WHERE run_id=? GROUP BY 1", rid):
    print("  ", r)
for r in q("SELECT entity_type,verification_status,COUNT(*) n FROM entities WHERE run_id=? GROUP BY 1,2 ORDER BY 1,2", rid):
    print("  ", r)
print("entities by crawl_state:")
for r in q("SELECT crawl_state,COUNT(*) n FROM entities WHERE run_id=? GROUP BY 1", rid):
    print("  ", r)
print("research_queue by status:")
for r in q("SELECT status,COUNT(*) n FROM research_queue WHERE run_id=? GROUP BY 1", rid):
    print("  ", r)
print("research_queue non-DONE rows:")
for r in q("SELECT task_id,entity_id,depth,status,attempts,not_before,substr(error,1,160) error FROM research_queue "
           "WHERE run_id=? AND status<>'DONE' ORDER BY task_id", rid):
    print("  ", r)
print("research_queue DONE rows with PARTIAL notes:")
for r in q("SELECT task_id,entity_id,depth,attempts,substr(error,1,120) error FROM research_queue "
           "WHERE run_id=? AND status='DONE' AND error IS NOT NULL ORDER BY task_id", rid):
    print("  ", r)
print("max depth:", conn.execute("SELECT MAX(depth) FROM research_queue WHERE run_id=?", (rid,)).fetchone()[0])
print("invocations by agent/status/failure_kind:")
for r in q("SELECT agent,status,failure_kind,COUNT(*) n,SUM(tokens_in) tin,SUM(tokens_out) tout,SUM(cache_hit) hits "
           "FROM agent_invocations WHERE run_id=? GROUP BY 1,2,3 ORDER BY 1,2,3", rid):
    print("  ", r)
print("llm_calls rows:", conn.execute("SELECT COUNT(*), COALESCE(SUM(tokens_in),0), COALESCE(SUM(tokens_out),0) FROM llm_calls WHERE run_id=?", (rid,)).fetchone()[:])
print("crawl_errors (LLM stage) top:")
for r in q("SELECT stage,error_type,substr(message,1,160) m,COUNT(*) n FROM crawl_errors WHERE run_id=? GROUP BY 1,2,3 ORDER BY n DESC LIMIT 15", rid):
    print("  ", r)

# 429 text scan in failed invocations
pat = re.compile(r"(per (minute|day)|TPM|RPM|TPD|RPD|try again in [0-9hms.]+|rate.?limit)", re.I)
c = Counter()
for r in q("SELECT agent,status,failure_kind,response_json FROM agent_invocations WHERE run_id=? AND status<>'OK'", rid):
    txt = r["response_json"] or ""
    for m in pat.finditer(txt):
        c[m.group(0).lower()] += 1
print("429-ish tokens in non-OK invocation response_json:", dict(c))
conn.close()
