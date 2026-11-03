import pathlib
import sys
import tempfile
sys.path.insert(0, ".")
import pytest
from tests.fixtures import section_site
from tests.fixtures.world import seeded_world

mp = pytest.MonkeyPatch()
w = seeded_world(pathlib.Path(tempfile.mkdtemp()), http=section_site.install(mp))
print(w.sup.run().status)
for r in w.q("SELECT name, verification_status FROM entities"):
    print(tuple(r))
for r in w.q("SELECT gate_code, item_ref, substr(detail,1,120) FROM gate_rejections"):
    print("REJ", tuple(r))
for r in w.q("SELECT task_id, status, attempts, error FROM research_queue"):
    print("Q", tuple(r))
print([u for u in w.page_requests()])
for r in w.q("SELECT i.task_id, i.status, i.correlation_id FROM agent_invocations i WHERE i.agent='extraction' ORDER BY 1"):
    print("INV", tuple(r))
