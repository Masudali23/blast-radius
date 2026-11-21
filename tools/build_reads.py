"""Index where class / module level names are *read* in the pinned checkout.

Usage: build_reads.py <repo_root> <maps_dir>

A change to `NAME = ...` in a class or module body can only be observed by code that reads
NAME.  For every name bound at class or module level somewhere, record the lines that load it
(as a bare name, as an attribute, or as an exact string such as getattr(x, "NAME")).
Lines that merely assign the name again (a subclass overriding it) are not reads.
"""
import ast
import os
import re
import pickle
import sys
import zlib

root = os.path.abspath(sys.argv[1])
out_dir = os.path.abspath(sys.argv[2])
files = []
for sub in ("sqlglot", "tests"):
    for d, _, fs in os.walk(os.path.join(root, sub)):
        if "__pycache__" in d or "fixtures" in d:
            continue
        for f in sorted(fs):
            if f.endswith(".py"):
                files.append(os.path.relpath(os.path.join(d, f), root))
files.sort()
trees = {}
bound = set()
for rel in files:
    try:
        tree = ast.parse(open(os.path.join(root, rel), encoding="utf-8").read())
    except SyntaxError:
        continue
    trees[rel] = tree

    def visit(body):
        for st in body:
            targets = []
            if isinstance(st, ast.Assign):
                targets = st.targets
            elif isinstance(st, (ast.AnnAssign, ast.AugAssign)):
                targets = [st.target]
            elif isinstance(st, ast.ClassDef):
                visit(st.body)
            elif isinstance(st, (ast.If, ast.Try, ast.With)):
                for field in ("body", "orelse", "finalbody"):
                    visit(getattr(st, field, []) or [])
            for t in targets:
                for sub in ast.walk(t):
                    if isinstance(sub, ast.Name):
                        bound.add(sub.id)

    visit(tree.body)
reads = {}
for rel, tree in trees.items():
    for node in ast.walk(tree):
        name = None
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            name = node.id
        elif isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load):
            name = node.attr
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            name = node.value
        if name in bound:
            lines = reads.setdefault(name, {}).setdefault(rel, set())
            lines.update(range(node.lineno, (node.end_lineno or node.lineno) + 1))
# Names read outside any function body, i.e. while modules load and classes are built
# (`TIME_FORMAT = MySQL.TIME_FORMAT` in another class, a sibling statement of the same class
# body, a decorator argument, a default value).  A copy made there keeps the value for good,
# so the tests that later read the original definition say nothing about who sees a change.
outer_reads = set()
# The same reads with the class they go through (`exp.Select.arg_types` -> "Select", a bare
# name in the body of class C -> "C", anything unclear -> "*"), leaving out reads that sit in
# an assignment to the same name (`TRANSFORMS = {**Parent.TRANSFORMS, ...}`: lookups in such
# copies are recorded under that name anyway).  What is listed here feeds something stored
# under ANOTHER name, or is used in some other way while loading.
outer_recv = {}
# ... the same, leaving out reads that only walk a dict's keys (`for k in NAME`, `k in NAME`,
# `NAME.keys()`): what is built from the keys alone does not change when a value is edited
outer_recv_vals = {}
KEYS_ONLY = set()  # id() of the expressions in such positions


def keys_only_positions(tree):
    for node in ast.walk(tree):
        if isinstance(node, (ast.comprehension, ast.For, ast.AsyncFor)):
            KEYS_ONLY.add(id(node.iter))
        elif isinstance(node, ast.Compare) and all(isinstance(op, (ast.In, ast.NotIn)) for op in node.ops):
            for comparator in node.comparators:
                KEYS_ONLY.add(id(comparator))
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "keys" and not node.args and not node.keywords:
            KEYS_ONLY.add(id(node.func.value))
