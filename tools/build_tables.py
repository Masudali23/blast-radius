"""Store the rows TestExecutor.setUpClass parses, for br/tables.py.

Usage: build_tables.py <repo_root> <out.pkl>

Runs the real set-up at the pinned commit, then the shortened one fed from the stored rows, and
writes the file only if both leave the class with the same tables.

Then runs the tests that compare against DuckDB and stores every frame DuckDB handed back, by
connection and SQL text; a text is kept only if asking DuckDB again gives an equal frame.
"""
import importlib
import os
import pickle
import sys
import unittest

DUCKDB_TESTS = ("test_execute_tpch", "test_optimized_tpch", "test_execute_tpcds")


class Recorder:
    """A DuckDB connection that notes the frame returned for each SQL text."""

    def __init__(self, real, sink):
        self.__dict__.update(_real=real, _sink=sink)

    def execute(self, *args, **kwargs):
        result = self._real.execute(*args, **kwargs)
        if len(args) == 1 and not kwargs and type(args[0]) is str:
            return Recorded(result, args[0], self._sink)
        return result

    def __getattr__(self, name):
        return getattr(self._real, name)


class Recorded:
    def __init__(self, result, sql, sink):
        self.__dict__.update(_result=result, _sql=sql, _sink=sink)

    def fetchdf(self, *args, **kwargs):
        frame = self._result.fetchdf(*args, **kwargs)
        if not args and not kwargs:
            self._sink.setdefault(self._sql, frame.copy(deep=True))
        return frame

    def __getattr__(self, name):
        return getattr(self._result, name)


def duckdb_answers(cls, tables):
    """{connection: {sql text: frame}} for everything the DuckDB tests ask at the pinned commit."""
    import duckdb

    real_connect = duckdb.connect

    def connect(*args, **kwargs):  # one thread, as at run time (br/worker.py)
        config = dict(kwargs.get("config") or {})
        config.setdefault("threads", 1)
        kwargs["config"] = config
        return real_connect(*args, **kwargs)

    duckdb.connect = connect
    answers = {name: {} for name in tables.CONNS}
    try:
        cls.setUpClass()
        try:
            real = {name: getattr(cls, name) for name in tables.CONNS}
            for name in tables.CONNS:
                setattr(cls, name, Recorder(real[name], answers[name]))
            result = unittest.TestResult()
            for meth in DUCKDB_TESTS:
                if hasattr(cls, meth):
                    cls(meth).run(result)
            for name in tables.CONNS:
                setattr(cls, name, real[name])
            if not result.wasSuccessful():
                return {}
            for name, per in answers.items():
                for sql in list(per):
                    try:
                        again = real[name].execute(sql).fetchdf()
                    except Exception:
                        again = None
                    if again is None or not per[sql].equals(again):
                        del per[sql]
        finally:
            cls.tearDownClass()
    finally:
        duckdb.connect = real_connect
    return answers


def main():
    root = os.path.realpath(sys.argv[1])
    out = os.path.abspath(sys.argv[2])
    os.chdir(root)
    sys.path.insert(0, root)
    os.environ.setdefault("SKIP_INTEGRATION", "0")
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from br import inlinepool, tables

    # before the test module is imported: its process pools then run in this process
    inlinepool.install()
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
    ntables = sum(len(v) for v in stored.values())
    nrows = sum(len(rows) for v in stored.values() for _, rows in v.values())
    try:
        answers = duckdb_answers(cls, tables)
    except Exception as e:  # the rows are still worth having
        print("no DuckDB answers stored: %r" % (e,))
        answers = {}
    stored[tables.ANSWERS] = answers
    with open(out, "wb") as fh:
        pickle.dump(stored, fh, protocol=5)
    print("tables:", ntables, "rows:", nrows, "DuckDB answers:", sum(len(v) for v in answers.values()), "bytes:", os.path.getsize(out))


if __name__ == "__main__":
    main()
