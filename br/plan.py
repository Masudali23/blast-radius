"""Turn one patch into a plan: which tests could possibly be affected, and how to check them."""
import ast
import importlib.util
import io
import os
import re
import sys
import tokenize

from . import analyze, boolrel, infect, lean, patch, relations

# Files that configure the test run itself.  We cannot model those, so such a commit runs everything.
# how far to narrow a class-attribute change: "off", "class" (instances of the class) or
# "family" (instances of any class of the same dialect)
REFINE_CLASS = os.environ.get("BR_REFINE_CLASS", "family")

# a comparison operator swapped for its partner (is/==, </<=, ...) is in play only for the
# tests in which the two ever give different answers
REFINE_RELATIONS = os.environ.get("BR_REFINE_RELATIONS", "1") == "1"
# an edited literal default value of a function is in play only for tests that call the function
REFINE_DEFAULTS = os.environ.get("BR_REFINE_DEFAULTS", "1") == "1"
# a def whose decorators or name alone changed: the tests that enter the function (see _plan)
REFINE_HEADERS = os.environ.get("BR_REFINE_HEADERS", "1") == "1"
# narrow an edited class-level constant to the tests that read it from that class
REFINE_ATTRS = os.environ.get("BR_REFINE_ATTRS", "1") == "1"
# hand a changed literal class constant to the warm worker instead of importing afresh
HOT_CONSTS = os.environ.get("BR_HOT_CONSTS", "1") == "1"
# narrow an edited class-level dict entry to the tests that look that entry up
REFINE_KEYS = os.environ.get("BR_REFINE_KEYS", "1") == "1"
# use the index of real reads of a name (rather than any textual mention) to find its users
USE_READS = os.environ.get("BR_USE_READS", "1") == "1"

# instrument a single changed expression to compare old and new values while tests run
# (diagnostic only: agreement in the tests that ran says nothing about the ones that did not)
PROBE_EXPR = os.environ.get("BR_PROBE_EXPR", "0") == "1"

RUNNER_CONFIG = ("conftest.py", "pytest.ini", "tox.ini", "sitecustomize.py", "usercustomize.py")
# Fixture files that the warm worker has read, but only inside the executor tests' class set-up
# (run there before forking to save its cost): no module depends on them, so a child of the
# warm worker that runs the set-up again on the patched file is where a fresh import would be.
SETUP_FILES = ("tests/fixtures/optimizer/tpc-h/tpc-h.sql", "tests/fixtures/optimizer/tpc-ds/tpc-ds.sql")
HOT_SETUP = os.environ.get("BR_HOT_SETUP", "1") == "1"


class Plan:
    __slots__ = ("commit", "unknown", "cand", "sure", "fresh", "files", "swaps", "notes", "syntax", "data_only", "kinds", "parts", "probe", "imports", "wide", "code_files", "partner_swaps", "entry", "consts", "uses", "lean", "redo_setup")

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
        self.code_files = 0  # python files whose code (not just comments) changes
        self.partner_swaps = 0  # changes narrowed as "operator swapped for its partner"
        # (path, lo, hi) of a narrowed function change -> f(test id): the fixture entries of a
        # sliced test the change can reach (bit mask), or None when that is not known
        self.entry = {}
        # class constants the warm worker can simply be given: [path, [class, ...], NAME,
        # repr(old), repr(new)]
        self.consts = []
        # (path, lo, hi) of a narrowed class-level change -> what code must do to notice it: a
        # list of conditions that must all hold, each a list of alternatives (kind, what) with
        # kind in classes / attr_reads / dict_keys / dict_calls / pairs [(file, line)] /
        # range [file, lo, hi].  (For asking whether one half of a test can notice it.)
        self.uses = {}
        # hot-swapped files whose change was read off reduced texts (br/lean.py): the worker
        # compiles the same two texts
        self.lean = []
        self.redo_setup = False  # class set-ups run before the fork must run again (SETUP_FILES)

    def add(self, kind, rel, lo, hi, mask):
        if mask:
            self.cand |= mask
            self.parts.append((kind, rel, lo, hi, mask))


# Project files that may carry pytest settings in a section of their own.
PROJECT_FILES = ("pyproject.toml", "setup.cfg")
_PYTEST_SECTION = re.compile(rb"^\[\s*(tool\.pytest[^\]]*|tool:pytest|pytest)\s*\]\s*$")


def _pytest_settings(data):
    """The lines of every pytest section of a project file (None when the file is absent)."""
    if data is None:
        return None
    out, inside = [], False
    for line in data.split(b"\n"):
        stripped = line.strip()
        if stripped.startswith(b"["):
            inside = bool(_PYTEST_SECTION.match(stripped))
        if inside and stripped:
            out.append(stripped)
    return out


