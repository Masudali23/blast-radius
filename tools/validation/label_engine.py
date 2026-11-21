"""Label patches by running the whole suite under each one with our own runner (fresh import).

Usage: label_engine.py <repo_root> <tests.json> <maps_dir> <patch_dir> <out_dir> <tmp_dir> [shard/of]
Writes <out_dir>/<id>.json = {"failing": [...], "ran": n, "cpu_ms": n, "err": ...}
The three slowest tests are skipped (assumed passing) when they never touch a changed file.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from br import patch  # noqa: E402
from br.engine import Lane  # noqa: E402
from br.maps import Maps  # noqa: E402

root, tests_json, maps_dir, patch_dir, out_dir, tmp = [os.path.abspath(p) for p in sys.argv[1:7]]
shard, of = (int(x) for x in sys.argv[7].split("/")) if len(sys.argv) > 7 else (0, 1)
tests = json.load(open(tests_json))["tests"]
maps = Maps(maps_dir)
idx = {i: n for n, i in enumerate(maps.ids)}
os.makedirs(out_dir, exist_ok=True)
lane = Lane(root, tmp, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
lane.ensure("bare")
SLOW = [t for t in tests if t["seconds"] > 6]
ids = sorted(f[:-6] for f in os.listdir(patch_dir) if f.endswith(".patch"))
for k, cid in enumerate(ids):
    if k % of != shard:
        continue
    out = os.path.join(out_dir, cid + ".json")
    if os.path.exists(out):
        continue
    data = open(os.path.join(patch_dir, cid + ".patch"), "rb").read()
    files = {}
    for ch in patch.parse(data):
        old = open(os.path.join(root, ch.old_path), "rb").read() if ch.old_path and os.path.exists(os.path.join(root, ch.old_path)) else None
        files[ch.path] = patch.apply(old, ch)
    # a test that never imports, executes or reads a changed file cannot be affected by it
    touched = 0
    for f in files:
        touched |= maps.any_line(f) | maps.opened.get(f, 0) | maps.imports.get(f, 0)
        if not maps.known(f) and f.endswith(".py") and f not in maps.opened:
            pass
    skip = {t["number"] for t in tests if not (touched >> idx[t["id"]]) & 1}
    run = [t for t in tests if t["number"] not in skip]
    try:
        for rel, content in files.items():
            lane.overlay.set_file(rel, content)
        jobs = [(t["id"], max(5.0, 8.0 * t["seconds"])) for t in run]
        results, err = lane.run("bare", jobs, stop_on_fail=False, deadline=900.0)
    finally:
        lane.overlay.restore()
    failing = sorted(run[i]["number"] for i, r in results.items() if r[0] == "F")
    missing = sorted(run[i]["number"] for i in range(len(run)) if i not in results)
    json.dump({"failing": failing, "ran": len(results), "not_run": missing, "skipped": len(skip), "cpu_ms": sum(r[1] for r in results.values()), "err": err}, open(out, "w"))
    print(cid, len(failing), "failing", len(results), "ran", err or "", flush=True)
lane.close()
