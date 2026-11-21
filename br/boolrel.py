"""Which tests can tell `and` from `or` in one condition.

A condition such as `a and not b or c` is a small formula over its operands.  While the maps
are built, every evaluation of every such formula in sqlglot is recorded per test: which
operands were evaluated and whether each was true.  When all operands are free of calls, the
ones the formula skipped are evaluated as well, so the whole truth assignment is known.

A patch that only regroups the same operands with different `and` / `or` operators gives a
second formula over the same operands.  Replaying a recorded evaluation through both shows
whether the patched formula would have asked for an operand the original never evaluated,
skipped a call the original made, or produced a different answer.  A test none of whose
recorded evaluations shows any of that runs exactly as before.
"""
import ast
import copy

START = "_br_bs_"
ATOM = "_br_ba_"
END = "_br_be_"
IMPURE = (ast.Call, ast.Await, ast.Yield, ast.YieldFrom, ast.NamedExpr, ast.Lambda, ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp, ast.Starred)
RAISES = 2  # truth value recorded for an operand whose evaluation raised
UNSURE = 1 << 20  # added to an operand's index when its truth could not be taken


def is_formula(node):
    """A boolean operator, possibly under `not`s."""
    while isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        node = node.operand
    return isinstance(node, ast.BoolOp)


def structure(node, atoms):
    """Nested description of a formula; operands are appended to `atoms`."""
    if isinstance(node, ast.BoolOp):
        return ("and" if isinstance(node.op, ast.And) else "or", [structure(v, atoms) for v in node.values])
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        return ("not", structure(node.operand, atoms))
    atoms.append(node)
    return ("atom", len(atoms) - 1)


def pure(node):
    return not any(isinstance(sub, IMPURE) for sub in ast.walk(node))


def site_key(node):
    return "%d:%d:%d:%d:bool" % (node.lineno, node.col_offset, node.end_lineno, node.end_col_offset)


class _Scopes(ast.NodeVisitor):
    """Find every outermost formula that may be instrumented, with how its value is used."""

    def __init__(self):
        self.found = []  # (node, uses_truth_only)
        self.stack = ["module"]

    def _scope(self, node, kind):
        self.stack.append(kind)
        self.generic_visit(node)
        self.stack.pop()

    def visit_FunctionDef(self, node):
        # decorators, defaults and annotations belong to the enclosing scope
        for d in node.decorator_list:
            self.visit(d)
        self.visit(node.args)
        if node.returns is not None:
            self.visit(node.returns)
        self.stack.append("func")
        for s in node.body:
            self.visit(s)
        self.stack.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Lambda(self, node):
        self.visit(node.args)
        self.stack.append("func")
        self.visit(node.body)
        self.stack.pop()

    def visit_ClassDef(self, node):
        for d in node.decorator_list + node.bases + [k.value for k in node.keywords]:
            self.visit(d)
        self.stack.append("class")
        for s in node.body:
            self.visit(s)
        self.stack.pop()

    def _comprehension(self, node):
        # the first iterable is evaluated outside; an assignment expression is not allowed in
        # any iterable, so formulas there are left alone
        gens = node.generators
        self.stack.append("iter")
        for g in gens:
            self.visit(g.iter)
        self.stack.pop()
        self.stack.append("comp")
        for g in gens:
            self.visit(g.target)
            for cond in g.ifs:
                self._expr(cond, True)
        if isinstance(node, ast.DictComp):
            self.visit(node.key)
            self.visit(node.value)
        else:
            self.visit(node.elt)
        self.stack.pop()

    visit_ListComp = visit_SetComp = visit_DictComp = visit_GeneratorExp = _comprehension

    def _ok(self):
        # the walrus target must land in a function: comprehensions pass it up, class bodies,
        # the module and comprehension iterables do not take it
        if "iter" in self.stack:
            return False
        for kind in reversed(self.stack):
            if kind == "comp":
                continue
            return kind == "func"
        return False

    def _expr(self, node, truth_only):
        if is_formula(node):
            if self._ok():
                self.found.append((node, truth_only or isinstance(node, ast.UnaryOp)))
            atoms = []
            structure(node, atoms)
            for a in atoms:
                self.visit(a)
        else:
            self.visit(node)

    def visit_If(self, node):
        self._expr(node.test, True)
        for s in node.body + node.orelse:
            self.visit(s)

    visit_While = visit_If

    def visit_IfExp(self, node):
        self._expr(node.test, True)
        self.visit(node.body)
        self.visit(node.orelse)

    def visit_Assert(self, node):
        self._expr(node.test, True)
        if node.msg is not None:
            self.visit(node.msg)

    def visit_BoolOp(self, node):
        self._expr(node, False)

    def visit_UnaryOp(self, node):
        if is_formula(node):
            self._expr(node, True)
        else:
            self.generic_visit(node)


