import requests
import json
import re
import time
from pathlib import Path
from bs4 import BeautifulSoup
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

PAGES = [
    {
        "name": "CSI",
        "url": "https://www.csisoftware.com/operating-groups",
        "expected": [
            "Harris", "Volaris", "Jonas Software",
            "Topicus.com", "Perseus", "Vela",
            "Lumine Group", "Modaxo", "Andromeda"
        ]
    },
    {
        "name": "Jonas",
        "url": "https://jonassoftware.com/our-companies",
        "expected": [
            "CORA Group", "Vertus Group",
            "Vesta Software Group", "Pyxis Software Group",
            "Jonas Software ANZ"
        ]
    },
    {
        "name": "Volaris",
        "url": "https://www.volarisgroup.com/portfolio",
        "expected": ["AskCody"]
    }
]

OUT = Path("bench/ollama_v2_results.json")
OUT.parent.mkdir(exist_ok=True)
results = []
UA = "CSIResearchBenchmark/2.0"

def normalize(text):
    return re.sub(r"\s+", " ", str(text)).strip().casefold()

def fetch_page(url):
    p = urlsplit(url)
    robots_url = f"{p.scheme}://{p.netloc}/robots.txt"

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

    r = requests.get(
        url,
        headers={"User-Agent": UA},
        timeout=30
    )
    r.raise_for_status()

    soup = BeautifulSoup(r.text, "html.parser")

    for tag in soup.select(
        "script, style, noscript, nav, footer"
    ):
        tag.decompose()

    # Preserve separate text lines instead of
    # flattening the webpage into one paragraph.
    lines = []
    previous = None

    for raw in soup.get_text("\n", strip=True).splitlines():
        line = re.sub(r"\s+", " ", raw).strip()
        if line and line != previous:
            lines.append(line)
            previous = line

    return "\n".join(lines)

def extract(chunk, site):
    prompt = f"""
You are extracting organization names from an
official {site} corporate webpage.

STRICT RULES:
1. Extract actual company or portfolio-group names.
2. Exclude people and contact names.
3. Exclude products, services, industries and headings.
4. Exclude the page owner itself.
5. Do not assume a name belongs to this portfolio
   just because it appears on the page.
6. Exclude explicitly unrelated operating groups.
7. Do not invent or reconstruct company names.
8. Every name must appear exactly in the source.
9. Provide a short, verbatim evidence quote for each.
10. If uncertain, omit the candidate.

Return ONLY JSON in this format:

{{
  "companies": [
    {{
      "name": "Exact Company Name",
      "evidence": "Exact quote from source text"
    }}
  ]
}}

SOURCE TEXT:
{chunk}
"""

    start = time.time()

    response = requests.post(
        "http://localhost:11434/api/generate",
        json={
            "model": "qwen3:4b",
            "think": False,
            "stream": False,
            "format": "json",
            "prompt": prompt,
            "keep_alive": "5m",
            "options": {
                "num_ctx": 2048,
                "num_predict": 400,
                "temperature": 0
            }
        },
        timeout=240
    )

    response.raise_for_status()
    data = response.json()

    return (
        json.loads(data.get("response") or "{}"),
        round(time.time() - start, 2),
        data.get("done_reason")
    )

for page in PAGES:
    print("\n" + "=" * 45, flush=True)
    print("TESTING:", page["name"], flush=True)

    item = {
        "site": page["name"],
        "url": page["url"],
        "expected": page["expected"],
        "companies": [],
        "errors": []
    }

    try:
        text = fetch_page(page["url"])
        item["source_characters"] = len(text)

        print("Source characters:", len(text), flush=True)

        if len(text) < 150:
            raise RuntimeError("Insufficient HTML page text")

        # Split larger portfolio pages into smaller chunks.
        chunk_size = 2800
        step = 2600
        chunks = [
            text[i:i + chunk_size]
            for i in range(0, len(text), step)
        ]

        found = {}

        for index, chunk in enumerate(chunks, 1):
            print(
                f"Processing chunk {index}/{len(chunks)}",
                flush=True
            )

            try:
                response, seconds, finish = extract(
                    chunk, page["name"]
                )

                print("Time:", seconds, "seconds", flush=True)
                print("Finish:", finish, flush=True)

                if finish != "stop":
                    item["errors"].append(
                        f"Chunk {index}: incomplete output ({finish})"
                    )

                entries = response.get("companies", [])

                if not isinstance(entries, list):
                    raise ValueError("companies must be a list")

                for entry in entries:
                    if not isinstance(entry, dict):
                        continue

                    name = str(entry.get("name") or "").strip()
                    evidence = str(
                        entry.get("evidence") or ""
                    ).strip()

                    if not name:
                        continue

                    grounded = (
                        bool(evidence)
                        and normalize(name) in normalize(chunk)
                        and normalize(evidence) in normalize(chunk)
                        and normalize(name) in normalize(evidence)
                    )

                    record = {
                        "name": name,
                        "evidence": evidence,
                        "source_url": page["url"],
                        "grounded": grounded
                    }

                    found.setdefault(name.casefold(), record)

            except Exception as e:
                item["errors"].append(
                    f"Chunk {index}: {type(e).__name__}: {e}"
                )

        item["companies"] = list(found.values())

        extracted = {
            x["name"].casefold()
            for x in item["companies"]
            if x["grounded"]
        }

        item["expected_found"] = [
            name for name in page["expected"]
            if name.casefold() in extracted
        ]

        item["expected_missing"] = [
            name for name in page["expected"]
            if name.casefold() not in extracted
        ]

        print("\nEXTRACTED NAMES:", flush=True)

        for company in item["companies"]:
            status = (
                "GROUNDED"
                if company["grounded"]
                else "UNVERIFIED"
            )
            print(
                f"  {company['name']} [{status}]",
                flush=True
            )

        print(
            "Expected found:",
            item["expected_found"],
            flush=True
        )
        print(
            "Expected missing:",
            item["expected_missing"],
            flush=True
        )

    except Exception as e:
        item["errors"].append(
            f"{type(e).__name__}: {e}"
        )
        print("PAGE ERROR:", e, flush=True)

    finally:
        results.append(item)
        OUT.write_text(
            json.dumps(results, indent=2, ensure_ascii=False),
            encoding="utf-8"
        )

print("\nBenchmark saved:", OUT, flush=True)
