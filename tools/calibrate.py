"""Measure what each test costs when run by our own runner (CPU ms, this machine).

Usage: calibrate.py <repo_root> <tests.json> <out_json> <tmp_dir>
Runs the whole suite once on the unpatched checkout in collection order, then the scaled
variants.  The selector uses these numbers (times a machine factor) to decide what is worth running.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from br import variants  # noqa: E402
from br.engine import Lane  # noqa: E402

root, tests_json, out, tmp = [os.path.abspath(p) for p in sys.argv[1:5]]
tests = json.load(open(tests_json))["tests"]
lane = Lane(root, tmp, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
lane.ensure("full")
jobs = [(t["id"], max(10.0, 10.0 * t["seconds"])) for t in tests]
results, err = lane.run("full", jobs, deadline=1200.0)
cpu = {}
bad = []
for i, t in enumerate(tests):
    r = results.get(i)
    if r is None or r[0] == "F":
        bad.append(t["id"])
    if r is not None:
        cpu[t["id"]] = r[1]
scaled = {}
jobs = [(tid, 60.0, {"scaled": 1}) for tid in variants.SCALED]
res2, err2 = lane.run("full", jobs, deadline=300.0)
for i, tid in enumerate(variants.SCALED):
    r = res2.get(i)
    if r is not None and r[0] == "P":
        scaled[tid] = r[1]
    else:
        bad.append("scaled:" + tid)
lane.close()
json.dump({"cpu_ms": cpu, "scaled_ms": scaled}, open(out, "w"))
total = sum(cpu.values())
rec = sum(t["seconds"] for t in tests) * 1000
print("tests", len(cpu), "cpu total %.1fs" % (total / 1000), "recorded total %.1fs" % (rec / 1000), "ratio %.3f" % (total / rec), "err", err, err2)
print("scaled", scaled)
print("not passing:", bad[:10])
