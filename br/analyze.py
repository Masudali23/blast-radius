"""Work out *what* a patch changes in a Python file, at statement / scope granularity.

The result is a list of Change records in OLD-file line numbers, which is what the
coverage maps (built from the pinned commit) are keyed by.
"""
import ast
import os

FUNC_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.GeneratorExp)
BODY_FIELDS = ("body", "orelse", "finalbody")


class Change:
    """One localized difference.

    level:  "func"  - inside a function-like scope (runs at call time)
            "mod"   - module or class body (runs at import time)
    lines:  (lo, hi) old-file line span to look up in the coverage map
    coarse: True when we could not localize below the enclosing scope
    names:  for "mod" changes that are plain `NAME = ...` assignments, the bound names
    scope_span: (lo, hi) of the enclosing function-like scope (func level only)
    cls:    name of the enclosing class for class-body changes (or None)
    """

    __slots__ = ("level", "lines", "coarse", "names", "scope_span", "cls", "cls_lines", "what", "stmts", "calls", "header")

    def __init__(self, level, lines, coarse=False, names=None, scope_span=None, cls=None, what="", stmts=None):
        self.stmts = stmts  # (old statement, new statement) for a module / class level change
        # span of the function whose literal default value is all that changed (else None):
        # such a change is only ever seen by a call of that function
        self.calls = None
        # ... or of the function whose decorators ("decorators") or name (("name", old, new)) are
        # all that changed outside its body: seen by a call of that function, or - unlike a default
        # value - by whatever looks at the function while modules are loading
        self.header = None
        self.level = level
        self.lines = lines
        self.coarse = coarse
        self.names = names
        self.scope_span = scope_span
        # enclosing class of a class-body change: its name and the lines its body may start on
        self.cls = getattr(cls, "name", None)
        self.cls_lines = ()
        if cls is not None:
            first = min([cls.lineno] + [d.lineno for d in cls.decorator_list])
            self.cls_lines = tuple(sorted({cls.lineno, first}))
        self.what = what

    def __repr__(self):
        return "Change(%s %s%s %s %s)" % (
            self.level,
            self.lines,
            " coarse" if self.coarse else "",
            self.names or "",
            self.what,
        )


def _span(node):
    lo = getattr(node, "lineno", None)
    hi = getattr(node, "end_lineno", None)
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.decorator_list:
        lo = min(lo, min(d.lineno for d in node.decorator_list))
    return lo, hi


def _expr_span(node, fallback):
    lo = hi = None
    for sub in ast.walk(node) if isinstance(node, ast.AST) else ():
        l = getattr(sub, "lineno", None)
        if l is not None:
            e = getattr(sub, "end_lineno", l) or l
            lo = l if lo is None else min(lo, l)
            hi = e if hi is None else max(hi, e)
    if lo is None:
        return fallback
    return lo, hi


def _target_names(stmt):
    names = []
    targets = []
    if isinstance(stmt, ast.Assign):
        targets = stmt.targets
    elif isinstance(stmt, (ast.AnnAssign, ast.AugAssign)):
        targets = [stmt.target]
    for t in targets:
        if isinstance(t, ast.Name):
            names.append(t.id)
        elif isinstance(t, (ast.Tuple, ast.List)):
            for e in t.elts:
                if isinstance(e, ast.Name):
                    names.append(e.id)
                else:
                    return None
        else:
            return None
    return names or None


# a body that only gained new, undecorated functions is compared statement by statement around
# them (else: one change the size of the whole body)
ADDED_DEFS = os.environ.get("BR_ADDED_DEFS", "1") == "1"


class _Ctx:
    __slots__ = ("scope", "stmt", "header", "cls", "new")

    def __init__(self, scope, stmt, header=False, cls=None, new=None):
        self.scope = scope  # innermost scope node (Module / ClassDef / function-like)
        self.stmt = stmt  # innermost statement (or Lambda / GeneratorExp body owner)
        self.header = header
        self.cls = cls
        self.new = new  # the statement's counterpart in the new file