_PINNED = {}


def _read(base, rel):
    """A file of the pinned checkout (which nothing writes to), read once."""
    key = (base, rel)
    if key not in _PINNED:
        try:
            with open(os.path.join(base, rel), "rb") as fh:
                _PINNED[key] = fh.read()
        except OSError:
            _PINNED[key] = None
    return _PINNED[key]


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
    plain = bool(name) and (name[0] == "_" or name[0].isalnum()) and (name[-1] == "_" or name[-1].isalnum())
    res = {}
    for rel in maps.files:
        if not rel.endswith(".py"):
            continue
        data = _read(base, rel)
        if data is None or name.encode() not in data:
            continue
        text = data.decode("utf-8", "replace")
        # The lines holding the name as a whole word.  For an identifier that is every place
        # the text has it with no word character right before or after: found by plain search
        # (the pattern has to be tried at every character).
        hits = []
        line, at = 1, 0
        if plain:
            size, end_of_text = len(name), len(text)
            start = text.find(name)
            while start >= 0:
                end = start + size
                before = text[start - 1] if start else " "
                after = text[end] if end < end_of_text else " "
                if not (before == "_" or before.isalnum() or after == "_" or after.isalnum()):
                    line += text.count("\n", at, start)
                    at = start
                    if not hits or hits[-1] != line:
                        hits.append(line)
                start = text.find(name, start + 1)
        else:
            for found in pat.finditer(text):
                line += text.count("\n", at, found.start())
                at = found.start()
                if not hits or hits[-1] != line:
                    hits.append(line)
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
    if plan.partner_swaps and plan.code_files > 1 and opts.get("relations", True):
        # The other operator was evaluated, while tracing, by the pinned code.  If the patch
        # changes code elsewhere too, that evaluation may run the changed code: plan again
        # without this narrowing.
        return make_plan(commit, data, base, maps, dict(opts, relations=False))
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
        if HOT_SETUP and rel in SETUP_FILES:
            # read by a class set-up alone: a child of the warm worker runs that set-up again
            plan.redo_setup = True
        else:
            plan.fresh = True  # the warm worker already consumed the old content
    if not rel.endswith(".py"):
        plan.kinds.add("data")
        plan.add("data", rel, 0, 0, readers)
        if name in PROJECT_FILES and _pytest_settings(old) != _pytest_settings(new):
            plan.unknown = True
            plan.notes.append("pytest settings in %s" % rel)
        return
    plan.data_only = False
    if old is None:
        _new_module(plan, rel, new, maps, base)
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
    small = lean.reduce(rel, old, new)
    reduced = False
    if small is not None:
        # every changed line sits in one function body: compare the two files with the bodies
        # of the other functions blanked.  That is the whole answer when it says "function
        # bodies differ and nothing else"; anything else is asked of the files themselves.
        small_old, small_new = _decode(small[0]), _decode(small[1])
        status, changes = analyze.diff_python(small_old, small_new)
        if status == "diff" and changes and all(c.level == "func" for c in changes):
            old_t, new_t = small_old, small_new
            reduced = True
    if not reduced:
        status, changes = analyze.diff_python(old_t, new_t)
    if status != "syntax" and new.split(b"\n", 2)[:2] != old.split(b"\n", 2)[:2]:
        # the first two lines may carry an encoding declaration: a "comment" that decides
        # whether the file can be read at all
        # ... and what its text says: under another encoding the same bytes are other characters.
        # Unless those all sit in comments and documentation, the file is handled like one
        # that cannot be read: every test that imports it.
        try:
            if ast.dump(ast.parse(new)) != ast.dump(ast.parse(new_t)):
                real = new.decode(tokenize.detect_encoding(io.BytesIO(new).readline)[0])
                if status == "diff" or analyze.diff_python(new_t, real)[0] not in ("same", "doc"):
                    status = "syntax"
        except (SyntaxError, ValueError, LookupError, RecursionError):
            status = "syntax"
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
    shapes = []  # worked out only when a function-level change asks

    def shapes_ok():
        if not shapes:
            shapes.append(analyze.shapes_same(old_t, new_t))
        return shapes[0]

    given = False  # the file's one change is a class constant handed to the warm worker
    plan.code_files += 1
    hot = True
    swap = regroup = operand = None
    if REFINE_RELATIONS and changes and all(c.level == "func" for c in changes) and analyze.TREES:
        # (each of these establishes by itself that the two modules differ in nothing else)
        same = analyze.SAME[0]
        swap = relations.swapped(analyze.TREES[0], analyze.TREES[1], same) if opts.get("relations", True) else None
        if swap is None:
            regroup = boolrel.swapped(analyze.TREES[0], analyze.TREES[1], same)
        if swap is None and regroup is None:
            operand = boolrel.one_operand(analyze.TREES[0], analyze.TREES[1], same)
    for c in changes:
        if c.level == "func":
            plan.kinds.add("func")
            lo, hi = c.lines
            if c.coarse or not shapes_ok():
                lo, hi = c.scope_span
            slo, shi = c.scope_span
            # Objects made while modules loaded (closures, lambdas stored in class dicts) hold
            # the old code of everything nested in them, so what matters is whether the
            # OUTERMOST function around the change ran then, not just the innermost scope.
            olo, ohi = _outermost(analyze.TREES[0], slo, shi) if analyze.TREES else (slo, shi)
            at_import = maps.ran_at_import(rel, olo, ohi)
            if swap:
                differ, warm = maps.relation(rel, swap)
                if differ is not None and not warm:
                    # Every evaluation of this comparison outside these tests (and while the
                    # worker loaded) gives what it gave before: nothing else can change.
                    mask = maps.func(rel, lo, hi)
                    plan.notes.append("relation-narrowed %s %s: %d -> %d" % (rel, swap, bin(mask).count("1"), bin(mask & differ).count("1")))
                    plan.add("func", rel, lo, hi, mask & differ)
                    plan.entry[(rel, lo, hi)] = _entries_swapped(maps, rel, swap)
                    plan.partner_swaps += 1
                    if at_import:
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
                    plan.entry[(rel, lo, hi)] = _entries_regrouped(maps, rel, regroup, analyze.TREES[0])
                    if at_import:
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
                    plan.entry[(rel, lo, hi)] = _entries_asked(maps, rel, operand)
                    if at_import:
                        hot = False
                    continue
            plan.add("func", rel, lo, hi, maps.func(rel, lo, hi))
            if at_import:
                hot = False
                # The function ran while modules were loading, so objects made then hold its
                # old code: import afresh.  What was BUILT then can only differ if the changed
                # statement itself ran then; otherwise the tests that reach the statement are
                # the only ones in play.
                if c.coarse or not shapes_ok() or maps.ran_at_import(rel, lo, hi):
                    plan.add("mod", rel, slo, shi, maps.func(rel, slo, shi))
        else:
            plan.kinds.add("mod")
            hot = False
            lo, hi = c.lines
            if c.header and c.header[0] in ("imported", "added") and not rel.startswith("sqlglot/"):
                # (the test runner reads test files and their set-up files by its own rules)
                plan.add("mod", rel, lo, hi, maps.imports.get(rel, 0) | maps.any_line(rel))
                continue
            if c.header and c.header[0] == "imported":
                # a new import of standard library modules: seen by nobody when this file does
                # not use the names it binds and no module takes all of this one's names
                everyone = maps.imports.get(rel, 0) | maps.any_line(rel)
                if REFINE_HEADERS and _names_free(base, maps, rel, c.header[1]):
                    plan.notes.append("header-narrowed %s:%d import: %d -> 0" % (rel, lo, bin(everyone).count("1")))
                    plan.add("mod", rel, lo, hi, 0)
                    _import_guard(plan, rel, c, everyone, 0)
                else:
                    plan.add("mod", rel, lo, hi, everyone)
                continue
            if c.header and c.header[0] == "added":
                # a new function next to the old ones: nobody can call it by name unless the
                # name is in use somewhere already; the module must still load (checked)
                everyone = maps.imports.get(rel, 0) | maps.any_line(rel)
                try:
                    unused = REFINE_HEADERS and not _ident_lines(base, c.header[1], maps)
                except Exception:
                    unused = False
                if unused:
                    plan.notes.append("header-narrowed %s:%d added: %d -> 0" % (rel, lo, bin(everyone).count("1")))
                    plan.add("mod", rel, lo, hi, 0)
                    _import_guard(plan, rel, c, everyone, 0)
                else:
                    plan.add("mod", rel, lo, hi, everyone)
                continue
            if c.coarse and hi >= 10**9:
                mask = maps.imports.get(rel, 0) | maps.any_line(rel)
            else:
                mask = maps.mod(rel, lo, hi)
                if not mask and not maps.known(rel):
                    # imported, but nothing in it was ever traced (an empty __init__.py, say)
                    mask = maps.imports.get(rel, 0)
            everyone = mask
            if mask and not c.coarse and maps.unobserved(rel, lo, hi, None if c.cls else c.names):
                # a typing-only definition: annotations that are never evaluated are all that
                # mention it, so only the import itself can go wrong
                plan.notes.append("unobserved %s:%d: %d -> 0" % (rel, lo, bin(mask).count("1")))
                plan.add("mod", rel, lo, hi, 0)
                _import_guard(plan, rel, c, everyone, 0)
                continue
            if c.names and any(n.startswith("__") for n in c.names):
                # __slots__, __all__, __init_subclass__ ...: read by Python while it builds the
                # class or module, with effects no reader index knows about
                plan.add("mod", rel, lo, hi, maps.imports.get(rel, 0) | maps.any_line(rel) | mask)
                continue
            if mask and c.calls and c.header and REFINE_HEADERS and rel.startswith("sqlglot/"):
                flo, fhi = c.calls
                if c.header in ("annotations", "stub"):
                    callers = 0
                else:
                    callers = None if maps.ran_at_import(rel, flo, fhi) else maps.func(rel, flo, fhi)
                if callers is not None and isinstance(c.header, tuple):
                    callers = _renamed_users(maps, base, rel, c, callers)
                if callers is not None:
                    # A function that only lost, gained or changed a decorator, or got another
                    # name: nothing called it while modules were loading, so a test that never
                    # entered it does not notice - unless the module no longer loads (checked),
                    # or something reads the old name while loading (those are counted in).
                    plan.notes.append("header-narrowed %s:%d %s: %d -> %d" % (rel, flo, c.header if isinstance(c.header, str) else "name", bin(mask).count("1"), bin(callers).count("1")))
                    plan.add("mod", rel, lo, hi, callers)
                    if c.header in ("decorators", "signature"):
                        # of a sliced test, the entries that enter the function (a renamed
                        # one can also be missed by a whole module: no such narrowing there)
                        plan.entry[(rel, lo, hi)] = (lambda r, a, b: lambda test_id: maps.entry_lines(test_id, r, a, b, loading=False))(rel, flo, fhi)
                        plan.uses[(rel, lo, hi)] = [[("range", [rel, flo, fhi])]]
                    _import_guard(plan, rel, c, everyone, callers)
                    continue
            if mask and c.calls and not c.header and REFINE_DEFAULTS:
                flo, fhi = c.calls
                if not maps.ran_at_import(rel, flo, fhi):
                    # nothing called the function while modules were loading, so the default is
                    # first used by a test that calls it
                    callers = maps.func(rel, flo, fhi)
                    plan.notes.append("default-narrowed %s:%d: %d -> %d" % (rel, flo, bin(mask).count("1"), bin(callers).count("1")))
                    plan.add("mod", rel, lo, hi, callers)
                    # of a sliced test, the entries that enter the function
                    plan.entry[(rel, lo, hi)] = (lambda r, a, b: lambda test_id: maps.entry_lines(test_id, r, a, b, loading=False))(rel, flo, fhi)
                    plan.uses[(rel, lo, hi)] = [[("range", [rel, flo, fhi])]]
                    continue
            if mask and c.names and not c.coarse and c.cls and len(c.names) == 1 and REFINE_ATTRS:
                readers = maps.attr_readers(["%s:%d" % (rel, line) for line in c.cls_lines], c.names[0])
                if readers is not None:
                    # every read of this constant from this class's definition was observed
                    plan.notes.append("attr-narrowed %s.%s: %d -> %d" % (c.cls, c.names[0], bin(mask).count("1"), bin(mask & readers).count("1")))
                    plan.add("mod", rel, lo, hi, mask & readers)
                    # of a sliced test, the entries that read the constant from this definition
                    plan.entry[(rel, lo, hi)] = (lambda items: lambda test_id: maps.entry_use(test_id, "attr_reads", items))(["%s:%d\t%s" % (rel, line, c.names[0]) for line in c.cls_lines])
                    plan.uses[(rel, lo, hi)] = [[("attr_reads", ["%s:%d\t%s" % (rel, line, c.names[0]) for line in c.cls_lines])]]
                    patch = _const_patch(c, rel) if HOT_CONSTS and len(changes) == 1 and analyze.harmless_at_import(c) else None
                    if patch is not None:
                        # Nothing read this constant while modules loaded (that is what made the
                        # narrowing possible), so a fresh import would differ from the warm
                        # worker in this one attribute only: set it there instead.
                        plan.consts.append(patch)
                        given = True
                    _import_guard(plan, rel, c, everyone, mask & readers)
                    continue
            factors = []  # the same narrowings, asked per fixture entry of a sliced test
            needs = []  # ... and as conditions on the code that could notice the change
            if mask and c.names and not c.coarse and opts.get("refine_names", True):
                mask &= _users(c.names, base, maps, rel, c.lines)
                mentions = _user_lines(c.names, base, maps, rel, c.lines)
                if mentions is not None:
                    factors.append((lambda pairs: lambda test_id: maps.entry_line_users(test_id, pairs))(mentions))
                    needs.append([("pairs", mentions)])
                if mask and c.cls and REFINE_CLASS != "off":
                    cu = _class_users(maps, base, rel, c)
                    if cu is not None:
                        plan.notes.append("class-narrowed %s.%s: %d -> %d" % (c.cls, c.names[0], bin(mask).count("1"), bin(mask & cu).count("1")))
                        mask &= cu
                        parts = _class_parts(maps, base, rel, c)
                        factors.append(_entries_class(maps, parts))
                        needs.append([("classes", parts[0]), ("pairs", parts[1])])
                        if HOT_CONSTS and len(changes) == 1 and c.names == ["arg_types"] and _arg_types_settled(maps, rel, c):
                            patch = _const_patch(c, rel)
                            if patch is not None:
                                # An argument table nobody looked at while modules loaded:
                                # the warm worker is given the new one, and works out again
                                # what the class hook derives from it.
                                plan.consts.append(patch)
                                given = True
                if mask and c.cls and REFINE_KEYS and len(c.names) == 1:
                    how = []
                    ku = _key_users(maps, c, rel, how)
                    if ku is not None:
                        plan.notes.append("key-narrowed %s.%s: %d -> %d" % (c.cls, c.names[0], bin(mask).count("1"), bin(mask & ku).count("1")))
                        mask &= ku
                        if how:
                            factors.append((lambda field, items: lambda test_id: maps.entry_use(test_id, field, items))(*how[0]))
                            needs.append([how[0]])
                        else:
                            needs = None  # narrowed in a way that has no per-code description
            plan.add("mod", rel, lo, hi, mask)
            if factors:
                plan.entry[(rel, lo, hi)] = _all_of(factors)
            if needs and (len(needs) > 1 or needs[0][0][0] != "pairs"):
                # (mentions of the name alone say little about a half of a test: require one of
                # the sharper conditions as well)
                plan.uses[(rel, lo, hi)] = needs
            _import_guard(plan, rel, c, everyone, mask)
    if given:
        pass  # no function changes and nothing to import afresh
    elif hot:
        plan.swaps.append(rel)
        if reduced and small[2] == 0:
            plan.lean.append(rel)
        if PROBE_EXPR and len(changes) == 1 and not changes[0].coarse and shapes_ok() and analyze.TREES:
            if infect.applicable(analyze.TREES[0], analyze.TREES[1]):
                plan.probe.append(rel)
    else:
        plan.fresh = True


