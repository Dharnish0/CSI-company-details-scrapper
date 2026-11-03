"""One-off probe (2 GETs, TLS verification on, robots.txt honoured): does a Vela vertical page name companies?"""
import re
import urllib.request
import urllib.robotparser

UA = "CorpDiscoveryBot/0.1"
rp = urllib.robotparser.RobotFileParser("https://velasoftwaregroup.com/robots.txt")
rp.read()
for u in ("https://velasoftwaregroup.com/automotive", "https://velasoftwaregroup.com/travel"):
    ok = rp.can_fetch(UA, u)
    print(u, "robots_allowed=", ok)
    if not ok:
        continue
    req = urllib.request.Request(u, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=20) as r:
        html = r.read().decode("utf-8", "replace")
        print(" status", r.status, "bytes", len(html), "final", r.geturl())
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.S)
    text = re.sub(r"<[^>]+>", "\n", text)
    lines = [x.strip() for x in text.splitlines() if x.strip()]
    body = [x for x in lines if len(x) > 2][14:60]
    print("  ", " | ".join(body).encode("ascii", "replace").decode()[:900])
