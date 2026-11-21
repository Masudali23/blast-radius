"""Turn one patch into a plan: which tests could possibly be affected, and how to check them."""
import os
import re
import sys

from . import analyze, boolrel, infect, patch, relations

# Files that configure the test run itself.  We cannot model those, so such a commit runs everything.
# how far to narrow a class-attribute change: "off", "class" (instances of the class) or
# "family" (instances of any class of the same dialect)
REFINE_CLASS = os.environ.get("BR_REFINE_CLASS", "family")

# a comparison operator swapped for its partner (is/==, </<=, ...) is in play only for the
# tests in which the two ever give different answers
REFINE_RELATIONS = os.environ.get("BR_REFINE_RELATIONS", "1") == "1"
# an edited literal default value of a function is in play only for tests that call the function
REFINE_DEFAULTS = os.environ.get("BR_REFINE_DEFAULTS", "1") == "1"
# narrow an edited class-level constant to the tests that read it from that class
REFINE_ATTRS = os.environ.get("BR_REFINE_ATTRS", "1") == "1"
# narrow an edited class-level dict entry to the tests that look that entry up
REFINE_KEYS = os.environ.get("BR_REFINE_KEYS", "1") == "1"
# use the index of real reads of a name (rather than any textual mention) to find its users
USE_READS = os.environ.get("BR_USE_READS", "1") == "1"

# instrument a single changed expression to compare old and new values while tests run
# (diagnostic only: agreement in the tests that ran says nothing about the ones that did not)
PROBE_EXPR = os.environ.get("BR_PROBE_EXPR", "0") == "1"

RUNNER_CONFIG = ("conftest.py", "pytest.ini", "tox.ini", "sitecustomize.py", "usercustomize.py")


class Plan:
    __slots__ = ("commit", "unknown", "cand", "sure", "fresh", "files", "swaps", "notes", "syntax", "data_only", "kinds", "parts", "probe", "imports", "wide")

    def __init__(self, commit):
        self.commit = commit
        self.unknown = False  # cannot reason about it: select every test
        self.cand = 0  # bitmask over map test indices: tests that may be affected
        self.sure = 0  # tests we consider failing without running them
        self.fresh = False  # needs a fresh import (module / class level change)
        self.files = {}  # rel path -> new bytes (None = deleted)
        self.swaps = []  # python files whose functions can be hot-swapped
        self.notes = []
        self.syntax = False
        self.data_only = True
        self.kinds = set()
        # why each test is a candidate: (kind, path, lo, hi, mask) with kind in func / mod / data
        self.parts = []
        self.probe = []  # python files whose single changed expression can be evaluated both ways
        # modules whose candidates were narrowed although the changed statement could raise
        # while importing; if one does not import any more, every test in `wide` is in play
        self.imports = []
        self.wide = 0

    def add(self, kind, rel, lo, hi, mask):
        if mask:
            self.cand |= mask
            self.parts.append((kind, rel, lo, hi, mask))


def _read(base, rel):
    try:
        with open(os.path.join(base, rel), "rb") as fh:
            return fh.read()
    except OSError:
        return None


def _decode(data):
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


_IDENT = {}


def _ident_lines(base, name, maps):
    """Lines (per file) in sqlglot/ and tests/ that mention the identifier `name`."""
    cache = _IDENT.get(name)
    if cache is not None:
        return cache
    pat = re.compile(r"\b%s\b" % re.escape(name))
    res = {}
    for rel in maps.files:
        if not rel.endswith(".py"):
            continue
        data = _read(base, rel)
        if data is None or name.encode() not in data:
            continue
        text = data.decode("utf-8", "replace")
        hits = [i for i, line in enumerate(text.split("\n"), 1) if pat.search(line)]
        if hits:
            res[rel] = hits
    _IDENT[name] = res
    return res


