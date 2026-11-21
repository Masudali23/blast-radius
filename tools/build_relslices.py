"""Pack the per-entry recordings of tools/trace_rel_slices_one.py for br/maps.py.

Usage: build_relslices.py <dir of <n>.pkl> <maps_dir> [dir of per-entry usage records]

The optional third directory holds what `BR_INSTRUMENT=1 BR_ENTRIES=<maps_dir> trace_one.py`
wrote: per entry, the classes instantiated, class constants read and dict entries looked up.
They go in under "use".

Writes <maps_dir>/relslices.pkl: {test id: {"n": entries, "hits": {site: entries},
"seen": {site: entries}, "vectors": {site: [(kind, vector, entries)]}, "aborted": {site:
entries}}} with entry sets as bit masks in the order of the test's slice map.  A test whose
recording did not pass in every child, or whose entries are not those of its slice map, is
left out: nothing is then concluded about its entries.
"""
import os
import pickle
import sys
import zlib

src, out_dir = sys.argv[1:3]
use_dir = sys.argv[3] if len(sys.argv) > 3 else None
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from br import maps as maps_mod  # noqa: E402

maps = maps_mod.Maps(out_dir)
packed = {}
for name in sorted(os.listdir(src)):
    if not name.endswith(".pkl"):
        continue
    with open(os.path.join(src, name), "rb") as fh:
        d = pickle.loads(zlib.decompress(fh.read()))
    tid = d.get("id")
    sm = maps.slices(tid) if tid else None
    if sm is None or not d.get("ok") or list(d.get("keys") or ()) != list(sm["keys"]):
        print("left out:", tid or name)
        continue
    vectors = {}
    for (site, kind, vec), mask in d["vectors"].items():
        vectors.setdefault(site, []).append((kind, tuple(vec), mask))
    # what ran before any entry (imports, class set-up) can reach every entry
    pre = d["pre"]
    pre_vectors = {}
    for site, kind, vec in pre["bools"]:
        pre_vectors.setdefault(site, []).append((kind, tuple(vec)))
    packed[tid] = {"n": len(d["keys"]), "hits": d["hits"], "seen": d["seen"], "vectors": vectors, "aborted": d["aborted"], "pre_hits": frozenset(pre["hits"]), "pre_vectors": pre_vectors, "pre_aborted": frozenset(pre["aborted"])}
for name in sorted(os.listdir(use_dir)) if use_dir else ():
    if not name.endswith(".pkl"):
        continue
    with open(os.path.join(use_dir, name), "rb") as fh:
        d = pickle.loads(zlib.decompress(fh.read()))
    tid = d.get("id")
    sm = maps.slices(tid) if tid else None
    if tid not in packed or sm is None or not d.get("ok") or list(d.get("keys") or ()) != list(sm["keys"]):
        print("usage left out:", tid or name)
        continue
    packed[tid]["use"] = {f: d[f] for f in ("classes", "attr_reads", "dict_keys", "dict_calls")}
    packed[tid]["setup_lines"] = {f: frozenset(ls) for f, ls in d.get("setup_lines", {}).items()}
blob = zlib.compress(pickle.dumps(packed, protocol=5), 6)
with open(os.path.join(out_dir, "relslices.pkl"), "wb") as fh:
    fh.write(blob)
print("tests:", len(packed), "entries:", sum(v["n"] for v in packed.values()), "bytes:", len(blob))
