"""Generate synthetic patches against the pinned sqlglot checkout for local validation.

Usage: gen_mutants.py <repo_root> <out_dir> <count> <seed> [kinds...]
Kinds: code comment fixture multi exotic testedit
These are only used to measure the selector locally; nothing derived from them ships.
"""
import ast
import difflib
import io
import json
import os
import random
import re
import sys
import tokenize

root = os.path.abspath(sys.argv[1])
out_dir = os.path.abspath(sys.argv[2])
count = int(sys.argv[3])
seed = int(sys.argv[4])
kinds = sys.argv[5:] or ["code", "comment", "fixture", "multi"]
rng = random.Random(seed)
# pick the file of a code mutant uniformly (as the kit's samples appear to) rather than by size
UNIFORM = os.environ.get("BR_GEN_UNIFORM") == "1"
os.makedirs(out_dir, exist_ok=True)

COMMENTS = [
    "Cheap check first",
    "Kept for backwards compatibility",
    "Keep in sync with the generator",
    "Intentionally not cached",
    "Mirrors the behavior of the reference implementation",
    "Order matters here",
    "See the dialect docs for details",
]


def walk(sub, exts):
    res = []
    for d, _, fs in os.walk(os.path.join(root, sub)):
        if "__pycache__" in d:
            continue
        for f in fs:
            if f.endswith(exts):
                res.append(os.path.relpath(os.path.join(d, f), root))
    return sorted(res)


SRC = walk("sqlglot", (".py",))
FIX = walk("tests/fixtures", (".sql",))
TESTPY = [f for f in walk("tests", (".py",))]
_cache = {}


def read(rel):
    if rel not in _cache:
        _cache[rel] = open(os.path.join(root, rel), encoding="utf-8").read()
    return _cache[rel]


def offsets(src):
    offs = [0]
    for line in src.splitlines(keepends=True):
        offs.append(offs[-1] + len(line.encode("utf-8")))
    return offs


def span(src_b, offs, node):
    return offs[node.lineno - 1] + node.col_offset, offs[node.end_lineno - 1] + node.end_col_offset


CMP = {
    ast.Is: [" == "], ast.IsNot: [" != "], ast.Eq: [" != "], ast.NotEq: [" == "],
    ast.Lt: [" <= ", " > "], ast.LtE: [" < "], ast.Gt: [" >= ", " < "], ast.GtE: [" > "],
    ast.In: [" not in "], ast.NotIn: [" in "],
}


def code_sites(rel, exotic=False):
    src = read(rel)
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return []
    b = src.encode("utf-8")
    offs = offsets(src)
    sites = []  # (start, end, replacement, op)
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare) and len(node.ops) == 1 and type(node.ops[0]) in CMP:
            s = span(b, offs, node.left)[1]
            e = span(b, offs, node.comparators[0])[0]
            mid = b[s:e].decode()
            if "\n" in mid or "#" in mid or "(" in mid or ")" in mid:
                continue
            for rep in CMP[type(node.ops[0])]:
                sites.append((s, e, rep, "cmp"))
        elif isinstance(node, ast.BoolOp):
            for a, c in zip(node.values, node.values[1:]):
                s = span(b, offs, a)[1]
                e = span(b, offs, c)[0]
                mid = b[s:e].decode()
                if "#" in mid or "(" in mid or ")" in mid:
                    continue
                old = "and" if isinstance(node.op, ast.And) else "or"
                new = "or" if old == "and" else "and"
                if re.search(r"\b%s\b" % old, mid):
                    sites.append((s, e, re.sub(r"\b%s\b" % old, new, mid, count=1), "bool"))
        elif isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            s, e = span(b, offs, node)
            inner = span(b, offs, node.operand)
            sites.append((s, inner[0], "", "not"))
        elif isinstance(node, ast.Constant):
            s, e = span(b, offs, node)
            v = node.value
            if v is True:
                sites.append((s, e, "False", "const"))
            elif v is False:
                sites.append((s, e, "True", "const"))
            elif isinstance(v, int) and not isinstance(v, bool) and b[s:e].isdigit():
                sites.append((s, e, str(v + 1), "const"))
                if v > 0:
                    sites.append((s, e, str(v - 1), "const"))
            elif exotic and isinstance(v, str) and 0 < len(v) < 30 and b[s:e][:1] in (b'"', b"'") and "\n" not in b[s:e].decode():
                txt = b[s:e].decode()
                sites.append((s, e, txt[0] + "X" + txt[1:], "str"))
        elif exotic and isinstance(node, ast.BinOp) and type(node.op) in (ast.Add, ast.Sub):
            s = span(b, offs, node.left)[1]
            e = span(b, offs, node.right)[0]
            mid = b[s:e].decode()
            if mid.strip() in ("+", "-"):
                sites.append((s, e, mid.replace("+", "-") if "+" in mid else mid.replace("-", "+"), "arith"))
        elif exotic and isinstance(node, ast.Return) and node.value is not None and not isinstance(node.value, ast.Constant):
            s, e = span(b, offs, node.value)
            sites.append((s, e, "None", "retnone"))
    return [(rel, s, e, r, op) for s, e, r, op in sites]


