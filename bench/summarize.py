"""Summarize one benchmark DB: python bench/summarize.py bench/<name>.db"""
import json
import sqlite3
import sys

db = sys.argv[1]
c = sqlite3.connect(db)


def q(sql, *a):
    return c.execute(sql, a).fetchall()


print("DB:", db)
for r in q("select run_id,status,llm_tokens_used,started_at,finished_at from runs"):
    print("run:", r)
print("queue by status/depth:", q("select status,depth,count(*) from research_queue group by 1,2"))
print("entities by status:", q("select verification_status,count(*) from entities group by 1"))
print("entities by type:", q("select entity_type,count(*) from entities group by 1"))
print("relationships by status:", q("select verification_status,count(*) from relationships group by 1"))
print("candidates:", q("select count(*) from candidates")[0][0])
print("pages:", q("select crawl_status,count(*) from crawl_pages group by 1"))
print("websites:", q("select count(*) from websites")[0][0], "evidence:", q("select count(*),sum(quote_verified) from evidence")[0])
print("invocations by agent/status/failure:")
for r in q("select agent,status,coalesce(failure_kind,''),count(*),sum(tokens_in),sum(tokens_out),round(avg(latency_ms)) "
           "from agent_invocations group by 1,2,3"):
    print("  ", r)
errs = {}
for (resp,) in q("select response_json from agent_invocations where status='FAILED'"):
    try:
        m = json.loads(resp).get("error", {}).get("message", "")
    except Exception:
        m = "?"
    errs[m[:120]] = errs.get(m[:120], 0) + 1
print("failure messages:", errs)
print("gate rejections:", q("select gate_code,count(*) from gate_rejections group by 1"))
print("entities:")
for r in q("select e.entity_id,e.name,e.entity_type,e.verification_status from entities e order by 1"):
    print("  ", r)
