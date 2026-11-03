import sys
import tempfile
import pathlib
sys.path.insert(0, ".")
import pytest
from tests.fixtures import sibling_site
from tests.fixtures.world import seeded_world

mp = pytest.MonkeyPatch()
tmp = pathlib.Path(tempfile.mkdtemp())
http = sibling_site.install(mp)
w = seeded_world(tmp, http=http)
w.sup.run()


def ev():
    return w.q("SELECT e.evidence_id, e.entity_id, e.relationship_id, e.supports, e.source_class, substr(e.quote,1,60) FROM evidence e ORDER BY 1")


before = ev()
w.conn.execute("UPDATE research_queue SET status='PENDING', attempts=0, error=NULL, finished_at=NULL, not_before=NULL")
w.conn.commit()
w.sup.run()
after = ev()
print(len(before), len(after))
for r in after:
    if tuple(r) not in {tuple(b) for b in before}:
        print("NEW:", tuple(r))