def formulas(tree):
    """[(node, truth_only)] for every formula in a module that gets recorded."""
    v = _Scopes()
    v.visit(tree)
    return v.found


def sites(tree):
    return sorted(site_key(n) for n, _ in formulas(tree))


def instrument(tree, rel):
    """Rewrite the module so that each formula reports its evaluations (in place)."""
    todo = {id(n): (n, truth) for n, truth in formulas(tree)}
    counter = [0]

    class Rewrite(ast.NodeTransformer):
        def visit(self, node):
            hit = todo.get(id(node))
            if hit is None:
                return self.generic_visit(node)
            atoms = []
            structure(node, atoms)
            plain = [copy.deepcopy(a) for a in atoms]  # for the skipped operands: no reporting
            all_pure = all(pure(a) for a in atoms)
            counter[0] += 1
            tmp = "_br_t%d_" % counter[0]
            where = {}
            for i, a in enumerate(atoms):
                new = self.generic_visit(a) if not todo.get(id(a)) else self.visit(a)
                where[id(a)] = ast.copy_location(
                    ast.Call(func=ast.Name(id=ATOM, ctx=ast.Load()), args=[ast.Name(id=tmp, ctx=ast.Load()), ast.Constant(value=i), new], keywords=[]),
                    a,
                )

            def rebuild(n):
                if id(n) in where:
                    return where[id(n)]
                if isinstance(n, ast.BoolOp):
                    n.values = [rebuild(v) for v in n.values]
                    return n
                n.operand = rebuild(n.operand)
                return n

            body = rebuild(node)
            if all_pure:
                thunks = ast.Tuple(
                    elts=[ast.Lambda(args=ast.arguments(posonlyargs=[], args=[], vararg=None, kwonlyargs=[], kw_defaults=[], kwarg=None, defaults=[]), body=p) for p in plain],
                    ctx=ast.Load(),
                )
            else:
                thunks = ast.Constant(value=None)
            start = ast.NamedExpr(
                target=ast.Name(id=tmp, ctx=ast.Store()),
                value=ast.Call(func=ast.Name(id=START, ctx=ast.Load()), args=[ast.Constant(value=rel + "|" + site_key(node))], keywords=[]),
            )
            call = ast.Call(func=ast.Name(id=END, ctx=ast.Load()), args=[start, body, thunks], keywords=[])
            return ast.copy_location(call, node)

    Rewrite().visit(tree)
    return tree


