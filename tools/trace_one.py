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
    derived = set()  # "name\tkey\tclass" (key "*" = whole) read by function code at import time
    # ... those of them that could see a value (not only whether a key is there, or the keys)
    derived_values = set()
    SKIP_DICTS = {"arg_types", "__annotations__", "__dict__"}

    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from br.keys import key_repr

    DEBUG_WHOLE = os.environ.get("BR_DEBUG_WHOLE")
    debug_done = []

    def note(owner, k, who="", value=True):
        if importing[0]:
            caller = sys._getframe(2).f_code
            if caller.co_flags & 1:
                entry = "%s\t%s\t%s" % (owner, "*" if k is None else key_repr(k), who)
                derived.add(entry)
                if value:
                    derived_values.add(entry)
        else:
            if k is None and owner == DEBUG_WHOLE and not debug_done:
                import traceback

                debug_done.append(1)
                traceback.print_stack(sys._getframe(2), limit=6, file=sys.stderr)
            dict_keys.add((owner, "*" if k is None else key_repr(k)))

    class TDict(dict):
        __slots__ = ("_br", "_brc")

        def __getitem__(self, k):
            note(self._br, k, self._brc)
            return dict.__getitem__(self, k)

        def get(self, k, default=None):
            note(self._br, k, self._brc)
            return dict.get(self, k, default)

        def __contains__(self, k):
            note(self._br, k, self._brc, value=False)
            return dict.__contains__(self, k)

        def setdefault(self, k, default=None):
            note(self._br, k, self._brc)
            return dict.setdefault(self, k, default)

        def pop(self, k, *default):
            note(self._br, k, self._brc)
            return dict.pop(self, k, *default)

        def __iter__(self):
            note(self._br, None, self._brc, value=False)
            return dict.__iter__(self)

        def keys(self):
            note(self._br, None, self._brc, value=False)
            return dict.keys(self)

        def values(self):
            note(self._br, None, self._brc)
            return dict.values(self)

        def items(self):
            note(self._br, None, self._brc)
            return dict.items(self)

        def copy(self):
            note(self._br, None, self._brc)
            return dict.copy(self)

        def __eq__(self, other):
            note(self._br, None, self._brc)
            return dict.__eq__(self, other)

        def __ne__(self, other):
            note(self._br, None, self._brc)
            return dict.__ne__(self, other)

        def __or__(self, other):
            note(self._br, None, self._brc)
            return dict.__or__(self, other)

        def __ror__(self, other):
            note(self._br, None, self._brc)
            return dict.__ror__(self, other)

        def __reversed__(self):
            note(self._br, None, self._brc, value=False)
            return dict.__reversed__(self)

        __hash__ = None

    tracked_dicts = set()
    dict_calls = set()  # (attribute name, key) whose stored callable was invoked at run time
    callable_keys = set()  # (attribute name, key) entries that hold a callable
    CALLABLES = (types.FunctionType, types.MethodType, functools.partial)

    def recording(value, owner, keyr, who=""):
        if isinstance(value, types.MethodType):
            # keep it a bound method (callers look at __self__)
            inner = value.__func__

            @functools.wraps(inner)
            def call_m(*a, **k):
                if importing[0]:
                    derived.add("%s\t%s\t%s" % (owner, keyr, who))
                    derived_values.add("%s\t%s\t%s" % (owner, keyr, who))
                else:
                    dict_calls.add((owner, keyr))
                return inner(*a, **k)

            call_m._br_w = True
            return types.MethodType(call_m, value.__self__)

        @functools.wraps(value)
        def call(*a, **k):
            if importing[0]:
                derived.add("%s\t%s" % (owner, keyr))
                derived_values.add("%s\t%s" % (owner, keyr))
            else:
                dict_calls.add((owner, keyr))
            return value(*a, **k)

        call._br_w = True
        return call

    def wrap_dicts(cls, cls_key):
        for attr, value in list(cls.__dict__.items()):
            if type(value) is dict and attr not in SKIP_DICTS and not attr.startswith("__"):
                t = TDict(value)
                t._br = attr
                t._brc = cls_key
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
                            dict.__setitem__(t, k, recording(v, attr, kr, cls_key))
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

    def wrap_namespace(ns, cls_key):
        """Wrap the dicts of a class body before its metaclass sees them, so that whatever the
        metaclass derives from them at class creation is observed too."""
        for attr, value in list(ns.items()):
            if type(value) is dict and attr not in SKIP_DICTS and not attr.startswith("__"):
                t = TDict(value)
                t._br = attr
                t._brc = cls_key
                for k, v in list(dict.items(t)):
                    if isinstance(v, CALLABLES):
                        try:
                            kr = key_repr(k)
                        except Exception:
                            continue
                        callable_keys.add((attr, kr))
                        if not getattr(getattr(v, "__func__", v), "_br_w", False):
                            dict.__setitem__(t, k, recording(v, attr, kr, cls_key))
                ns[attr] = t
                tracked_dicts.add(attr)

    # --- which tests read a class-level constant, and from which class's definition?
    # Plain constants of the dialect / tokenizer / parser / generator / expression families are
    # replaced (once their class exists) by a descriptor that notes every read.  A read that
    # resolves to class C's own definition is recorded under C, whatever subclass it went through.
    import enum

    attr_reads = set()  # "class\tNAME" read at run time
    attr_derived = set()  # "class\tNAME" read by function code while classes are being built
    attr_tracked = set()
    FAMILY_BASES = {
        ("sqlglot.dialects.dialect", "Dialect"),
        ("sqlglot.tokens", "Tokenizer"),
        ("sqlglot.parser", "Parser"),
        ("sqlglot.generator", "Generator"),
        ("sqlglot.expressions.core", "Expr"),
    }
    PLAIN = (bool, int, float, str, bytes, tuple, frozenset, set, list, type(None))

    class Rec:
        __slots__ = ("v", "k")

        def __init__(self, v, k):
            self.v = v
            self.k = k

        def __get__(self, obj, objtype=None):
            if importing[0]:
                if sys._getframe(1).f_code.co_flags & 1:
                    attr_derived.add(self.k)
            else:
                attr_reads.add(self.k)
            return self.v

    def in_family(cls):
        if issubclass(cls, enum.Enum):
            return False
        for b in cls.__mro__:
            if (getattr(b, "__module__", None), b.__name__) in FAMILY_BASES:
                return True
        return False

    def wrap_attrs(cls, cls_key):
        if not in_family(cls):
            return
        for attr, value in list(cls.__dict__.items()):
            if attr.startswith("__") or attr in ("_abc_impl",):
                continue
            if type(value) in PLAIN or isinstance(value, enum.Enum):
                try:
                    type.__setattr__(cls, attr, Rec(value, "%s\t%s" % (cls_key, attr)))
                    attr_tracked.add("%s\t%s" % (cls_key, attr))
                except Exception:
                    pass

    def real_metaclass(bases, kw):
        meta = kw.get("metaclass")
        if meta is None:
            meta = type
            for b in bases:
                bm = type(b)
                if issubclass(bm, meta):
                    meta = bm
        return meta

    def build_class(func, name, *bases, **kw):
        code = func.__code__
        ours = code.co_filename.startswith(prefix + "sqlglot" + os.sep)
        if ours:
            try:
                meta = real_metaclass(bases, kw)
                cls_key = "%s:%d" % (code.co_filename[len(prefix):], code.co_firstlineno)
                if isinstance(meta, type):

                    def proxy(cname, cbases, ns, **ckw):
                        try:
                            wrap_namespace(ns, cls_key)
                        except Exception:
                            pass
                        return meta(cname, cbases, ns, **ckw)

                    proxy.__prepare__ = getattr(meta, "__prepare__")
                    kw = dict(kw, metaclass=proxy)
            except Exception:
                pass
        importing[0] += 1
        try:
            cls = orig_build(func, name, *bases, **kw)
        finally:
            importing[0] -= 1
        if ours:
            try:
                wrap_attrs(cls, "%s:%d" % (code.co_filename[len(prefix):], code.co_firstlineno))
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

    if INSTRUMENT and os.environ.get("BR_COLD_HANDED"):
        # What does the slow half of an executor entry instantiate, read and look up?  Every
        # hand-over recorded by tools/trace_memo.py (a pickled plan with its tables) is
        # executed in a forked child of this process, which has only loaded the modules:
        # nothing the quick half would have cached is there to hide a read.  Nothing is
        # cleared before a child runs, so whatever loading the modules instantiated counts for
        # every entry.
        import pickle

        mon.set_events(tool, 0)
        import sqlglot.executor  # noqa: F401
        from sqlglot.executor.python import PythonExecutor

        per = []
        with open(os.environ["BR_COLD_HANDED"], "rb") as handed:
            while True:
                try:
                    plan, tables = pickle.load(handed)
                except EOFError:
                    break
                r, w = os.pipe()
                pid = os.fork()
                if pid == 0:
                    os.close(r)
                    try:
                        PythonExecutor(tables=tables).execute(plan)
                    except BaseException:
                        pass
                    key_of = {c: "%s:%d" % (f, line) for c, f, line in created}
                    classes = set()
                    for c in used:
                        for b in c.__mro__:
                            k = key_of.get(b)
                            if k:
                                classes.add(k)
                    payload = {
                        "classes": sorted(classes),
                        "attr_reads": sorted(attr_reads),
                        "dict_keys": sorted("%s\t%s" % kv for kv in dict_keys),
                        "dict_calls": sorted("%s\t%s" % kv for kv in dict_calls),
                    }
                    with os.fdopen(w, "wb") as fh:
                        fh.write(pickle.dumps(payload))
                    os._exit(0)
                os.close(w)
                with os.fdopen(r, "rb") as fh:
                    data = fh.read()
                os.waitpid(pid, 0)
                per.append(pickle.loads(data) if data else None)
        with open(out, "wb") as fh:
            pickle.dump(per, fh)
        sys.stdout.flush()
        os._exit(0)

    if INSTRUMENT and os.environ.get("BR_ENTRIES"):
        # Per fixture entry of a sliced test: which classes it instantiates, which class-level
        # constants it reads and which dict entries it looks up.  The class is set up once and
        # the test runs in forked children, each allowed a single entry (plus one that sees
        # none), as tools/trace_slices_one.py does; entries are numbered as in the slice map.
        import pickle
        import zlib

        from br import maps as maps_mod, worker

        mon.set_events(tool, 0)
        worker._limit_duckdb_threads()
        sm = maps_mod.Maps(os.environ["BR_ENTRIES"]).slices(test_id)
        record = {"id": test_id, "keys": [], "ok": False}
        if sm is not None:
            keys = list(sm["keys"])
            runner = worker.Runner(os.open(os.devnull, os.O_WRONLY))
            path, cls_name, meth = test_id.split("::")
            module = runner._module(path[:-3].replace("/", "."))
            if isinstance(module, BaseException):
                raise module
            # function lines the class set-up runs: what they compute is shared by every entry
            func.clear()
            mon.restart_events()
            mon.set_events(tool, mon.events.LINE | mon.events.PY_START)
            runner._class(getattr(module, cls_name))
            mon.set_events(tool, 0)
            record["setup_lines"] = {f[len(prefix):]: sorted(ls) for f, ls in func.items()}
            fields = ("classes", "attr_reads", "dict_keys", "dict_calls")

            def in_child(allow):
                r, w = os.pipe()
                pid = os.fork()
                if pid == 0:
                    os.close(r)
                    try:
                        # (nothing is cleared: what loading the modules and setting up the
                        # class instantiated, read and looked up is shared by every entry)
                        outcome = runner.run_one(test_id, {"allow": list(allow)})
                        key_of = {c: "%s:%d" % (f, line) for c, f, line in created}
                        classes = set()
                        for c in used:
                            for b in c.__mro__:
                                k = key_of.get(b)
                                if k:
                                    classes.add(k)
                        payload = {
                            "outcome": outcome,
                            "classes": sorted(classes),
                            "attr_reads": sorted(attr_reads),
                            "dict_keys": sorted("%s\t%s" % kv for kv in dict_keys),
                            "dict_calls": sorted("%s\t%s" % kv for kv in dict_calls),
                        }
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

            base = in_child(())
            ok = base.get("outcome") == "P"
            tables = {f: {} for f in fields}
            for i, k in enumerate(keys):
                r = in_child((k,))
                if r.get("outcome") != "P":
                    ok = False
                    sys.stderr.write("%s entry %s: %r\n" % (test_id, k, r.get("outcome", r.get("error"))))
                    continue
                for f in fields:
                    t = tables[f]
                    for item in r[f]:
                        t[item] = t.get(item, 0) | (1 << i)
            record.update(keys=keys, ok=ok, **tables)
            sys.stderr.write("%s entries=%d ok=%s classes=%d constants=%d dict entries=%d\n" % (test_id, len(keys), ok, len(tables["classes"]), len(tables["attr_reads"]), len(tables["dict_keys"])))
        with open(out, "wb") as fh:
            fh.write(zlib.compress(pickle.dumps(record, protocol=5), 6))
        sys.stdout.flush()
        os._exit(0)

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
    ancestry = {}
    for cls in key_of:
        ancestry[key_of[cls]] = sorted({key_of[b] for b in cls.__mro__ if b in key_of})
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
                "attr_reads": sorted(attr_reads),
                "attr_derived": sorted(attr_derived),
                "attr_tracked": sorted(attr_tracked),
                "dict_callable": sorted("%s\t%s" % kv for kv in callable_keys),
                "dict_derived": sorted(derived),
                "dict_derived_values": sorted(derived_values),
                "ancestry": ancestry,
                "dict_names": sorted(tracked_dicts),
            },
            fh,
        )


if __name__ == "__main__":
    main()
