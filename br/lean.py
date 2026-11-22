"""Reading only the part of a module that a patch can have changed.

Most of a source file is functions the patch does not touch, and parsing or compiling them
again tells nothing: lines that both versions share give the same code in both.  When every
changed line lies inside the body of ONE function that sits directly in a module or class
body, two small texts stand in for the two versions of the file.  Every line keeps its number.

  - the small form: the module's import statements, a bare `class Name:` line for each class
    around the function, and the function itself; all other lines are empty.  (What a function
    compiles to depends on the module only through its imports - a call on an imported module
    is compiled differently from a method call - and through the names of the classes around
    it.)  Used for a function only if, at the pinned commit, it compiles to the very same code
    from the small text as from the file: tools/build_spans.py checks each one.
  - the blanked form, for the others in a large file: the bodies of all OTHER such functions
    are replaced by `pass`.

Whoever uses the two texts checks that they differ in function bodies only and goes back to
the whole files otherwise (br/plan.py).  The worker compiles the same two texts the planner
read when the patch keeps the number of lines: a function that only moves to other lines is a
new code object too, and for those the whole files are compiled as before.
"""
import os
import pickle
import zlib
from bisect import bisect_right

PATH = os.environ.get("BR_SPANS", "/app/spans.pkl")
ON = os.environ.get("BR_LEAN", "1") == "1"
MIN_BYTES = 20000  # the blanked form is for large files only
TINY = os.environ.get("BR_LEAN_TINY", "1") == "1"  # the small form, where the index allows it
_cache = [None, False]


def load():
    """{path: (size, crc32, heads, firsts, lasts, afters, indents, kept statements, classes
    around each function, functions the small form is right for)} or None."""
    if not _cache[1]:
        _cache[1] = True
        try:
            with open(PATH, "rb") as fh:
                _cache[0] = pickle.load(fh)
        except Exception:
            _cache[0] = None
    return _cache[0]


def _shared(a, b, start, limit):
    """Number of equal items of a[start:] and b[start:] from the front, at most `limit`."""
    n = 0
    step = 512
    while n < limit:
        k = min(step, limit - n)
        if a[start + n : start + n + k] == b[start + n : start + n + k]:
            n += k
            continue
        if k == 1:
            break
        step = max(1, k // 8)
    return n


def _shared_tail(a, b, limit):
    n = 0
    step = 512
    la, lb = len(a), len(b)
    while n < limit:
        k = min(step, limit - n)
        if a[la - n - k : la - n] == b[lb - n - k : lb - n]:
            n += k
            continue
        if k == 1:
            break
        step = max(1, k // 8)
    return n


def blank_all(data, entry):
    """`data` with every indexed body blanked (how the index is checked when it is built)."""
    firsts, lasts, indents = entry[3], entry[4], entry[6]
    lines = data.split(b"\n")
    for first, last, indent in zip(firsts, lasts, indents):
        lines[first - 1] = lines[first - 1][:indent] + b"pass"
        if last > first:
            lines[first:last] = [b""] * (last - first)
    return b"\n".join(lines)


def small_form(lines, entry, k, shift=0, top=None):
    """The small form of a file (given as its lines) around function k of its index.  `shift`:
    how many lines the function grew by in this version; statements after it sit that much
    lower.  None when a kept statement is not where the index says (it overlaps the change)."""
    heads, lasts, keep, chains = entry[2], entry[4], entry[7], entry[8]
    head, last = heads[k], lasts[k]
    out = [b""] * len(lines)
    for start, end in keep:
        if end < head:
            if top is not None and end > top:
                return None
            out[start - 1 : end] = lines[start - 1 : end]
        elif start > last:
            out[start - 1 + shift : end + shift] = lines[start - 1 + shift : end + shift]
        else:
            return None
    for line, indent, name in chains[k]:
        out[line - 1] = lines[line - 1][:indent] + b"class " + name + b":"
    out[head - 1 : last + shift] = lines[head - 1 : last + shift]
    return out


def reduce(rel, old, new):
    """(old text, new text, lines added) standing in for the two versions of a file, or None
    when the change is not confined to the body of one indexed function."""
    if not ON or old is None or new is None:
        return None
    index = load()
    entry = index.get(rel) if index else None
    if entry is None or len(entry) < 10:
        return None
    size, crc, heads, firsts, lasts, afters, indents = entry[:7]
    if size != len(old) or zlib.crc32(old) != crc:
        return None
    if b"\r" in new or b"\x0c" in new:
        return None
    ol, nl = old.split(b"\n"), new.split(b"\n")
    shortest = min(len(ol), len(nl))
    top = _shared(ol, nl, 0, shortest)  # lines the two share at the top ...
    if top < 2 or top == shortest and len(ol) == len(nl):
        return None  # (the first two lines can name an encoding)
    bottom = _shared_tail(ol, nl, shortest - top)  # ... and at the bottom
    a, b = top + 1, len(ol) - bottom  # the old lines in between (none when b < a)
    lo, hi = (a, b) if a <= b else (b, a)  # ... or the two lines an insertion sits between
    k = bisect_right(heads, lo) - 1
    if k < 0 or firsts[k] > lo or hi > lasts[k]:
        return None  # not inside one function body
    if k > 0 and afters[k - 1] > top:
        return None
    if k + 1 < len(heads) and heads[k + 1] <= b:
        return None
    shift = len(nl) - len(ol)
    if TINY and entry[9][k]:
        small_old = small_form(ol, entry, k, 0, top)
        small_new = small_form(nl, entry, k, shift, top) if small_old is not None else None
        if small_new is not None:
            return b"\n".join(small_old), b"\n".join(small_new), shift
    if len(old) < MIN_BYTES:
        return None
    blanked = 0
    for j in range(len(heads)):
        if j == k:
            continue
        first, last = firsts[j], lasts[j]
        if last <= first:
            continue
        indent = indents[j]
        text = ol[first - 1][:indent] + b"pass"
        empty = [b""] * (last - first)
        ol[first - 1] = text
        ol[first:last] = empty
        if j > k:
            first += shift
            last += shift
        nl[first - 1] = text
        nl[first:last] = empty
        blanked += len(empty)
    if blanked * 3 < len(ol):
        return None  # most of the file would still be read: not worth a second look
    return b"\n".join(ol), b"\n".join(nl), shift
