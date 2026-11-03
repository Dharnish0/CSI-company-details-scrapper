import sqlite3
import sys
import pathlib

c = sqlite3.connect("file:" + sys.argv[1] + "?mode=ro", uri=True)
tp, hp = c.execute("select text_path, html_path from crawl_pages where page_id=8").fetchone()
print(tp, hp)
for p in (tp,):
    f = pathlib.Path(p)
    if not f.is_absolute():
        for base in (pathlib.Path("."), pathlib.Path("./.cache")):
            if (base / p).exists():
                f = base / p
                break
    t = f.read_text(encoding="utf-8", errors="replace")
    print(len(t))
    print(t[:3500].encode("ascii", "replace").decode())
