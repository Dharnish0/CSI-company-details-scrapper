import pathlib
import sys

sys.path.insert(0, "src")
from corpdisc.services.links import build_link_index, section_links, with_text_urls

html = pathlib.Path(".cache/pages/ebe343b6c44a813b0d478cb0fd3c04d954d36794cfd3f59c378d2dca67ea42f5.html").read_text(encoding="utf-8", errors="replace")
idx = build_link_index(html, "https://velasoftwaregroup.com/portfolio/")
sl = section_links(idx, "https://velasoftwaregroup.com/portfolio/", listing_page=True)
print(len(idx.links), "links;", len(sl), "section links")
for lk in sl[:30]:
    print(lk.link_id, lk.location, lk.url, "|", lk.anchor_text.encode("ascii", "replace").decode())
t = "CORA Group\nwww.thecoragroup.com\nContact - Zach\ninfo@velasoftwaregroup.com visit https://x.example/a/b. Node.js index.html foo.bar.co.uk,"
ti = with_text_urls(idx, t)
for lk in ti.links[len(idx.links):]:
    print("TEXT", lk.link_id, lk.url, lk.raw_href, lk.text_offset)
