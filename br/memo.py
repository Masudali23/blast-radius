"""Skip the expensive half of an executor test entry when its input is provably unchanged.

`TestExecutor.test_execute_tpcds` spends nearly all of its time inside
`PythonExecutor.execute(plan)` (a pure-Python query engine walking table rows) and in comparing
that result with DuckDB's.  Everything before it (parsing, optimizing, planning, transpiling the
query for DuckDB) is quick.

For a patch that changes no code the slow half runs, the slow half is a fixed function of what
it is handed: the plan, the tables, and the SQL text given to DuckDB.  The maps keep a
fingerprint of those three things per fixture entry, taken at the pinned commit (where every
entry passes).  When the patched code hands over exactly the same things, the entry must pass
again, and its slow half is not run.  Any entry whose fingerprint differs is executed and
compared for real.

Which lines "the slow half runs" is recorded while the fingerprints are taken (tools/
trace_memo.py); the selector only asks for this variant when the patch touches none of them and
changes nothing at module or class level.
"""
import enum
import hashlib
import os
import pickle
import sys
import time

from br import inlinepool

# an allowance this large means "run whatever is not recognised"
WHOLE = 1e11
# CPU seconds a quick half may take, and a slow half: so much plus a multiple of its usual cost
QUICK_LEASH = float(os.environ.get("BR_QUICK_LEASH", "2.0"))
CHECK_LEASH = 6.0  # ... and comparing an entry's result with DuckDB's
SLOW_LEASH = float(os.environ.get("BR_SLOW_LEASH", "2.0"))
SLOW_FACTOR = float(os.environ.get("BR_SLOW_FACTOR", "4.0"))
# validation only: a file noting, for every slow half that is skipped, what running it gives
CHECK = os.environ.get("BR_MEMO_CHECK")
# measuring only: a file noting what each session did
NOTE = os.environ.get("BR_MEMO_NOTE")

TESTS = {
    # test id -> (class attribute with the fixture entries, is_tpch flag value)
    "tests/test_executor.py::TestExecutor::test_execute_tpcds": "tpcds_sqls",
}
MAX_DEPTH = 400


class Unstable(Exception):
    """Something in the hand-over has no stable description: never treat it as unchanged."""


def _name(t):
    return "%s.%s" % (getattr(t, "__module__", "?"), getattr(t, "__qualname__", getattr(t, "__name__", "?")))