# class name -> names of the classes it inherits from (by name, over the whole package)
class_bases = {}
# Function-level lines that read a name off what looks like a class object rather than an
# instance, with what is known about that class:
#   "=R"  the class R, named outright (`exp.Select.NAME`)
#   "<K"  the class a classmethod of K was called on: K, or any subclass of K
#   "*"   unknown (`klass.NAME`, `Dialect[...].NAME`, getattr(x, "NAME"), ...)
# Such a read needs no instance, so "who creates an instance of the class" does not cover it.
classlike = {}
# NAME -> names stored on classes by a function that runs when a class is created (a
# metaclass's __new__/__init__, __init_subclass__) and reads NAME there: what those hold was
# worked out from NAME.  "*" when it cannot be told where the result goes.
derives = {}
class_names = set()
metaclasses = set()
for rel, tree in trees.items():
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            class_names.add(node.name)
            for b in node.bases:
                last = b.attr if isinstance(b, ast.Attribute) else b.id if isinstance(b, ast.Name) else None
                if isinstance(b, ast.Subscript):
                    v = b.value
                    last = v.attr if isinstance(v, ast.Attribute) else v.id if isinstance(v, ast.Name) else None
                if last:
                    class_bases.setdefault(node.name, set()).add(last)
                if last in ("type", "ABCMeta", "EnumMeta", "EnumType"):
                    metaclasses.add(node.name)
            for k in node.keywords:
                if k.arg == "metaclass":
                    v = k.value
                    metaclasses.add(v.attr if isinstance(v, ast.Attribute) else v.id if isinstance(v, ast.Name) else "*")
grew = True
while grew:  # a subclass of a metaclass is one too
    grew = False
    for name, bases in class_bases.items():
        if name not in metaclasses and bases & metaclasses:
            metaclasses.add(name)
            grew = True
CLASSY = re.compile(r"(^cls$|^klass$|cls$|class$|klass$|_type$|^type$|^[A-Z])")
# classmethods that were seen to hand back `cls(...)` every time they ran (tools/check_instantiating.py):
# a test that runs them creates an instance, which is recorded
instantiating = {}
_inst_path = os.path.join(out_dir, "instantiating.json")
if os.path.exists(_inst_path):
    import json

    instantiating = json.load(open(_inst_path))


def ends_in_instance(fn):
    """Is `fn`'s last statement `return <first parameter>(...)`, with no other way out?"""
    params = fn.args.posonlyargs + fn.args.args
    if not params or not fn.body:
        return False
    last = fn.body[-1]
    if not (isinstance(last, ast.Return) and isinstance(last.value, ast.Call) and isinstance(last.value.func, ast.Name) and last.value.func.id == params[0].arg):
        return False
    for sub in ast.walk(fn):
        if sub is not last and isinstance(sub, (ast.Return, ast.Yield, ast.YieldFrom, ast.Try, ast.Raise)):
            return False
    return True


def stores(fn):
    """Attribute names a function stores on anything ("*" if one cannot be read off)."""
    out = set()
    for sub in ast.walk(fn):
        if isinstance(sub, ast.Attribute) and isinstance(sub.ctx, (ast.Store, ast.Del)):
            out.add(sub.attr)
        elif isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name) and sub.func.id in ("setattr", "delattr"):
            if len(sub.args) > 1 and isinstance(sub.args[1], ast.Constant) and isinstance(sub.args[1].value, str):
                out.add(sub.args[1].value)
            else:
                out.add("*")
        elif isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute) and sub.func.attr in ("__setattr__", "__delattr__"):
            named = [a.value for a in sub.args[:2] if isinstance(a, ast.Constant) and isinstance(a.value, str)]
            out.update(named or ["*"])  # type.__setattr__(cls, "NAME", ...)
        elif isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute) and sub.func.attr == "update" and isinstance(sub.func.value, ast.Call):
            out.add("*")  # vars(cls).update(...)
    return out


