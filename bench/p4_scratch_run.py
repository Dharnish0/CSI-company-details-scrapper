"""Offline (fake provider, no network, no billable calls) run of the Phase 4 entry-page fixture into bench/p4_scratch."""
import pathlib
import shutil
import sys

sys.path.insert(0, ".")
import pytest

from tests.fixtures import entry_site
from tests.fixtures.world import seeded_world

out = pathlib.Path("bench/p4_scratch")
shutil.rmtree(out, ignore_errors=True)
out.mkdir(parents=True)
mp = pytest.MonkeyPatch()
w = seeded_world(out, http=entry_site.install(mp), settings_kw={"max_links_per_entity": 4})
s = w.sup.run()
w.conn.commit()
print(s.status, s.stopped_reason, dict(s.outcomes))
print("db:", out / "data" / "test.db")
