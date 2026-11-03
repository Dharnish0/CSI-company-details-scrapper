import sqlite3
import sys

c = sqlite3.connect("file:" + sys.argv[1] + "?mode=ro", uri=True)
print("gates (non-G6):")
for r in c.execute("select gate_code,item_ref,substr(detail,1,200) from gate_rejections where gate_code<>'G6'"):
    print("  ", r)
print("pages for Harris/Jonas/Vela/Topicus:")
for r in c.execute("""select domain,page_type,http_status,crawl_status,entities_found,pages_found,url from crawl_pages
  where domain in ('www.harriscomputer.com','jonassoftware.com','velasoftwaregroup.com','topicus.com')"""):
    print("  ", r)
print("review items:")
for r in c.execute("select entity_id,reason_code,substr(reason_text,1,100) from review_items"):
    print("  ", r)