def apply_code(rel, s, e, rep):
    b = read(rel).encode("utf-8")
    return (b[:s] + rep.encode() + b[e:]).decode("utf-8")


def mutate_code(exotic=False, by_line=False):
    for _ in range(50):
        rel = rng.choice(SRC)
        if by_line:
            # weight files by size
            rel = rng.choices(SRC, weights=[len(read(f)) for f in SRC])[0]
        sites = code_sites(rel, exotic)
        if exotic:
            sites = [x for x in sites if x[4] in ("str", "arith", "retnone")] or sites
        if not sites:
            continue
        # choose a line first, then a site on that line, to mimic line-based selection
        rel, s, e, rep, op = rng.choice(sites)
        new = apply_code(rel, s, e, rep)
        try:
            ast.parse(new)
        except SyntaxError:
            continue
        return {rel: new}, op
    raise RuntimeError("no site")


def class_level_sites(rel):
    """Constant sites that sit directly in a class body (not inside any function or lambda)."""
    src = read(rel)
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return []
    b = src.encode("utf-8")
    offs = offsets(src)
    sites = []

    def visit(node, in_class, in_func):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                visit(child, in_class, True)
            elif isinstance(child, ast.ClassDef):
                visit(child, True, False)
            else:
                if in_class and not in_func and isinstance(child, ast.Constant):
                    s_, e_ = span(b, offs, child)
                    v = child.value
                    txt = b[s_:e_].decode()
                    if v is True:
                        sites.append((rel, s_, e_, "False", "cls-const"))
                    elif v is False:
                        sites.append((rel, s_, e_, "True", "cls-const"))
                    elif isinstance(v, int) and txt.isdigit():
                        sites.append((rel, s_, e_, str(v + 1), "cls-const"))
                    elif isinstance(v, str) and 0 < len(v) < 30 and txt[:1] in "\"'" and "\n" not in txt and len(txt) == len(v) + 2:
                        sites.append((rel, s_, e_, txt[0] + "X" + txt[1:], "cls-str"))
                visit(child, in_class, in_func)

    visit(tree, False, False)
    return sites


def mutate_class_level():
    for _ in range(100):
        rel = rng.choices(SRC, weights=[len(read(f)) ** 0.5 for f in SRC])[0]
        sites = class_level_sites(rel)
        if not sites:
            continue
        rel, s_, e_, rep, op = rng.choice(sites)
        new = apply_code(rel, s_, e_, rep)
        try:
            ast.parse(new)
        except SyntaxError:
            continue
        return {rel: new}, op
    raise RuntimeError


def mutate_delete_stmt():
    for _ in range(50):
        rel = rng.choice(SRC)
        src = read(rel)
        tree = ast.parse(src)
        cands = []
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for st in ast.walk(node):
                    if isinstance(st, (ast.Assign, ast.Expr, ast.AugAssign)) and st.lineno == st.end_lineno:
                        cands.append(st)
        if not cands:
            continue
        st = rng.choice(cands)
        lines = src.splitlines(keepends=True)
        line = lines[st.lineno - 1]
        indent = line[: len(line) - len(line.lstrip())]
        lines[st.lineno - 1] = indent + "pass\n"
        new = "".join(lines)
        try:
            ast.parse(new)
        except SyntaxError:
            continue
        return {rel: new}, "delstmt"
    raise RuntimeError


def mutate_comment(files=None):
    lines = []
    while not lines:  # (an empty __init__.py has nowhere to put a comment)
        rel = rng.choice(files or SRC)
        src = read(rel)
        lines = src.splitlines(keepends=True)
    for _ in range(100):
        i = rng.randrange(len(lines))
        line = lines[i]
        if not line.strip() or line.lstrip().startswith(("#", '"""', "'''", ")", "]", "}")):
            continue
        indent = line[: len(line) - len(line.lstrip())]
        new_lines = lines[:i] + [indent + "# " + rng.choice(COMMENTS) + "\n"] + lines[i:]
        new = "".join(new_lines)
        if rel.endswith(".py"):
            try:
                if ast.dump(ast.parse(new)) != ast.dump(ast.parse(src)):
                    continue  # landed inside a string
            except SyntaxError:
                continue
        return {rel: new}, "comment"
    raise RuntimeError


