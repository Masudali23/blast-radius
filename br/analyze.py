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

    __slots__ = ("level", "lines", "coarse", "names", "scope_span", "cls", "cls_lines", "what", "stmts", "calls")

    def __init__(self, level, lines, coarse=False, names=None, scope_span=None, cls=None, what="", stmts=None):
        self.stmts = stmts  # (old statement, new statement) for a module / class level change
        # span of the function whose literal default value is all that changed (else None):
        # such a change is only ever seen by a call of that function
        self.calls = None
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
    __slots__ = ("scope", "stmt", "header", "cls", "new")

    def __init__(self, scope, stmt, header=False, cls=None, new=None):
        self.scope = scope  # innermost scope node (Module / ClassDef / function-like)
        self.stmt = stmt  # innermost statement (or Lambda / GeneratorExp body owner)
        self.header = header
        self.cls = cls
        self.new = new  # the statement's counterpart in the new file


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
                ch = Change("mod", lines, names=_target_names(stmt), cls=ctx.cls, what=what, stmts=(stmt, ctx.new))
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
        ctx = _Ctx(scope, o, cls=cls, new=n)
        if isinstance(o, (ast.FunctionDef, ast.AsyncFunctionDef)):
            # name / decorators / defaults / annotations are evaluated in the enclosing scope
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
    if change.calls:
        return True  # literal default values (see _defaults_only)
    if type(o) is not type(n) or not isinstance(o, (ast.Assign, ast.AnnAssign)):
        return False
    if o.value is None or n.value is None:
        return False
    targets_o = o.targets if isinstance(o, ast.Assign) else [o.target]
    targets_n = n.targets if isinstance(n, ast.Assign) else [n.target]
    if [ast.dump(t) for t in targets_o] != [ast.dump(t) for t in targets_n] or not all(isinstance(t, ast.Name) for t in targets_o):
        return False
    if isinstance(o, ast.AnnAssign) and ast.dump(o.annotation) != ast.dump(n.annotation):
        return False
    return _same_but_constants(o.value, n.value, True)


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
    whether both dicts have the same keys).

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
    return keys, set(a[0]), set(a[0]) == set(b[0])
