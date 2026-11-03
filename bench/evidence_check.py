import sqlite3
import sys

c = sqlite3.connect(sys.argv[1])
print("relationship -> evidence (CONFIRMED needs verified first-party OWNERSHIP quote):")
rows = c.execute("""
select p.name, ch.name, r.verification_status, round(r.confidence,2),
  (select count(*) from evidence e where e.relationship_id=r.relationship_id and e.quote_verified=1
     and e.supports='OWNERSHIP' and e.source_class in ('FIRST_PARTY_PARENT','FIRST_PARTY_CHILD','OFFICIAL_DOCUMENT')) as good_ev,
  (select count(*) from evidence e where e.relationship_id=r.relationship_id) as all_ev
from relationships r join entities p on p.entity_id=r.parent_id join entities ch on ch.entity_id=r.child_id
order by r.relationship_id""").fetchall()
for r in rows:
    flag = "  <-- CONFIRMED WITHOUT GOOD EVIDENCE" if r[2] == "CONFIRMED" and r[4] == 0 else ""
    print(f"  {r[0]} -> {r[1]} | {r[2]} conf={r[3]} good_ev={r[4]} all_ev={r[5]}{flag}")
print("sample quote:")
q = c.execute("select e.quote, e.url, e.source_class from evidence e where e.quote_verified=1 limit 2").fetchall()
for x in q:
    print("  ", x[2], x[1], "|", x[0][:150].replace("\n", " "))
print("queue:", c.execute("select e.name, q.depth, q.status, q.attempts from research_queue q join entities e on e.entity_id=q.entity_id order by q.task_id").fetchall())
print("review reasons:", c.execute("select reason, count(*) from review_items group by 1").fetchall())
