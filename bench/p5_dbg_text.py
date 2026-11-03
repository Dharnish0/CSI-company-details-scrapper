import pathlib
import sys
import tempfile
sys.path.insert(0, ".")
import pytest
from tests.fixtures import text_url_site
from tests.fixtures.world import seeded_world

mp = pytest.MonkeyPatch()
w = seeded_world(pathlib.Path(tempfile.mkdtemp()), http=text_url_site.install(mp))
print(w.sup.run().status)
for r in w.q("SELECT name, verification_status, confidence FROM entities"):
    print(tuple(r))
for r in w.q("SELECT e.name, ri.reason_code, ri.reason_text FROM review_items ri JOIN entities e USING(entity_id)"):
    print("REV", tuple(r))
for r in w.q("SELECT gate_code, item_ref, detail FROM gate_rejections"):
    print("REJ", tuple(r))
for r in w.q("SELECT raw_name, decision, reason FROM candidates"):
    print("CAND", tuple(r))
print(w.q("SELECT url FROM websites"))
