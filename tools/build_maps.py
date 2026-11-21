"""Compile per-test traces into compact per-file maps.

Usage: build_maps.py <trace_dir> <tests.json> <import_trace.json> <out_dir>

Output (all zlib-compressed pickles of plain dicts; bitmasks are Python ints, bit i = test index i):
  index.pkl          {"ids": [test ids in index order], "files": {src_path: blob_name}, "opened": {path: mask},
                      "imports": {path: mask}, "spawn": mask, "import_func": {path: [lines]}}
  f_<n>.pkl          {"func": {line: mask}, "mod": {line: mask}}
"""
import json
import os
import pickle
import sys
import zlib

trace_dir, tests_json, import_trace, out_dir = sys.argv[1:5]
tests = json.load(open(tests_json))["tests"]
os.makedirs(out_dir, exist_ok=True)
ids = [t["id"] for t in tests]
func = {}
mod = {}
opened = {}
imports = {}
spawn = 0
class_users = {}  # "file:line" of a class -> tests that instantiate it or a subclass
tracked = set()  # classes whose instantiation we can observe
bad = []
for i, t in enumerate(tests):
    p = os.path.join(trace_dir, "%d.json" % t["number"])
    d = json.load(open(p))
    assert d["id"] == t["id"]
    if d["outcome"] != "pass":
        bad.append((t["id"], d["outcome"], d["detail"][-200:]))
    bit = 1 << i
    for f, lines in d["func"].items():
        m = func.setdefault(f, {})
        for ln in lines:
            m[ln] = m.get(ln, 0) | bit
    for f, lines in d["mod"].items():
        m = mod.setdefault(f, {})
        for ln in lines:
            m[ln] = m.get(ln, 0) | bit
    for f in d["files"]:
        opened[f] = opened.get(f, 0) | bit
    for f in d["imports"]:
        imports[f] = imports.get(f, 0) | bit
    if d["spawn"]:
        spawn |= bit
    for k in d.get("classes", ()):
        class_users[k] = class_users.get(k, 0) | bit
    tracked.update(d.get("tracked", ()))
files = {}
total = 0
for n, f in enumerate(sorted(set(func) | set(mod))):
    name = "f_%d.pkl" % n
    blob = zlib.compress(pickle.dumps({"func": func.get(f, {}), "mod": mod.get(f, {})}, protocol=5), 6)
    open(os.path.join(out_dir, name), "wb").write(blob)
    files[f] = name
    total += len(blob)
sliced = {}
slice_dir = sys.argv[5] if len(sys.argv) > 5 else None
if slice_dir and os.path.isdir(slice_dir):
    for t in tests:
        sp = os.path.join(slice_dir, "%d.pkl" % t["number"])
        if not os.path.exists(sp):
            continue
        d = pickle.loads(zlib.decompress(open(sp, "rb").read()))
        if not d.get("keys") or d.get("bad") or not d.get("ok") or len(d["keys"]) < 2:
            continue
        name = "s_%d.pkl" % ids.index(t["id"])
        blob = zlib.compress(pickle.dumps({"keys": d["keys"], "lines": d["lines"], "share": d["share"]}, protocol=5), 6)
        open(os.path.join(out_dir, name), "wb").write(blob)
        sliced[t["id"]] = name
        total += len(blob)
print("sliced tests", len(sliced))
calib = {}
calib_path = sys.argv[6] if len(sys.argv) > 6 else None
if calib_path and os.path.exists(calib_path):
    calib = json.load(open(calib_path))
index = {
    # CPU ms each test costs in our own runner on the machine the maps were built on
    "cpu_ms": [calib.get("cpu_ms", {}).get(i) for i in ids],
    "scaled_ms": calib.get("scaled_ms", {}),
    "sliced": sliced,
    "class_users": class_users,
    "tracked": sorted(tracked),
    "ids": ids,
    "files": files,
    "opened": opened,
    "imports": imports,
    "spawn": spawn,
    "import_func": {k: v for k, v in json.load(open(import_trace)).items() if k != "__files__"},
    "import_files": json.load(open(import_trace)).get("__files__", []),
}
blob = zlib.compress(pickle.dumps(index, protocol=5), 6)
open(os.path.join(out_dir, "index.pkl"), "wb").write(blob)
print("files", len(files), "bytes", total + len(blob), "non-pass traces", len(bad))
for b in bad[:20]:
    print("  ", b)
