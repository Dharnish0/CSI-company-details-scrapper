import json
import sqlite3
import sys

c = sqlite3.connect("file:" + sys.argv[1] + "?mode=ro", uri=True)
for inv, req, resp in c.execute("select invocation_id, request_json, response_json from agent_invocations where agent='deduplication' order by 1"):
    print("inv", inv)
    try:
        rq = json.loads(req)
        print(" request:", json.dumps(rq.get("payload", rq))[:900].encode("ascii", "replace").decode())
    except Exception as e:
        print(" request unparsed", e)
    try:
        print(" response:", json.dumps(json.loads(resp).get("payload"))[:700].encode("ascii", "replace").decode())
    except Exception as e:
        print(" response unparsed", e)
