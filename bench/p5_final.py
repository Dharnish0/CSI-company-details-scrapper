import json
import sqlite3
import sys

c = sqlite3.connect("file:" + sys.argv[1] + "?mode=ro", uri=True)
q = lambda s, *a: c.execute(s, a).fetchall()
print("RUN:", q("select status,llm_tokens_used,llm_token_budget from runs"))
print("QUEUE:")
for r in q("select q.task_id,e.name,q.depth,q.status,q.attempts,substr(coalesce(q.error,''),1,70),e.crawl_state from research_queue q join entities e using(entity_id) order by 1"):
    print("  ", r)
print("ENTITIES (id>10) with parent, status, website, evidence:")
for r in q("""select e.entity_id,e.name,e.entity_type,e.verification_status,e.crawl_state,
  (select p.name from relationships r join entities p on p.entity_id=r.parent_id where r.child_id=e.entity_id and r.is_primary=1 limit 1),
  (select group_concat(distinct w.url||'['||w.kind||']') from websites w where w.entity_id=e.entity_id),
  (select count(*) from evidence v where v.entity_id=e.entity_id and v.quote_verified=1)
  from entities e where e.entity_id>10 order by 1"""):
    print("  ", r)
print("CANDIDATES by parent/decision:")
for r in q("select p.name,k.decision,count(*) from candidates k join entities p on p.entity_id=k.parent_entity_id group by 1,2 order by 1,2"):
    print("  ", r)
print("DEPTH queue max:", q("select max(depth) from research_queue")[0][0], "| entity depth via relationships:")
print("  children-of-children (grandchildren of CSI):", q("""select count(*) from relationships r2 join relationships r1 on r1.child_id=r2.parent_id
   join entities root on root.entity_id=r1.parent_id where root.entity_type='Parent Company'""")[0][0])
print("PAGES:", q("select crawl_status,count(*) from crawl_pages group by 1"))
print("GATES:", q("select gate_code,count(*) from gate_rejections group by 1"))
print("REVIEW:", q("select reason_code,count(*) from review_items group by 1"))
print("INVOCATIONS:")
for r in q("select agent,status,coalesce(failure_kind,''),count(*),sum(tokens_in),sum(tokens_out) from agent_invocations where invocation_id>37 group by 1,2,3"):
    print("  ", r)
errs = {}
for (resp,) in q("select response_json from agent_invocations where status='FAILED'"):
    try:
        m = json.loads(resp).get("error", {}).get("message", "")[:80]
    except Exception:
        m = "?"
    errs[m] = errs.get(m, 0) + 1
print("FAIL MSGS:", errs)
print("DUP names:", q("select normalized_name,count(*) from entities group by 1 having count(*)>1"))
