"""DuckDB's answers to the SQL texts tests hand it, worked out once when the image is built.

Many optimizer tests ask DuckDB for the rows of a query before and after sqlglot rewrote it
and compare the two.  What DuckDB answers depends on the statements run on that connection so
far and on the text asked, not on sqlglot.  `tools/build_answers.py` runs those tests at the
pinned commit and stores, for each state of a connection (the statements that built it) and
each query text asked in that state, what came back.  A text is kept only if a new connection
that is given the same statements answers the same again, from an empty directory and with
none of the test's Python objects in reach.

At run time a connection that a test opens in memory is not opened until it is needed:

  * a statement that leads to a state seen when the store was built is only noted;
  * a query whose answer is stored for the current state gets that answer;
  * anything else opens the real connection, replays the noted statements and asks DuckDB.

So a rewritten query that differs from every text seen at the pinned commit is always run by
DuckDB itself.  A query that could depend on something other than the connection's own tables
(a file, the clock, random numbers, a Python object) is never stored; a statement that could
change the connection in ways its text does not tell (a setting, an extension, a sequence)
ends the use of the store on that connection.
"""
import hashlib
import os
import pickle
import re

PATH = os.environ.get("BR_ANSWERS", "/app/answers.pkl")
# validation only: a file that collects every stored answer DuckDB itself disagrees with
CHECK = os.environ.get("BR_ANSWER_CHECK")
VERSION = 1
START = b"start"
# what a test may ask of a result that is answered from the store
METHODS = ("fetchall", "df", "fetchdf")
# attributes that read the connection's current result and change nothing
READS = frozenset(METHODS) | {"fetchone", "fetchmany", "fetchnumpy", "fetch_df", "arrow", "fetch_arrow_table", "pl", "description", "rowcount"}

# commands, and functions that leave something behind: such a statement changes the connection
# in ways its text does not tell, so the store is not used after it
_COMMANDS = re.compile(
    r"\b(copy|attach|detach|install|load|import|export|pragma|set|reset|call|checkpoint|use|nextval|currval|setseed"
    r"|set_\w+|load_\w+|install_\w+|force_\w+|enable_\w+|disable_\w+|pragma_\w+)\b",
    re.I,
)
# words that make an answer depend on more than the connection's own tables
_OUTSIDE = re.compile(
    r"\b(now|today|current_\w+|get_current_\w+|localtime|localtimestamp|\w+_timestamp|random|uuid\w*|gen_random_uuid"
    r"|sample|tablesample|age|getenv|getvariable|read_\w+|\w+_scan|scan_\w+|glob|sniff_\w+|parquet_\w+|query"
    r"|query_table|duckdb_\w+|pg_\w+|txid_\w+|timeofday|getdate|sysdate|\w*shuffle\w*)\b",
    re.I,
)
# words by which a statement that starts like a query may still change the connection
_WRITES = re.compile(
    r"\b(insert|update|delete|create|drop|alter|truncate|merge|into|vacuum|analyze|begin|commit|rollback|abort"
    r"|prepare|execute|deallocate|grant|revoke|comment|summarize)\b",
    re.I,
)
_QUOTED = re.compile(r"'(?:[^']|'')*'|\"(?:[^\"]|\"\")*\"|\$\$.*?\$\$", re.S)
_FILE = re.compile(r"[/\\]|\.\w{1,8}$|^(now|today|tomorrow|yesterday|allballs)$", re.I)
_FIRST = re.compile(r"[\s(]*([A-Za-z_]+)")


def kind(sql):
    """How a statement is treated:

    "query"   only reads the connection's own tables: its answer may be stored;
    "ask"     reads, but the answer may depend on something else (a file, the clock, chance):
              DuckDB is asked every time, and the connection is as it was;
    "change"  may alter the connection: its text becomes part of the state;
    None      may alter it in ways the text does not tell: no store on this connection after it.
    """
    if _COMMANDS.search(sql):
        return None
    outside = _OUTSIDE.search(sql) is not None
    if not outside:
        for quoted in _QUOTED.findall(sql):
            body = quoted[2:-2] if quoted.startswith("$$") else quoted[1:-1]
            if _FILE.search(body.strip()):
                outside = True
                break
    first = _FIRST.match(sql)
    if first is not None and first.group(1).lower() in ("select", "with", "from", "values") and not _WRITES.search(sql):
        return "ask" if outside else "query"
    return None if outside else "change"


def _bytes(sql):
    return sql.encode("utf-8", "surrogatepass")


def step(state, sql):
    """The state a connection is in after `sql` changed it in `state`."""
    return hashlib.sha1(state + b"\x00" + _bytes(sql)).digest()


def ask(state, sql):
    """The key of query `sql` asked in `state`."""
    return hashlib.sha1(state + b"\x01" + _bytes(sql)).digest()