_FUNCS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)


def _outermost(tree, lo, hi):
    """Line span of the outermost function-like node of `tree` that contains lines lo..hi
    (decorators included); (lo, hi) when there is none."""
    stack = [tree]
    while stack:
        node = stack.pop()
        for child in ast.iter_child_nodes(node):
            first = getattr(child, "lineno", None)
            last = getattr(child, "end_lineno", None)
            if isinstance(child, _FUNCS):
                if isinstance(child, ast.Lambda):
                    a, b = first, last
                else:
                    a, b = min([first] + [d.lineno for d in child.decorator_list]), last
                if a is not None and a <= lo and hi <= b:
                    return a, b
            elif first is None or last is None or (first <= lo and hi <= last):
                stack.append(child)
    return lo, hi


_FORMULAS = [None, None]


def _formulas(tree):
    """boolrel.formulas(tree), walked once per tree."""
    if _FORMULAS[0] is not tree:
        _FORMULAS[0], _FORMULAS[1] = tree, boolrel.formulas(tree)
    return _FORMULAS[1]


def _regrouped(maps, rel, regroup, old_tree):
    """Tests in which the regrouped condition can behave differently, from its recorded
    evaluations; None when nothing can be said."""
    node, old, new, impure = regroup
    seen = maps.formula(rel, boolrel.site_key(node))
    if seen is None:
        return None
    truth_only = None
    for n, flag in _formulas(old_tree):
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