class Differ:
    def __init__(self, same=None):
        self.changes = []
        self._seen = set()
        self.same = same  # optional test for two statements being written identically

    # -- reporting ---------------------------------------------------------
    def _report(self, ctx, node=None, coarse=False, what=""):
        scope = ctx.scope
        is_func = isinstance(scope, FUNC_SCOPES)
        if is_func:
            sspan = _span(scope)
            if isinstance(scope, (ast.Lambda, ast.GeneratorExp)):
                # the whole lambda / generator expression is the unit
                lines = sspan
            elif coarse or ctx.stmt is None:
                lines = sspan
                coarse = True
            elif ctx.header and node is not None:
                lo, hi = _expr_span(node, _span(ctx.stmt))
                lines = (min(lo, ctx.stmt.lineno), hi) if not isinstance(ctx.stmt, (ast.Try,)) else (lo, hi)
            elif ctx.header:
                lines = (ctx.stmt.lineno, ctx.stmt.lineno)
            else:
                lines = _span(ctx.stmt)
            ch = Change("func", lines, coarse=coarse, scope_span=sspan, what=what)
        else:
            stmt = ctx.stmt
            if coarse or stmt is None:
                lines = _span(scope) if not isinstance(scope, ast.Module) else (1, 10**9)
                ch = Change("mod", lines, coarse=True, cls=ctx.cls, what=what)
            else:
                if ctx.header and node is not None:
                    lo, hi = _expr_span(node, _span(stmt))
                    lines = (min(lo, stmt.lineno), hi)
                else:
                    lines = _span(stmt)
                ch = Change("mod", lines, names=_target_names(stmt), cls=ctx.cls, what=what, stmts=(stmt, ctx.new))
        key = (ch.level, ch.lines, ch.coarse)
        if key not in self._seen:
            self._seen.add(key)
            self.changes.append(ch)

    # -- comparison --------------------------------------------------------
    def stmts(self, olist, nlist, ctx_scope, cls):
        """Compare two statement lists that live directly in `ctx_scope`."""
        pairs = None
        if len(olist) != len(nlist) or any(type(a) is not type(b) for a, b in zip(olist, nlist)):
            found = _added_defs(olist, nlist, isinstance(ctx_scope, ast.Module)) if ADDED_DEFS and len(nlist) > len(olist) and not isinstance(ctx_scope, FUNC_SCOPES) else None
            if found is None:
                # try to salvage: common prefix / suffix are unchanged, the middle is a structural edit
                self._report(_Ctx(ctx_scope, None, cls=cls), coarse=True, what="stmt-list")
                return
            pairs, added = found
            for anchor, name in added:
                line = anchor or getattr(ctx_scope, "lineno", None) or 1
                ch = Change("mod", (line, line), cls=cls, what="def-added")
                ch.header = ("added", name)
                self.changes.append(ch)
        same = self.same
        for o, n in pairs if pairs is not None else zip(olist, nlist):
            if same is not None and same(o, n):
                continue  # written identically: nothing to find
            self.stmt(o, n, ctx_scope, cls)

    def stmt(self, o, n, scope, cls):
        if _bare_text(o) and _bare_text(n):
            # two strings that stand alone as statements: evaluating either does nothing, and
            # whichever comes first in a module is only its documentation (as for the
            # documentation of functions and classes, which is not compared at all)
            return
        ctx = _Ctx(scope, o, cls=cls, new=n)
        if isinstance(o, (ast.FunctionDef, ast.AsyncFunctionDef)):
            # name / decorators / defaults / annotations are evaluated in the enclosing scope
            start = len(self.changes)
            if o.name != n.name:
                self._report(ctx, what="def-name")
            hctx = _Ctx(scope, o, header=True, cls=cls)
            before = len(self.changes)
            for f in ("decorator_list", "args", "returns", "type_params"):
                self.generic(getattr(o, f, None), getattr(n, f, None), hctx)
            if len(self.changes) > before and not isinstance(scope, FUNC_SCOPES) and o.name == n.name and _defaults_only(o, n):
                for ch in self.changes[before:]:
                    if ch.level == "mod" and not ch.coarse:
                        ch.calls = _span(o)
            elif len(self.changes) > start and not isinstance(scope, FUNC_SCOPES):
                kind = _header_only(o, n)
                if kind not in ("annotations", "signature") and (o.name.startswith("__") or n.name.startswith("__")):
                    kind = None  # (Python itself calls these, and not only by entering them)
                if kind:
                    for ch in self.changes[start:]:
                        if ch.level == "mod" and not ch.coarse:
                            ch.calls = _span(o)
                            ch.header = kind
            self.stmts(_strip_doc(o.body), _strip_doc(n.body), o, None)
            return
        if isinstance(o, ast.ClassDef):
            if o.name != n.name:
                self._report(ctx, what="class-name")
            hctx = _Ctx(scope, o, header=True, cls=cls)
            for f in ("decorator_list", "bases", "keywords", "type_params"):
                self.generic(getattr(o, f, None), getattr(n, f, None), hctx)
            if isinstance(scope, FUNC_SCOPES):
                # class defined inside a function: treat its body as part of that function
                if ast.dump(o) != ast.dump(n):
                    self._report(ctx, what="local-class")
                return
            self.stmts(_strip_doc(o.body), _strip_doc(n.body), o, o)
            return
        compound = False
        for f in BODY_FIELDS:
            if isinstance(getattr(o, f, None), list) and getattr(o, f) and isinstance(getattr(o, f)[0], ast.stmt):
                compound = True
        if isinstance(o, (ast.Try, getattr(ast, "TryStar", ast.Try), ast.Match)):
            compound = True
        if not compound:
            self.generic(o, n, ctx)
            return
        hctx = _Ctx(scope, o, header=True, cls=cls)
        for f in o._fields:
            ov, nv = getattr(o, f, None), getattr(n, f, None)
            if f in BODY_FIELDS and isinstance(ov, list) and (not ov or isinstance(ov[0], ast.stmt)) and (not nv or isinstance(nv[0], ast.stmt)):
                self.block(ov, nv, scope, cls, o)
            elif f == "handlers":
                if len(ov) != len(nv):
                    self._report(_Ctx(scope, None, cls=cls), coarse=True, what="handlers")
                    continue
                for oh, nh in zip(ov, nv):
                    hh = _Ctx(scope, oh, header=True, cls=cls)
                    if (oh.name or "") != (nh.name or ""):
                        self._report(hh, what="handler-name")
                    self.generic(oh.type, nh.type, hh)
                    self.block(oh.body, nh.body, scope, cls, o)
            elif f == "cases":
                if len(ov) != len(nv):
                    self._report(_Ctx(scope, None, cls=cls), coarse=True, what="cases")
                    continue
                for oc, nc in zip(ov, nv):
                    self.generic(oc.pattern, nc.pattern, hctx)
                    self.generic(oc.guard, nc.guard, hctx)
                    self.block(oc.body, nc.body, scope, cls, o)
            else:
                self.generic(ov, nv, hctx)

    def block(self, olist, nlist, scope, cls, owner):
        if len(olist) != len(nlist) or any(type(a) is not type(b) for a, b in zip(olist, nlist)):
            self._report(_Ctx(scope, None, cls=cls), coarse=True, what="block")
            return
        same = self.same
        for o, n in zip(olist, nlist):
            if same is not None and same(o, n):
                continue
            self.stmt(o, n, scope, cls)

    def generic(self, o, n, ctx):
        """Compare arbitrary AST values; report into ctx, opening new scopes for lambdas/genexprs."""
        if o is None and n is None:
            return
        if type(o) is not type(n):
            self._report(ctx, o if isinstance(o, ast.AST) else None, what="type")
            return
        if isinstance(o, list):
            if len(o) != len(n):
                self._report(ctx, o[0] if o and isinstance(o[0], ast.AST) else None, what="list-len")
                return
            for a, b in zip(o, n):
                self.generic(a, b, ctx)
            return
        if not isinstance(o, ast.AST):
            if o != n or type(o) is not type(n):
                self._report(ctx, None, what="value")
            return
        if isinstance(o, ast.Lambda):
            # defaults are evaluated outside, the body inside
            self.generic(o.args, n.args, ctx)
            if ast.dump(o.body) != ast.dump(n.body):
                self._descend_scope(o, n, o.body, n.body)
            return
        if isinstance(o, ast.GeneratorExp):
            # the first iterable is evaluated in the enclosing scope
            self.generic(o.generators[0].iter, n.generators[0].iter, ctx) if o.generators and n.generators else None
            rest_o = ast.dump(ast.GeneratorExp(elt=o.elt, generators=[_no_iter(g, i) for i, g in enumerate(o.generators)]))
            rest_n = ast.dump(ast.GeneratorExp(elt=n.elt, generators=[_no_iter(g, i) for i, g in enumerate(n.generators)]))
            if rest_o != rest_n:
                self._report(_Ctx(o, o), what="genexpr")
            return
        if isinstance(o, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            # a def nested in an expression context cannot happen; statements are handled by stmt()
            self.stmt(o, n, ctx.scope, ctx.cls)
            return
        # leaf-ish comparison: find whether anything differs at this node itself
        differs_here = False
        for f in o._fields:
            ov, nv = getattr(o, f, None), getattr(n, f, None)
            if isinstance(ov, (ast.AST, list)) or isinstance(nv, (ast.AST, list)):
                continue
            if ov != nv or type(ov) is not type(nv):
                differs_here = True
        if differs_here:
            self._report(ctx, o, what=type(o).__name__)
            return
        for f in o._fields:
            ov, nv = getattr(o, f, None), getattr(n, f, None)
            if isinstance(ov, (ast.AST, list)) or isinstance(nv, (ast.AST, list)):
                self.generic(ov, nv, ctx)

    def _descend_scope(self, o, n, obody, nbody):
        # anything different inside a lambda body belongs to the lambda's own code object,
        # unless the difference is confined to a nested lambda / genexpr
        sub = Differ()
        sub.generic(obody, nbody, _Ctx(o, o))
        for ch in sub.changes:
            key = (ch.level, ch.lines, ch.coarse)
            if key not in self._seen:
                self._seen.add(key)
                self.changes.append(ch)


def _number(e):
    return isinstance(e, ast.UnaryOp) and isinstance(e.op, (ast.USub, ast.UAdd)) and isinstance(e.operand, ast.Constant) and type(e.operand.value) in (int, float)


def _hashable(e):
    return isinstance(e, ast.Constant) or _number(e) or (isinstance(e, ast.Tuple) and all(_hashable(x) for x in e.elts))


def _literal(e):
    """An expression whose evaluation cannot fail or do anything: constants and displays of them."""
    if isinstance(e, ast.Constant) or _number(e):
        return True
    if isinstance(e, (ast.Tuple, ast.List)):
        return all(_literal(x) for x in e.elts)
    if isinstance(e, ast.Set):
        return all(_hashable(x) for x in e.elts)
    if isinstance(e, ast.Dict):
        return all(k is not None and _hashable(k) for k in e.keys) and all(_literal(v) for v in e.values)
    return False


def _same_but_constants(o, n, display):
    """Are two expressions the same apart from constants that sit directly in displays?

    A constant held by a dict / tuple / list / set display is only stored, so swapping it for
    another constant cannot make the statement raise.  A constant handed to a call, an operator
    or a subscript can."""
    if isinstance(o, ast.Constant) and isinstance(n, ast.Constant):
        return (o.value == n.value and type(o.value) is type(n.value)) or display
    if _number(o) and _number(n):
        return ast.dump(o) == ast.dump(n) or display
    if type(o) is not type(n):
        return False
    if isinstance(o, (ast.Tuple, ast.List, ast.Set)):
        return len(o.elts) == len(n.elts) and all(_same_but_constants(a, b, display) for a, b in zip(o.elts, n.elts))
    if isinstance(o, ast.Dict):
        if len(o.keys) != len(n.keys):
            return False
        for a, b in zip(o.keys, n.keys):
            if (a is None) != (b is None) or (a is not None and not _same_but_constants(a, b, display)):
                return False
        return all(_same_but_constants(a, b, display and k is not None) for a, b, k in zip(o.values, n.values, o.keys))
    return ast.dump(o) == ast.dump(n)


def harmless_at_import(change):
    """True when a module / class level change cannot make its statement raise while importing:
    a plain assignment whose value differs only in constants stored by displays."""
    if not change.stmts:
        return False
    o, n = change.stmts
    if change.calls and not change.header:
        return True  # literal default values (see _defaults_only)
    if type(o) is not type(n) or not isinstance(o, (ast.Assign, ast.AnnAssign)):
        return False
    if o.value is None or n.value is None:
        return False
    targets_o = o.targets if isinstance(o, ast.Assign) else [o.target]
    targets_n = n.targets if isinstance(n, ast.Assign) else [n.target]
    if [ast.dump(t) for t in targets_o] != [ast.dump(t) for t in targets_n] or not all(isinstance(t, ast.Name) for t in targets_o):
        return False
    if any(t.id.startswith("__") for t in targets_o):
        return False  # __slots__, __all__, ...: Python itself reads these while building the class
    if isinstance(o, ast.AnnAssign) and ast.dump(o.annotation) != ast.dump(n.annotation):
        return False
    return _same_but_constants(o.value, n.value, True)


def _bare_args(args):
    """The arguments of a def without their annotations, as text."""
    import copy

    args = copy.deepcopy(args)
    for arg in args.posonlyargs + args.args + args.kwonlyargs + [x for x in (args.vararg, args.kwarg) if x is not None]:
        arg.annotation = None
    return ast.dump(args)


def _added_defs(olist, nlist, module=False):
    """When `nlist` is `olist` with plain new functions put in between: (the statements that
    pair up, [(old line the function follows, its name)]).  None otherwise.  A new function
    counts only if nothing about it is evaluated beyond its own definition (no decorators) and
    its name is not one Python or sqlglot look up by pattern.  In a module (not in a class,
    where an enumeration would take it for a member) a new name given a literal value counts
    as well."""
    taken = set()
    for st in olist:
        if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            taken.add(st.name)
        else:
            taken.update(_target_names(st) or ())
    pairs, added = [], []
    i = 0
    anchor = None
    for n in nlist:
        o = olist[i] if i < len(olist) else None
        if o is not None and type(o) is type(n) and (not isinstance(o, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) or o.name == n.name):
            pairs.append((o, n))
            anchor = o.end_lineno
            i += 1
            continue
        if module and isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name) and _literal(n.value):
            name = n.targets[0].id
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and not n.decorator_list:
            name = n.name
        else:
            return None
        if name in taken or name.startswith("__") or name.endswith("_sql") or name.startswith("test"):
            return None
        taken.add(name)
        added.append((anchor, name))
    if i != len(olist) or not added:
        return None
    return pairs, added


