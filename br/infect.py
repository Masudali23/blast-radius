"""Differential evaluation of a changed expression ("did the change ever matter?").

When a patch alters one expression inside a function, the new function is instrumented so that
each time the expression is evaluated, the old version of that expression is evaluated as well
from the very same operand values, and the two results are compared.  A test during which the
two never differ behaves exactly as it did before the patch.

Operands that contain calls are evaluated once only (by the new code); the old expression may
reuse those values but never triggers a call the new code did not make.  If the old expression
would have needed such a value, or would have skipped one the new code computed, the
evaluation is counted as differing.
"""
import ast

OPERATORS = (ast.BoolOp, ast.Compare, ast.IfExp)
IMPURE = (ast.Call, ast.Await, ast.Yield, ast.YieldFrom, ast.NamedExpr, ast.Lambda, ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp, ast.Starred)
FUNC_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)
HELPER = "_br_eval_"
ARG = "_br_E_"

# [evaluations, evaluations where old and new differ]
STATS = [0, 0]


def _is_op(node):
    return isinstance(node, OPERATORS) or (isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not))


def _pure(node):
    return not any(isinstance(sub, IMPURE) for sub in ast.walk(node))


class _Walker:
    """Parallel walk of two trees collecting where they diverge, with the ancestor chain."""

    def __init__(self):
        self.div = []  # list of ancestor chains [(old, new, parent_new, field, index), ...]

    def walk(self, o, n, chain):
        if type(o) is not type(n):
            self.div.append(list(chain))
            return
        if isinstance(o, list):
            if len(o) != len(n):
                self.div.append(list(chain))
                return
            for i, (a, b) in enumerate(zip(o, n)):
                self._child(a, b, chain, i)
            return
        if not isinstance(o, ast.AST):
            if o != n:
                self.div.append(list(chain))
            return
        for f in o._fields:
            ov, nv = getattr(o, f, None), getattr(n, f, None)
            if isinstance(ov, list) and isinstance(nv, list):
                if len(ov) != len(nv):
                    self.div.append(list(chain))
                    continue
                for i, (a, b) in enumerate(zip(ov, nv)):
                    self._node(a, b, chain, n, f, i)
            else:
                self._node(ov, nv, chain, n, f, None)

    def _node(self, a, b, chain, parent, field, index):
        if isinstance(a, ast.AST) and isinstance(b, ast.AST) and type(a) is type(b):
            chain.append((a, b, parent, field, index))
            self.walk(a, b, chain)
            chain.pop()
        elif isinstance(a, ast.AST) or isinstance(b, ast.AST):
            if isinstance(a, ast.AST) and isinstance(b, ast.AST) and isinstance(a, ast.expr) and isinstance(b, ast.expr):
                # two different kinds of expression in the same slot: that slot is the divergence
                self.div.append(list(chain) + [(a, b, parent, field, index)])
            else:
                self.div.append(list(chain))
        elif a != b or type(a) is not type(b):
            self.div.append(list(chain))

    def _child(self, a, b, chain, i):
        self._node(a, b, chain, None, None, i)


def _atoms(node, out):
    if isinstance(node, ast.BoolOp):
        for v in node.values:
            _atoms(v, out)
    elif isinstance(node, ast.Compare):
        _atoms(node.left, out)
        for c in node.comparators:
            _atoms(c, out)
    elif isinstance(node, ast.IfExp):
        _atoms(node.test, out)
        _atoms(node.body, out)
        _atoms(node.orelse, out)
    elif isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        _atoms(node.operand, out)
    else:
        out.append(node)


class _Sub(ast.NodeTransformer):
    """Replace each atom (by identity) with ARG(i, lambda: atom)."""

    def __init__(self, atoms):
        self.where = {id(a): i for i, a in enumerate(atoms)}

    def visit(self, node):
        i = self.where.get(id(node))
        if i is not None:
            return ast.Call(
                func=ast.Name(id=ARG, ctx=ast.Load()),
                args=[ast.Constant(value=i), ast.Lambda(args=_no_args(), body=node)],
                keywords=[],
            )
        return self.generic_visit(node)


def _no_args():
    return ast.arguments(posonlyargs=[], args=[], vararg=None, kwonlyargs=[], kw_defaults=[], kwarg=None, defaults=[])


def _one_arg():
    a = _no_args()
    a.args = [ast.arg(arg=ARG, annotation=None)]
    return a


