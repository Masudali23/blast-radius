"""Minimal, strict unified-diff reader and applier (no git or patch binary at run time)."""
import re

_HUNK = re.compile(rb"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


class FileChange:
    __slots__ = ("old_path", "new_path", "hunks", "binary", "new_file", "deleted", "renamed", "mode_only")

    def __init__(self):
        self.old_path = None
        self.new_path = None
        self.hunks = []  # (old_start, old_len, new_start, new_len, lines)
        self.binary = False
        self.new_file = False
        self.deleted = False
        self.renamed = False
        self.mode_only = False

    @property
    def path(self):
        return self.new_path if (self.new_path and not self.deleted) else self.old_path


def _strip(p):
    p = p.strip()
    if p.startswith(b'"') and p.endswith(b'"'):
        p = p[1:-1].decode("unicode_escape").encode("latin-1")
    # drop a trailing tab-separated timestamp
    p = p.split(b"\t")[0]
    if p == b"/dev/null":
        return None
    if p.startswith((b"a/", b"b/")):
        p = p[2:]
    return p.decode("utf-8", "surrogateescape")


def parse(data):
    """Return a list of FileChange. Raises ValueError on anything we cannot read."""
    lines = data.split(b"\n")
    out = []
    cur = None
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        if line.startswith(b"diff --git "):
            cur = FileChange()
            out.append(cur)
            m = re.match(rb'^diff --git "?a/(.*?)"? "?b/(.*?)"?$', line)
            if m:
                cur.old_path = m.group(1).decode("utf-8", "surrogateescape")
                cur.new_path = m.group(2).decode("utf-8", "surrogateescape")
            i += 1
            continue
        if cur is None and line.startswith(b"--- ") and i + 1 < n and lines[i + 1].startswith(b"+++ "):
            cur = FileChange()
            out.append(cur)
        if cur is None:
            i += 1
            continue
        if line.startswith(b"new file mode"):
            cur.new_file = True
        elif line.startswith(b"deleted file mode"):
            cur.deleted = True
        elif line.startswith(b"rename from "):
            cur.renamed = True
            cur.old_path = line[len(b"rename from "):].decode("utf-8", "surrogateescape")
        elif line.startswith(b"rename to "):
            cur.renamed = True
            cur.new_path = line[len(b"rename to "):].decode("utf-8", "surrogateescape")
        elif line.startswith(b"copy from ") or line.startswith(b"copy to "):
            cur.renamed = True
            if line.startswith(b"copy to "):
                cur.new_path = line[len(b"copy to "):].decode("utf-8", "surrogateescape")
        elif line.startswith(b"GIT binary patch") or line.startswith(b"Binary files "):
            cur.binary = True
        elif line.startswith(b"--- ") and i + 1 < n and lines[i + 1].startswith(b"+++ "):
            old = _strip(line[4:])
            new = _strip(lines[i + 1][4:])
            if old is None:
                cur.new_file = True
            else:
                cur.old_path = old if not cur.renamed else cur.old_path
            if new is None:
                cur.deleted = True
            else:
                cur.new_path = new if not cur.renamed else cur.new_path
            i += 2
            continue
        elif line.startswith(b"@@ "):
            m = _HUNK.match(line)
            if not m:
                raise ValueError("bad hunk header")
            os_, ol, ns, nl = m.group(1), m.group(2), m.group(3), m.group(4)
            os_ = int(os_)
            ol = 1 if ol is None else int(ol)
            ns = int(ns)
            nl = 1 if nl is None else int(nl)
            body = []
            i += 1
            seen_old = seen_new = 0
            while i < n and (seen_old < ol or seen_new < nl):
                l = lines[i]
                tag = l[:1]
                if tag == b" " or l == b"":
                    seen_old += 1
                    seen_new += 1
                    body.append((b" ", l[1:], True))
                elif tag == b"-":
                    seen_old += 1
                    body.append((b"-", l[1:], True))
                elif tag == b"+":
                    seen_new += 1
                    body.append((b"+", l[1:], True))
                elif tag == b"\\":
                    if body:
                        t, txt, _ = body[-1]
                        body[-1] = (t, txt, False)
                else:
                    raise ValueError("bad hunk line")
                i += 1
            if i < n and lines[i].startswith(b"\\"):
                if body:
                    t, txt, _ = body[-1]
                    body[-1] = (t, txt, False)
                i += 1
            cur.hunks.append((os_, ol, ns, nl, body))
            continue
        i += 1
    for c in out:
        if not c.hunks and not c.binary and not c.new_file and not c.deleted and not c.renamed:
            c.mode_only = True
    return out


def apply(old, change):
    """Apply one FileChange to `old` (bytes, or None for a new file). Returns new bytes (None if deleted)."""
    if change.binary:
        raise ValueError("binary patch")
    if change.deleted:
        return None
    if old is None:
        old = b""
    src = old.split(b"\n")
    # src[-1] == b"" when the file ends with a newline
    had_nl = old.endswith(b"\n") or old == b""
    if had_nl and src and src[-1] == b"":
        src.pop()
    res = []
    pos = 0  # index into src
    ends_nl = had_nl
    for os_, ol, ns, nl, body in change.hunks:
        start = os_ - 1 if ol > 0 else os_
        if old == b"" and os_ == 0:
            start = 0
        if start < pos or start > len(src):
            raise ValueError("hunk out of order")
        res.extend(src[pos:start])
        pos = start
        for tag, txt, nlflag in body:
            if tag == b" ":
                if pos >= len(src) or src[pos] != txt:
                    raise ValueError("context mismatch")
                res.append(txt)
                pos += 1
                if pos == len(src):
                    ends_nl = nlflag and had_nl
            elif tag == b"-":
                if pos >= len(src) or src[pos] != txt:
                    raise ValueError("removal mismatch")
                pos += 1
                if pos == len(src):
                    ends_nl = True  # decided by a later '+' line, if any
            else:
                res.append(txt)
                if pos == len(src):
                    ends_nl = nlflag
    res.extend(src[pos:])
    if pos < len(src):
        ends_nl = had_nl
    if not res:
        return b""
    return b"\n".join(res) + (b"\n" if ends_nl else b"")