def _header_only(o, n):
    """How two versions of a def differ outside the body: in their decorators only
    ("decorators"), in their name only (("name", old, new)), or - when neither has decorators -
    in annotations only ("annotations") or otherwise in arguments ("signature").  Else None."""

    def part(node, f):
        v = getattr(node, f, None)
        if isinstance(v, list):
            return ast.dump(ast.Tuple(elts=list(v), ctx=ast.Load()))
        return ast.dump(v) if v is not None else ""

    same = {f: part(o, f) == part(n, f) for f in ("decorator_list", "args", "returns", "type_params")}
    if type(o) is not type(n):
        return None
    if not (same["args"] and same["returns"]):
        # annotations are never looked at by the code under test (nothing asks for a function's
        # type hints); an undecorated function whose annotations alone changed is the same
        # function, provided the module still loads
        if o.name == n.name and same["decorator_list"] and same["type_params"] and not o.decorator_list:
            # ... and one that takes other arguments, or has other defaults, is noticed where
            # it is called (nothing wraps it or reads its signature when it has no decorator)
            return "annotations" if _bare_args(o.args) == _bare_args(n.args) else "signature"
        return None
    if not same["type_params"]:
        return None
    if "cache" in part(o, "decorator_list").lower() or "cache" in part(n, "decorator_list").lower():
        return None  # a cached function is not entered by every test that uses it
    if o.name == n.name:
        return None if same["decorator_list"] else "decorators"
    return ("name", o.name, n.name) if same["decorator_list"] else None


