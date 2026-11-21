"""Re-run only the tests a label says fail, to weed out spurious (load-induced) failures.

Usage: relabel_check.py <repo_root> <tests.json> <patch_dir> <labels_dir> <tmp_dir>
Rewrites each label in place, keeping the first verdict under "failing_first".
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from br import patch  # noqa: E402
from br.engine import Lane  # noqa: E402

root, tests_json, patch_dir, labels, tmp = [os.path.abspath(p) for p in sys.argv[1:6]]
tests = {t["number"]: t for t in json.load(open(tests_json))["tests"]}
lane = Lane(root, tmp, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
lane.ensure("bare")
changed = 0
for f in sorted(os.listdir(labels)):
    p = os.path.join(labels, f)
    d = json.load(open(p))
    if not d.get("failing") or "failing_first" in d:
        continue
    cid = f[:-5]
    files = {}
    for ch in patch.parse(open(os.path.join(patch_dir, cid + ".patch"), "rb").read()):
        old = open(os.path.join(root, ch.old_path), "rb").read() if ch.old_path and os.path.exists(os.path.join(root, ch.old_path)) else None
        files[ch.path] = patch.apply(old, ch)
    run = [tests[n] for n in sorted(d["failing"])]
    try:
        for rel, content in files.items():
            lane.overlay.set_file(rel, content)
        results, err = lane.run("bare", [(t["id"], max(10.0, 10.0 * t["seconds"])) for t in run], deadline=900.0)
    finally:
        lane.overlay.restore()
    again = sorted(run[i]["number"] for i, r in results.items() if r[0] == "F")
    again += [run[i]["number"] for i in range(len(run)) if i not in results]
    d["failing_first"] = d["failing"]
    d["failing"] = sorted(set(again))
    json.dump(d, open(p, "w"))
    if d["failing"] != d["failing_first"]:
        changed += 1
        print(cid, "first", d["failing_first"][:8], "now", d["failing"][:8], flush=True)
lane.close()
print("rechecked", labels, "changed", changed)
