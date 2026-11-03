import csv
import sys

rows = list(csv.DictReader(open(sys.argv[1])))
cpu = [float(r["cpu_pct"]) for r in rows]
free = [float(r["free_ram_gb"]) for r in rows]
ws = [float(r["ollama_ws_gb"]) for r in rows]
print(f"{sys.argv[1]}: samples={len(rows)} ({rows[0]['time']} to {rows[-1]['time']}) "
      f"cpu avg={sum(cpu)/len(cpu):.0f}% max={max(cpu):.0f}% | free RAM min={min(free):.2f} GB | ollama working set max={max(ws):.2f} GB")