def _defaults_only(o, n):
    """Do two versions of a def differ, outside the body, only in literal default values?"""
    for f in ("decorator_list", "returns", "type_params"):
        if ast.dump(ast.Tuple(elts=list(getattr(o, f, None) or []) if isinstance(getattr(o, f, None), list) else [getattr(o, f, None) or ast.Constant(value=None)], ctx=ast.Load())) != ast.dump(
            ast.Tuple(elts=list(getattr(n, f, None) or []) if isinstance(getattr(n, f, None), list) else [getattr(n, f, None) or ast.Constant(value=None)], ctx=ast.Load())
        ):
            return False
    oa, na = o.args, n.args
    if len(oa.defaults) != len(na.defaults) or len(oa.kw_defaults) != len(na.kw_defaults):
        return False
    for f in ("posonlyargs", "args", "vararg", "kwonlyargs", "kwarg"):
        ov, nv = getattr(oa, f), getattr(na, f)
        if ast.dump(ast.Tuple(elts=ov if isinstance(ov, list) else [ov] if ov else [], ctx=ast.Load())) != ast.dump(ast.Tuple(elts=nv if isinstance(nv, list) else [nv] if nv else [], ctx=ast.Load())):
            return False
    differ = 0
    for a, b in list(zip(oa.defaults, na.defaults)) + list(zip(oa.kw_defaults, na.kw_defaults)):
        if (a is None) != (b is None):
            return False
        if a is None or ast.dump(a) == ast.dump(b):
            continue
        if not (_literal(a) and _literal(b)):
            return False
        differ += 1
    return differ > 0


