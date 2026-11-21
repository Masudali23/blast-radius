"""The executor tests' TPC-H / TPC-DS tables, parsed once when the image is built.

`TestExecutor.setUpClass` turns a few dozen gzip'd CSV fixtures into lists of Python tuples.
That is over a second of CPU each time a worker process needs the class, and the outcome never
changes: the rows depend only on the fixture files and on the set-up code in the test module.
`tools/build_tables.py` runs the real set-up at the pinned commit and stores the rows.  Here the
set-up runs as written, except that the CSV reader stops after the header line; each table is
then built again by the same constructor call from the stored rows.

Not used when the patch touches anything the rows depend on (DEPENDS).
"""
import csv
import os
import pickle

OWNER = ("tests.test_executor", "TestExecutor")
ATTRS = ("tpch_tables", "tpcds_tables")
# files (or directory prefixes) that decide what the rows are
DEPENDS = ("tests/test_executor.py", "tests/helpers.py", "tests/fixtures/optimizer/tpc-h/", "tests/fixtures/optimizer/tpc-ds/")
PATH = os.environ.get("BR_TABLES", "/app/tables.pkl")
_cache = [None, False]


class Mismatch(Exception):
    """The stored rows do not fit what the set-up produced: use the ordinary set-up."""


def load():
    """The stored rows ({attribute: {table: (columns, rows)}}), or None when there are none."""
    if not _cache[1]:
        _cache[1] = True
        try:
            with open(PATH, "rb") as fh:
                _cache[0] = pickle.load(fh)
        except Exception:
            _cache[0] = None
    return _cache[0]


def usable(cls, patched=()):
    if (cls.__module__, cls.__name__) != OWNER or load() is None:
        return False
    return not any(rel.startswith(DEPENDS) for rel in patched)


def dump(cls):
    return {attr: {name: (tuple(t.columns), list(t.rows)) for name, t in getattr(cls, attr).items()} for attr in ATTRS}


def setup(cls, stored=None):
    """Run cls.setUpClass() with the stored rows in place of parsing the CSV files."""
    stored = stored if stored is not None else load()
    real = csv.reader

    def header_only(source, *args, **kwargs):
        reader = real(source, *args, **kwargs)
        return iter([next(reader)])

    csv.reader = header_only
    try:
        cls.setUpClass()
    finally:
        csv.reader = real
    for attr in ATTRS:
        tables = getattr(cls, attr, None)
        rows_of = stored.get(attr)
        if not isinstance(tables, dict) or rows_of is None or set(tables) != set(rows_of):
            raise Mismatch(attr)
        for name in list(tables):
            columns, rows = rows_of[name]
            table = tables[name]
            if tuple(table.columns) != tuple(columns) or len(table.rows):
                raise Mismatch(name)
            # the same call the set-up makes, now with the rows
            tables[name] = type(table)(columns=table.columns, rows=rows)
