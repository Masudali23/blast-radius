"""Ground-truth labeler: apply each patch to a copy of the pinned repo and run the full pytest suite.

Usage: label.py <repo_root> <tests.json> <patch_dir> <out_dir> [jobs]
Writes <out_dir>/<id>.json = {"failing": [numbers], "collect_failed": [...], "exit": n}
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor

root, tests_json, patch_dir, out_dir = [os.path.abspath(p) for p in sys.argv[1:5]]
jobs = int(sys.argv[5]) if len(sys.argv) > 5 else 5
here = os.path.dirname(os.path.abspath(__file__))
tests = json.load(open(tests_json))["tests"]
num = {t["id"]: t["number"] for t in tests}
by_file = {}
for t in tests:
    by_file.setdefault(t["file"], []).append(t["number"])
os.makedirs(out_dir, exist_ok=True)
COPY = ["sqlglot", "tests", "sqlglotc", "pyproject.toml", "setup.py", "setup.cfg", "MANIFEST.in", "README.md", "Makefile", "benchmarks"]
TIMEOUT = os.environ.get("BR_TIMEOUT", "120")


def run(cid):
    out = os.path.join(out_dir, cid + ".json")
    if os.path.exists(out):
        return
    tmp = tempfile.mkdtemp(prefix="brl_")
    try:
        for name in COPY:
            src = os.path.join(root, name)
            if os.path.isdir(src):
                shutil.copytree(src, os.path.join(tmp, name), ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            elif os.path.exists(src):
                shutil.copy2(src, os.path.join(tmp, name))
        p = subprocess.run(["git", "apply", os.path.join(patch_dir, cid + ".patch")], capture_output=True, text=True, cwd=tmp)
        if p.returncode != 0:
            # fall back to patch(1)
            p = subprocess.run(["patch", "-p1", "-s", "-i", os.path.join(patch_dir, cid + ".patch")], capture_output=True, text=True, cwd=tmp)
            if p.returncode != 0:
                json.dump({"error": "apply: " + p.stderr[-500:] + p.stdout[-500:]}, open(out, "w"))
                return
        res = os.path.join(tmp, "_br_result.json")
        env = dict(os.environ, SKIP_INTEGRATION="0", PYTHONPATH=tmp + os.pathsep + here, BR_OUT=res, PYTHONDONTWRITEBYTECODE="1")
        p = subprocess.run(
            [sys.executable, "-m", "pytest", "tests", "-q", "-p", "no:cacheprovider", "-p", "brplugin", "--timeout", TIMEOUT, "-o", "addopts="],
            capture_output=True, text=True, cwd=tmp, env=env,
        )
        if not os.path.exists(res):
            json.dump({"error": "pytest: " + p.stdout[-1500:] + p.stderr[-1500:]}, open(out, "w"))
            return
        r = json.load(open(res))
        failing = set()
        unknown = []
        for nodeid in r["failed"]:
            if nodeid in num:
                failing.add(num[nodeid])
            else:
                unknown.append(nodeid)
        for nodeid in r["collect_failed"]:
            f = nodeid.split("::")[0]
            failing.update(by_file.get(f, []))
        json.dump({"failing": sorted(failing), "collect_failed": r["collect_failed"], "unknown": unknown, "exit": r["exit"], "tail": p.stdout[-300:]}, open(out, "w"))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


ids = sorted(f[:-6] for f in os.listdir(patch_dir) if f.endswith(".patch"))
with ThreadPoolExecutor(jobs) as ex:
    list(ex.map(run, ids))
print("labeled", len(ids))