def _no_iter(g, i):
    return ast.comprehension(target=g.target, iter=ast.Constant(value=None) if i == 0 else g.iter, ifs=g.ifs, is_async=g.is_async)


def _bare_text(node):
    return isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)


def _strip_doc(body):
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str):
        return body[1:] or [ast.Pass(lineno=body[0].lineno, end_lineno=body[0].end_lineno, col_offset=0, end_col_offset=0)]
    return body


TREES = []  # the (old, new) trees of the last diff_python call that found differences
SAME = [None]  # ... and its test for "these two statements are written identically" (or None)


def same_text(old_src, new_src):
    """A cheap test for two statements being the same code: the same source lines at the same
    columns.  (A statement written identically parses identically, so a comparison of the two
    trees has nothing to find below such a pair.)  None when lines cannot be told apart simply."""
    if not isinstance(old_src, str) or not isinstance(new_src, str) or "\r" in old_src or "\r" in new_src:
        return None
    old_lines, new_lines = old_src.split("\n"), new_src.split("\n")

    def start(node):
        lo, co = node.lineno, node.col_offset
        for d in getattr(node, "decorator_list", None) or ():
            if (d.lineno, d.col_offset) < (lo, co):
                lo, co = d.lineno, d.col_offset
        return lo, co

    def same(o, n):
        try:
            olo, oco = start(o)
            nlo, nco = start(n)
            ohi, nhi = o.end_lineno, n.end_lineno
            if oco != nco or o.end_col_offset != n.end_col_offset or ohi - olo != nhi - nlo:
                return False
            return old_lines[olo - 1 : ohi] == new_lines[nlo - 1 : nhi]
        except (AttributeError, TypeError):
            return False

    return same


