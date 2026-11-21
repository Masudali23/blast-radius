"""Work out *what* a patch changes in a Python file, at statement / scope granularity.

The result is a list of Change records in OLD-file line numbers, which is what the
coverage maps (built from the pinned commit) are keyed by.
"""
import ast

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

    __slots__ = ("level", "lines", "coarse", "names", "scope_span", "cls", "cls_lines", "what")

    def __init__(self, level, lines, coarse=False, names=None, scope_span=None, cls=None, what=""):
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


class _Ctx:
    __slots__ = ("scope", "stmt", "header", "cls")

    def __init__(self, scope, stmt, header=False, cls=None):
        self.scope = scope  # innermost scope node (Module / ClassDef / function-like)
        self.stmt = stmt  # innermost statement (or Lambda / GeneratorExp body owner)
        self.header = header
        self.cls = cls


class Differ:
    def __init__(self):
        self.changes = []
        self._seen = set()

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
                ch = Change("mod", lines, names=_target_names(stmt), cls=ctx.cls, what=what)
        key = (ch.level, ch.lines, ch.coarse)
        if key not in self._seen:
            self._seen.add(key)
            self.changes.append(ch)

    # -- comparison --------------------------------------------------------
    def stmts(self, olist, nlist, ctx_scope, cls):
        """Compare two statement lists that live directly in `ctx_scope`."""
        if len(olist) != len(nlist) or any(type(a) is not type(b) for a, b in zip(olist, nlist)):
            # try to salvage: common prefix / suffix are unchanged, the middle is a structural edit
            self._report(_Ctx(ctx_scope, None, cls=cls), coarse=True, what="stmt-list")
            return
        for o, n in zip(olist, nlist):
            self.stmt(o, n, ctx_scope, cls)

    def stmt(self, o, n, scope, cls):
        ctx = _Ctx(scope, o, cls=cls)
        if isinstance(o, (ast.FunctionDef, ast.AsyncFunctionDef)):
            # name / decorators / defaults / annotations are evaluated in the enclosing scope
            if o.name != n.name:
                self._report(ctx, what="def-name")
            hctx = _Ctx(scope, o, header=True, cls=cls)
            for f in ("decorator_list", "args", "returns", "type_params"):
                self.generic(getattr(o, f, None), getattr(n, f, None), hctx)
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
        for o, n in zip(olist, nlist):
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


def _no_iter(g, i):
    return ast.comprehension(target=g.target, iter=ast.Constant(value=None) if i == 0 else g.iter, ifs=g.ifs, is_async=g.is_async)


def _strip_doc(body):
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str):
        return body[1:] or [ast.Pass(lineno=body[0].lineno, end_lineno=body[0].end_lineno, col_offset=0, end_col_offset=0)]
    return body


TREES = []  # the (old, new) trees of the last diff_python call that found differences


def diff_python(old_src, new_src):
    """Return (status, changes).

    status: "same"   - identical syntax trees (comments / formatting only)
            "syntax" - the new file does not parse
            "diff"   - see changes
    """
    TREES.clear()
    try:
        new_tree = ast.parse(new_src)
    except (SyntaxError, ValueError, RecursionError):
        return "syntax", []
    try:
        old_tree = ast.parse(old_src)
    except (SyntaxError, ValueError, RecursionError):
        return "diff", [Change("mod", (1, 10**9), coarse=True, what="old-unparseable")]
    if ast.dump(old_tree) == ast.dump(new_tree):
        return "same", []
    d = Differ()
    d.stmts(old_tree.body, new_tree.body, old_tree, None)
    if not d.changes:
        # only docstrings differ (we strip them): harmless for the test-suite, but stay honest
        return "doc", []
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
