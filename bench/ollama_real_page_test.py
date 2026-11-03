import requests
import json
import time
import re
from pathlib import Path
from bs4 import BeautifulSoup
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

PAGES = [
    ("CSI", "https://www.csisoftware.com/operating-groups", "operating groups", "Harris"),
    ("Volaris", "https://www.volarisgroup.com/portfolio", "portfolio companies", "AskCody"),
    ("Jonas", "https://jonassoftware.com/our-companies", "portfolio companies and groups", "CORA"),
]

results = []
output = Path("bench/ollama_real_page_results.json")
output.parent.mkdir(exist_ok=True)
ua = "CSIResearchBenchmark/1.0"

for label, url, kind, hint in PAGES:
    print(f"\n=== {label} ===", flush=True)
    item = {"site": label, "url": url}

    try:
        parsed = urlsplit(url)
        robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
        rr = requests.get(robots_url, timeout=20,
                          headers={"User-Agent": ua})
        rr.raise_for_status()

        robots = RobotFileParser()
        robots.parse(rr.text.splitlines())

        if not robots.can_fetch(ua, url):
            item["error"] = "Robots policy disallows fetch"
            results.append(item)
            continue

        delay = robots.crawl_delay(ua) or robots.crawl_delay("*") or 1
        time.sleep(delay)

        response = requests.get(
            url, timeout=30, headers={"User-Agent": ua}
        )
        response.raise_for_status()

        soup = BeautifulSoup(response.text, "html.parser")
        for tag in soup(["script", "style", "footer"]):
            tag.decompose()

        text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))

        # Keep the benchmark small for CPU-only inference.
        pos = text.lower().find(hint.lower())
        if pos < 0:
            pos = 0

        excerpt = text[max(0, pos-200):pos+2400]

        item["source_chars"] = len(text)
        item["excerpt_chars"] = len(excerpt)
        item["excerpt_preview"] = excerpt[:250]
        print("Page characters:", len(text), flush=True)
        print("Excerpt preview:", excerpt[:250], flush=True)

        if len(excerpt) < 150:
            item["error"] = "Insufficient extracted page text"
            results.append(item)
            continue

        prompt = (
            f"Extract {kind} explicitly listed in this webpage text. "
            "Do not invent names or infer ownership. "
            "Exclude products, unrelated companies, and the page owner. "
            'Return JSON exactly as {"companies": ["name"]}. '
            "If there are no clear names, return an empty list.\n\n"
            f"Webpage text:\n{excerpt}"
        )

        start = time.time()
        r = requests.post(
            "http://localhost:11434/api/generate",
            json={
                "model": "qwen3:4b",
                "think": False,
                "stream": False,
                "format": "json",
                "prompt": prompt,
                "options": {
                    "num_ctx": 2048,
                    "num_predict": 220,
                    "temperature": 0
                }
            },
            timeout=240
        )
        r.raise_for_status()

        data = r.json()
        raw = data.get("response", "")
        parsed_result = json.loads(raw)
        companies = parsed_result.get("companies", [])

        item["companies"] = companies
        item["seconds"] = round(time.time()-start, 1)
        item["input_tokens"] = data.get("prompt_eval_count")
        item["output_tokens"] = data.get("eval_count")
        item["finish_reason"] = data.get("done_reason")
        item["not_found_in_excerpt"] = [
            name for name in companies
            if isinstance(name, str)
            and name.casefold() not in excerpt.casefold()
        ]

        print("Companies:", companies, flush=True)
        print("Seconds:", item["seconds"], flush=True)
        print("Not found in excerpt:",
              item["not_found_in_excerpt"], flush=True)

    except Exception as exc:
        item["error"] = f"{type(exc).__name__}: {exc}"
        print("ERROR:", item["error"], flush=True)

    finally:
        results.append(item) if item not in results else None
        output.write_text(
            json.dumps(results, indent=2, ensure_ascii=False),
            encoding="utf-8"
        )

print("\nResults saved to:", output)
