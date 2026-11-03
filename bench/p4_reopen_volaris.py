import sqlite3
c = sqlite3.connect("bench/groq_p4.db")
q = ("SELECT COUNT(*) FROM agent_invocations WHERE run_id=1 AND cache_hit=0 AND status<>'SKIPPED' "
     "AND (failure_kind IS NULL OR failure_kind NOT IN ('BUDGET','CIRCUIT_OPEN'))")
print("task3 provider calls:", c.execute(q + " AND task_id=3").fetchone()[0], "run total:", c.execute(q).fetchone()[0])
print(c.execute("select distinct failure_kind from agent_invocations").fetchall())
c.execute("update research_queue set status='PENDING', attempts=0, error=NULL, not_before=NULL, started_at=NULL, finished_at=NULL where task_id=3")
c.execute("update entities set crawl_state='PENDING' where entity_id=3")
c.execute("update runs set status='RUNNING', finished_at=NULL where run_id=1")
c.commit()