class Fn:
    """What is known about the function a read sits in."""

    __slots__ = ("first", "kind", "owner", "stored", "stmt")

    def __init__(self, node, owner, rel):
        params = node.args.posonlyargs + node.args.args
        self.first = params[0].arg if params else None
        self.owner = owner
        self.stored = None
        self.stmt = None  # names the statement being walked stores, when that is all it does
        decorated = {d.id if isinstance(d, ast.Name) else d.attr if isinstance(d, ast.Attribute) else "" for d in node.decorator_list}
        if owner is None:
            self.kind = "plain"
        elif node.name == "__init_subclass__" or (owner in metaclasses and node.name in ("__new__", "__init__")):
            self.kind = "build"  # runs when a class is created
            self.stored = stores(node)
        elif owner in metaclasses and "staticmethod" not in decorated and "classmethod" not in decorated:
            self.kind = "meta"  # first parameter: any class made by this metaclass
        elif "classmethod" in decorated or node.name in ("__class_getitem__", "__new__"):
            self.kind = "made" if ends_in_instance(node) and instantiating.get("%s:%d" % (rel, node.lineno)) == node.name else "cls"
        elif "staticmethod" in decorated:
            self.kind = "static"
        else:
            self.kind = "plain"


def plain_stores(st):
    """Names stored by a statement that only assigns attributes (`cls.A, cls.B = ...`), else None."""
    if isinstance(st, ast.Assign):
        targets = st.targets
    elif isinstance(st, (ast.AnnAssign, ast.AugAssign)):
        targets = [st.target]
    else:
        return None
    out = set()
    for t in targets:
        for el in t.elts if isinstance(t, (ast.Tuple, ast.List)) else [t]:
            if not isinstance(el, ast.Attribute):
                return None
            out.add(el.attr)
    for sub in ast.walk(st):
        if isinstance(sub, (ast.NamedExpr, ast.Lambda)):
            return None
        if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name) and sub.func.id in ("setattr", "delattr"):
            return None
    return out


def receiver(value, fn):
    """(what the class an attribute is read through is known to be, does it look like a class
    object at all)."""
    if isinstance(value, ast.Name):
        if fn is not None and value.id == fn.first:
            if fn.kind == "cls":
                return "<" + fn.owner, True
            if fn.kind == "made":
                return None, False  # goes on to create an instance of it
            if fn.kind == "meta":
                return "*", True
            if fn.kind == "plain" and fn.owner is not None:
                return None, False  # self
        if value.id in class_names:
            return "=" + value.id, True
        return "*", bool(CLASSY.search(value.id))
    if isinstance(value, ast.Attribute):
        if value.attr in class_names:
            return "=" + value.attr, True
        return "*", bool(CLASSY.search(value.attr))
    if isinstance(value, ast.Call) and isinstance(value.func, ast.Name) and value.func.id == "type" and len(value.args) == 1:
        return None, False  # the class of something that exists
    return "*", True  # a call or subscript: Dialect["name"], get_class(), ...


