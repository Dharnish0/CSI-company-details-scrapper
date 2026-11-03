import re
import pathlib

h = pathlib.Path("pages/ebe343b6c44a813b0d478cb0fd3c04d954d36794cfd3f59c378d2dca67ea42f5.html")
for base in (pathlib.Path("."), pathlib.Path("./.cache"), pathlib.Path("./bench/p4_scratch"), pathlib.Path("./data")):
    if (base / h).exists():
        h = base / h
        break
t = h.read_text(encoding="utf-8", errors="replace")
print(len(t))
for m in re.finditer(r'<a\s[^>]*href="([^"]+)"[^>]*>(.*?)</a>', t, re.S):
    txt = re.sub(r"<[^>]+>", "", m.group(2)).strip()[:50]
    print(m.group(1)[:90], "|", txt.encode("ascii", "replace").decode())
print("script tags:", len(re.findall(r"<script", t)), "| has 'vertical' filter markup:", "data-vertical" in t or "filter" in t.lower())
