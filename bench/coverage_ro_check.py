import sqlite3

c = sqlite3.connect("file:bench/csi_coverage_test.db?mode=ro", uri=True)
q = lambda s: c.execute(s).fetchall()
print("runs:", q("select run_id,status,llm_tokens_used,llm_token_budget,started_at,finished_at from runs"))
print("queue:", q("select status,depth,count(*) from research_queue group by 1,2"))
print("waiting:", q("select e.name,q.status,q.attempts,q.not_before,substr(coalesce(q.error,''),1,80) from research_queue q join entities e using(entity_id) where q.status<>'DONE' order by q.not_before"))
print("entities:", q("select verification_status,count(*) from entities group by 1"))
print("provider calls so far (cache_hit=0, not skipped):", q("select count(*) from agent_invocations where cache_hit=0 and status<>'SKIPPED'")[0][0])
print("pages:", q("select count(*) from crawl_pages")[0][0])
print("max depth in queue:", q("select max(depth) from research_queue")[0][0])
