import sqlite3
import sys

c = sqlite3.connect("file:" + sys.argv[1] + "?mode=ro", uri=True)
r = c.execute("select response_json from agent_invocations where status='FAILED' and failure_kind='TRANSPORT' limit 1").fetchone()
print(r[0][:600])
print(c.execute("select sqlite_version()").fetchone())
print(c.execute("select count(*) from agent_invocations where json_valid(response_json) and json_extract(response_json,'$.error.rate_limited')=1").fetchone())
