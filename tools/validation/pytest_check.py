"""Cross-check our runner against real pytest on the tests it reports as failing.

Usage: pytest_check.py <repo_root> <tests.json> <patch_dir> <labels_dir> <out_json> [max_failing]

For every labelled patch with failing tests, apply the patch to a copy of the repo and run
exactly those tests under pytest (as the organisers do).  A test our runner calls failing but
pytest passes is a false positive of the runner.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

root, tests_json, patch_dir, labels, out = [os.path.abspath(p) for p in sys.argv[1:6]]
max_failing = int(sys.argv[6]) if len(sys.argv) > 6 else 40
here = os.path.dirname(os.path.abspath(__file__))
tests = json.load(open(tests_json))["tests"]
ident = {t["number"]: t["id"] for t in tests}
num = {t["id"]: t["number"] for t in tests}
COPY = ["sqlglot", "tests", "sqlglotc", "pyproject.toml", "setup.py", "setup.cfg"]
done = json.load(open(out)) if os.path.exists(out) else {}
for f in sorted(os.listdir(labels)):
    cid = f[:-5]
    if cid in done:
        continue
    d = json.load(open(os.path.join(labels, f)))
    failing = d.get("failing") or []
    if not failing or len(failing) > max_failing:
        continue
    tmp = tempfile.mkdtemp(prefix="brp_")
    try:
        for name in COPY:
            src = os.path.join(root, name)
            if os.path.isdir(src):
                shutil.copytree(src, os.path.join(tmp, name), ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            elif os.path.exists(src):
                shutil.copy2(src, os.path.join(tmp, name))
        p = subprocess.run(["git", "apply", os.path.join(patch_dir, cid + ".patch")], capture_output=True, text=True, cwd=tmp)
        if p.returncode != 0:
            done[cid] = {"error": "apply"}
            continue
        res = os.path.join(tmp, "_res.json")
        env = dict(os.environ, SKIP_INTEGRATION="0", PYTHONPATH=tmp + os.pathsep + here, BR_OUT=res, PYTHONDONTWRITEBYTECODE="1")
        ids = [ident[n] for n in failing]
        p = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-p", "brplugin", "--timeout", "900", "-o", "addopts="] + ids, capture_output=True, text=True, cwd=tmp, env=env)
        if not os.path.exists(res):
            done[cid] = {"error": p.stdout[-300:] + p.stderr[-300:]}
            continue
        r = json.load(open(res))
        py_fail = set()
        for nodeid in r["failed"]:
            if nodeid in num:
                py_fail.add(num[nodeid])
        for nodeid in r["collect_failed"]:
            path = nodeid.split("::")[0]
            py_fail.update(n for n in failing if ident[n].startswith(path + "::"))
        false_pos = sorted(set(failing) - py_fail)
        done[cid] = {"engine": failing, "pytest": sorted(py_fail), "false_positive": false_pos}
        if false_pos:
            print(cid, "runner-only failures:", [ident[n] for n in false_pos], flush=True)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        json.dump(done, open(out, "w"))
print("checked", len(done), "with false positives:", sum(1 for v in done.values() if v.get("false_positive")))
