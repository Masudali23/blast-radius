"""Per fixture entry: which comparisons disagree and how each condition is evaluated.

Usage: trace_rel_slices_one.py <repo_root> <maps_dir> <test_id> <out_pickle>

The same recording as tools/trace_rel_one.py (every comparison evaluated both ways, every
evaluation of every and/or condition), taken once for each fixture entry of a sliced test: the
class is set up once and the test then runs in forked children, each allowed a single entry
(plus one child that sees none), exactly as tools/trace_slices_one.py does for line coverage.
Entries are numbered as in the test's slice map, so bit i here is bit i there.

Written as {"keys", "ok", "base": record, "pre": record, "hits": {site: entries},
"seen": {site: entries}, "vectors": {(site, kind, vector): entries}, "aborted": {site:
entries}} with entry sets as bit masks; "ok" is false when some child did not pass, and then
nothing may be concluded.  "pre" is what loading the modules and setting up the class
evaluated before any entry ran: whatever differs there can reach every entry.
"""
import ast
import builtins
import hashlib
import importlib.machinery as machinery
import marshal
import os
import pickle
import sys
import zlib


def main():
    root = os.path.realpath(sys.argv[1])
    maps_dir = os.path.abspath(sys.argv[2])
    test_id = sys.argv[3]
    out = os.path.abspath(sys.argv[4])
    os.chdir(root)
    sys.path.insert(0, root)
    os.environ.setdefault("SKIP_INTEGRATION", "0")
    for var in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS"):
        os.environ[var] = "1"
    sys.dont_write_bytecode = True
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from br import boolrel, inlinepool, maps as maps_mod, relations

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

    from br import worker

    worker._limit_duckdb_threads()
    slices = maps_mod.Maps(maps_dir).slices(test_id)
    result = {"id": test_id, "keys": [], "ok": False}
    if slices is None:
        with open(out, "wb") as fh:
            fh.write(zlib.compress(pickle.dumps(result, protocol=5), 6))
        return
    keys = list(slices["keys"])
    runner = worker.Runner(os.open(os.devnull, os.O_WRONLY))
    path, cls_name, meth = test_id.split("::")
    module = runner._module(path[:-3].replace("/", "."))
    if isinstance(module, BaseException):
        raise module
    cls = getattr(module, cls_name)
    runner._class(cls)

    def in_child(allow):
        r, w = os.pipe()
        pid = os.fork()
        if pid == 0:
            os.close(r)
            try:
                # only what this entry does counts: forget what loading and set-up evaluated
                hits.clear()
                seen.clear()
                del active[:]
                formulas.seen.clear()
                formulas.started.clear()
                formulas.finished.clear()
                formulas.reentered.clear()
                outcome = runner.run_one(test_id, {"allow": list(allow)})
                payload = {"outcome": outcome, "hits": sorted(hits), "seen": sorted(seen), "bools": sorted((site, kind, tuple(vec)) for site, kind, vec in formulas.seen), "aborted": formulas.aborted()}
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

    # everything evaluated so far (imports, class set-up) is shared by all entries
    pre = {"hits": sorted(hits), "bools": sorted((site, kind, tuple(vec)) for site, kind, vec in formulas.seen), "aborted": formulas.aborted()}
    base = in_child(())
    ok = base.get("outcome") == "P"
    hit_of, seen_of, vec_of, aborted_of = {}, {}, {}, {}
    for i, k in enumerate(keys):
        r = in_child((k,))
        if r.get("outcome") != "P":
            ok = False
            sys.stderr.write("%s entry %s: %r\n" % (test_id, k, r.get("outcome", r.get("error"))))
            continue
        bit = 1 << i
        for site in r["hits"]:
            hit_of[site] = hit_of.get(site, 0) | bit
        for site in r["seen"]:
            seen_of[site] = seen_of.get(site, 0) | bit
        for item in r["bools"]:
            vec_of[item] = vec_of.get(item, 0) | bit
        for site in r["aborted"]:
            aborted_of[site] = aborted_of.get(site, 0) | bit
    result.update(keys=keys, ok=ok, base=base, pre=pre, hits=hit_of, seen=seen_of, vectors=vec_of, aborted=aborted_of)
    with open(out, "wb") as fh:
        fh.write(zlib.compress(pickle.dumps(result, protocol=5), 6))
    sys.stderr.write("%s entries=%d ok=%s sites with a disagreement=%d conditions=%d\n" % (test_id, len(keys), ok, len(hit_of), len({s for s, _, _ in vec_of})))


if __name__ == "__main__":
    main()
