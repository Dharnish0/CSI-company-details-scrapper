import sqlite3
import sys

c = sqlite3.connect("file:" + sys.argv[1] + "?mode=ro", uri=True)
print("run:", c.execute("select run_id,status,llm_tokens_used,llm_token_budget,llm_cost_usd from runs").fetchall())
print("queue:")
for r in c.execute("select q.task_id,e.name,q.depth,q.status,q.attempts,q.not_before,substr(coalesce(q.error,''),1,110),e.crawl_state "
                   "from research_queue q join entities e using(entity_id) order by q.task_id"):
    print("  ", r)
print("entities beyond depth 1:")
for r in c.execute("""select e.entity_id,e.name,e.entity_type,e.verification_status,e.crawl_state,
   (select p.name from relationships r join entities p on p.entity_id=r.parent_id where r.child_id=e.entity_id and r.is_primary=1 limit 1),
   (select group_concat(distinct w.url) from websites w where w.entity_id=e.entity_id)
   from entities e where e.entity_id>10 order by 1"""):
    print("  ", r)
print("review items:")
for r in c.execute("select entity_id,reason_code,substr(reason_text,1,90) from review_items order by 1"):
    print("  ", r)
