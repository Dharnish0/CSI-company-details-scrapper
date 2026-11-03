"""Numbers for .agents/tasks/csi-p4-results.md. usage: python bench/p4_report_queries.py <p3.db> <p4.db>"""
import sqlite3
import sys

p3 = sqlite3.connect("file:" + sys.argv[1] + "?mode=ro", uri=True)
c = sqlite3.connect("file:" + sys.argv[2] + "?mode=ro", uri=True)
base = p3.execute("select max(invocation_id) from agent_invocations").fetchone()[0]
print("baseline max invocation id (p3):", base, "p3 tokens:", p3.execute("select llm_tokens_used from runs").fetchone()[0])
print("p4 runs:", c.execute("select llm_tokens_used, llm_cost_usd, llm_token_budget from runs").fetchone())
print("-- this re-test: invocations id >", base)
for r in c.execute("""select agent,status,coalesce(failure_kind,''),cache_hit,count(*),sum(tokens_in),sum(tokens_out)
    from agent_invocations where invocation_id>? group by 1,2,3,4 order by 1,2""", (base,)):
    print("  ", r)
tot = c.execute("""select count(*), sum(tokens_in), sum(tokens_out) from agent_invocations
    where invocation_id>? and cache_hit=0 and status<>'SKIPPED'""", (base,)).fetchone()
print("non-cached, non-skipped rows:", tot)
tin, tout = tot[1] or 0, tot[2] or 0
print("est cost USD = %.4f" % (tin * 0.8 / 1e6 + tout * 4.0 / 1e6))
print("cache hits:", c.execute("select count(*) from agent_invocations where invocation_id>? and cache_hit=1", (base,)).fetchone()[0])
print("provider-reaching calls whole run (budget definition):", c.execute(
    "select count(*) from agent_invocations where cache_hit=0 and status<>'SKIPPED' and (failure_kind is null or failure_kind not in ('BUDGET','CIRCUIT_OPEN'))").fetchone()[0])
print("-- candidates by parent/decision")
for r in c.execute("""select e.name, k.decision, count(*) from candidates k join entities e on e.entity_id=k.parent_entity_id
    where k.parent_entity_id<>1 group by 1,2"""):
    print("  ", r)
print("-- children by parent")
for r in c.execute("""select p.name, ch.name, ch.entity_type, r.verification_status, round(r.confidence,2), r.is_primary,
    (select count(*) from evidence e where e.relationship_id=r.relationship_id and e.quote_verified=1 and e.source_class like 'FIRST_PARTY%') as fp_verified,
    (select group_concat(distinct e.source_class) from evidence e where e.relationship_id=r.relationship_id) as classes,
    ch.crawl_state
    from relationships r join entities p on p.entity_id=r.parent_id join entities ch on ch.entity_id=r.child_id
    where r.parent_id<>1 order by r.relationship_id"""):
    print("  ", r)
print("-- review reasons for non-root-children:", c.execute("""select e.name, ri.reason_code from review_items ri join entities e on e.entity_id=ri.entity_id where e.entity_id>10""").fetchall())
print("-- queue:", c.execute("select q.task_id,e.name,q.depth,q.status,q.attempts,coalesce(q.error,'') from research_queue q join entities e using(entity_id) order by 1").fetchall())
print("-- max depth in queue:", c.execute("select max(depth) from research_queue").fetchone()[0])
print("-- queue status:", c.execute("select status,count(*) from research_queue group by 1").fetchall())
print("-- pages:", c.execute("select crawl_status,count(*) from crawl_pages group by 1").fetchall(), "total", c.execute("select count(*) from crawl_pages").fetchone()[0])
print("-- pages in p3:", p3.execute("select count(*) from crawl_pages").fetchone()[0])
print("-- crawl_errors:", c.execute("select error_type, count(*) from crawl_errors group by 1").fetchall() if c.execute("select 1 from sqlite_master where name='crawl_errors'").fetchone() else "no table")
print("-- sample Jonas evidence:")
for r in c.execute("""select ch.name, e.source_class, e.supports, e.quote_verified, e.url, substr(e.quote,1,200) from evidence e
    join relationships r using(relationship_id) join entities ch on ch.entity_id=r.child_id where r.parent_id=4 limit 4"""):
    print("  ", tuple(str(x).encode("ascii", "replace").decode() for x in r))
print("-- entities total:", c.execute("select count(*) from entities").fetchone()[0], "dup normalized names:", c.execute(
    "select normalized_name,count(*) from entities group by 1 having count(*)>1").fetchall())
print("-- websites for Jonas children:", c.execute("""select ch.name, w.url, w.reachable from relationships r join entities ch on ch.entity_id=r.child_id
    left join websites w on w.entity_id=ch.entity_id where r.parent_id=4""").fetchall())
