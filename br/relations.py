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


def make_helper(hits, busy=None):
    """The function HELPER is bound to while tracing; `hits` collects sites that disagreed.
    `busy` (a one-element list) is non-zero while code runs that the real program would not."""
    busy = busy if busy is not None else [0]

    def helper(site, a, b, op):
        if busy[0]:
            # we are inside the other way of some comparison: not a step of the real run
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
        try:
            if op == "is":
                r = a is b
            elif op == "==":
                r = a == b
            elif op == "is not":
                r = a is not b
            elif op == "!=":
                r = a != b
            elif op == "<":
                r = a < b
            elif op == "<=":
                r = a <= b
            elif op == ">":
                r = a > b
            else:
                r = a >= b
        except BaseException:
            hits.add(site)  # the partner might not have raised
            raise
        busy[0] += 1
        try:
            if op == "is":
                alt = a == b
            elif op == "==":
                alt = a is b
            elif op == "is not":
                alt = a != b
            elif op == "!=":
                alt = a is not b
            elif op == "<":
                alt = a <= b
            elif op == "<=":
                alt = a < b
            elif op == ">":
                alt = a >= b
            else:
                alt = a > b
        except BaseException:
            hits.add(site)
            return r
        finally:
            busy[0] -= 1
        # True and False are single objects, so "the same answer" is identity; anything else a
        # comparison returns (an array, NotImplemented, ...) counts as a disagreement
        if alt is not r or (r is not True and r is not False):
            hits.add(site)
        return r

    return helper


def swapped(old_tree, new_tree):
    """If two modules differ in exactly one comparison operator, swapped for its partner with
    both operands untouched, return the old comparison's site key; otherwise None."""
    found = []

    def walk(o, n):
        if len(found) > 1:
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
