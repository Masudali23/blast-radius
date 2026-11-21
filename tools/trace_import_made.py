"""Which sqlglot classes get an instance while the warm worker loads?

Usage: trace_import_made.py <repo_root> <maps_dir>

Runs what the warm worker runs before any test (the same calls as tools/trace_import.py, plus
the class set-ups it performs ahead of time) with every `__init__` defined in the repository
wrapped, and writes <maps_dir>/import_made.json: the "file:line" keys of the classes that were
instantiated then, each with its base classes.  An instance made that early may have looked at
its class's attributes already, so what those hold cannot simply be swapped in later.
"""
import builtins
import functools
import json
import os
import sys
import types


def main():
    root = os.path.realpath(sys.argv[1])
    out_dir = os.path.abspath(sys.argv[2])
    os.chdir(root)
    sys.path.insert(0, root)
    os.environ.setdefault("SKIP_INTEGRATION", "0")
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    import unittest  # noqa: F401  (sqlglot checks more when a test library is loaded)

    from br import inlinepool, worker

    inlinepool.install()
    prefix = root + os.sep
    used, created = set(), []
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
        except Exception:
            pass
        return cls

    builtins.__build_class__ = build_class
    worker._limit_duckdb_threads()
    worker.preload_core(root)
    worker.preload(root, skip=worker.LAZY_MODULES)
    worker.prerun_class_setup()
    key_of = {cls: "%s:%d" % (f, line) for cls, f, line in created}
    keys = set()
    for cls in used:
        for b in cls.__mro__:
            k = key_of.get(b)
            if k:
                keys.add(k)
    with open(os.path.join(out_dir, "import_made.json"), "w") as fh:
        json.dump(sorted(keys), fh, indent=0)
        fh.write("\n")
    print("classes defined:", len(created), "instantiated while loading (with bases):", len(keys))


if __name__ == "__main__":
    main()