def _equal(o, n, same):
    """ast.dump(o) == ast.dump(n), without dumping statements that are written identically."""
    if type(o) is not type(n):
        return False
    if isinstance(o, list):
        return len(o) == len(n) and all(_equal(a, b, same) for a, b in zip(o, n))
    if not isinstance(o, ast.AST):
        return o == n
    if isinstance(o, ast.stmt):
        if same(o, n):
            return True
        return all(_equal(getattr(o, f, None), getattr(n, f, None), same) for f in o._fields)
    return ast.dump(o) == ast.dump(n)


def diff_python(old_src, new_src):
    """Return (status, changes).

    status: "same"   - identical syntax trees (comments / formatting only)
            "syntax" - the new file does not parse
            "diff"   - see changes
    """
    TREES.clear()
    SAME[0] = None
    try:
        new_tree = ast.parse(new_src)
    except (SyntaxError, ValueError, RecursionError):
        return "syntax", []
    try:
        old_tree = ast.parse(old_src)
    except (SyntaxError, ValueError, RecursionError):
        return "diff", [Change("mod", (1, 10**9), coarse=True, what="old-unparseable")]
    same = SAME[0] = same_text(old_src, new_src)
    d = Differ(same)
    d.stmts(old_tree.body, new_tree.body, old_tree, None)
    if not d.changes:
        # (two equal trees give no changes, so whether they are equal is asked only here)
        if same is None:
            equal = ast.dump(old_tree) == ast.dump(new_tree)
        else:
            equal = _equal(old_tree.body, new_tree.body, same) and _equal(old_tree.type_ignores, new_tree.type_ignores, same)
        # "doc": only docstrings differ (we strip them): harmless for the test-suite, but stay honest
        return ("same" if equal else "doc"), []
    TREES.extend((old_tree, new_tree))
    return "diff", d.changes


