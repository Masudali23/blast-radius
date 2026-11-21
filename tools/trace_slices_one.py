"""Per-fixture-entry coverage for one test.

Usage: trace_slices_one.py <repo_root> <test_id> <out_pickle>

The test class is set up once; then the test runs in forked children, each allowed to see a
single fixture entry (plus one child that sees none), with line tracing on.  Children start
with cold caches, so every entry's coverage includes whatever it would compute for itself.
"""
import json
import os
import pickle
import sys
import time
import zlib

root = os.path.realpath(sys.argv[1])
test_id = sys.argv[2]
out = os.path.abspath(sys.argv[3])
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(root)
sys.path.insert(0, root)
os.environ.setdefault("SKIP_INTEGRATION", "0")
for var in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS"):
    os.environ[var] = "1"

from br import inlinepool, variants, worker  # noqa: E402

inlinepool.install()
worker._limit_duckdb_threads()
prefix = root + os.sep
mon = sys.monitoring
tool = mon.COVERAGE_ID
mon.use_tool_id(tool, "blast")
lines = set()


def on_line(code, line):
    fn = code.co_filename
    if (code.co_flags & 1) and fn.startswith(prefix):
        lines.add((fn, line))
    return mon.DISABLE


def on_start(code, offset):
    fn = code.co_filename
    if (code.co_flags & 1) and fn.startswith(prefix):
        lines.add((fn, code.co_firstlineno))
    return mon.DISABLE


EVENTS = mon.events.LINE | mon.events.PY_START
mon.register_callback(tool, mon.events.LINE, on_line)
mon.register_callback(tool, mon.events.PY_START, on_start)
runner = worker.Runner(os.open(os.devnull, os.O_WRONLY))
path, cls_name, meth = test_id.split("::")
module = runner._module(path[:-3].replace("/", "."))
if isinstance(module, BaseException):
    raise module
cls = getattr(module, cls_name)
runner._class(cls)


def in_child(fn):
    r, w = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(r)
        try:
            payload = fn()
        except BaseException as e:  # noqa: BLE001
            payload = {"error": repr(e)}
        with os.fdopen(w, "wb") as fh:
            fh.write(pickle.dumps(payload))
        os._exit(0)
    os.close(w)
    with os.fdopen(r, "rb") as fh:
        data = fh.read()
    os.waitpid(pid, 0)
    return pickle.loads(data) if data else {"error": "no data"}


def record():
    variants.SL.record = []
    res = runner.result
    test = cls(meth)
    variants.apply_slices(test, cls, None)
    test.run(res)
    ok = not (res.failures or res.errors or res.unexpectedSuccesses)
    seen = []
    known = set()
    for k in variants.SL.record:
        if k not in known:
            known.add(k)
            seen.append(k)
    return {"keys": seen, "ok": ok}


def traced(allow):
    def go():
        mon.set_events(tool, EVENTS)
        t0 = time.process_time()
        outcome = runner.run_one(test_id, {"allow": list(allow)})
        cpu = time.process_time() - t0
        mon.set_events(tool, 0)
        per_file = {}
        for fn, line in lines:
            per_file.setdefault(fn[len(prefix):], []).append(line)
        return {"outcome": outcome, "lines": per_file, "cpu": cpu}

    return go


rec = in_child(record)
result = {"id": test_id, "keys": [], "ok": bool(rec.get("ok")), "bad": []}
keys = rec.get("keys") or []
if keys and rec.get("ok"):
    base = in_child(traced(()))
    if base.get("outcome") != "P":
        result["bad"].append("<base>")
    table = {}
    cost = []
    base_cpu = base.get("cpu", 0.0)
    for i, k in enumerate(keys):
        r = in_child(traced((k,)))
        cost.append(max(0.0, r.get("cpu", 0.0) - base_cpu))
        if r.get("outcome") != "P":
            result["bad"].append(k)
            continue
        bit = 1 << i
        for f, ls in r["lines"].items():
            t = table.setdefault(f, {})
            for ln in ls:
                t[ln] = t.get(ln, 0) | bit
    result["keys"] = keys
    total = sum(cost) or 1.0
    result["share"] = [c / total for c in cost]  # each entry's share of the test's run time
    result["base"] = {f: sorted(ls) for f, ls in base.get("lines", {}).items()}
    result["lines"] = table
with open(out, "wb") as fh:
    fh.write(zlib.compress(pickle.dumps(result, protocol=5), 6))
print(test_id, "keys", len(keys), "bad", len(result["bad"]), "ok", result["ok"])