def make_plan(commit, data, base, maps, opts=None):
    opts = opts or {}
    plan = Plan(commit)
    try:
        changes = patch.parse(data)
    except Exception as e:  # unreadable patch
        plan.unknown = True
        plan.notes.append("patch parse: %r" % (e,))
        return plan
    if not changes and data.strip():
        plan.unknown = True
        plan.notes.append("no file sections found")
        return plan

    for ch in changes:
        try:
            _one_file(plan, ch, base, maps, opts)
        except Exception as e:
            plan.unknown = True
            plan.notes.append("analysis error on %s: %r" % (ch.path, e))
    return plan


def _one_file(plan, ch, base, maps, opts):
    rel = ch.path
    if rel is None:
        plan.unknown = True
        plan.notes.append("change without a path")
        return
    name = os.path.basename(rel)
    if ch.mode_only:
        return
    if ch.binary:
        # we cannot build the new content; every test reading the file is taken as failing
        plan.kinds.add("binary")
        plan.sure |= maps.opened.get(rel, 0)
        if rel.endswith(".py"):
            plan.unknown = True
        return
    if ch.renamed and ch.old_path != ch.new_path:
        old = _read(base, ch.old_path)
        if old is None:
            plan.unknown = True
            return
        new = patch.apply(old, ch) if ch.hunks else old
        # model as: old path deleted, new path created
        _changed(plan, ch.old_path, old, None, base, maps, opts)
        _changed(plan, ch.new_path, _read(base, ch.new_path), new, base, maps, opts)
        return
    old = _read(base, ch.old_path) if ch.old_path else None
    if ch.new_file and old is None:
        new = patch.apply(None, ch)
        _changed(plan, rel, None, new, base, maps, opts)
        return
    if old is None:
        plan.unknown = True
        plan.notes.append("patch touches a file we do not have: %s" % rel)
        return
    new = patch.apply(old, ch)
    if new == old:
        return
    _changed(plan, rel, old, new, base, maps, opts)


