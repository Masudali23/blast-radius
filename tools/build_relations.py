"""Compile the per-test comparison traces into maps/relations.pkl.

Usage: build_relations.py <trace_dir>[,<trace_dir>...] <tests.json> <repo_root> <maps_dir> <import_trace.json>[,...]

<import_trace.json> is trace_rel_one.py's output for "@import".  Several trace directories
(runs with different hash seeds) are merged: what any run saw counts.

  sites   {source file: [site keys]}   every comparison that was evaluated both ways
  hits    {"file|site key": mask}      tests in which the two ways disagreed
  seen    {"file|site key": mask}      tests in which the comparison was evaluated at all while
          traced; a test that runs the line without the tracer having seen the comparison
          stays a candidate
  always  mask of tests whose trace is unusable (they did not pass while being traced):
          they stay candidates for every comparison
  warm    comparisons that disagreed while the warm worker loaded: whatever was built from
          them then is already in every worker
"""
import ast
import json
import os
import pickle
import sys
import zlib

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from br import boolrel, relations  # noqa: E402

trace_dir, tests_json, root, out_dir, import_trace = sys.argv[1:6]
tests = json.load(open(tests_json))["tests"]
with open(os.path.join(out_dir, "index.pkl"), "rb") as fh:
    ids = pickle.loads(zlib.decompress(fh.read()))["ids"]
index = {tid: i for i, tid in enumerate(ids)}
hits = {}
seen = {}  # comparison site -> tests in which the tracer saw it evaluated at all
always = 0
bad = []
vectors = {}  # formula site -> {(kind, vector): mask}   (see br/boolrel.py)
aborted = {}  # formula site -> mask of tests in which an evaluation of it did not finish


def load(path):
    if os.path.exists(path + ".gz"):
        import gzip

        with gzip.open(path + ".gz", "rt") as fh:
            return json.load(fh)
    if os.path.exists(path):
        with open(path) as fh:
            return json.load(fh)
    return None


for t in tests:
    i = index.get(t["id"])
    if i is None:
        continue
    for n, one_dir in enumerate(trace_dir.split(",")):
        d = load(os.path.join(one_dir, "%d.json" % t["number"]))
        if d is None:
            if n == 0:
                always |= 1 << i
                bad.append((t["id"], "missing"))
            continue
        if d["outcome"] != "pass" or not d.get("modules"):
            always |= 1 << i
            bad.append((t["id"], d["outcome"], d["detail"][-160:]))
            continue
        for site in d["hits"]:
            hits[site] = hits.get(site, 0) | (1 << i)
        for site in d.get("seen", ()):
            seen[site] = seen.get(site, 0) | (1 << i)
        for site, kind, vec in d.get("bools", ()):
            per = vectors.setdefault(site, {})
            k = (kind, tuple(vec))
            per[k] = per.get(k, 0) | (1 << i)
        for site in d.get("aborted", ()):
            aborted[site] = aborted.get(site, 0) | (1 << i)
sites = {}
formulas = {}
for base, _, files in os.walk(os.path.join(root, "sqlglot")):
    for f in files:
        if f.endswith(".py"):
            path = os.path.join(base, f)
            rel = os.path.relpath(path, root)
            tree = ast.parse(open(path, "rb").read())
            keys = relations.sites(tree)
            if keys:
                sites[rel] = keys
            keys = boolrel.sites(tree)
            if keys:
                formulas[rel] = keys
imp = None
for one in import_trace.split(","):
    d = load(one[:-3] if one.endswith(".gz") else one)
    if imp is None:
        imp = d
    else:
        imp["hits"] = sorted(set(imp["hits"]) | set(d["hits"]))
        if "bools" in imp and "bools" in d:
            imp["bools"] = imp["bools"] + d["bools"]
            imp["aborted"] = sorted(set(imp.get("aborted", ())) | set(d.get("aborted", ())))
warm = sorted(imp["hits"])
have_bools = "bools" in imp
packed = {"sites": sites, "hits": hits, "seen": seen, "always": always, "warm": warm}
if have_bools:
    packed.update(
        formulas=formulas,
        vectors=vectors,
        aborted=aborted,
        warm_vectors=sorted({(site, kind, tuple(vec)) for site, kind, vec in imp["bools"]}),
        warm_aborted=sorted(imp.get("aborted", ())),
    )
blob = zlib.compress(pickle.dumps(packed, protocol=5), 6)
open(os.path.join(out_dir, "relations.pkl"), "wb").write(blob)
print("comparisons:", sum(len(v) for v in sites.values()), "with a disagreement in some test:", len(hits), "unusable traces:", len(bad), "bytes", len(blob))
if have_bools:
    print("formulas:", sum(len(v) for v in formulas.values()), "evaluated:", len(vectors), "distinct evaluations:", sum(len(v) for v in vectors.values()))
for b in bad[:20]:
    print("  unusable:", b)
