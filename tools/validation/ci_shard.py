"""End-to-end check of one shard of synthetic patches, meant to run inside the image.

Usage: ci_shard.py <src_dir> <out_dir> <seed> <count> <shard> <of> [kinds...]

For each patch of the shard:
  truth  = tests that fail when the whole suite runs under pytest on the patched checkout
  ours   = what selector.py selects
and report misses (a failing test we did not select) and cost against the best possible.
"""
import json
import os
import shutil
import subprocess
import sys
import time

src, out = os.path.abspath(sys.argv[1]), os.path.abspath(sys.argv[2])
seed, count, shard, of = (int(x) for x in sys.argv[3:7])
kinds = sys.argv[7:] or ["code"] * 16 + ["fixture"] * 8 + ["multi"] * 3 + ["comment"] * 3
REPO = os.environ.get("BR_REPO", "/app/repo")
sys.path.insert(0, src)
from br import patch  # noqa: E402

os.makedirs(out, exist_ok=True)
mut = os.path.join(out, "mut")
subprocess.run([sys.executable, os.path.join(src, "tools/validation/gen_mutants.py"), REPO, mut, str(count), str(seed)] + kinds, check=True, env=dict(os.environ, BR_GEN_UNIFORM="1"))
ids = sorted(f[:-6] for f in os.listdir(mut) if f.endswith(".patch"))
mine = [c for i, c in enumerate(ids) if i % of == shard]
tests = json.load(open(os.path.join(src, "sample/tests.json")))["tests"]
num = {t["id"]: t["number"] for t in tests}
by_file = {}
for t in tests:
    by_file.setdefault(t["file"], []).append(t["number"])
ms = {t["number"]: int(t["seconds"] * 1000 + 0.5) for t in tests}

# --- our selection, all of the shard's patches in one batch
data = os.path.join(out, "data")
os.makedirs(os.path.join(data, "commits"), exist_ok=True)
shutil.copy(os.path.join(src, "sample/tests.json"), data)
for c in mine:
    shutil.copy(os.path.join(mut, c + ".patch"), os.path.join(data, "commits"))
t0 = time.time()
cpu0 = os.times()
p = subprocess.run([sys.executable, os.path.join(src, "selector.py")], capture_output=True, text=True,
                   env=dict(os.environ, BR_DATA=data, BR_REPO=REPO, BR_MAPS=os.path.join(src, "maps"), BR_TMP="/tmp/br_v", BR_DEBUG=os.path.join(out, "debug.json"), BR_SOFT="100000"))
cpu1 = os.times()
sel_cpu = (cpu1.children_user + cpu1.children_system) - (cpu0.children_user + cpu0.children_system)
ours = {}
for line in p.stdout.splitlines():
    j = json.loads(line)
    ours[j["commit"]] = j["workers"]
print("selector: %d commits, %.1fs wall" % (len(ours), time.time() - t0), p.stderr[-400:], flush=True)

# --- truth: the whole suite under pytest, per patch
plugin_dir = os.path.join(src, "tools/validation")
truth = {}
for c in mine:
    work = "/tmp/w_" + c
    shutil.rmtree(work, ignore_errors=True)
    for name in ("sqlglot", "tests", "sqlglotc"):
        shutil.copytree(os.path.join(REPO, name), os.path.join(work, name), ignore=shutil.ignore_patterns("__pycache__"))
    for name in ("pyproject.toml", "setup.py", "setup.cfg"):
        if os.path.exists(os.path.join(REPO, name)):
            shutil.copy(os.path.join(REPO, name), work)
    for ch in patch.parse(open(os.path.join(mut, c + ".patch"), "rb").read()):
        old = None
        if ch.old_path and os.path.exists(os.path.join(REPO, ch.old_path)):
            old = open(os.path.join(REPO, ch.old_path), "rb").read()
        new = patch.apply(old, ch)
        dst = os.path.join(work, ch.path)
        if new is None:
            os.remove(dst)
        else:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            open(dst, "wb").write(new)
    res = os.path.join(work, "_res.json")
    t1 = time.time()
    subprocess.run([sys.executable, "-m", "pytest", "tests", "-q", "-p", "no:cacheprovider", "-p", "brplugin", "--timeout", "900", "-o", "addopts="],
                   capture_output=True, text=True, cwd=work,
                   env=dict(os.environ, SKIP_INTEGRATION="0", PYTHONPATH=work + os.pathsep + plugin_dir, BR_OUT=res, PYTHONDONTWRITEBYTECODE="1"))
    failing = None
    if os.path.exists(res):
        r = json.load(open(res))
        failing = set(num[n] for n in r["failed"] if n in num)
        for nodeid in r["collect_failed"]:
            failing.update(by_file.get(nodeid.split("::")[0], []))
        failing = sorted(failing)
    truth[c] = failing
    shutil.rmtree(work, ignore_errors=True)
    print(c, "pytest failing:", None if failing is None else len(failing), "%.0fs" % (time.time() - t1), flush=True)


def best(nums):
    if not nums:
        return 0
    items = sorted(nums, key=lambda n: -ms[n])
    b = None
    for k in range(1, 9):
        loads = [0] * k
        for n in items:
            i = loads.index(min(loads))
            loads[i] += ms[n]
        c = max(loads) + 2000 * k
        b = c if b is None or c < b else b
    return b


meta = json.load(open(os.path.join(mut, "_meta.json")))
debug = json.load(open(os.path.join(out, "debug.json"))) if os.path.exists(os.path.join(out, "debug.json")) else {}
rows, misses, cost, oracle = [], 0, 0, 0
for c in mine:
    sel = {n for w in ours.get(c, []) for n in w}
    f = truth.get(c)
    w = ours.get(c, [])
    mycost = (max(sum(ms[n] for n in x) for x in w) + 2000 * len(w)) if w else 0
    miss = sorted(set(f) - sel) if f is not None else None
    if miss:
        misses += 1
    cost += mycost
    oracle += best(f or [])
    rows.append({"commit": c, "kind": meta[c]["kind"], "op": meta[c]["op"], "files": meta[c]["files"], "truth": f, "selected": sorted(sel), "missed": miss, "cost": mycost, "oracle": best(f or []), "how": debug.get(c, {}).get("how")})
    if miss:
        print("MISS", c, meta[c], "missed", miss[:8], "how", debug.get(c, {}).get("how"), flush=True)
json.dump({"seed": seed, "shard": shard, "rows": rows, "selector_cpu": sel_cpu}, open(os.path.join(out, "result_%d_%d.json" % (seed, shard)), "w"))
for c in mine:
    shutil.copy(os.path.join(mut, c + ".patch"), out)
print("SHARD %d/%d seed %d: commits %d misses %d cost %.1f oracle %.1f selector cpu %.1fs" % (shard, of, seed, len(mine), misses, cost / 1000, oracle / 1000, sel_cpu))
