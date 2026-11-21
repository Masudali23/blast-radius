"""Store the rows TestExecutor.setUpClass parses, for br/tables.py.

Usage: build_tables.py <repo_root> <out.pkl> [<frames.pkl>]

Runs the real set-up at the pinned commit, then the shortened one fed from the stored rows, and
writes the file only if both leave the class with the same tables.

Then runs the tests that compare against DuckDB and stores every frame DuckDB handed back, by
connection and SQL text; a text is kept only if asking DuckDB again gives an equal frame.
(Only DuckDB's side is wanted here, so the two tests that also run sqlglot's own engine are
given an empty table in its place: they ask DuckDB the same questions and take no time.)

With <frames.pkl> (maps/frames.pkl: those frames, written by an earlier run of this script in
the same image) the DuckDB tests are not run again: the frames are taken from the file.
`--frames-only <out>` writes just that file.
"""
import importlib
import os
import pickle
import sys
import unittest

DUCKDB_TESTS = ("test_execute_tpch", "test_optimized_tpch", "test_execute_tpcds")
FRAMES_VERSION = 1


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


def duckdb_answers(cls, tables, module):
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
    real_engine = module.mp_execute
    try:
        tables.setup(cls)
        try:
            real = {name: getattr(cls, name) for name in tables.CONNS}
            for name in tables.CONNS:
                setattr(cls, name, Recorder(real[name], answers[name]))
            for meth in DUCKDB_TESTS:
                if hasattr(cls, meth):
                    with_engine = "execute" not in meth
                    module.mp_execute = real_engine if with_engine else tables.no_engine
                    result = unittest.TestResult()
                    cls(meth).run(result)
                    if with_engine and not result.wasSuccessful():
                        return {}
            module.mp_execute = real_engine
            for name in tables.CONNS:
                setattr(cls, name, real[name])
            for name, per in answers.items():
                for sql in list(per):
                    try:
                        again = real[name].execute(sql).fetchdf()
                    except Exception:
                        again = None
                    if again is None or not per[sql].equals(again):
                        del per[sql]
        finally:
            module.mp_execute = real_engine
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
    tables._cache[0], tables._cache[1] = stored, True  # (what load() hands out from here on)
    answers = None
    frames = sys.argv[3] if len(sys.argv) > 3 else None
    if frames and os.path.exists(frames):
        try:
            with open(frames, "rb") as fh:
                kept = pickle.load(fh)
            if kept.get("version") == FRAMES_VERSION and set(kept["frames"]) == set(tables.CONNS):
                answers = kept["frames"]
        except Exception as e:
            print("stored frames not usable: %r" % (e,))
    if answers is None:
        try:
            answers = duckdb_answers(cls, tables, module)
        except Exception as e:  # the rows are still worth having
            print("no DuckDB answers stored: %r" % (e,))
            answers = {}
    stored[tables.ANSWERS] = answers
    with open(out, "wb") as fh:
        pickle.dump(stored, fh, protocol=5)
    with open(out + ".info", "w") as fh:  # for the selector, which never loads the rows itself
        fh.write("answers %d\n" % sum(len(v) for v in answers.values()))
    print("tables:", ntables, "rows:", nrows, "DuckDB answers:", sum(len(v) for v in answers.values()), "bytes:", os.path.getsize(out))
    if "--frames-only" in sys.argv:
        target = sys.argv[sys.argv.index("--frames-only") + 1]
        with open(target, "wb") as fh:
            pickle.dump({"version": FRAMES_VERSION, "frames": answers}, fh, protocol=5)
        print("frames:", os.path.getsize(target), "bytes")


if __name__ == "__main__":
    main()
