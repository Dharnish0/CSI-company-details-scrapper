import sqlite3

c = sqlite3.connect("file:bench/groq_p5.db?mode=ro", uri=True)
print("child | parent | status | conf | first-party verified OWNERSHIP quotes | website kind")
for r in c.execute("""select ch.name, p.name, r.verification_status, round(r.confidence,2),
  (select count(*) from evidence e where e.relationship_id=r.relationship_id and e.quote_verified=1 and e.supports='OWNERSHIP'
     and e.source_class in ('FIRST_PARTY_PARENT','FIRST_PARTY_CHILD','OFFICIAL_DOCUMENT')),
  (select group_concat(w.kind) from websites w where w.entity_id=ch.entity_id)
  from relationships r join entities ch on ch.entity_id=r.child_id join entities p on p.entity_id=r.parent_id
  where p.name in ('Volaris','Jonas Software') order by p.name, ch.name"""):
    print("  ", r)
bad = c.execute("""select count(*) from relationships r where r.verification_status='CONFIRMED' and not exists
  (select 1 from evidence e where e.relationship_id=r.relationship_id and e.quote_verified=1 and e.supports='OWNERSHIP'
   and e.source_class in ('FIRST_PARTY_PARENT','FIRST_PARTY_CHILD','OFFICIAL_DOCUMENT'))""").fetchone()[0]
print("CONFIRMED edges without verified first-party OWNERSHIP evidence:", bad)
q = c.execute("""select e.source_class, e.url, substr(replace(e.quote,char(10),' '),1,170) from evidence e
  join relationships r on r.relationship_id=e.relationship_id join entities x on x.entity_id=r.child_id
  where x.name='AskCody' and e.quote_verified=1 limit 1""").fetchone()
print("sample AskCody evidence:", q)
print("multi-parent review items:", c.execute("select entity_id, substr(reason_text,1,120) from review_items where reason_code in ('MULTI_PARENT','AMBIGUOUS_NAME')").fetchall())
