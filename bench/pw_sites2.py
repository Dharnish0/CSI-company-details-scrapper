import re
import time

import requests
from playwright.sync_api import sync_playwright

from corpdisc.config import load_settings

UA = load_settings().user_agent
URLS = [("https://www.csisoftware.com/operating-groups", 2), ("https://jonassoftware.com/our-companies", 10)]


def words(h):
    t = re.sub(r"(?is)<(script|style).*?</\1>", " ", h)
    return len(re.sub(r"<[^>]+>", " ", t).split())


with sync_playwright() as p:
    b = p.chromium.launch()
    ctx = b.new_context(user_agent=UA)
    for u, delay in URLS:
        s = requests.get(u, headers={"User-Agent": UA}, timeout=20)
        time.sleep(delay)
        pg = ctx.new_page()
        resp = pg.goto(u, wait_until="networkidle", timeout=30000)
        txt = pg.inner_text("body")
        print(u, "| static", s.status_code, "words", words(s.text), "| playwright", resp.status, "words", len(txt.split()))
        pg.close()
        time.sleep(delay)
    b.close()