def _changed(plan, rel, old, new, base, maps, opts):
    name = os.path.basename(rel)
    if rel in plan.files:
        plan.unknown = True
        plan.notes.append("file patched twice: %s" % rel)
        return
    plan.files[rel] = new
    if name in RUNNER_CONFIG:
        plan.unknown = True
        plan.notes.append("test-runner configuration: %s" % rel)
        return
    readers = maps.opened.get(rel, 0)
    if rel in maps.import_files:
        plan.fresh = True  # the warm worker already consumed the old content
    if not rel.endswith(".py"):
        plan.kinds.add("data")
        plan.add("data", rel, 0, 0, readers)
        return
    plan.data_only = False
    if old is None:
        _new_module(plan, rel, new, maps)
        return
    if new is None:
        # deleted module: everything that imported or read it is in play, on a fresh import
        plan.kinds.add("deleted")
        plan.fresh = True
        plan.add("mod", rel, 0, 0, maps.imports.get(rel, 0) | readers | maps.any_line(rel))
        return
    if not maps.known(rel) and not readers and not maps.imports.get(rel, 0):
        plan.kinds.add("unused-py")
        return  # a python file no test imports, runs or reads
    old_t, new_t = _decode(old), _decode(new)
    if old_t is None or new_t is None:
        plan.kinds.add("syntax")
        plan.fresh = True
        plan.syntax = True
        plan.add("mod", rel, 0, 0, maps.imports.get(rel, 0) | readers | maps.any_line(rel))
        return
    status, changes = analyze.diff_python(old_t, new_t)
    plan.add("data", rel, 0, 0, readers)  # tests that read this source file as data see any textual change
    if status in ("same", "doc"):
        plan.kinds.add("comment")
        return
    if status == "syntax":
        plan.kinds.add("syntax")
        plan.fresh = True
        plan.syntax = True
        plan.add("mod", rel, 0, 0, maps.imports.get(rel, 0) | maps.any_line(rel))
        return
    shapes_ok = analyze.code_shapes(old_t) == analyze.code_shapes(new_t)
    hot = True
    swap = regroup = operand = None
    if REFINE_RELATIONS and changes and all(c.level == "func" for c in changes) and analyze.TREES:
        # (each of these establishes by itself that the two modules differ in nothing else)
        swap = relations.swapped(analyze.TREES[0], analyze.TREES[1])
        if swap is None:
            regroup = boolrel.swapped(analyze.TREES[0], analyze.TREES[1])
        if swap is None and regroup is None:
            operand = boolrel.one_operand(analyze.TREES[0], analyze.TREES[1])
    for c in changes:
        if c.level == "func":
            plan.kinds.add("func")
            lo, hi = c.lines
            if not shapes_ok or c.coarse:
                lo, hi = c.scope_span
            slo, shi = c.scope_span
            if swap:
                differ, warm = maps.relation(rel, swap)
                if differ is not None and not warm:
                    # Every evaluation of this comparison outside these tests (and while the
                    # worker loaded) gives what it gave before: nothing else can change.
                    mask = maps.func(rel, lo, hi)
                    plan.notes.append("relation-narrowed %s %s: %d -> %d" % (rel, swap, bin(mask).count("1"), bin(mask & differ).count("1")))
                    plan.add("func", rel, lo, hi, mask & differ)
                    if maps.ran_at_import(rel, slo, shi):
                        # nothing computed while loading differs, but function objects made
                        # then (closures, lambdas) still hold the old code: import afresh
                        hot = False
                    continue
            if regroup:
                narrowed = _regrouped(maps, rel, regroup, analyze.TREES[0])
                if narrowed is not None:
                    mask = maps.func(rel, lo, hi)
                    plan.notes.append("formula-narrowed %s %s: %d -> %d" % (rel, boolrel.site_key(regroup[0]), bin(mask).count("1"), bin(mask & narrowed).count("1")))
                    plan.add("func", rel, lo, hi, mask & narrowed)
                    if maps.ran_at_import(rel, slo, shi):
                        hot = False  # as above: objects made while loading hold the old code
                    continue
            if operand:
                asked = _asked(maps, rel, operand)
                if asked is not None:
                    # the edit sits inside one operand of a condition: a test in which the
                    # condition never got as far as that operand runs as before
                    mask = maps.func(rel, lo, hi)
                    plan.notes.append("operand-narrowed %s %s[%d]: %d -> %d" % (rel, boolrel.site_key(operand[0]), operand[2], bin(mask).count("1"), bin(mask & asked).count("1")))
                    plan.add("func", rel, lo, hi, mask & asked)
                    if maps.ran_at_import(rel, slo, shi):
                        hot = False
                    continue
            plan.add("func", rel, lo, hi, maps.func(rel, lo, hi))
            if maps.ran_at_import(rel, slo, shi):
                hot = False
                # The function ran while modules were loading, so objects made then hold its
                # old code: import afresh.  What was BUILT then can only differ if the changed
                # statement itself ran then; otherwise the tests that reach the statement are
                # the only ones in play.
                if c.coarse or not shapes_ok or maps.ran_at_import(rel, lo, hi):
                    plan.add("mod", rel, slo, shi, maps.func(rel, slo, shi))
        else:
            plan.kinds.add("mod")
            hot = False
            lo, hi = c.lines
            if c.coarse and hi >= 10**9:
                mask = maps.imports.get(rel, 0) | maps.any_line(rel)
            else:
                mask = maps.mod(rel, lo, hi)
                if not mask and not maps.known(rel):
                    # imported, but nothing in it was ever traced (an empty __init__.py, say)
                    mask = maps.imports.get(rel, 0)
            everyone = mask
            if mask and c.calls and REFINE_DEFAULTS:
                flo, fhi = c.calls
                if not maps.ran_at_import(rel, flo, fhi):
                    # nothing called the function while modules were loading, so the default is
                    # first used by a test that calls it
                    callers = maps.func(rel, flo, fhi)
                    plan.notes.append("default-narrowed %s:%d: %d -> %d" % (rel, flo, bin(mask).count("1"), bin(callers).count("1")))
                    plan.add("mod", rel, lo, hi, callers)
                    continue
            if mask and c.names and not c.coarse and c.cls and len(c.names) == 1 and REFINE_ATTRS:
                readers = maps.attr_readers(["%s:%d" % (rel, line) for line in c.cls_lines], c.names[0])
                if readers is not None:
                    # every read of this constant from this class's definition was observed
                    plan.notes.append("attr-narrowed %s.%s: %d -> %d" % (c.cls, c.names[0], bin(mask).count("1"), bin(mask & readers).count("1")))
                    plan.add("mod", rel, lo, hi, mask & readers)
                    _import_guard(plan, rel, c, everyone, mask & readers)
                    continue
            if mask and c.names and not c.coarse and opts.get("refine_names", True):
                mask &= _users(c.names, base, maps, rel, c.lines)
                if mask and c.cls and REFINE_CLASS != "off":
                    cu = _class_users(maps, base, rel, c)
                    if cu is not None:
                        plan.notes.append("class-narrowed %s.%s: %d -> %d" % (c.cls, c.names[0], bin(mask).count("1"), bin(mask & cu).count("1")))
                        mask &= cu
                if mask and c.cls and REFINE_KEYS and len(c.names) == 1:
                    ku = _key_users(maps, c, rel)
                    if ku is not None:
                        plan.notes.append("key-narrowed %s.%s: %d -> %d" % (c.cls, c.names[0], bin(mask).count("1"), bin(mask & ku).count("1")))
                        mask &= ku
            plan.add("mod", rel, lo, hi, mask)
            _import_guard(plan, rel, c, everyone, mask)
    if hot:
        plan.swaps.append(rel)
        if PROBE_EXPR and len(changes) == 1 and not changes[0].coarse and shapes_ok and analyze.TREES:
            if infect.applicable(analyze.TREES[0], analyze.TREES[1]):
                plan.probe.append(rel)
    else:
        plan.fresh = True