def _arg_types_settled(maps, rel, c):
    """Can the warm worker take a new `arg_types` for this class as if it had been imported
    with it?  Only when no instance of the class existed while modules loaded (an instance
    may have been checked against the table already) and no line reading the table off the
    class ran then.  (Copies made in other class bodies were ruled out by the caller.)"""
    if maps.made_while_loading(["%s:%d" % (rel, line) for line in c.cls_lines]):
        return False
    lines = maps.class_object_lines(c.names, c.cls)
    if lines is None:
        return False
    return not any(maps.ran_at_import(f, line, line) for f, line in lines)


def _const_patch(c, rel):
    """[path, [class, ...], NAME, repr(old), repr(new)] when a change replaces one literal
    class constant by another, else None."""
    if c.stmts is None or not c.cls or len(c.names) != 1 or not analyze.TREES:
        return None
    values = []
    for st in c.stmts:
        if isinstance(st, ast.Assign) and len(st.targets) == 1 and isinstance(st.targets[0], ast.Name):
            target, value = st.targets[0].id, st.value
        elif isinstance(st, ast.AnnAssign) and isinstance(st.target, ast.Name) and st.value is not None:
            target, value = st.target.id, st.value
        else:
            return None
        if target != c.names[0]:
            return None
        try:
            values.append(ast.literal_eval(value))
        except Exception:
            return None
    old, new = c.stmts
    if type(old) is not type(new):
        return None
    if isinstance(old, ast.AnnAssign) and ast.dump(old.annotation) != ast.dump(new.annotation):
        return None
    path, body = [], analyze.TREES[0].body
    while True:
        inner = next((n for n in body if isinstance(n, ast.ClassDef) and n.lineno <= old.lineno <= n.end_lineno), None)
        if inner is None:
            break
        path.append(inner.name)
        body = inner.body
    if not path or path[-1] != c.cls or not any(n.lineno == old.lineno and type(n) is type(old) for n in body):
        return None
    return [rel, path, c.names[0], repr(values[0]), repr(values[1])]