def find_target(old_tree, new_tree):
    """Locate the single changed expression.  Returns None when the change is not of that shape.

    Result: dict(old=node, new=node, parent=new parent, field=..., index=..., lines=(lo, hi) old)
    """
    w = _Walker()
    w.walk(old_tree, new_tree, [])
    if not w.div:
        return None
    # lowest common ancestor of every divergence
    common = w.div[0]
    for chain in w.div[1:]:
        k = 0
        while k < len(common) and k < len(chain) and common[k][0] is chain[k][0]:
            k += 1
        common = common[:k]
    if not common:
        return None
    # the change must sit inside one function body, in expression position
    scope_at = None
    for i, (o, n, _, _, _) in enumerate(common):
        if isinstance(o, (ast.ClassDef,) + FUNC_SCOPES):
            scope_at = i
    if scope_at is None or not isinstance(common[scope_at][0], FUNC_SCOPES):
        return None
    scope_old, scope_new, _, _, _ = common[scope_at]
    # a difference in the def line itself (arguments, decorators) runs outside the function
    if len(common) > scope_at + 1:
        first = common[scope_at + 1]
        if first[3] != "body":
            return None
    else:
        return None
    # innermost expression pair on the chain
    k = len(common) - 1
    while k > scope_at and not (isinstance(common[k][0], ast.expr) and isinstance(common[k][1], ast.expr)):
        k -= 1
    if k <= scope_at:
        return None
    # grow upwards through boolean / comparison operators
    while k - 1 > scope_at and _is_op(common[k - 1][0]) and _is_op(common[k - 1][1]):
        k -= 1
    # a Compare whose operator changed reports the Compare itself as the innermost pair
    o_t, n_t, parent, field, index = common[k]
    if parent is None or not isinstance(o_t, ast.expr):
        return None
    # no scope boundary or assignment expression between the function and the target
    for o, n, _, _, _ in common[scope_at + 1 : k + 1]:
        if isinstance(o, (ast.Lambda, ast.GeneratorExp, ast.ListComp, ast.SetComp, ast.DictComp, ast.NamedExpr, ast.Await, ast.Yield, ast.YieldFrom)):
            return None
    if getattr(o_t, "ctx", None) is not None and not isinstance(o_t.ctx, ast.Load):
        return None
    for sub in list(ast.walk(o_t)) + list(ast.walk(n_t)):
        if isinstance(sub, (ast.NamedExpr, ast.Await, ast.Yield, ast.YieldFrom)):
            return None
    return {"old": o_t, "new": n_t, "parent": parent, "field": field, "index": index}


def operand_codes(target):
    """Per operand: 0 same & has calls, 1 same & call-free, 2 different & call-free.  None if unusable."""
    ao, an = [], []
    _atoms(target["old"], ao)
    _atoms(target["new"], an)
    if len(ao) != len(an) or not ao or len(ao) > 64:
        return None
    codes = []
    for a, b in zip(ao, an):
        if ast.dump(a) == ast.dump(b):
            codes.append(1 if _pure(a) else 0)
        elif _pure(a) and _pure(b):
            codes.append(2)
        else:
            return None
    return ao, an, codes


def build(target):
    """The instrumented replacement for target["new"], or None when the operands do not line up."""
    o_t, n_t = target["old"], target["new"]
    res = operand_codes(target)
    if res is None:
        return None
    ao, an, codes = res
    new_expr = _Sub(an).visit(n_t)
    old_expr = _Sub(ao).visit(o_t)
    call = ast.Call(
        func=ast.Name(id=HELPER, ctx=ast.Load()),
        args=[
            ast.Lambda(args=_one_arg(), body=new_expr),
            ast.Lambda(args=_one_arg(), body=old_expr),
            ast.Constant(value=tuple(codes)),
        ],
        keywords=[],
    )
    return call


def instrument(old_src, new_src, filename):
    """Compile `new_src` with its one changed expression instrumented.  None if not applicable."""
    try:
        old_tree = ast.parse(old_src)
        new_tree = ast.parse(new_src)
    except Exception:
        return None
    target = find_target(old_tree, new_tree)
    if target is None:
        return None
    anchor = target["new"]
    call = build(target)
    if call is None:
        return None
    ast.copy_location(call, anchor)
    parent, field, index = target["parent"], target["field"], target["index"]
    if index is None:
        setattr(parent, field, call)
    else:
        getattr(parent, field)[index] = call
    ast.fix_missing_locations(new_tree)
    try:
        return compile(new_tree, filename, "exec", dont_inherit=True)
    except Exception:
        return None


def applicable(old_tree, new_tree):
    """Can the change between these two parsed modules be probed?  (Does not modify the trees.)"""
    try:
        target = find_target(old_tree, new_tree)
    except Exception:
        return False
    return target is not None and operand_codes(target) is not None


class _Unknown(Exception):
    pass


def _same(a, b):
    if a is b:
        return True
    try:
        return type(a) is type(b) and bool(a == b)
    except BaseException:
        return False


def _br_eval_(new_fn, old_fn, codes):
    STATS[0] += 1
    vals = {}

    def e_new(i, thunk):
        v = thunk()
        vals[i] = v
        return v

    try:
        new_val = new_fn(e_new)
    except BaseException:
        STATS[1] += 1
        raise
    asked = set()

    def e_old(i, thunk):
        asked.add(i)
        c = codes[i]
        if c != 2 and i in vals:
            return vals[i]
        if c:
            return thunk()
        raise _Unknown()

    try:
        old_val = old_fn(e_old)
    except BaseException:
        STATS[1] += 1
        return new_val
    if not _same(old_val, new_val):
        STATS[1] += 1
    else:
        for i in vals:
            if codes[i] == 0 and i not in asked:
                STATS[1] += 1
                break
    return new_val


def install():
    import builtins

    setattr(builtins, HELPER, _br_eval_)