class Recorder:
    """The three helpers the rewritten code calls, and what they collect."""

    def __init__(self, busy):
        self.busy = busy  # shared one-element list: non-zero while skipped operands are tried
        self.seen = set()  # (site, kind, vector)
        self.started = {}
        self.finished = {}

    def start(self, site):
        if self.busy[0]:
            return None
        self.started[site] = self.started.get(site, 0) + 1
        return [site]

    def atom(self, t, i, value):
        if t is not None:
            # (the formula itself may never ask this operand for its truth - the last one of
            # an `x = a or b` is just handed on - so asking must not be able to break anything)
            try:
                t.append(i if value else ~i)
            except BaseException:
                t.append(i + UNSURE)
        return value

    def end(self, t, result, thunks):
        if t is None:
            return result
        site = t[0]
        self.finished[site] = self.finished.get(site, 0) + 1
        if thunks is None:
            self.seen.add((site, "e", tuple(t[1:])))
            return result
        known = {}
        for x in t[1:]:
            if x >= UNSURE:
                known[x - UNSURE] = RAISES
            else:
                known[x if x >= 0 else ~x] = 1 if x >= 0 else 0
        full = []
        self.busy[0] += 1
        try:
            for i, thunk in enumerate(thunks):
                if i in known:
                    full.append(known[i])
                    continue
                try:
                    full.append(1 if thunk() else 0)
                except BaseException:
                    full.append(RAISES)
        finally:
            self.busy[0] -= 1
        # also keep which operands really ran: needed to know which value the formula returned
        self.seen.add((site, "f", tuple(full)))
        return result

    def aborted(self):
        """Sites with an evaluation that never reached its end (an operand raised)."""
        return sorted(s for s, n in self.started.items() if self.finished.get(s, 0) != n)


# ---------------------------------------------------------------------------- deciding
class _Unknown(Exception):
    pass


def _run(struct, value_of, order):
    """Evaluate a formula the way Python does.  Returns (truth, index of the operand whose value
    is the result, or None when the result is a `not`).  `order` collects operands asked for."""
    kind = struct[0]
    if kind == "atom":
        i = struct[1]
        order.append(i)
        return value_of(i), i
    if kind == "not":
        t, _ = _run(struct[1], value_of, order)
        return (not t), None
    want = kind == "or"
    t = src = None
    for part in struct[1]:
        t, src = _run(part, value_of, order)
        if bool(t) == want:
            break
    return t, src


def differs(old, new, kind, vector, impure, truth_only):
    """Could the patched formula behave differently on this recorded evaluation?"""
    if kind == "f":
        def value_of(i):
            if vector[i] == RAISES:
                raise _Unknown()
            return bool(vector[i])
    else:
        known = {}
        for x in vector:
            if x < UNSURE:
                known[x if x >= 0 else ~x] = x >= 0

        def value_of(i):
            if i not in known:
                raise _Unknown()  # the original never evaluated this operand (or its truth)
            return known[i]

    o_order, n_order = [], []
    try:
        o_truth, o_src = _run(old, value_of, o_order)
        n_truth, n_src = _run(new, value_of, n_order)
    except _Unknown:
        return True
    if bool(o_truth) != bool(n_truth):
        return True
    if not truth_only and o_src != n_src:
        return True  # same truth, but a different operand's value is handed on
    # operands with calls must be evaluated by both, in the same order
    if [i for i in o_order if impure[i]] != [i for i in n_order if impure[i]]:
        return True
    return False


def swapped(old_tree, new_tree):
    """If two modules differ only in how one formula combines its (unchanged) operands, return
    (old formula node, old structure, new structure, impure flags); otherwise None."""
    found = []

    def walk(o, n):
        if len(found) > 1:
            return
        if is_formula(o) or is_formula(n):
            if not (isinstance(o, ast.AST) and isinstance(n, ast.AST)):
                found.append(None)
                return
            ao, an = [], []
            so, sn = structure(o, ao), structure(n, an)
            if len(ao) == len(an) and all(ast.dump(a) == ast.dump(b) for a, b in zip(ao, an)):
                if so != sn:
                    found.append((o, so, sn, [not pure(a) for a in ao]))
                return
            if so == sn:
                for a, b in zip(ao, an):
                    walk(a, b)
                return
            found.append(None)
            return
        if type(o) is not type(n):
            found.append(None)
            return
        if isinstance(o, list):
            if len(o) != len(n):
                found.append(None)
                return
            for a, b in zip(o, n):
                walk(a, b)
            return
        if not isinstance(o, ast.AST):
            if o != n:
                found.append(None)
            return
        for f in o._fields:
            walk(getattr(o, f, None), getattr(n, f, None))

    walk(old_tree, new_tree)
    if len(found) == 1 and found[0] is not None:
        return found[0]
    return None
