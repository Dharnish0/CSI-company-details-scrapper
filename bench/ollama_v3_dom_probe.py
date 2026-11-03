import requests
import json
import re
import time
from pathlib import Path
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser

PAGES = {
    "CSI": "https://www.csisoftware.com/operating-groups",
    "Jonas": "https://jonassoftware.com/our-companies",
    "Volaris": "https://www.volarisgroup.com/portfolio"
}

UA = "CSIResearchBenchmark/2.0"
results = {}

for site, url in PAGES.items():
    print(f"\n{'=' * 45}", flush=True)
    print(f"TESTING: {site}", flush=True)

    try:
        parts = urlsplit(url)
        robots_url = f"{parts.scheme}://{parts.netloc}/robots.txt"

        rr = requests.get(
            robots_url,
            headers={"User-Agent": UA},
            timeout=20
        )
        rr.raise_for_status()

        robots = RobotFileParser()
        robots.parse(rr.text.splitlines())

        if not robots.can_fetch(UA, url):
            raise RuntimeError("Robots policy disallows fetch")

        delay = robots.crawl_delay(UA)
        if delay is None:
            delay = robots.crawl_delay("*")
        time.sleep(max(1, delay or 1))

        response = requests.get(
            url,
            headers={"User-Agent": UA},
            timeout=30
        )
        response.raise_for_status()

        soup = BeautifulSoup(response.text, "html.parser")

        for tag in soup.select(
            "script, style, noscript, nav, footer"
        ):
            tag.decompose()

        root = soup.select_one("main") or soup.body or soup

        candidates = []
        seen = set()

        for tag in root.find_all(["h2", "h3", "h4", "a"]):
            name = re.sub(
                r"\s+", " ",
                tag.get_text(" ", strip=True)
            ).strip()

            if not 2 <= len(name) <= 100:
                continue

            link = ""
            if tag.name == "a" and tag.get("href"):
                link = urljoin(url, tag["href"])

            parent = tag.find_parent(
                ["article", "li", "section", "div"]
            )

            evidence = re.sub(
                r"\s+", " ",
                parent.get_text(" ", strip=True)
                if parent else name
            )[:250]

            key = (name.casefold(), link)
            if key in seen:
                continue

            seen.add(key)
            candidates.append({
                "name": name,
                "tag": tag.name,
                "link": link,
                "evidence": evidence
            })

        results[site] = {
            "url": url,
            "candidates": candidates
        }

        print("Candidates:", len(candidates), flush=True)

        for item in candidates[:40]:
            print(f"[{item['tag']}] {item['name']}")
            if item["link"]:
                print("  URL:", item["link"])

    except Exception as e:
        print("ERROR:", str(e), flush=True)
        results[site] = {"error": str(e)}

output = Path("bench/ollama_v3_dom_candidates.json")
output.write_text(
    json.dumps(results, indent=2, ensure_ascii=False),
    encoding="utf-8"
)

print("\nSaved:", output)
