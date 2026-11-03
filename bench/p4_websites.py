import json
import sqlite3
import sys

c = sqlite3.connect("file:" + sys.argv[1] + "?mode=ro", uri=True)
for inv, req, resp in c.execute("select invocation_id, request_json, response_json from agent_invocations where agent='website_resolution' and status='OK' order by 1"):
    try:
        rq = json.loads(req)["input"]
        rp = json.loads(resp)["payload"]
        print(inv, "REQ keys:", list(rq.keys()), "| name:", str(rq.get("entity", rq.get("name")))[:120])
        print("   candidates:", json.dumps(rq.get("candidates", rq.get("urls", "")))[:400])
        print("   RESP:", json.dumps(rp)[:500])
    except Exception as e:
        print(inv, "unparsed", e)
print("crawl_errors:")
for r in c.execute("select error_type, entity_id, substr(message,1,200) from crawl_errors"):
    print("  ", r)
print("G6/other gate rows:")
for r in c.execute("select gate_code,item_ref,substr(detail,1,200) from gate_rejections where gate_code='G6'"):
    print("  ", r)
