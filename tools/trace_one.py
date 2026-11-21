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

    # --- which entries of class-level dicts does the test look up?
    # Every dict stored on a sqlglot class is replaced by a recording subclass.  Reads made while
    # classes are being built (imports) are not the test's: if function code (a metaclass, a
    # helper) reads a dict then, something else was derived from it and the name is marked.
    importing = [0]
    dict_keys = set()  # (attribute name, key) looked up at run time; key "*" = read as a whole
    derived = set()  # "name\tkey" (key "*" = whole) read by function code at import time
    where = set()
    SKIP_DICTS = {"arg_types", "__annotations__", "__dict__"}

    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from br.keys import key_repr

    DEBUG_WHOLE = os.environ.get("BR_DEBUG_WHOLE")
    debug_done = []

    def note(owner, k):
        if importing[0]:
            caller = sys._getframe(2).f_code
            if caller.co_flags & 1:
                derived.add("%s\t%s" % (owner, "*" if k is None else key_repr(k)))
                where.add("%s\t%s:%d" % (owner, caller.co_filename[len(prefix):], caller.co_firstlineno))
        else:
            if k is None and owner == DEBUG_WHOLE and not debug_done:
                import traceback

                debug_done.append(1)
                traceback.print_stack(sys._getframe(2), limit=6, file=sys.stderr)
            dict_keys.add((owner, "*" if k is None else key_repr(k)))

    class TDict(dict):
        __slots__ = ("_br",)

        def __getitem__(self, k):
            note(self._br, k)
            return dict.__getitem__(self, k)

        def get(self, k, default=None):
            note(self._br, k)
            return dict.get(self, k, default)

        def __contains__(self, k):
            note(self._br, k)
            return dict.__contains__(self, k)

        def setdefault(self, k, default=None):
            note(self._br, k)
            return dict.setdefault(self, k, default)

        def pop(self, k, *default):
            note(self._br, k)
            return dict.pop(self, k, *default)

        def __iter__(self):
            note(self._br, None)
            return dict.__iter__(self)

        def keys(self):
            note(self._br, None)
            return dict.keys(self)

        def values(self):
            note(self._br, None)
            return dict.values(self)

        def items(self):
            note(self._br, None)
            return dict.items(self)

        def copy(self):
            note(self._br, None)
            return dict.copy(self)

        def __eq__(self, other):
            note(self._br, None)
            return dict.__eq__(self, other)

        def __ne__(self, other):
            note(self._br, None)
            return dict.__ne__(self, other)

        def __or__(self, other):
            note(self._br, None)
            return dict.__or__(self, other)

        def __ror__(self, other):
            note(self._br, None)
            return dict.__ror__(self, other)

        def __reversed__(self):
            note(self._br, None)
            return dict.__reversed__(self)

        __hash__ = None

    tracked_dicts = set()
    dict_calls = set()  # (attribute name, key) whose stored callable was invoked at run time
    callable_keys = set()  # (attribute name, key) entries that hold a callable
    CALLABLES = (types.FunctionType, types.MethodType, functools.partial)

    def recording(value, owner, keyr):
        if isinstance(value, types.MethodType):
            # keep it a bound method (callers look at __self__)
            inner = value.__func__

            @functools.wraps(inner)
            def call_m(*a, **k):
                if importing[0]:
                    derived.add("%s\t%s" % (owner, keyr))
                else:
                    dict_calls.add((owner, keyr))
                return inner(*a, **k)

            call_m._br_w = True
            return types.MethodType(call_m, value.__self__)

        @functools.wraps(value)
        def call(*a, **k):
            if importing[0]:
                derived.add("%s\t%s" % (owner, keyr))
            else:
                dict_calls.add((owner, keyr))
            return value(*a, **k)

        call._br_w = True
        return call

    def wrap_dicts(cls):
        for attr, value in list(cls.__dict__.items()):
            if type(value) is dict and attr not in SKIP_DICTS and not attr.startswith("__"):
                t = TDict(value)
                t._br = attr
                # entries holding a callable also record when that callable is invoked, so that
                # copies of the dict (dispatch tables and the like) are still accounted for
                for k, v in list(dict.items(t)):
                    if isinstance(v, CALLABLES):
                        try:
                            kr = key_repr(k)
                        except Exception:
                            continue
                        callable_keys.add((attr, kr))
                        if not getattr(getattr(v, "__func__", v), "_br_w", False):
                            dict.__setitem__(t, k, recording(v, attr, kr))
                try:
                    setattr(cls, attr, t)
                    tracked_dicts.add(attr)
                except Exception:
                    pass

    orig_import = builtins.__import__

    def tracking_import(*a, **k):
        importing[0] += 1
        try:
            return orig_import(*a, **k)
        finally:
            importing[0] -= 1

    import importlib

    orig_import_module = importlib.import_module

    def tracking_import_module(*a, **k):
        importing[0] += 1
        try:
            return orig_import_module(*a, **k)
        finally:
            importing[0] -= 1

    # Wrapping classes and their dicts can nudge execution down other paths, so line coverage
    # is taken from plain runs and only class / dict usage from instrumented ones.
    INSTRUMENT = os.environ.get("BR_INSTRUMENT") == "1"
    if INSTRUMENT:
        builtins.__import__ = tracking_import
        importlib.import_module = tracking_import_module

    def build_class(func, name, *bases, **kw):
        importing[0] += 1
        try:
            cls = orig_build(func, name, *bases, **kw)
        finally:
            importing[0] -= 1
        try:
            code = func.__code__
            if code.co_filename.startswith(prefix + "sqlglot" + os.sep):
                wrap_dicts(cls)
        except Exception:
            pass
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

    if INSTRUMENT:
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
                "instrumented": INSTRUMENT,
                "classes": sorted(used_keys),
                "tracked": tracked,
                "dict_keys": sorted("%s\t%s" % kv for kv in dict_keys),
                "dict_calls": sorted("%s\t%s" % kv for kv in dict_calls),
                "dict_callable": sorted("%s\t%s" % kv for kv in callable_keys),
                "dict_derived": sorted(derived),
                "dict_derived_where": sorted(where),
                "dict_names": sorted(tracked_dicts),
            },
            fh,
        )


if __name__ == "__main__":
    main()
