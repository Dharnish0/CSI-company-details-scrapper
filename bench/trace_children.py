import json
import sqlite3
import sys

c = sqlite3.connect("file:" + sys.argv[1] + "?mode=ro", uri=True)
names = dict(c.execute("select task_id, entity_id from research_queue"))
ent = dict(c.execute("select entity_id, name from entities"))
print("candidates by parent/decision:")
for r in c.execute("select e.name, k.decision, count(*) from candidates k join entities e on e.entity_id=k.parent_entity_id group by 1,2"):
    print("  ", r)
print("rejected/unresolved candidate reasons (sample):")
for r in c.execute("select e.name, k.raw_name, k.candidate_kind, k.decision, substr(coalesce(k.reason,''),1,80), k.gate_code from candidates k join entities e on e.entity_id=k.parent_entity_id where k.parent_entity_id<>1 order by k.candidate_id limit 40"):
    print("  ", r)
print("gate rejections:", c.execute("select gate_code, count(*) from gate_rejections group by 1").fetchall())
for task in sorted(names):
    if task == 1:
        continue
    print(f"--- task {task} {ent[names[task]]}")
    for inv, agent, status, kind, resp in c.execute(
        "select invocation_id,agent,status,coalesce(failure_kind,''),response_json from agent_invocations where task_id=? order by 1", (task,)
    ):
        n = ""
        try:
            d = json.loads(resp)
            p = d.get("payload") or {}
            if "entities" in p:
                n = f"entities={len(p['entities'])}" + (f" names={[x.get('name') for x in p['entities']][:8]}" if p["entities"] else "")
            if "assessments" in p:
                n = f"assessments={len(p['assessments'])} " + str([(a.get('local_ref'), a.get('decision') or a.get('verdict')) for a in p['assessments']][:6])
            if d.get("error"):
                n += " err=" + d["error"].get("message", "")[:90]
        except Exception as ex:
            n = "unparsed"
        print(f"   inv{inv} {agent} {status} {kind} {n}")
    pg = c.execute("select count(*), group_concat(distinct crawl_status) from crawl_pages where entity_id=?", (names[task],)).fetchone()
    print("   pages:", pg)