def _regrouped(maps, rel, regroup, old_tree):
    """Tests in which the regrouped condition can behave differently, from its recorded
    evaluations; None when nothing can be said."""
    node, old, new, impure = regroup
    seen = maps.formula(rel, boolrel.site_key(node))
    if seen is None:
        return None
    truth_only = None
    for n, flag in boolrel.formulas(old_tree):
        if n is node:
            truth_only = flag
    if truth_only is None:
        return None
    vectors, keep, warm = seen
    if any(boolrel.differs(old, new, kind, vec, impure, truth_only) for kind, vec in warm):
        return None  # already behaved differently while the worker loaded
    mask = keep
    for (kind, vec), tests in vectors.items():
        if boolrel.differs(old, new, kind, vec, impure, truth_only):
            mask |= tests
    return mask


def _asked(maps, rel, operand):
    """Tests in which the condition ever evaluated the edited operand; None when unknown."""
    node, struct, index = operand
    seen = maps.formula(rel, boolrel.site_key(node))
    if seen is None:
        return None
    vectors, keep, warm = seen
    if any(boolrel.asks_for(struct, kind, vec, index) for kind, vec in warm):
        return None  # evaluated while the worker loaded: its old value is baked in
    mask = keep
    for (kind, vec), tests in vectors.items():
        if boolrel.asks_for(struct, kind, vec, index):
            mask |= tests
    return mask


def _import_guard(plan, rel, change, everyone, narrowed):
    """Narrowing assumes the module still imports.  Unless the edit plainly cannot raise, have
    the import checked; should it fail, everyone who runs that statement is in play."""
    if narrowed == everyone or analyze.harmless_at_import(change):
        return
    if rel not in plan.imports:
        plan.imports.append(rel)
    plan.wide |= everyone


FAMILY_DIRS = ("sqlglot/dialects/", "sqlglot/parsers/", "sqlglot/generators/", "sqlglot/typing/")


