"""Back up bench/groq_p5.db, then reopen Volaris (task 3) and Vela (task 5) that were skipped by the per-task call cap."""
import shutil
import sqlite3

shutil.copy2("bench/groq_p5.db", "bench/groq_p5_before_reopen.db")
c = sqlite3.connect("bench/groq_p5.db")
for tid, eid in c.execute("select task_id, entity_id from research_queue where task_id in (3,5)").fetchall():
    c.execute("update research_queue set status='PENDING', attempts=0, error=NULL, not_before=NULL, started_at=NULL, finished_at=NULL where task_id=?", (tid,))
    c.execute("update entities set crawl_state='PENDING' where entity_id=?", (eid,))
c.execute("delete from review_items where reason_code='EXTRACTION_FAILED' and entity_id in (select entity_id from research_queue where task_id in (3,5))")
c.execute("update runs set status='RUNNING' where run_id=1")
c.commit()
print(c.execute("select q.task_id,e.name,q.status from research_queue q join entities e using(entity_id) where q.task_id in (3,5)").fetchall())
print("tokens used:", c.execute("select llm_tokens_used, llm_token_budget from runs").fetchone())