class _Canon:
    """A description of a plan that is the same in every process when the plan is the same."""

    def __init__(self):
        self.out = []
        self.steps = {}
        mods = sys.modules
        self.expr = mods["sqlglot.expressions"].Expr if "sqlglot.expressions" in mods else ()
        planner = mods.get("sqlglot.planner")
        self.step = planner.Step if planner is not None else ()
        self.plan = planner.Plan if planner is not None else ()
        table = mods.get("sqlglot.executor.table")
        self.table = table.Table if table is not None else ()

    def text(self):
        return "".join(self.out)

    def _catalogue(self, mapping, depth):
        if type(mapping) is dict and all(type(k) is str for k in mapping):
            self.out.append("{")
            for k in sorted(mapping):
                self.out.append("s%d:%s:" % (len(k), k))
                self._catalogue(mapping[k], depth + 1)
            self.out.append("}")
        else:
            self.add(mapping, depth, None)

    def _sub(self):
        sub = _Canon.__new__(_Canon)
        sub.out, sub.steps, sub.expr, sub.step, sub.plan, sub.table = [], self.steps, self.expr, self.step, self.plan, self.table
        return sub

    def add(self, obj, depth=0, parent=None):
        out = self.out
        if depth > MAX_DEPTH:
            raise Unstable("too deep")
        t = type(obj)
        if obj is None or t is bool or t is int or t is float:
            out.append(repr(obj) + ";")
        elif t is str:
            out.append("s%d:%s;" % (len(obj), obj))
        elif t is bytes:
            out.append("b" + obj.hex() + ";")
        elif self.expr and isinstance(obj, self.expr):
            out.append("E<" + _name(t))
            for k, v in obj.args.items():
                out.append("," + k + "=")
                self.add(v, depth + 1, obj)
            out.append("|t=")
            self.add(getattr(obj, "_type", None), depth + 1, None)
            out.append("|c=")
            self.add(getattr(obj, "comments", None), depth + 1, None)
            out.append("|m=")
            self.add(getattr(obj, "_meta", None), depth + 1, None)
            # where the node believes it sits: code downstream may walk upwards
            p = getattr(obj, "parent", None)
            out.append("|k=%r,%r,%s>" % (getattr(obj, "arg_key", None), getattr(obj, "index", None), "-" if p is None else "^" if p is parent else "?"))
        elif t is list or t is tuple:
            out.append("[" if t is list else "(")
            for v in obj:
                self.add(v, depth + 1, parent)
            out.append("]" if t is list else ")")
        elif t is dict:
            out.append("{")
            for k, v in obj.items():
                self.add(k, depth + 1, None)
                out.append(":")
                self.add(v, depth + 1, parent)
            out.append("}")
        elif t is set or t is frozenset:
            parts = []
            for v in obj:
                sub = self._sub()
                sub.add(v, depth + 1, None)
                parts.append(sub.text())
            out.append("S{" + "".join(sorted(parts)) + "}")
        elif isinstance(obj, enum.Enum):
            out.append("N<%s.%s>" % (_name(t), obj.name))
        elif isinstance(obj, type):
            out.append("C<%s>" % _name(obj))
        elif self.step and isinstance(obj, self.step):
            # Steps form a graph held together by sets, whose order differs from process to
            # process: describe each step by what it is and what it depends on (spelled out in
            # full, so no numbering is needed) and name the steps that depend on it.
            text = self.steps.get(id(obj))
            if text is None:
                self.steps[id(obj)] = "!cycle;"
                sub = self._sub()
                sub.out.append("P<" + _name(t))
                for k in sorted(vars(obj)):
                    if k == "dependents":
                        continue
                    sub.out.append("," + k + "=")
                    sub.add(vars(obj)[k], depth + 1, None)
                sub.out.append(",dependents=" + "|".join(sorted("%s/%s" % (type(d).__name__, getattr(d, "name", None)) for d in obj.dependents)))
                sub.out.append(">")
                text = sub.text()
                self.steps[id(obj)] = text
            out.append(text)
        elif self.plan and isinstance(obj, self.plan):
            # (the step graph hanging off the root; the dag is derived from it on demand)
            out.append("Q<")
            self.add(obj.expression, depth + 1, None)
            self.add(obj.ctes, depth + 1, None)
            self.add(obj.root, depth + 1, None)
            out.append(">")
        elif self.table and isinstance(obj, self.table):
            rows = obj.rows
            out.append("T<")
            self.add(obj.columns, depth + 1, None)
            out.append(repr(obj.column_range) + ";%d;" % len(rows))
            for r in list(rows[:2]) + list(rows[-2:]):
                self.add(tuple(r) if isinstance(r, (list, tuple)) else repr(type(r)), depth + 1, None)
            out.append(">")
        elif t is range:
            out.append(repr(obj) + ";")
        elif hasattr(obj, "mapping") and hasattr(obj, "supported_table_args"):
            # the executor's table catalogue: looked up by name, and filled in whatever order a
            # set of table names happened to come out
            out.append("M<" + _name(t))
            self._catalogue(obj.mapping, depth + 1)
            out.append(">")
        else:
            raise Unstable(_name(t))


def fingerprint(*things):
    c = _Canon()
    for thing in things:
        c.add(thing)
        c.out.append("#")
    c.out.append("steps=%d" % len(c.steps))  # the same description twice is not the same step
    return hashlib.sha1(c.text().encode("utf-8", "surrogatepass")).hexdigest()


def text_key(text):
    return hashlib.sha1(text.encode("utf-8", "surrogatepass")).hexdigest()


class Unchanged:
    """Stands in for the table an entry would produce; travels through the pool's pickling."""

    def __init__(self, key):
        self.key = key


