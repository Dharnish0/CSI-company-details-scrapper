"""Create bench/groq_p4.db from bench/groq_p3.db (consistent sqlite backup) and re-open the three depth-1 tasks that
failed in the p3 run (Volaris FAILED, Jonas TERMINAL, Vela UNREACHABLE) so `resume` can redo them with the repaired code.
Only the bench copy is touched."""
import sqlite3
import sys

SRC, DST = "bench/groq_p3.db", "bench/groq_p4.db"
if len(sys.argv) > 1 and sys.argv[1] == "--force":
    pass
else:
    import os
    if os.path.exists(DST):
        sys.exit(f"{DST} already exists; refusing to overwrite (pass --force)")
src = sqlite3.connect(f"file:{SRC}?mode=ro", uri=True)
dst = sqlite3.connect(DST)
src.backup(dst)
src.close()
names = ("Volaris", "Jonas Software", "Vela")
rows = dst.execute(
    "select q.task_id, q.entity_id, e.name, q.status, q.attempts from research_queue q join entities e using(entity_id) "
    "where e.name in (?,?,?) and q.depth=1", names).fetchall()
print("before:", rows)
for tid, eid, _name, _st, _att in rows:
    dst.execute("update research_queue set status='PENDING', attempts=0, error=NULL, not_before=NULL, started_at=NULL, "
                "finished_at=NULL where task_id=?", (tid,))
    dst.execute("update entities set crawl_state='PENDING' where entity_id=?", (eid,))
dst.execute("update runs set status='RUNNING' where run_id=1")
dst.commit()
print("after:", dst.execute("select q.task_id,e.name,q.status,q.attempts,e.crawl_state from research_queue q join entities e using(entity_id)").fetchall())
print("integrity:", dst.execute("pragma integrity_check").fetchone())
dst.close()
