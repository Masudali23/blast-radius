"""Cheaper but equivalent ways to run a slow test.

Slicing: many sqlglot tests loop over the entries of a fixture file.  When a change can only
reach some entries (known from per-entry coverage), the same test method is run with the
fixture loader yielding just those entries.  The test's own code is untouched.

Scaling: two tests are slow only because of a size constant (a million parsed nodes, eleven
hundred generator instances per dialect).  They are re-run with the constant reduced; every
assertion and code path stays the same.
"""
import ast
import types

# class attributes that hold a fixture file loaded once in setUpClass
CLASS_LISTS = {
    "tpch_sqls": "optimizer/tpc-h/tpc-h.sql",
    "tpcds_sqls": "optimizer/tpc-ds/tpc-ds.sql",
}
LOADERS = ("load_sql_fixtures", "load_sql_fixture_pairs")

# test id -> ({int constant: replacement}, {str constant: replacement})
SCALED = {
    "tests/test_parser.py::TestParser::test_max_nodes": (
        {10000: 100, 1000000: 10000, 50000: 500},
        {"(50000)": "(500)"},
    ),
    "tests/test_generator.py::TestGenerator::test_generator_memory": (
        {100: 10, 1000: 100},
        {},
    ),
}


class _State:
    active = None  # None: loaders pass everything through; else the set of allowed keys
    record = None  # a list collecting every key seen (tracing only)
    changed = None  # fixture files edited by the patch: yield only their entries that differ
    base_fixtures = None  # the pinned checkout's tests/fixtures directory


SL = _State()


def key(filename, index):
    return "%s#%d" % (filename, index)


def _pinned_entries(fn, filename):
    """What the loader `fn` yields for the pinned (unpatched) version of a fixture file."""
    import sys

    helpers = sys.modules.get(getattr(fn, "__module__", None) or "tests.helpers")
    current = helpers.FIXTURES_DIR
    helpers.FIXTURES_DIR = SL.base_fixtures
    try:
        return list(fn(filename))
    finally:
        helpers.FIXTURES_DIR = current


def changed_entries(fn, filename, new_items):
    """The entries of an edited fixture file that differ from the pinned version."""
    try:
        old_items = _pinned_entries(fn, filename)
    except Exception:
        return new_items
    if len(old_items) != len(new_items):
        return new_items
    return [n for o, n in zip(old_items, new_items) if o != n]


def _wrap(fn):
    def wrapped(filename, *args, **kwargs):
        allow, rec = SL.active, SL.record
        if SL.changed is not None and filename in SL.changed:
            # an edited file: its entries that differ (all of it when it is read in a way
            # that cannot be compared entry by entry)
            if not args and not kwargs:
                yield from changed_entries(fn, filename, list(fn(filename)))
            else:
                yield from fn(filename, *args, **kwargs)
            return
        if allow is None and rec is None:
            yield from fn(filename, *args, **kwargs)
            return
        for i, item in enumerate(fn(filename, *args, **kwargs)):
            k = key(filename, i)
            if rec is not None:
                rec.append(k)
            if allow is None or k in allow:
                yield item

    wrapped._br_wrapped = True
    return wrapped


def wrap_module(module):
    """Route a test module's fixture loaders through the slicer (idempotent)."""
    for name in LOADERS:
        fn = getattr(module, name, None)
        if fn is not None and not getattr(fn, "_br_wrapped", False):
            setattr(module, name, _wrap(fn))


class RecList(list):
    """A class-level fixture list that reports which entries a test walks over."""

    def __init__(self, items, filename, rec):
        super().__init__(items)
        self._fn = filename
        self._rec = rec

    def __iter__(self):
        for i in range(len(self)):
            self._rec.append(key(self._fn, i))
            yield list.__getitem__(self, i)

    def __getitem__(self, i):
        if isinstance(i, int):
            self._rec.append(key(self._fn, i if i >= 0 else len(self) + i))
        return list.__getitem__(self, i)


def apply_slices(test, cls, allow, changed=None):
    """Restrict class-level fixture lists on this test instance.  Call before test.run()."""
    for attr, filename in CLASS_LISTS.items():
        full = cls.__dict__.get(attr)
        if isinstance(full, list):
            if changed is not None:
                if filename in changed:
                    import sys

                    loader = sys.modules["tests.helpers"].load_sql_fixture_pairs
                    setattr(test, attr, changed_entries(loader, filename, list(full)))
            elif allow is None:
                setattr(test, attr, RecList(full, filename, SL.record))
            else:
                setattr(test, attr, [x for i, x in enumerate(full) if key(filename, i) in allow])


class _Rescale(ast.NodeTransformer):
    def __init__(self, ints, strs):
        self.ints = ints
        self.strs = strs
        self.hits = 0

    def visit_Constant(self, node):
        v = node.value
        if type(v) is int and v in self.ints:
            self.hits += 1
            return ast.copy_location(ast.Constant(value=self.ints[v]), node)
        if type(v) is str and v in self.strs:
            self.hits += 1
            return ast.copy_location(ast.Constant(value=self.strs[v]), node)
        return node


def find_method(tree, cls_name, meth):
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == cls_name:
            for sub in node.body:
                if isinstance(sub, ast.FunctionDef) and sub.name == meth:
                    return sub
    return None


def scaled_method(test, module, cls_name, meth, test_id):
    """A bound method running the scaled-down body of `meth`, or None when that is not possible."""
    rules = SCALED.get(test_id)
    if rules is None:
        return None
    try:
        with open(module.__file__, "rb") as fh:
            tree = ast.parse(fh.read())
    except Exception:
        return None
    fn = find_method(tree, cls_name, meth)
    if fn is None or fn.decorator_list:
        return None
    tr = _Rescale(*rules)
    fn = tr.visit(fn)
    if not tr.hits:
        return None
    mod = ast.Module(body=[fn], type_ignores=[])
    ast.fix_missing_locations(mod)
    ns = {}
    try:
        exec(compile(mod, module.__file__, "exec"), module.__dict__, ns)
    except Exception:
        return None
    return types.MethodType(ns[meth], test)


def method_fingerprint(source, cls_name, meth):
    """A stable description of a test method's code, to notice when a patch edits the test itself."""
    try:
        fn = find_method(ast.parse(source), cls_name, meth)
    except Exception:
        return None
    return ast.dump(fn) if fn is not None else None
