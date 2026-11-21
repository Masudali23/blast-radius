"""Store the rows TestExecutor.setUpClass parses, for br/tables.py.

Usage: build_tables.py <repo_root> <out.pkl>

Runs the real set-up at the pinned commit, then the shortened one fed from the stored rows, and
writes the file only if both leave the class with the same tables.
"""
import importlib
import os
import pickle
import sys


def main():
    root = os.path.realpath(sys.argv[1])
    out = os.path.abspath(sys.argv[2])
    os.chdir(root)
    sys.path.insert(0, root)
    os.environ.setdefault("SKIP_INTEGRATION", "0")
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from br import tables

    module = importlib.import_module(tables.OWNER[0])
    cls = getattr(module, tables.OWNER[1])
    cls.setUpClass()
    stored = tables.dump(cls)
    sqls = (list(cls.tpch_sqls), list(cls.tpcds_sqls))
    cls.tearDownClass()
    tables.setup(cls, stored)
    again = tables.dump(cls)
    same = again == stored and (list(cls.tpch_sqls), list(cls.tpcds_sqls)) == sqls
    for attr in tables.ATTRS:
        for name, table in getattr(cls, attr).items():
            same = same and type(table).__name__ == "Table" and all(type(r) is tuple for r in table.rows[:50])
    cls.tearDownClass()
    if not same:
        print("stored rows do not reproduce the set-up: nothing written")
        return
    with open(out, "wb") as fh:
        pickle.dump(stored, fh, protocol=5)
    print("tables:", sum(len(v) for v in stored.values()), "rows:", sum(len(rows) for v in stored.values() for _, rows in v.values()), "bytes:", os.path.getsize(out))


main()
