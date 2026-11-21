"""Trace every test in tests.json in its own process.

Usage: trace_rel_all.py <repo_root> <tests.json> <out_dir> [jobs] [light-first]
The pass that records comparisons and and/or conditions (see trace_rel_one.py).  Set
BR_REL_CACHE to a directory to compile the rewritten modules only once."""
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
    if os.path.exists(out + ".gz"):
        return
    p = subprocess.run(
        [sys.executable, os.path.join(here, "trace_rel_one.py"), root, t["id"], out],
        capture_output=True,
        text=True,
    )
    if not os.path.exists(out + ".gz"):
        sys.stderr.write("FAILED %s\n%s\n" % (t["id"], p.stderr[-500:]))


# heavy tests first so they do not straggle
tests.sort(key=lambda t: -t["seconds"])
if len(sys.argv) > 5 and sys.argv[5] == "light-first":
    tests.reverse()  # lets a second copy of this script work from the other end
with ThreadPoolExecutor(jobs) as ex:
    list(ex.map(run, tests))
print("done", len(os.listdir(out_dir)))