def _entries_swapped(maps, rel, swap):
    """The same question as maps.relation, asked per fixture entry of a sliced test."""
    return lambda test_id: maps.entry_relation(test_id, rel, swap)


def _entries_regrouped(maps, rel, regroup, old_tree):
    node, old, new, impure = regroup
    key = boolrel.site_key(node)
    truth_only = None
    for n, flag in _formulas(old_tree):
        if n is node:
            truth_only = flag

    def entries(test_id):
        got = maps.entry_formula(test_id, rel, key) if truth_only is not None else None
        if got is None:
            return None
        vectors, mask, before = got
        if any(boolrel.differs(old, new, kind, vec, impure, truth_only) for kind, vec in before):
            return None  # differs while the class is set up: every entry builds on that
        for kind, vec, where in vectors:
            if boolrel.differs(old, new, kind, vec, impure, truth_only):
                mask |= where
        return mask

    return entries


def _entries_asked(maps, rel, operand):
    node, struct, index = operand
    key = boolrel.site_key(node)

    def entries(test_id):
        got = maps.entry_formula(test_id, rel, key)
        if got is None:
            return None
        vectors, mask, before = got
        if any(boolrel.asks_for(struct, kind, vec, index) for kind, vec in before):
            return None  # asked for while the class is set up
        for kind, vec, where in vectors:
            if boolrel.asks_for(struct, kind, vec, index):
                mask |= where
        return mask

    return entries