def code_shapes(src, filename="<x>"):
    """Multiset of code-object shapes; a change in shape means statement-level localisation is unsafe."""
    import types

    out = {}
    try:
        top = compile(src, filename, "exec", dont_inherit=True)
    except Exception:
        return None
    stack = [top]
    while stack:
        c = stack.pop()
        key = (c.co_qualname, c.co_varnames, c.co_freevars, c.co_cellvars, c.co_flags, c.co_argcount, c.co_kwonlyargcount)
        out[key] = out.get(key, 0) + 1
        for k in c.co_consts:
            if isinstance(k, types.CodeType):
                stack.append(k)
    return out


def _changed_members(olist, nlist, same, chain, out):
    """Collect into `out` the (old, new, enclosing class names) statements of two bodies that
    are not written identically, looking inside classes whose header is unchanged.  False when
    the bodies do not pair up or a statement decides how the whole module is compiled."""
    if len(olist) != len(nlist):
        return False
    for o, n in zip(olist, nlist):
        if type(o) is not type(n):
            return False
        if same(o, n):
            continue
        if isinstance(o, ast.ImportFrom) and "__future__" in (o.module, n.module):
            return False
        if (
            isinstance(o, ast.ClassDef)
            and o.name == n.name
            and not getattr(o, "type_params", None)
            and not getattr(n, "type_params", None)
            and all(ast.dump(ast.Tuple(elts=list(getattr(o, f)), ctx=ast.Load())) == ast.dump(ast.Tuple(elts=list(getattr(n, f)), ctx=ast.Load())) for f in ("bases", "decorator_list"))
            and [ast.dump(k) for k in o.keywords] == [ast.dump(k) for k in n.keywords]
        ):
            if not _changed_members(o.body, n.body, same, chain + (o.name,), out):
                return False
            continue
        out.append((o, n, chain))
    return True


