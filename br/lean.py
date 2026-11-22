"""Reading only the part of a large module that a patch can have changed.

Most of a big source file is functions the patch does not touch, and parsing or compiling
them again tells nothing: lines that both versions share give the same code in both.  When
every changed line lies inside the body of ONE function, the bodies of all the other
functions are replaced by `pass` in both versions (each on its own lines, so every line keeps
its number) and the two small texts stand in for the files.

The index of function bodies comes from the pinned commit (tools/build_spans.py).  It lists
only functions that sit directly in a module or class body, with a body that starts on a line
of its own, so blanking one never changes how any other line is read.  Whoever uses the
reduced texts checks that they differ in function bodies only and goes back to the whole
files otherwise (br/plan.py).  The worker compiles the same two texts the planner read when
the patch keeps the number of lines: a function that only moves to other lines is a new code
object too, and for those the whole files are compiled as before.
"""
import os
import pickle
import zlib
from bisect import bisect_right

PATH = os.environ.get("BR_SPANS", "/app/spans.pkl")
ON = os.environ.get("BR_LEAN", "1") == "1"
MIN_BYTES = 20000  # smaller files are read whole: nothing to gain
_cache = [None, False]


def load():
    """{path: (size, crc32, heads, firsts, lasts, afters, indents)} or None."""
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
    _, _, _, firsts, lasts, _, indents = entry
    lines = data.split(b"\n")
    for first, last, indent in zip(firsts, lasts, indents):
        lines[first - 1] = lines[first - 1][:indent] + b"pass"
        if last > first:
            lines[first:last] = [b""] * (last - first)
    return b"\n".join(lines)


def reduce(rel, old, new):
    """(old text, new text, lines added) with the bodies of the functions the patch leaves
    alone blanked, or None when the change is not confined to one indexed function body."""
    if not ON or old is None or new is None or len(old) < MIN_BYTES:
        return None
    index = load()
    entry = index.get(rel) if index else None
    if entry is None:
        return None
    size, crc, heads, firsts, lasts, afters, indents = entry
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
