"""Run one sqlglot test in this (fresh) process and record what it touched.

Usage: trace_one.py <repo_root> <test_id> <out_json>

Records, for the single test:
  - func: lines executed inside function-like code objects (call time)
  - mod:  lines executed in module / class bodies (import time)
  - files: repo files opened that were not plain imports
  - dirs:  repo directories listed
  - spawn: whether a subprocess was started
"""
import json
import os
import sys
import time
import unittest


def main():
    root = os.path.realpath(sys.argv[1])
    test_id = sys.argv[2]
    out = sys.argv[3]
    os.chdir(root)
    sys.path.insert(0, root)
    os.environ.setdefault("SKIP_INTEGRATION", "0")
    prefix = root + os.sep

    func = {}
    mod = {}
    opened = set()
    listed = set()
    state = {"spawn": False}

    def audit(event, args):
        try:
            if event == "open":
                p = args[0]
                if isinstance(p, bytes):
                    p = os.fsdecode(p)
                if isinstance(p, str):
                    p = os.path.realpath(p) if not p.startswith(prefix) else p
                    if p.startswith(prefix) and "__pycache__" not in p:
                        opened.add(p[len(prefix):])
            elif event in ("os.listdir", "os.scandir", "glob.glob", "os.walk"):
                p = args[0]
                if isinstance(p, bytes):
                    p = os.fsdecode(p)
                if isinstance(p, str):
                    p = os.path.realpath(p)
                    if (p + os.sep).startswith(prefix):
                        listed.add(p[len(prefix):])
            elif event in ("subprocess.Popen", "os.system", "os.exec", "os.posix_spawn", "os.fork"):
                state["spawn"] = True
        except Exception:
            pass

    sys.addaudithook(audit)
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from br import inlinepool

    inlinepool.install()

    # --- which classes does the test instantiate?  Wrap every __init__ defined in the repo.
    import builtins
    import functools
    import types

    used = set()
    created = []
    wrapped = set()
    orig_build = builtins.__build_class__

    def build_class(func, name, *bases, **kw):
        cls = orig_build(func, name, *bases, **kw)
        try:
            code = func.__code__
            if code.co_filename.startswith(prefix):
                created.append((cls, code.co_filename[len(prefix):], code.co_firstlineno))
                init = cls.__dict__.get("__init__")
                if isinstance(init, types.FunctionType):

                    @functools.wraps(init)
                    def __init__(self, *a, _br_orig=init, **k):
                        used.add(type(self))
                        return _br_orig(self, *a, **k)

                    cls.__init__ = __init__
                    wrapped.add(cls)
        except Exception:
            pass
        return cls

    builtins.__build_class__ = build_class

    mon = sys.monitoring
    tool = mon.COVERAGE_ID
    mon.use_tool_id(tool, "blast")
    DISABLE = mon.DISABLE

    def on_line(code, line):
        fn = code.co_filename
        if fn.startswith(prefix):
            d = func if (code.co_flags & 1) else mod
            s = d.get(fn)
            if s is None:
                s = d[fn] = set()
            s.add(line)
        return DISABLE

    def on_start(code, offset):
        # A body that sits on the same line as its `def` / `lambda` produces no LINE event of
        # its own, so entering a code object counts as executing its first line.
        fn = code.co_filename
        if fn.startswith(prefix):
            d = func if (code.co_flags & 1) else mod
            s = d.get(fn)
            if s is None:
                s = d[fn] = set()
            s.add(code.co_firstlineno)
        return DISABLE

    mon.register_callback(tool, mon.events.LINE, on_line)
    mon.register_callback(tool, mon.events.PY_START, on_start)
    mon.set_events(tool, mon.events.LINE | mon.events.PY_START)

    path, cls_name, meth = test_id.split("::")
    module_name = path[:-3].replace("/", ".")
    t0 = time.perf_counter()
    outcome = "pass"
    detail = ""
    try:
        module = __import__(module_name, fromlist=["x"])
        cls = getattr(module, cls_name)
        suite = unittest.TestSuite([cls(meth)])
        result = unittest.TestResult()
        suite.run(result)
        if result.failures or result.errors or result.unexpectedSuccesses:
            outcome = "fail"
            detail = (result.failures + result.errors)[0][1][-2000:] if (result.failures or result.errors) else "unexpected success"
        elif result.skipped:
            outcome = "skip"
            detail = result.skipped[0][1]
    except BaseException as e:  # import error etc.
        outcome = "fail"
        detail = "import: %r" % (e,)
    elapsed = time.perf_counter() - t0
    mon.set_events(tool, 0)

    loaded = set()
    for m in list(sys.modules.values()):
        f = getattr(m, "__file__", None)
        if f and f.startswith(prefix):
            loaded.add(f[len(prefix):])

    key_of = {cls: "%s:%d" % (f, line) for cls, f, line in created}
    tracked = sorted({key_of[cls] for cls in key_of if any(b in wrapped for b in cls.__mro__)})
    used_keys = set()
    for cls in used:
        for b in cls.__mro__:
            k = key_of.get(b)
            if k:
                used_keys.add(k)
    rel = lambda d: {k[len(prefix):]: sorted(v) for k, v in d.items()}
    with open(out, "w") as fh:
        json.dump(
            {
                "id": test_id,
                "outcome": outcome,
                "detail": detail,
                "seconds": round(elapsed, 4),
                "func": rel(func),
                "mod": rel(mod),
                "files": sorted(opened - loaded),
                "imports": sorted(loaded),
                "dirs": sorted(listed),
                "spawn": state["spawn"],
                "classes": sorted(used_keys),
                "tracked": tracked,
            },
            fh,
        )


if __name__ == "__main__":
    main()