def same(a, b):
    """Are two things DuckDB handed back the same answer?"""
    if type(a) is not type(b):
        return False
    if type(a).__name__ == "DataFrame":
        try:
            from pandas.testing import assert_frame_equal

            assert_frame_equal(a, b, check_exact=True)
            return list(a.dtypes) == list(b.dtypes)
        except Exception:
            return False
    try:
        # rows are tuples of plain values, whose text form tells them apart exactly
        return repr(a) == repr(b)
    except Exception:
        return False


_cache = [None, False]


def load():
    """(answers, states) from the store, or None when the image has none."""
    if not _cache[1]:
        _cache[1] = True
        try:
            with open(PATH, "rb") as fh:
                data = pickle.load(fh)
            if data.get("version") == VERSION:
                _cache[0] = (data["answers"], data["states"])
        except Exception:
            _cache[0] = None
    return _cache[0]


class StoreConn:
    """Stands in for an in-memory DuckDB connection (see the module text)."""

    def __init__(self, factory, args, kwargs, store):
        self.__dict__.update(_factory=(factory, args, kwargs), _store=store, _real=None, _noted=[], _state=START, _last=None, _closed=False)

    def _open(self):
        """The real connection, brought to where this one stands."""
        d = self.__dict__
        if d["_real"] is None:
            factory, args, kwargs = d["_factory"]
            real = factory(*args, **kwargs)
            for sql in d["_noted"]:
                real.execute(sql)
            d["_real"] = real
            if d["_closed"]:
                real.close()
        return d["_real"]

    def execute(self, *args, **kwargs):
        d = self.__dict__
        state = d["_state"]
        d["_last"] = None
        if state is None or d["_closed"] or len(args) != 1 or kwargs or type(args[0]) is not str:
            d["_state"] = None
            return self._open().execute(*args, **kwargs)
        sql = args[0]
        what = kind(sql)
        if what is None:
            d["_state"] = None
            return self._open().execute(sql)
        if what == "ask":
            return self._open().execute(sql)
        answers, states = d["_store"]
        if what == "query":
            found = answers.get(ask(state, sql))
            if found is None:
                return self._open().execute(sql)
            d["_last"] = Stored(self, sql, found)
            return d["_last"]
        nxt = step(state, sql)
        if d["_real"] is None and nxt in states:
            # ran without error from this very state when the store was built: note it
            d["_noted"].append(sql)
            d["_state"] = nxt
            return self
        d["_state"] = None  # (stays unknown if the statement fails)
        result = self._open().execute(sql)
        d["_state"] = nxt
        return result

    def close(self):
        d = self.__dict__
        d["_closed"] = True
        d["_state"] = None
        d["_last"] = None
        if d["_real"] is not None:
            d["_real"].close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    def __getattr__(self, name):
        # anything else is DuckDB's business: hand over the real connection, holding the result
        # of whatever was asked last
        d = self.__dict__
        real = self._open()
        last = d["_last"]
        if last is not None:
            d["_last"] = None
            last._rerun(real)
        if name not in READS:
            d["_state"] = None
        return getattr(real, name)


class Stored:
    """What `execute` hands back for a query whose answer is in the store."""

    def __init__(self, conn, sql, found):
        self.__dict__.update(_conn=conn, _sql=sql, _found=found, _served=[])

    def _rerun(self, real):
        """Make the real connection hold this query's result, read as far as the test read it."""
        real.execute(self._sql)
        for name in self._served:
            getattr(real, name)()

    def _serve(self, name, args, kwargs):
        conn = self._conn
        if args or kwargs or self._served or name not in self._found or conn.__dict__["_last"] is not self:
            return getattr(conn, name)(*args, **kwargs)
        value = pickle.loads(self._found[name])
        if CHECK:
            _recheck(conn, self._sql, name, value)
        self._served.append(name)
        return value

    def fetchall(self, *args, **kwargs):
        return self._serve("fetchall", args, kwargs)

    def df(self, *args, **kwargs):
        return self._serve("df", args, kwargs)

    def fetchdf(self, *args, **kwargs):
        return self._serve("fetchdf", args, kwargs)

    def __getattr__(self, name):
        return getattr(self._conn, name)


def _recheck(conn, sql, name, value):
    """Validation: ask DuckDB as well and note a stored answer it does not give."""
    try:
        real = conn._open()
        other = getattr(real.execute(sql), name)()
        ok = same(value, other)
    except BaseException as e:  # noqa: BLE001
        ok, other = False, repr(e)
    if not ok:
        try:
            with open(CHECK, "a") as fh:
                fh.write("MISMATCH %s %r\n  stored %r\n  duckdb %r\n" % (name, sql[:300], str(value)[:300], str(other)[:300]))
        except OSError:
            pass
    else:
        try:
            with open(CHECK + ".ok", "a") as fh:
                fh.write(".")
        except OSError:
            pass