def _member_shapes(stmt, chain, out):
    """Add the code-object shapes of one statement, compiled inside classes named as the ones
    that enclose it (their names decide qualified names, private names and `__class__`)."""
    import types

    node = stmt
    for name in reversed(chain):
        # (the statement comes from the parser, with every position it needs; the classes put
        # around it get the positions ast.fix_missing_locations would give them)
        node = ast.ClassDef(name=name, bases=[], keywords=[], body=[node], decorator_list=[], lineno=1, col_offset=0, end_lineno=1, end_col_offset=0)
        if "type_params" in ast.ClassDef._fields:
            node.type_params = []
    module = ast.Module(body=[node], type_ignores=[])
    stack = [compile(module, "<x>", "exec", dont_inherit=True)]
    while stack:
        c = stack.pop()
        key = (c.co_qualname, c.co_varnames, c.co_freevars, c.co_cellvars, c.co_flags, c.co_argcount, c.co_kwonlyargcount)
        out[key] = out.get(key, 0) + 1
        for k in c.co_consts:
            if isinstance(k, types.CodeType):
                stack.append(k)


def shapes_same(old_src, new_src):
    """code_shapes(old_src) == code_shapes(new_src) for the two sources of the last diff_python
    call that found differences.  Statements written identically give the same code objects in
    both, so only the others are compiled, each by itself."""
    same = SAME[0]
    if same is not None and TREES:
        pairs = []
        if _changed_members(TREES[0].body, TREES[1].body, same, (), pairs):
            old_shapes, new_shapes = {}, {}
            try:
                for o, n, chain in pairs:
                    _member_shapes(o, chain, old_shapes)
                    _member_shapes(n, chain, new_shapes)
                return old_shapes == new_shapes
            except Exception:
                pass  # (a statement that does not compile: let the whole files decide)
    return code_shapes(old_src) == code_shapes(new_src)


def _key_text(node):
    """The tracer's text form (see br/keys.py) of a dict key written in source, or None."""
    if isinstance(node, ast.Constant):
        return ("s:" + node.value) if isinstance(node.value, str) else ("r:" + repr(node.value)[:80])
    if isinstance(node, ast.Attribute):
        return "n:" + node.attr
    if isinstance(node, ast.Name):
        return "n:" + node.id
    if isinstance(node, ast.Tuple):
        parts = [_key_text(e) for e in node.elts]
        return None if None in parts else "t:" + ",".join(parts)
    return None


def changed_dict_keys(change):
    """For `NAME = {...}` edited in place: (keys whose entry differs, keys of the old dict,
    whether both dicts have the same keys, whether they have them in the same order).

    None when the statement is not a plain dict display on both sides, a key cannot be read off
    the source, or anything besides individual entries changed (a `**spread`, say).
    """
    if not change.stmts or change.stmts[1] is None:
        return None
    old, new = (getattr(st, "value", None) for st in change.stmts)
    if not isinstance(old, ast.Dict) or not isinstance(new, ast.Dict):
        return None

    def entries(d):
        table, spreads = {}, []
        for k, v in zip(d.keys, d.values):
            if k is None:
                spreads.append(ast.dump(v))
                continue
            text = _key_text(k)
            if text is None:
                return None
            table.setdefault(text, []).append(ast.dump(k) + "=>" + ast.dump(v))
        return table, spreads

    a, b = entries(old), entries(new)
    if a is None or b is None or a[1] != b[1]:
        return None
    keys = {k for k in set(a[0]) | set(b[0]) if a[0].get(k) != b[0].get(k)}
    # (dicts keep insertion order: the same keys in the same order is what "only values
    # differ" means to anything that walked the keys)
    return keys, set(a[0]), set(a[0]) == set(b[0]), list(a[0]) == list(b[0])