def _key_users(maps, c, rel):
    """Tests that look up one of the dict entries a class-level `NAME = {...}` edit touches.

    Entries are found by name and key in any class's dict of that name, so copies inherited by
    subclasses count.  None (no narrowing) when the dict is read as a whole or by one of these
    keys while classes are being built: something else may then have been derived from it.
    """
    name = c.names[0]
    if name not in maps.dict_names:
        return None
    res = analyze.changed_dict_keys(c)
    if res is None:
        return None
    keys, old_keys = res[0], res[1]
    if not keys:
        return None
    known = set(maps.dict_all_keys.get(name, ()))
    cls_keys = ["%s:%d" % (rel, line) for line in c.cls_lines]
    for k in keys:
        if maps.derived(name, k, cls_keys):
            return None
        if k in old_keys and k not in known:
            return None  # the key as written does not match what the dict really holds
    if keys <= old_keys and res[2]:
        # Same keys, different values, and every edited entry holds a callable: only tests that
        # invoke one of those callables can tell.  (This follows copies of the dict, too.)
        mask = 0
        for k in keys:
            callers = maps.dict_callers(name, k)
            if callers is None:
                mask = None
                break
            mask |= callers
        if mask is not None:
            return mask
    if maps.derived(name, "*", cls_keys):
        return None
    mask = maps.dict_users(name, "*")
    for k in keys:
        mask |= maps.dict_users(name, k)
    return mask


def _class_users(maps, base, rel, c):
    """Tests that can observe a class attribute of class c.cls: those that create an instance of
    the class (or a subclass), plus those running a line that names `Class.ATTR` outright.

    A dialect's tokenizer, parser and generator classes feed each other when the dialect class
    is built, so for those the whole dialect family counts.  None when we cannot tell.
    """
    key = None
    for line in c.cls_lines:
        k = "%s:%d" % (rel, line)
        if k in maps.tracked:
            key = k
            break
    if key is None:
        return None
    users = maps.class_users.get(key, 0)
    for d in FAMILY_DIRS:
        if rel.startswith(d) and REFINE_CLASS == "family":
            stem = rel[len(d):]
            prefixes = tuple(fd + stem + ":" for fd in FAMILY_DIRS)
            for k, m in maps.class_users.items():
                if k.startswith(prefixes):
                    users |= m
            break
    for name in c.names:
        pat = re.compile(r"\b%s\s*\.\s*%s\b" % (re.escape(c.cls), re.escape(name)))
        for f, lines in _ident_lines(base, name, maps).items():
            data = _read(base, f)
            if data is None:
                continue
            text = data.decode("utf-8", "replace").split("\n")
            for line in lines:
                if pat.search(text[line - 1]):
                    users |= maps.func(f, line, line) | maps.mod(f, line, line)
    return users


def _users(names, base, maps, rel, own_lines):
    """Tests that execute some line mentioning one of `names` (outside the assignment itself)."""
    mask = 0
    for name in names:
        if name.startswith("__") and name.endswith("__"):
            return maps.all
        where = maps.reads(name) if USE_READS else None
        if where is None:
            where = _ident_lines(base, name, maps)  # not indexed: any line mentioning the name
        for f, lines in where.items():
            for line in lines:
                if f == rel and own_lines[0] <= line <= own_lines[1]:
                    continue
                mask |= maps.func(f, line, line) | maps.mod(f, line, line)
    return mask


def _new_module(plan, rel, new, maps):
    name = os.path.basename(rel)
    plan.kinds.add("new-py")
    top = rel.split("/")[0]
    stem = name[:-3]
    if "/" not in rel:
        # a new top-level module can shadow an installed one for every test
        if stem in sys.stdlib_module_names or stem in ("duckdb", "pandas", "numpy", "pytz", "dateutil", "setuptools", "pytest", "sqlglot", "tests"):
            plan.unknown = True
            plan.notes.append("new top-level module shadows %s" % stem)
        return
    d = os.path.dirname(rel)
    # tests that read sibling files as data usually enumerate the directory
    for f, mask in maps.opened.items():
        if os.path.dirname(f) == d:
            plan.add("mod", rel, 0, 0, mask)
    if name == "__init__.py":
        plan.fresh = True
        for f in maps.files:
            if f.startswith(d + "/"):
                plan.add("mod", rel, 0, 0, maps.imports.get(f, 0) | maps.any_line(f))
    if plan.cand:
        plan.fresh = True
