"""Pack the per-entry recordings of tools/trace_rel_slices_one.py for br/maps.py.

Usage: build_relslices.py <dir of <n>.pkl> <maps_dir>

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
    packed[tid] = {"n": len(d["keys"]), "hits": d["hits"], "seen": d["seen"], "vectors": vectors, "aborted": d["aborted"]}
blob = zlib.compress(pickle.dumps(packed, protocol=5), 6)
with open(os.path.join(out_dir, "relslices.pkl"), "wb") as fh:
    fh.write(blob)
print("tests:", len(packed), "entries:", sum(v["n"] for v in packed.values()), "bytes:", len(blob))
