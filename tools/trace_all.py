"""Trace every test in tests.json in its own process.

Usage: trace_all.py <repo_root> <tests.json> <out_dir> [jobs]
Set BR_INSTRUMENT=1 for the pass that records class and dict usage (see trace_one.py)."""
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

root, tests_json, out_dir = sys.argv[1:4]
jobs = int(sys.argv[4]) if len(sys.argv) > 4 else 6
os.makedirs(out_dir, exist_ok=True)
tests = json.load(open(tests_json))["tests"]
here = os.path.dirname(os.path.abspath(__file__))


def run(t):
    out = os.path.join(out_dir, "%d.json" % t["number"])
    if os.path.exists(out):
        return
    p = subprocess.run(
        [sys.executable, os.path.join(here, "trace_one.py"), root, t["id"], out],
        capture_output=True,
        text=True,
    )
    if not os.path.exists(out):
        sys.stderr.write("FAILED %s\n%s\n" % (t["id"], p.stderr[-500:]))


# heavy tests first so they do not straggle
tests.sort(key=lambda t: -t["seconds"])
with ThreadPoolExecutor(jobs) as ex:
    list(ex.map(run, tests))
print("done", len(os.listdir(out_dir)))
