import requests
from corpdisc.config import load_settings

UA = load_settings().user_agent
for host in ("https://www.csisoftware.com", "https://jonassoftware.com", "https://velasoftwaregroup.com"):
    r = requests.get(host + "/robots.txt", headers={"User-Agent": UA}, timeout=20)
    print(host, "HTTP", r.status_code, "bytes", len(r.text))
    lines = [l for l in r.text.splitlines() if l.strip() and not l.startswith("#")][:12]
    for l in lines:
        print("    ", l[:100])
