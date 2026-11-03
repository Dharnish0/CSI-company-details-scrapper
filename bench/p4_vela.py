import sqlite3
import sys

c = sqlite3.connect("file:" + sys.argv[1] + "?mode=ro", uri=True)
print([r[1] for r in c.execute("pragma table_info(crawl_pages)")])
for r in c.execute("select raw_name,candidate_kind,reason from candidates k join entities e on e.entity_id=k.parent_entity_id where e.name='Vela'"):
    print(r)
print(c.execute("select page_id,url,page_type,entities_found,pages_found from crawl_pages where domain like 'vela%'").fetchall())
