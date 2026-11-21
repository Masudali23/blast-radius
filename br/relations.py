"""Which tests can tell two comparison operators apart.

`a is b` and `a == b` give the same answer unless an object equals something it is not;
`a < b` and `a <= b` unless the two are equal.  While the maps are built, every comparison of
that kind in sqlglot is evaluated both ways each time it runs, and the tests in which the two
ever disagree are recorded per comparison (tools/trace_rel_one.py).

A patch that does nothing but swap such an operator for its partner can only change the
behaviour of those tests.  In any other test every evaluation of the comparison gives exactly
what it gave before the patch, so the patched run is, step by step, the unpatched run.
"""
import ast

# operator -> the operator it is evaluated against
PARTNER = {
    ast.Is: ast.Eq,
    ast.Eq: ast.Is,
    ast.IsNot: ast.NotEq,
    ast.NotEq: ast.IsNot,
    ast.Lt: ast.LtE,
    ast.LtE: ast.Lt,
    ast.Gt: ast.GtE,
    ast.GtE: ast.Gt,
}
NAMES = {ast.Is: "is", ast.Eq: "==", ast.IsNot: "is not", ast.NotEq: "!=", ast.Lt: "<", ast.LtE: "<=", ast.Gt: ">", ast.GtE: ">="}
HELPER = "_br_rel_"


def eligible(node):
    return isinstance(node, ast.Compare) and len(node.ops) == 1 and type(node.ops[0]) in PARTNER


def site_key(node):
    """Where a comparison sits in its (pinned) source file, and what it does."""
    return "%d:%d:%d:%d:%s" % (node.lineno, node.col_offset, node.end_lineno, node.end_col_offset, NAMES[type(node.ops[0])])


def sites(tree):
    """Keys of every comparison in a module that is evaluated both ways."""
    return sorted(site_key(n) for n in ast.walk(tree) if eligible(n))


class Instrument(ast.NodeTransformer):
    """Route each eligible comparison through HELPER(site, left, right, operator)."""

    def __init__(self, rel):
        self.rel = rel

    def visit_Compare(self, node):
        key = site_key(node) if eligible(node) else None
        self.generic_visit(node)
        if key is None:
            return node
        call = ast.Call(
            func=ast.Name(id=HELPER, ctx=ast.Load()),
            args=[ast.Constant(value=self.rel + "|" + key), node.left, node.comparators[0], ast.Constant(value=NAMES[type(node.ops[0])])],
            keywords=[],
        )
        return ast.copy_location(call, node)


def _plain(op, a, b):
    if op == "is":
        return a is b
    if op == "==":
        return a == b
    if op == "is not":
        return a is not b
    if op == "!=":
        return a != b
    if op == "<":
        return a < b
    if op == "<=":
        return a <= b
    if op == ">":
        return a > b
    return a >= b


_OTHER = {"is": "==", "==": "is", "is not": "!=", "!=": "is not", "<": "<=", "<=": "<", ">": ">=", ">=": ">"}


def make_helper(hits, active=None, seen=None):
    """The function HELPER is bound to while tracing; `hits` collects sites that disagreed,
    `seen` every site that was evaluated at all.

    `active` (a list shared with the condition recorder) holds the sites whose other operator
    is being evaluated right now.  Whatever that evaluation runs is recorded like anything
    else: it is what the patched program would run at that point, and anything it leaves
    behind (a cached hash, say) may keep the real program from running the same code later."""
    active = active if active is not None else []
    seen = seen if seen is not None else set()

    def helper(site, a, b, op):
        seen.add(site)
        if site in active:
            # The other operator of this very comparison is being evaluated, and evaluating
            # it came back to the comparison (an `is` inside its own __eq__, for instance).
            # In the patched program the inner evaluation would use the other operator again,
            # possibly without end: no record can stand for that.
            hits.add(site)
            return _plain(op, a, b)
        try:
            r = _plain(op, a, b)
        except BaseException:
            hits.add(site)  # the partner might not have raised
            raise
        active.append(site)
        try:
            alt = _plain(_OTHER[op], a, b)
        except BaseException:
            hits.add(site)
            return r
        finally:
            active.pop()
        # True and False are single objects, so "the same answer" is identity; anything else a
        # comparison returns (an array, NotImplemented, ...) counts as a disagreement
        if alt is not r or (r is not True and r is not False):
            hits.add(site)
        return r

    return helper


def swapped(old_tree, new_tree, same=None):
    """If two modules differ in exactly one comparison operator, swapped for its partner with
    both operands untouched, return the old comparison's site key; otherwise None.
    `same` may tell which pairs of statements are written identically (nothing to find there)."""
    found = []

    def walk(o, n):
        if len(found) > 1:
            return
        if type(o) is not type(n):
            found.append(None)
            return
        if same is not None and isinstance(o, ast.stmt) and same(o, n):
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
        if isinstance(o, ast.Compare) and len(o.ops) == 1 and len(n.ops) == 1 and type(o.ops[0]) is not type(n.ops[0]):
            if ast.dump(o.left) == ast.dump(n.left) and ast.dump(o.comparators[0]) == ast.dump(n.comparators[0]) and PARTNER.get(type(o.ops[0])) is type(n.ops[0]):
                found.append(o)
            else:
                found.append(None)
            return
        for f in o._fields:
            walk(getattr(o, f, None), getattr(n, f, None))

    walk(old_tree, new_tree)
    if len(found) == 1 and found[0] is not None:
        return site_key(found[0])
    return None