def outside_functions(node, rel, inside=False, cls=None, target=None, fn=None, own=False):
    """`cls`: the class whose body we are in (for bare names read there).  `fn`: the innermost
    function that is a class's own method, kept for whatever is nested in it."""
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        # decorators, defaults and annotations run where the def statement runs
        for sub in node.decorator_list + node.args.defaults + [d for d in node.args.kw_defaults if d is not None]:
            outside_functions(sub, rel, inside, cls, target, fn)
        if own or fn is None:
            inner = Fn(node, cls if own else None, rel)
            for sub in node.body:
                inner.stmt = plain_stores(sub) if inner.kind == "build" else None
                outside_functions(sub, rel, True, None, None, inner)
            inner.stmt = None
        else:
            for sub in node.body:
                outside_functions(sub, rel, True, None, None, fn)
        return
    if isinstance(node, ast.Lambda):
        for sub in node.args.defaults + [d for d in node.args.kw_defaults if d is not None]:
            outside_functions(sub, rel, inside, cls, target, fn)
        outside_functions(node.body, rel, True, None, None, fn)
        return
    if isinstance(node, ast.ClassDef):
        # a class body runs when its statement runs: at load time unless inside a function
        for sub in node.decorator_list + node.bases + [k.value for k in node.keywords]:
            outside_functions(sub, rel, inside, cls, target, fn)
        for sub in node.body:
            outside_functions(sub, rel, inside, node.name, None, fn, own=True)
        return
    if isinstance(node, (ast.Assign, ast.AnnAssign)):
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        target = targets[0].id if len(targets) == 1 and isinstance(targets[0], ast.Name) else None
    name = recv = None
    classy = False
    if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
        name, recv = node.id, (cls or "*")
    elif isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load):
        name = node.attr
        recv, classy = receiver(node.value, fn)
    elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in ("getattr", "hasattr", "setattr", "delattr"):
        if len(node.args) > 1 and isinstance(node.args[1], ast.Constant) and isinstance(node.args[1].value, str):
            name = node.args[1].value
            recv, classy = receiver(node.args[0], fn)
    elif isinstance(node, ast.Constant) and isinstance(node.value, str) and inside and fn is not None and fn.kind == "build":
        name, recv, classy = node.value, "*", True  # attrs["NAME"], cls.__dict__.get("NAME")
    if name is not None and not inside:
        outer_reads.add(name)
        if name != target:
            r = recv if recv is not None else "*"
            r = r[1:] if r[:1] in "=<" and len(r) > 1 else r
            outer_recv.setdefault(name, set()).add(r)
            if id(node) not in KEYS_ONLY:
                outer_recv_vals.setdefault(name, set()).add(r)
    elif name is not None and classy:
        if fn is not None and fn.kind == "build":
            # what this statement stores was worked out from `name`; when the read is not in a
            # plain assignment, anything the function stores may have been
            derives.setdefault(name, set()).update(fn.stmt if fn.stmt is not None else fn.stored)
        else:
            lines = classlike.setdefault(name, {}).setdefault(rel, {})
            for line in range(node.lineno, (node.end_lineno or node.lineno) + 1):
                lines.setdefault(line, set()).add(recv)
    for sub in ast.iter_child_nodes(node):
        outside_functions(sub, rel, inside, cls, target, fn)


for rel, tree in trees.items():
    keys_only_positions(tree)
    outside_functions(tree, rel)


def ancestors(name, seen=None):
    seen = seen if seen is not None else set()
    for b in class_bases.get(name, ()):
        if b not in seen:
            seen.add(b)
            ancestors(b, seen)
    return seen


# names read inside metaclass methods and __init_subclass__: whatever those derive from a class
# attribute at class creation cannot be observed afterwards
meta_reads = set()
for rel, tree in trees.items():
    for node in ast.walk(tree):
        fns = []
        if isinstance(node, ast.ClassDef):
            is_meta = any((isinstance(b, ast.Name) and b.id == "type") or (isinstance(b, ast.Attribute) and b.attr in ("ABCMeta", "EnumMeta")) or (isinstance(b, ast.Name) and b.id.startswith("_") and b.id[1:2].isupper() and b.id != node.name and False) for b in node.bases)
            for sub in node.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)) and (is_meta or sub.name == "__init_subclass__"):
                    fns.append(sub)
        for fn in fns:
            for sub in ast.walk(fn):
                if isinstance(sub, ast.Attribute):
                    meta_reads.add(sub.attr)
                elif isinstance(sub, ast.Name):
                    meta_reads.add(sub.id)
                elif isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                    meta_reads.add(sub.value)
packed = {n: {f: sorted(ls) for f, ls in per.items()} for n, per in reads.items()}
blob = zlib.compress(pickle.dumps({"bound": sorted(bound), "reads": packed, "meta_reads": sorted(meta_reads & bound), "outer_reads": sorted(outer_reads & bound), "outer_recv": {n: sorted(r) for n, r in outer_recv.items() if n in bound}, "outer_recv_vals": {n: sorted(outer_recv_vals.get(n, ())) for n in outer_recv if n in bound}, "class_anc": {c: sorted(ancestors(c)) for c in class_names}, "classlike": {n: {f: {line: sorted(r) for line, r in ls.items()} for f, ls in per.items()} for n, per in classlike.items() if n in bound}, "derives": {n: sorted(d - {n}) for n, d in derives.items() if n in bound}}, protocol=5), 6)
open(os.path.join(out_dir, "reads.pkl"), "wb").write(blob)
print("names bound at class/module level:", len(bound), "with reads:", len(packed), "read by metaclasses:", len(meta_reads & bound), "read while loading:", len(outer_reads & bound), "bytes", len(blob))
