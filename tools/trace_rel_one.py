"""Run one sqlglot test in this (fresh) process with every comparison evaluated both ways.

Usage: trace_rel_one.py <repo_root> <test_id> <out_json>

Records the comparisons (file|line:col:end_line:end_col:operator) at which the operator and its
partner disagreed at least once during the test (see br/relations.py), every comparison that
was evaluated at all, and every distinct
evaluation of every and/or condition (see br/boolrel.py).  Written to <out_json>.gz.  With "@import" in
place of a test id: during what the warm worker loads before any test runs.
"""
import ast
import builtins
import gzip
import hashlib
import importlib.machinery as machinery
import json
import marshal
import os
import sys
import unittest


def main():
    root = os.path.realpath(sys.argv[1])
    test_id = sys.argv[2]
    out = sys.argv[3]
    os.chdir(root)
    sys.path.insert(0, root)
    os.environ.setdefault("SKIP_INTEGRATION", "0")
    sys.dont_write_bytecode = True
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from br import boolrel, inlinepool, relations

    inlinepool.install()
    hits = set()
    active = []
    seen = set()
    setattr(builtins, relations.HELPER, relations.make_helper(hits, active, seen))
    formulas = boolrel.Recorder(active)
    setattr(builtins, boolrel.START, formulas.start)
    setattr(builtins, boolrel.ATOM, formulas.atom)
    setattr(builtins, boolrel.END, formulas.end)
    prefix = root + os.sep
    package = prefix + "sqlglot" + os.sep
    cache = os.environ.get("BR_REL_CACHE")
    if cache:
        os.makedirs(cache, exist_ok=True)

    class Loader(machinery.SourceFileLoader):
        def get_code(self, fullname):
            path = self.get_filename(fullname)
            if not os.path.realpath(path).startswith(package):
                return super().get_code(fullname)
            rel = os.path.realpath(path)[len(prefix):]
            data = self.get_data(path)
            cached = None
            if cache:
                # the rewritten module is the same for every test: compile it once
                cached = os.path.join(cache, hashlib.sha1(rel.encode() + b"\0" + data).hexdigest())
                try:
                    with open(cached, "rb") as fh:
                        return marshal.loads(fh.read())
                except (OSError, ValueError, EOFError):
                    pass
            tree = boolrel.instrument(ast.parse(data, path), rel)
            tree = relations.Instrument(rel).visit(tree)
            ast.fix_missing_locations(tree)
            code = compile(tree, path, "exec", dont_inherit=True)
            if cached:
                tmp = "%s.%d" % (cached, os.getpid())
                with open(tmp, "wb") as fh:
                    fh.write(marshal.dumps(code))
                os.replace(tmp, cached)
            return code

    sys.path_hooks.insert(
        0,
        machinery.FileFinder.path_hook(
            (machinery.ExtensionFileLoader, machinery.EXTENSION_SUFFIXES),
            (Loader, machinery.SOURCE_SUFFIXES),
            (machinery.SourcelessFileLoader, machinery.BYTECODE_SUFFIXES),
        ),
    )
    sys.path_importer_cache.clear()

    def save(record):
        record["hits"] = sorted(hits)
        record["seen"] = sorted(seen)
        record["bools"] = sorted([site, kind, list(vec)] for site, kind, vec in formulas.seen)
        record["aborted"] = formulas.aborted()
        with gzip.open(out + ".gz", "wt") as fh:
            json.dump(record, fh, separators=(",", ":"))

    if test_id == "@import":
        # what the warm worker does before any test runs (the same calls as trace_import.py)
        from br import worker

        worker._limit_duckdb_threads()
        worker.preload_core(root)
        worker.preload(root, skip=worker.LAZY_MODULES)
        save({"id": test_id, "outcome": "pass", "detail": "", "modules": 1})
        os._exit(0)
    path, cls_name, meth = test_id.split("::")
    outcome, detail = "pass", ""
    try:
        module = __import__(path[:-3].replace("/", "."), fromlist=["x"])
        cls = getattr(module, cls_name)
        result = unittest.TestResult()
        unittest.TestSuite([cls(meth)]).run(result)
        if result.failures or result.errors or result.unexpectedSuccesses:
            outcome = "fail"
            detail = (result.failures + result.errors)[0][1][-2000:] if (result.failures or result.errors) else "unexpected success"
        elif result.skipped:
            outcome = "skip"
            detail = result.skipped[0][1]
    except BaseException as e:  # import error etc.
        outcome = "fail"
        detail = "import: %r" % (e,)
    instrumented = sum(1 for m in list(sys.modules.values()) if isinstance(getattr(m, "__loader__", None), Loader) and (getattr(m, "__file__", "") or "").startswith(package))
    save({"id": test_id, "outcome": outcome, "detail": detail, "modules": instrumented})
    sys.stdout.flush()
    os._exit(0)


main()
