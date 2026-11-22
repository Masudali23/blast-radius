"""Repack the two largest map stores so that a run decodes only the parts it asks about.

usage: pack_lazy.py <maps_dir>

relslices.pkl: {test id: record} becomes {test id: packed record}.
s_<n>.pkl:     "lines" {file: {line: entries}} becomes {file: packed table}, "layout": 2.
dictkeys.pkl:  "users" {"NAME\\tkey": tests} becomes {NAME: packed {key: tests}}, "layout": 2.
br/maps.py reads both layouts.  Running this twice changes nothing.
"""
import os
import pickle
import sys
import zlib


def pack(value):
    return zlib.compress(pickle.dumps(value, protocol=pickle.HIGHEST_PROTOCOL), 6)


def main():
    maps = sys.argv[1]
    path = os.path.join(maps, "relslices.pkl")
    with open(path, "rb") as fh:
        table = pickle.loads(zlib.decompress(fh.read()))
    if any(type(v) is not bytes for v in table.values()):
        table = {k: (v if type(v) is bytes else pack(v)) for k, v in table.items()}
        with open(path, "wb") as fh:
            fh.write(zlib.compress(pickle.dumps(table, protocol=pickle.HIGHEST_PROTOCOL), 0))
    # the per-entry coverage of each sliced test: one packed table per source file
    with open(os.path.join(maps, "index.pkl"), "rb") as fh:
        sliced = pickle.loads(zlib.decompress(fh.read())).get("sliced", {})
    for name in sorted(set(sliced.values())):
        path = os.path.join(maps, name)
        with open(path, "rb") as fh:
            record = pickle.loads(zlib.decompress(fh.read()))
        if record.get("layout") != 2:
            record["lines"] = {rel: pack(table) for rel, table in record["lines"].items()}
            record["layout"] = 2
            with open(path, "wb") as fh:
                fh.write(zlib.compress(pickle.dumps(record, protocol=pickle.HIGHEST_PROTOCOL), 0))
    path = os.path.join(maps, "dictkeys.pkl")
    with open(path, "rb") as fh:
        d = pickle.loads(zlib.decompress(fh.read()))
    if "users" not in d:
        d = {"users": d, "calls": {}, "callable": []}
    if d.get("layout") != 2:
        parts = {}
        for k, tests in d["users"].items():
            name, key = k.split("\t", 1)
            parts.setdefault(name, {})[key] = tests
        d["users"] = {name: pack(part) for name, part in parts.items()}
        d["layout"] = 2
        with open(path, "wb") as fh:
            fh.write(zlib.compress(pickle.dumps(d, protocol=pickle.HIGHEST_PROTOCOL), 6))


if __name__ == "__main__":
    main()
