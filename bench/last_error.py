import json
import sqlite3
import sys

c = sqlite3.connect(sys.argv[1])
for inv, agent, status, kind, tin, tout, resp in c.execute(
    "select invocation_id,agent,status,failure_kind,tokens_in,tokens_out,response_json from agent_invocations order by 1"
):
    print(inv, agent, status, kind, tin, tout)
    print("   ", resp[:600])
print("queue:", c.execute("select task_id,status,attempts,not_before,error from research_queue").fetchall())