SQLTOK = re.compile(r"[A-Za-z_][A-Za-z_0-9]*|\d+|<>|<=|>=|[<>=(),.*+\-/]")


def mutate_fixture(allow_comment=False):
    for _ in range(100):
        rel = rng.choice(FIX)
        src = read(rel)
        lines = src.splitlines(keepends=True)
        i = rng.randrange(len(lines))
        line = lines[i]
        body = line.rstrip("\n")
        if not body.strip():
            continue
        is_comment = body.lstrip().startswith(("--", "#"))
        if is_comment and not allow_comment:
            continue
        toks = list(SQLTOK.finditer(body))
        if not toks:
            continue
        m = rng.choice(toks)
        how = rng.choice(["insert", "insert", "delete", "suffix", "prefix"])
        if how == "insert":
            ins = rng.choice(["42", "0", "1", "x", "NULL"])
            nb = body[: m.start()] + ins + " " + body[m.start():]
        elif how == "delete":
            nb = body[: m.start()] + body[m.end():]
        elif how == "suffix":
            nb = body[: m.end()] + rng.choice(["42", "a", "x"]) + " " + body[m.end():]
        else:
            nb = body[: m.start()] + rng.choice(["x", "a"]) + body[m.start():]
        if nb == body:
            continue
        lines[i] = nb + ("\n" if line.endswith("\n") else "")
        return {rel: "".join(lines)}, "fixture"
    raise RuntimeError


def mutate_testedit():
    """Edit a string literal inside a test function (a kind the samples do not show)."""
    for _ in range(100):
        rel = rng.choice([f for f in TESTPY if os.path.basename(f).startswith("test_") or f.endswith("helpers.py")])
        sites = [x for x in code_sites(rel, exotic=True) if x[4] in ("str", "const", "cmp")]
        if not sites:
            continue
        rel, s, e, rep, op = rng.choice(sites)
        new = apply_code(rel, s, e, rep)
        try:
            ast.parse(new)
        except SyntaxError:
            continue
        return {rel: new}, "testedit-" + op
    raise RuntimeError


def make_patch(changes):
    out = []
    for rel in sorted(changes):
        old = read(rel).splitlines(keepends=True)
        new = changes[rel].splitlines(keepends=True)
        diff = list(difflib.unified_diff(old, new, "a/" + rel, "b/" + rel, n=3))
        if not diff:
            continue
        out.append("diff --git a/%s b/%s\n" % (rel, rel))
        for line in diff:
            out.append(line if line.endswith("\n") else line + "\n\\ No newline at end of file\n")
    return "".join(out)


def one(kind):
    if kind == "code":
        ch, op = mutate_code(by_line=(not UNIFORM) and rng.random() < 0.5)
    elif kind == "comment":
        ch, op = mutate_comment(FIX if rng.random() < 0.15 else None)
    elif kind == "fixture":
        ch, op = mutate_fixture(allow_comment=rng.random() < 0.1)
    elif kind == "exotic":
        ch, op = mutate_delete_stmt() if rng.random() < 0.3 else mutate_code(exotic=True)
    elif kind == "testedit":
        ch, op = mutate_testedit()
    elif kind == "classlevel":
        ch, op = mutate_class_level()
    elif kind == "multi":
        ch, ops = {}, []
        for _ in range(rng.choice([2, 3, 3])):
            sub = rng.choice(["code", "comment", "comment", "fixture"])
            c, o = one_raw(sub)
            for k, v in c.items():
                if k not in ch:
                    ch[k] = v
            ops.append(o)
        op = "+".join(ops)
    return ch, op


def one_raw(kind):
    return one(kind)


alphabet = "23456789abcdefghjkmnpqrstuvwxyz"
made = 0
meta = {}
while made < count:
    kind = kinds[made % len(kinds)]
    try:
        ch, op = one(kind)
    except RuntimeError:
        continue
    patch = make_patch(ch)
    if not patch:
        continue
    cid = "m" + "".join(rng.choice(alphabet) for _ in range(5))
    with open(os.path.join(out_dir, cid + ".patch"), "w", encoding="utf-8") as fh:
        fh.write(patch)
    meta[cid] = {"kind": kind, "op": op, "files": sorted(ch)}
    made += 1
mp = os.path.join(out_dir, "_meta.json")
old = json.load(open(mp)) if os.path.exists(mp) else {}
old.update(meta)
json.dump(old, open(mp, "w"), indent=0)
print("generated", made)