class Session:
    """Wraps the two hand-over points for the duration of one test.

    record=None: replay against `known` ({sql key: [plan fingerprint, duckdb text key]}).
    record=dict: run everything for real and fill the dict the same way (map building).
    After a strict session gave up, `gave_up` is set and the test's outcome means nothing.
    """

    def __init__(self, module, cls, attr, known=None, record=None, phase=None, strict=False, costs=None, budget=0.0, capture=None, must=(), sample=0.0, leash=None):
        self.module = module
        self.cls = cls
        self.attr = attr
        self.known = known or {}
        self.plans = {v[0] for v in self.known.values()}
        self.record = record
        self.phase = phase  # callable(bool): tells the tracer when the slow half runs
        self.capture = capture  # map building: callable(plan, tables), given every hand-over
        self.must = frozenset(must)  # entries whose slow half runs patched code: never skipped
        self.pending = {}
        # strict: on the first entry that is not recognised, stop doing work and report so
        self.strict = strict
        self.gave_up = False
        # A strict session may still run entries whose hand-over is new, for real, as long as
        # their estimated cost ({sql key: ms}) stays within `budget`.
        self.costs = costs or {}
        self.budget = budget
        # no limit: every entry is settled, either recognised or run for real
        self.whole = budget >= WHOLE
        # When the entries that are handed something new cost more than `budget` together, the
        # least expensive of them are still run, up to `sample` (ms): then `only` holds their
        # keys and a pass says no more than that this sample passed.
        self.sample = sample
        self.only = None
        self.cleared = False  # weighed, and what is to be run fits the allowance
        # callable(seconds): how much CPU time may pass before the next hand-over or result;
        # an entry that hangs is then cut off long before the test's own limit
        self.leash = leash
        self.slowest = max(self.costs.values(), default=1000.0)
        self.spent = 0.0
        self.cost_record = {}  # map building: CPU ms of each entry's slow half
        self.depth = 0
        self.fronts = []  # plan fingerprint of every hand-over, in the order they were made
        self.looked = 0  # entries decided so far (strict sessions)
        self.total = 0
        self.weighed = False
        self.duckdb_text = {}
        self.skipped = 0
        self.ran = 0
        self._saved = []

    # -- plumbing ----------------------------------------------------------
    def _patch(self, owner, name, value):
        self._saved.append((owner, name, owner.__dict__[name] if isinstance(owner, type) else getattr(owner, name)))
        setattr(owner, name, value)

    def __enter__(self):
        py = sys.modules["sqlglot.executor.python"]
        session = self
        real_execute = py.PythonExecutor.execute
        real_helper = self.cls.subtestHelper
        real_transpile = self.module.transpile

        def back(on):
            if session.phase is not None:
                session.phase(on)

        def hold(cost=None, base=QUICK_LEASH):
            """Allow the next step its time: a quick half, or a slow half of about `cost` ms."""
            if session.leash is not None:
                session.leash(base if cost is None else SLOW_LEASH + SLOW_FACTOR * cost / 1000.0)

        def execute(executor, plan, outer_scope=None):
            if session.depth or outer_scope is not None:
                # a subquery, executed from inside the slow half
                return real_execute(executor, plan, outer_scope)
            hold()
            if session.gave_up:
                return Unchanged(None)
            try:
                key = fingerprint(plan, executor.tables)
            except Unstable:
                key = None
            session.fronts.append(key)
            if session.strict and key is None and session.whole:
                return slow(real_execute, executor, plan)  # cannot be recognised: run it
            if session.strict and (key is None or (key not in session.plans and session.budget <= 0)):
                # not a plan seen at the pinned commit, and no allowance to run it for real
                session.gave_up = True
                return Unchanged(None)
            if session.record is None and key is not None:
                # hand back a stand-in; the helper decides whether the real thing is needed
                session.pending[key] = (executor, plan)
                return Unchanged(key)
            if session.capture is not None:
                session.capture(plan, executor.tables)
            t0 = time.process_time()
            table = slow(real_execute, executor, plan)
            if session.record is not None:
                table._br_plan = key
                table._br_cost = time.process_time() - t0
            return table

        def slow(fn, *args, cost=None, **kwargs):
            session.depth += 1
            back(True)
            hold(session.slowest if cost is None else cost)
            try:
                return fn(*args, **kwargs)
            finally:
                session.depth -= 1
                back(False)
                hold(base=CHECK_LEASH)

        def transpile(sql, *args, **kwargs):
            # front half: its lines are not part of the slow half, its output is compared
            back(False)
            try:
                return real_transpile(sql, *args, **kwargs)
            finally:
                back(True)

        def unchanged(skey, sql, key):
            """Is this entry handed exactly what the pinned commit handed over?"""
            want = session.known.get(skey)
            if want is None or key is None or want[0] != key or skey in session.must:
                return False
            text = session.duckdb_text.get(skey)
            if text is None:
                try:
                    text = text_key(real_transpile(sql, write="duckdb")[0])
                except Exception:
                    text = ""
                session.duckdb_text[skey] = text
            return text == want[1]

        def helper(test, i, table, tpch=True):
            entries = test.tpch_sqls if tpch else test.tpcds_sqls
            sql = entries[i][1]
            skey = text_key(sql)
            if session.gave_up:
                return None
            hold(base=CHECK_LEASH)  # (asking DuckDB may mean opening its connection first)
            if session.strict and not session.weighed and not session.whole:
                # Every quick half has run by now (the pool takes all queries before the first
                # result is looked at), so it is known which entries are handed something new.
                # If running all of those for real costs more than the allowance, stop before
                # running any: the caller keeps the test.
                session.weighed = True
                ran = [e for e in entries if e[0].get("execute")]
                session.total = len(ran)
                if len(ran) == len(session.fronts):
                    new = []
                    for (_, text, *_rest), key in zip(ran, session.fronts):
                        k = text_key(text)
                        if not unchanged(k, text, key):
                            new.append((session.costs.get(k, float("inf")), k))
                    if sum(cost for cost, _ in new) <= session.budget:
                        session.cleared = True
                    else:
                        take, spent = set(), 0.0
                        for cost, k in sorted(new):
                            if spent + cost > session.sample:
                                break
                            take.add(k)
                            spent += cost
                        if not take:
                            session.gave_up = True
                            return None
                        session.only = take
                        session.cleared = True
            session.looked += 1
            if isinstance(table, Unchanged):
                executor, plan = session.pending.pop(table.key)
                same = unchanged(skey, sql, table.key)
                if same and CHECK:
                    # validation: run the slow half all the same; it must not fail
                    try:
                        real_helper(test, i, slow(real_execute, executor, plan), tpch=tpch)
                        note = "ok"
                    except BaseException as e:  # noqa: BLE001
                        note = "FAILS %s %r" % (skey, repr(e)[:200])
                    try:
                        with open(CHECK, "a") as fh:
                            fh.write(note + "\n")
                    except OSError:
                        pass
                if same:
                    session.skipped += 1
                    return None
                if session.only is not None and skey not in session.only:
                    return None  # not in the sample: nothing is said about this entry
                if session.strict and not session.whole and not session.cleared:
                    # a new hand-over: run this entry for real if the allowance covers it,
                    # otherwise stop here and let the caller keep the test
                    cost = session.costs.get(skey)
                    if cost is None or session.spent + cost > session.budget:
                        session.gave_up = True
                        return None
                    if session.looked >= 8 and session.total and (session.spent + cost) * session.total / (session.looked + 1) > session.budget:
                        # at this rate the allowance will not cover the test: stop now
                        session.gave_up = True
                        return None
                    session.spent += cost
                table = slow(real_execute, executor, plan, cost=session.costs.get(skey))
                # (the test gets its table back from a process pool: the same round trip)
                table = pickle.loads(pickle.dumps(table))
                session.ran += 1
                return real_helper(test, i, table, tpch=tpch)
            session.ran += 1
            if session.record is None:
                return real_helper(test, i, table, tpch=tpch)
            # map building: run for real, note what was handed over
            session.module.transpile = transpile
            t0 = time.process_time()
            try:
                out = slow(real_helper, test, i, table, tpch=tpch)
            finally:
                session.module.transpile = real_transpile
            session.cost_record[skey] = (getattr(table, "_br_cost", 0.0) + time.process_time() - t0) * 1000.0
            plan_key = getattr(table, "_br_plan", None)
            if plan_key is not None:
                try:
                    session.record[skey] = [plan_key, text_key(real_transpile(sql, write="duckdb")[0])]
                except Exception:
                    pass
            return out

        def mp_execute(expression, meta):
            if session.gave_up:
                return None  # the remaining entries would only cost time
            return real_mp(expression, meta)

        real_mp = getattr(self.module, "mp_execute", None)
        if real_mp is not None and self.strict:
            # the pool ships the function by name: keep the name it is found under
            mp_execute.__module__ = real_mp.__module__
            mp_execute.__qualname__ = real_mp.__qualname__
            mp_execute.__name__ = real_mp.__name__
            self._patch(self.module, "mp_execute", mp_execute)
        if self.strict and self.whole and getattr(self.module, "ProcessPoolExecutor", None) is inlinepool.InlinePool:
            # look at each entry as soon as its quick half is done: a slow half that fails
            # then ends the test before the other entries have cost anything
            # (a session with an allowance wants every quick half first, to weigh what is new)
            self._patch(self.module, "ProcessPoolExecutor", inlinepool.LazyPool)
        self._patch(py.PythonExecutor, "execute", execute)
        self._patch(self.cls, "subtestHelper", helper)
        return self

    def __exit__(self, *exc):
        for owner, name, value in reversed(self._saved):
            setattr(owner, name, value)
        self._saved = []
        self.pending.clear()
        return False
