"""Create bench/groq_p5.db from the clean bench/groq_p3.db and re-open Volaris, Jonas Software and Vela (depth 1).
Also sets runs.llm_token_budget = inherited tokens + 250,000 (native abort guard). Only the bench copy is touched."""
import os
import sqlite3
import sys

SRC, DST = "bench/groq_p3.db", "bench/groq_p5.db"
if os.path.exists(DST):
    sys.exit(f"{DST} exists; refusing to overwrite")
src = sqlite3.connect(f"file:{SRC}?mode=ro", uri=True)
dst = sqlite3.connect(DST)
src.backup(dst)
src.close()
rows = dst.execute(
    "select q.task_id, q.entity_id, e.name, q.status, q.attempts from research_queue q join entities e using(entity_id) "
    "where e.name in ('Volaris','Jonas Software','Vela') and q.depth=1").fetchall()
print("reopening:", rows)
for tid, eid, *_ in rows:
    dst.execute("update research_queue set status='PENDING', attempts=0, error=NULL, not_before=NULL, started_at=NULL, "
                "finished_at=NULL where task_id=?", (tid,))
    dst.execute("update entities set crawl_state='PENDING' where entity_id=?", (eid,))
used = dst.execute("select llm_tokens_used from runs where run_id=1").fetchone()[0] or 0
dst.execute("update runs set status='RUNNING', llm_token_budget=? where run_id=1", (used + 250000,))
dst.commit()
print("inherited tokens:", used, "-> budget:", used + 250000)
print("queue:", dst.execute("select q.task_id,e.name,q.depth,q.status from research_queue q join entities e using(entity_id)").fetchall())
print("entities:", dst.execute("select count(*) from entities").fetchone()[0], "integrity:", dst.execute("pragma integrity_check").fetchone()[0])
dst.close()
