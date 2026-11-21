"""Run trace_slices_one.py for every fixture-driven test worth slicing.

Usage: trace_slices_all.py <repo_root> <tests.json> <out_dir> [jobs] [min_seconds]
"""
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

root, tests_json, out_dir = [os.path.abspath(p) for p in sys.argv[1:4]]
jobs = int(sys.argv[4]) if len(sys.argv) > 4 else 3
min_seconds = float(sys.argv[5]) if len(sys.argv) > 5 else 0.05
here = os.path.dirname(os.path.abspath(__file__))
os.makedirs(out_dir, exist_ok=True)
tests = json.load(open(tests_json))["tests"]
# only modules that use the fixture loaders can be sliced
uses = set()
for t in tests:
    f = t["file"]
    if f in uses:
        continue
    try:
        src = open(os.path.join(root, f), encoding="utf-8").read()
    except OSError:
        continue
    if "load_sql_fixture" in src:
        uses.add(f)
todo = [t for t in tests if t["file"] in uses and t["seconds"] >= min_seconds]
todo.sort(key=lambda t: -t["seconds"])


def run(t):
    out = os.path.join(out_dir, "%d.pkl" % t["number"])
    if os.path.exists(out):
        return
    p = subprocess.run([sys.executable, "-W", "ignore", os.path.join(here, "trace_slices_one.py"), root, t["id"], out], capture_output=True, text=True)
    last = [l for l in p.stdout.strip().split("\n") if l][-1:] or [p.stderr[-300:]]
    print(last[0], flush=True)


with ThreadPoolExecutor(jobs) as ex:
    list(ex.map(run, todo))
print("SLICES DONE", len(todo))
