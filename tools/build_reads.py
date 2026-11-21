"""Index where class / module level names are *read* in the pinned checkout.

Usage: build_reads.py <repo_root> <maps_dir>

A change to `NAME = ...` in a class or module body can only be observed by code that reads
NAME.  For every name bound at class or module level somewhere, record the lines that load it
(as a bare name, as an attribute, or as an exact string such as getattr(x, "NAME")).
Lines that merely assign the name again (a subclass overriding it) are not reads.
"""
import ast
import os
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
blob = zlib.compress(pickle.dumps({"bound": sorted(bound), "reads": packed, "meta_reads": sorted(meta_reads & bound)}, protocol=5), 6)
open(os.path.join(out_dir, "reads.pkl"), "wb").write(blob)
print("names bound at class/module level:", len(bound), "with reads:", len(packed), "read by metaclasses:", len(meta_reads & bound), "bytes", len(blob))
