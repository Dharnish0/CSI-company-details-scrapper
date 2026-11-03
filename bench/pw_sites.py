import re
import time
import urllib.robotparser as rp
from urllib.parse import urlsplit

import requests
from playwright.sync_api import sync_playwright

from corpdisc.config import load_settings

UA = load_settings().user_agent
URLS = [
    "https://www.csisoftware.com/operating-groups",
    "https://velasoftwaregroup.com/portfolio",
    "https://jonassoftware.com/our-companies",
]


def words(html_or_text):
    t = re.sub(r"(?is)<(script|style).*?</\1>", " ", html_or_text)
    t = re.sub(r"<[^>]+>", " ", t)
    return len(t.split())


with sync_playwright() as p:
    b = p.chromium.launch()
    ctx = b.new_context(user_agent=UA)
    for u in URLS:
        o = urlsplit(u)
        r = rp.RobotFileParser()
        r.set_url(f"{o.scheme}://{o.netloc}/robots.txt")
        try:
            r.read()
            allowed = r.can_fetch(UA, u)
        except Exception as e:
            allowed = f"robots unreadable: {type(e).__name__}"
        print(u)
        print("   robots allowed:", allowed)
        if allowed is not True:
            continue
        s = requests.get(u, headers={"User-Agent": UA}, timeout=20)
        sw = words(s.text)
        time.sleep(2)  # REQUEST_DELAY_SECONDS
        pg = ctx.new_page()
        resp = pg.goto(u, wait_until="networkidle", timeout=30000)
        rendered = pg.inner_text("body")
        print(f"   static http {s.status_code} words={sw} | playwright http {resp.status} words={len(rendered.split())}")
        pg.close()
        time.sleep(2)
    b.close()
