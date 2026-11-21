"""Which classmethods always hand back an instance of the class they were called on?

Usage: check_instantiating.py <repo_root> <maps_dir>

A classmethod whose last statement is `return cls(...)` (and that has no other way out) reads
class attributes off `cls` and then creates an instance of it, so a test that runs it shows up
among the tests creating that class.  That only holds if nothing raises on the way; here the
whole suite is run at the pinned commit and every such classmethod that was called, and never
let an exception out, is written to <maps_dir>/instantiating.json as {"file:line": name}.
tools/build_reads.py treats reads through `cls` in exactly those as reads through an instance.
"""
import ast
import importlib
import json
import os
import sys


def ends_in_instance(fn):
    params = fn.args.posonlyargs + fn.args.args
    if not params or not fn.body:
        return False
    last = fn.body[-1]
    if not (isinstance(last, ast.Return) and isinstance(last.value, ast.Call) and isinstance(last.value.func, ast.Name) and last.value.func.id == params[0].arg):
        return False
    for sub in ast.walk(fn):
        if sub is not last and isinstance(sub, (ast.Return, ast.Yield, ast.YieldFrom, ast.Try, ast.Raise)):
            return False
    return True


def main():
    root = os.path.realpath(sys.argv[1])
    out_dir = os.path.abspath(sys.argv[2])
    os.chdir(root)
    sys.path.insert(0, root)
    # sqlglot looks for these when it is first imported and checks more when it finds them
    import unittest  # noqa: F401

    import pytest
    found = []
    for d, _, fs in os.walk("sqlglot"):
        for f in sorted(fs):
            if not f.endswith(".py"):
                continue
            rel = os.path.join(d, f)
            try:
                tree = ast.parse(open(rel, encoding="utf-8").read())
            except SyntaxError:
                continue
            for cls in tree.body:
                if not isinstance(cls, ast.ClassDef):
                    continue
                for fn in cls.body:
                    if isinstance(fn, ast.FunctionDef) and any(isinstance(x, ast.Name) and x.id == "classmethod" for x in fn.decorator_list) and ends_in_instance(fn):
                        found.append((rel, fn.lineno, cls.name, fn.name))
    calls, raised = {}, set()

    def wrap(owner, name, key):
        orig = owner.__dict__[name].__func__

        def wrapper(cls, *a, **k):
            calls[key] = calls.get(key, 0) + 1
            try:
                return orig(cls, *a, **k)
            except BaseException:
                raised.add(key)
                raise

        wrapper.__name__ = orig.__name__
        wrapper.__qualname__ = orig.__qualname__
        wrapper.__doc__ = orig.__doc__
        setattr(owner, name, classmethod(wrapper))

    names = {}
    for rel, line, cls_name, fn_name in found:
        try:
            module = importlib.import_module(rel[:-3].replace(os.sep, "."))
            owner = getattr(module, cls_name)
            if not isinstance(owner.__dict__.get(fn_name), classmethod):
                continue
        except Exception:
            continue
        key = "%s:%d" % (rel.replace(os.sep, "/"), line)
        names[key] = fn_name
        wrap(owner, fn_name, key)
    # tests that start a process pool would start this script again in every worker
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from br import inlinepool

    inlinepool.install()

    code = pytest.main(["-q", "-p", "no:cacheprovider", "-x", "tests"] + sys.argv[3:])
    good = {k: n for k, n in names.items() if calls.get(k) and k not in raised} if code == 0 else {}
    for k, n in sorted(names.items()):
        sys.stderr.write("%s %s calls=%d raised=%s\n" % (k, n, calls.get(k, 0), k in raised))
    with open(os.path.join(out_dir, "instantiating.json"), "w") as fh:
        json.dump(good, fh, indent=1, sort_keys=True)
        fh.write("\n")
    sys.stderr.write("suite exit code %s; confirmed %d of %d\n" % (code, len(good), len(names)))


if __name__ == "__main__":
    main()
