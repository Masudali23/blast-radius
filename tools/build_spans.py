"""Index the function bodies of the pinned checkout's larger modules (see br/lean.py).

usage: build_spans.py <repo> <out.pkl>

For each file: its size and checksum, and for every function that sits directly in a module
or class body and whose body starts on a line of its own: the first line of the function
(decorators included), the first and last line of its body, the first line of code after it
and the indentation of the body.

A file gets an index only if blanking every indexed body gives a module that parses to the
same tree as the file with those bodies replaced by `pass`, every other node starting where
it started before.
"""
import ast
import os
import pickle
import sys
import zlib

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from br import lean  # noqa: E402

FUNCS = (ast.FunctionDef, ast.AsyncFunctionDef)


def index(data):
    if b"\r" in data or b"\x0c" in data:
        return None
    try:
        tree = ast.parse(data.decode("utf-8"))
    except (SyntaxError, ValueError, UnicodeDecodeError, RecursionError):
        return None
    lines = data.split(b"\n")
    rows = []
    nodes = []

    def visit(body):
        for st in body:
            if isinstance(st, ast.ClassDef):
                visit(st.body)
            elif isinstance(st, FUNCS):
                b0 = st.body[0]
                first = min([b0.lineno] + [d.lineno for d in getattr(b0, "decorator_list", ())])
                last = st.end_lineno
                head = min([st.lineno] + [d.lineno for d in st.decorator_list])
                line = lines[first - 1]
                indent = len(line) - len(line.lstrip(b" \t"))
                if indent == 0 or first <= st.lineno or last < first:
                    continue
                if not getattr(b0, "decorator_list", None) and b0.col_offset != indent:
                    continue  # the body starts on the line the header ends on
                after = last + 1
                while after <= len(lines) and (not lines[after - 1].strip() or lines[after - 1].lstrip().startswith(b"#")):
                    after += 1
                rows.append((head, first, last, after, indent))
                nodes.append(st)

    visit(tree.body)
    if not rows:
        return None
    for (h, f, l, a, _), nxt in zip(rows, rows[1:]):
        if not (h < f <= l < a <= nxt[0]):
            return None
    entry = (len(data), zlib.crc32(data)) + tuple(tuple(r[i] for r in rows) for i in range(5))
    # check: the blanked text is the same module with `pass` for those bodies
    try:
        small = ast.parse(lean.blank_all(data, entry).decode("utf-8"))
    except (SyntaxError, ValueError, RecursionError):
        return None
    for st, row in zip(nodes, rows):
        st.body = [ast.Pass(lineno=row[1], col_offset=row[4])]
    if not _alike(tree, small):
        return None
    return entry


def _alike(a, b):
    """The same tree, every node starting at the same place."""
    stack = [(a, b)]
    while stack:
        x, y = stack.pop()
        if type(x) is not type(y):
            return False
        if isinstance(x, list):
            if len(x) != len(y):
                return False
            stack.extend(zip(x, y))
        elif isinstance(x, ast.AST):
            if getattr(x, "lineno", None) != getattr(y, "lineno", None) or getattr(x, "col_offset", None) != getattr(y, "col_offset", None):
                return False
            for f in x._fields:
                stack.append((getattr(x, f, None), getattr(y, f, None)))
        elif x != y:
            return False
    return True


def main():
    repo, out = sys.argv[1], sys.argv[2]
    table = {}
    for top in ("sqlglot", "tests"):
        for d, _, files in os.walk(os.path.join(repo, top)):
            for f in sorted(files):
                path = os.path.join(d, f)
                if not f.endswith(".py") or os.path.getsize(path) < lean.MIN_BYTES:
                    continue
                with open(path, "rb") as fh:
                    data = fh.read()
                entry = index(data)
                if entry is not None:
                    table[os.path.relpath(path, repo)] = entry
    with open(out, "wb") as fh:
        pickle.dump(table, fh, protocol=pickle.HIGHEST_PROTOCOL)
    print("indexed %d files, %d functions" % (len(table), sum(len(e[2]) for e in table.values())), file=sys.stderr)


if __name__ == "__main__":
    main()