def _import_guard(plan, rel, change, everyone, narrowed):
    """Narrowing assumes the module still imports.  Unless the edit plainly cannot raise, have
    the import checked; should it fail, everyone who runs that statement is in play."""
    if narrowed == everyone or analyze.harmless_at_import(change):
        return
    if rel not in plan.imports:
        plan.imports.append(rel)
    plan.wide |= everyone


FAMILY_DIRS = ("sqlglot/dialects/", "sqlglot/parsers/", "sqlglot/generators/", "sqlglot/typing/")


def _key_users(maps, c, rel, how=None):
    """Tests that look up one of the dict entries a class-level `NAME = {...}` edit touches.
    (`how`, a list, receives the per-entry question that matches: (field, items) for
    maps.entry_use.)

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
    # An edit that leaves the keys alone (and in their order) cannot reach what was built from
    # the keys only: reads that merely walked the keys, or asked whether one is there, while
    # modules loaded do not count against it.
    values_only = keys <= old_keys and res[2] and res[3]
    if maps.feeds_elsewhere(name, c.cls, values_only=values_only):
        return None  # e.g. `TYPE_MAPPING = {..., **STRING_TYPE_MAPPING}`: looked up under another name
    known = set(maps.dict_all_keys.get(name, ()))
    cls_keys = ["%s:%d" % (rel, line) for line in c.cls_lines]
    for k in keys:
        if maps.derived(name, k, cls_keys, values_only):
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
            if how is not None:
                how.append(("dict_calls", ["%s\t%s" % (name, k) for k in keys]))
            return mask
    if maps.derived(name, "*", cls_keys, values_only):
        return None
    mask = maps.dict_users(name, "*")
    for k in keys:
        mask |= maps.dict_users(name, k)
    if how is not None:
        how.append(("dict_keys", ["%s\t*" % name] + ["%s\t%s" % (name, k) for k in keys]))
    return mask


def _class_users(maps, base, rel, c):
    """Tests that can observe a class attribute of class c.cls: those that create an instance of
    the class (or a subclass), plus those running a line that names `Class.ATTR` outright.

    A dialect's tokenizer, parser and generator classes feed each other when the dialect class
    is built, so for those the whole dialect family counts.  None when we cannot tell.
    """
    parts = _class_parts(maps, base, rel, c)
    if parts is None:
        return None
    keys, lines = parts
    users = 0
    for k in keys:
        users |= maps.class_users.get(k, 0)
    for f, line in lines:
        users |= maps.func(f, line, line) | maps.mod(f, line, line)
    return users


def _class_parts(maps, base, rel, c):
    """What _class_users is made of: (classes whose instances count, lines that count)."""
    key = None
    for line in c.cls_lines:
        k = "%s:%d" % (rel, line)
        if k in maps.tracked:
            key = k
            break
    if key is None:
        return None
    if any(maps.feeds_elsewhere(name, c.cls) for name in c.names):
        return None  # a copy stored under another name can be reached without this class
    # read off a class object (`cls.NAME` in a classmethod, `Dialect[...].NAME`): no instance needed
    lines = maps.class_object_lines(c.names, c.cls)
    if lines is None:
        return None
    lines = list(lines)
    keys = [key]
    for d in FAMILY_DIRS:
        if rel.startswith(d) and REFINE_CLASS == "family":
            stem = rel[len(d):]
            prefixes = tuple(fd + stem + ":" for fd in FAMILY_DIRS)
            keys.extend(k for k in maps.class_users if k.startswith(prefixes) and k != key)
            break
    for name in c.names:
        pat = re.compile(r"\b%s\s*\.\s*%s\b" % (re.escape(c.cls), re.escape(name)))
        for f, ls in _ident_lines(base, name, maps).items():
            data = _read(base, f)
            if data is None:
                continue
            text = data.decode("utf-8", "replace").split("\n")
            for line in ls:
                if pat.search(text[line - 1]):
                    lines.append((f, line))
    return keys, lines


def _entries_class(maps, parts):
    """_class_users asked per fixture entry of a sliced test."""
    keys, lines = parts

    def entries(test_id):
        mask = maps.entry_use(test_id, "classes", keys)
        if mask is None:
            return None
        rest = maps.entry_line_users(test_id, lines)
        return None if rest is None else mask | rest

    return entries


def _all_of(factors):
    """Entries that pass every narrowing that can say something about a test (each one names
    a superset of the entries the change can reach, so any of them may be left out)."""

    def entries(test_id):
        mask = None
        for f in factors:
            m = f(test_id)
            if m is not None:
                mask = m if mask is None else mask & m
        return mask

    def explained(test_id):
        """Does every narrowing that says something name at least one entry?  Then entries
        account for the test under each of them, and an empty intersection means that no
        single entry meets them all (one uses the class, another looks up the key)."""
        said = [m for m in (f(test_id) for f in factors) if m is not None]
        return len(said) > 1 and all(said)

    entries.explained = explained
    return entries


def _names_free(base, maps, rel, names):
    """True when none of `names` is written in file `rel` and no file imports `*` from it."""
    try:
        data = _read(base, rel)
        if data is None:
            return False
        text = data.decode("utf-8", "replace")
        if any(re.search(r"\b%s\b" % re.escape(name), text) for name in names):
            return False
        dotted = rel[:-3].replace("/", ".")
        if dotted.endswith(".__init__"):
            dotted = dotted[: -len(".__init__")]
        tail = dotted.rsplit(".", 1)[-1]
        star = re.compile(r"^\s*from\s+(\S+)\s+import\s+\*", re.M)
        for f in maps.files:
            if not f.endswith(".py"):
                continue
            other = _read(base, f)
            if other is None or b"import *" not in other:
                continue
            for m in star.findall(other.decode("utf-8", "replace")):
                if m == dotted or m.lstrip(".") == tail or m.endswith("." + tail):
                    return False
        return True
    except Exception:
        return False


def _renamed_users(maps, base, rel, c, callers):
    """`callers` plus the tests that run, while modules load, a line that mentions the old name
    of a renamed function (an import of it, a table that refers to it); None when such a line
    sits in a function that ran while modules were loading, or the mentions cannot be listed."""
    _, name, new_name = c.header
    try:
        if _ident_lines(base, new_name, maps):
            return None  # the new name is in use already: the function now stands in for another
        where = _ident_lines(base, name, maps)
    except Exception:
        return None
    flo, fhi = c.calls
    for f, lines in where.items():
        if f.startswith("tests/"):
            # a test file that mentions the name: all of its tests, and whoever imports it
            callers |= maps.imports.get(f, 0) | maps.any_line(f)
            continue
        for line in lines:
            if f == rel and flo <= line <= fhi:
                continue
            if maps.ran_at_import(f, line, line):
                return None
            # (a line that only reads the name - to store the function, to pass it on - fails
            # without entering it: every test that runs a line naming it counts)
            callers |= maps.mod(f, line, line) | maps.func(f, line, line)
    return callers


def _users(names, base, maps, rel, own_lines):
    """Tests that execute some line mentioning one of `names` (outside the assignment itself)."""
    lines = _user_lines(names, base, maps, rel, own_lines)
    if lines is None:
        return maps.all
    mask = 0
    for f, line in lines:
        mask |= maps.func(f, line, line) | maps.mod(f, line, line)
    return mask


def _user_lines(names, base, maps, rel, own_lines):
    """The (file, line) pairs behind _users; None when every test counts."""
    out = []
    for name in names:
        if name.startswith("__") and name.endswith("__"):
            return None
        where = maps.reads(name) if USE_READS else None
        if where is None:
            where = _ident_lines(base, name, maps)  # not indexed: any line mentioning the name
        for f, lines in where.items():
            for line in lines:
                if f == rel and own_lines[0] <= line <= own_lines[1]:
                    continue
                out.append((f, line))
    return out


def _new_module(plan, rel, new, maps, base):
    """A python file that did not exist.  Nothing imports it by name yet, but it can still
    matter: it can shadow a module, and packages are sometimes walked (pkgutil) by tests."""
    name = os.path.basename(rel)
    plan.kinds.add("new-py")
    parts = rel.split("/")
    top = parts[0][:-3] if len(parts) == 1 else parts[0]
    if len(parts) == 1 or not os.path.isdir(os.path.join(base, parts[0])):
        # a new top-level module or package: on sys.path ahead of everything installed
        try:
            taken = top in sys.stdlib_module_names or importlib.util.find_spec(top) is not None
        except Exception:
            taken = True
        if taken or top in ("sqlglot", "tests", "sqlglotc", "duckdb", "pandas", "numpy", "pytz", "dateutil", "setuptools", "pytest"):
            plan.unknown = True
            plan.notes.append("new top-level module or package shadows %s" % top)
        return
    d = os.path.dirname(rel)
    here = d
    fresh_dirs = []
    while here and not os.path.isdir(os.path.join(base, here)):
        fresh_dirs.append(here)
        here = os.path.dirname(here)
    if fresh_dirs:
        # the file sits in a directory that is new as well; `here` is the nearest existing one
        shadowed = fresh_dirs[-1] + ".py"
        if os.path.exists(os.path.join(base, shadowed)):
            # pkg/mod/ next to pkg/mod.py: the import system takes the directory
            plan.fresh = True
            plan.notes.append("new directory shadows %s" % shadowed)
            plan.add("mod", rel, 0, 0, maps.imports.get(shadowed, 0) | maps.any_line(shadowed) | maps.opened.get(shadowed, 0))
        d = here
    # tests that read sibling files as data usually enumerate the directory
    for f, mask in maps.opened.items():
        if os.path.dirname(f) == d:
            plan.add("mod", rel, 0, 0, mask)
    if rel.startswith("sqlglot/"):
        # a package of sqlglot can be walked to find its modules: whoever imports a module
        # that lives directly in this directory is in play
        for f in maps.files:
            if os.path.dirname(f) == d:
                plan.add("mod", rel, 0, 0, maps.imports.get(f, 0) | maps.any_line(f))
    if name == "__init__.py":
        plan.fresh = True
        for f in maps.files:
            if f.startswith(d + "/"):
                plan.add("mod", rel, 0, 0, maps.imports.get(f, 0) | maps.any_line(f))
    if plan.cand:
        plan.fresh = True
