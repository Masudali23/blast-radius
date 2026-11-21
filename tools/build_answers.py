"""Store what DuckDB answers to the queries the optimizer tests hand it, for br/answers.py.

Usage: build_answers.py <repo_root> <out.pkl>

Each test of tests/test_optimizer.py is run by itself at the pinned commit, with every
in-memory DuckDB connection it opens watched: which statements changed the connection, which
queries were asked in which state, and what came back.  Afterwards every state is built again
on a new connection, from an empty directory and without access to Python objects, and every
query is asked again there; only what gives the same answer both times is written.
"""
import importlib
import os
import pickle
import sys
import tempfile
import unittest

MODULES = ("tests.test_optimizer",)
MAX_BYTES = 200_000


class Book:
    def __init__(self):
        self.parent = {}  # state -> (state before, statement)
        self.asked = {}  # (state, sql) -> {method: value}


class Watched:
    """A real connection whose statements and answers are written into the book."""

    def __init__(self, real, book, answers):
        self.__dict__.update(_real=real, _book=book, _a=answers, _state=answers.START)

    def execute(self, *args, **kwargs):
        d, a = self.__dict__, self._a
        state = d["_state"]
        if state is None or len(args) != 1 or kwargs or type(args[0]) is not str:
            d["_state"] = None
            return d["_real"].execute(*args, **kwargs)
        sql = args[0]
        what = a.kind(sql)
        if what is None:
            d["_state"] = None
            return d["_real"].execute(sql)
        if what == "ask":
            return d["_real"].execute(sql)
        if what == "query":
            return Answer(d["_real"].execute(sql), state, sql, d["_book"])
        d["_state"] = None
        result = d["_real"].execute(sql)
        nxt = a.step(state, sql)
        d["_book"].parent[nxt] = (state, sql)
        d["_state"] = nxt
        return result

    def close(self):
        self.__dict__["_state"] = None
        self._real.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    def __getattr__(self, name):
        if name not in self._a.READS:
            self.__dict__["_state"] = None
        return getattr(self._real, name)


class Answer:
    def __init__(self, result, state, sql, book):
        self.__dict__.update(_result=result, _key=(state, sql), _book=book, _read=False)

    def _note(self, name, args, kwargs):
        value = getattr(self._result, name)(*args, **kwargs)
        if not args and not kwargs and not self._read:
            try:
                self._book.asked.setdefault(self._key, {}).setdefault(name, pickle.loads(pickle.dumps(value)))
            except Exception:
                pass
        self.__dict__["_read"] = True
        return value

    def fetchall(self, *args, **kwargs):
        return self._note("fetchall", args, kwargs)

    def df(self, *args, **kwargs):
        return self._note("df", args, kwargs)

    def fetchdf(self, *args, **kwargs):
        return self._note("fetchdf", args, kwargs)

    def __getattr__(self, name):
        self.__dict__["_read"] = True
        return getattr(self._result, name)


def history(book, state, start):
    out = []
    while state != start:
        state, sql = book.parent[state]
        out.append(sql)
    return out[::-1]


def main():
    root = os.path.realpath(sys.argv[1])
    out = os.path.abspath(sys.argv[2])
    os.chdir(root)
    sys.path.insert(0, root)
    os.environ.setdefault("SKIP_INTEGRATION", "0")
    sys.dont_write_bytecode = True
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    import pytest  # noqa: F401  (sqlglot's test mode looks for it before it is imported)
    from br import answers, inlinepool

    inlinepool.install()
    import duckdb

    real_connect = duckdb.connect
    book = Book()

    def one_thread(kwargs):  # as at run time (br/worker.py)
        config = dict(kwargs.get("config") or {})
        config.setdefault("threads", 1)
        kwargs["config"] = config
        return kwargs

    def connect(*args, **kwargs):
        plain = not args and not kwargs
        conn = real_connect(*args, **one_thread(kwargs))
        return Watched(conn, book, answers) if plain else conn

    duckdb.connect = connect
    ran = 0
    for name in MODULES:
        module = importlib.import_module(name)
        for cls in vars(module).values():
            if not (isinstance(cls, type) and issubclass(cls, unittest.TestCase) and cls.__module__ == name):
                continue
            for meth in sorted(m for m in dir(cls) if m.startswith("test")):
                try:
                    cls.setUpClass()
                    cls(meth).run(unittest.TestResult())
                    ran += 1
                except BaseException:  # noqa: BLE001
                    pass
                finally:
                    try:
                        cls.tearDownClass()
                        cls.doClassCleanups()
                    except BaseException:  # noqa: BLE001
                        pass
    duckdb.connect = real_connect

    # every state again, away from the repository and from the tests' objects
    by_state = {}
    for (state, sql), methods in book.asked.items():
        by_state.setdefault(state, []).append((sql, methods))
    stored, states, dropped = {}, set(), 0
    here = os.getcwd()
    with tempfile.TemporaryDirectory() as empty:
        os.chdir(empty)
        try:
            for state in set(book.parent) | set(by_state):
                try:
                    statements = history(book, state, answers.START)
                except KeyError:
                    continue
                config = {"threads": 1}
                try:
                    conn = real_connect(config=dict(config, python_enable_replacement=False))
                except Exception:
                    conn = real_connect(config=config)
                try:
                    for sql in statements:
                        conn.execute(sql)
                except Exception:
                    conn.close()
                    continue
                states.add(state)
                for sql, methods in by_state.get(state, ()):
                    kept = {}
                    for meth, value in methods.items():
                        try:
                            again = getattr(conn.execute(sql), meth)()
                            data = pickle.dumps(again, protocol=5)
                        except Exception:
                            again, data = None, None
                        if data is not None and len(data) <= MAX_BYTES and answers.same(value, again):
                            kept[meth] = data
                        else:
                            dropped += 1
                    if kept:
                        stored[answers.ask(state, sql)] = kept
                conn.close()
        finally:
            os.chdir(here)
    states.discard(answers.START)
    with open(out, "wb") as fh:
        pickle.dump({"version": answers.VERSION, "answers": stored, "states": states}, fh, protocol=5)
    sys.stderr.write("tests run %d, states %d, answers %d (dropped %d), %d bytes\n" % (ran, len(states), len(stored), dropped, os.path.getsize(out)))


if __name__ == "__main__":
    main()
