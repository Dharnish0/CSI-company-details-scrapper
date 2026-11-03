import sqlite3
import sys

c = sqlite3.connect(sys.argv[1])
cols = [d[1] for d in c.execute("pragma table_info(review_items)")]
print("review_items cols:", cols)
for r in c.execute("select * from review_items").fetchall():
    print("  ", [str(x)[:90] for x in r])
print("HTTP_ERROR pages:")
for r in c.execute("select url, http_status, error from crawl_pages where crawl_status='HTTP_ERROR'").fetchall():
    print("  ", r)
print("websites for review entities:")
for r in c.execute("""select e.name, w.url, w.kind, w.reachable, w.last_http_status from entities e
  left join websites w on w.entity_id=e.entity_id where e.verification_status='REVIEW_REQUIRED'""").fetchall():
    print("  ", r)
print("gate rejections:", c.execute("select gate_code,item_ref,detail from gate_rejections").fetchall())
