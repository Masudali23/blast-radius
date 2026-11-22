"""A writable mirror of the read-only repository, built from symlinks under /tmp.

Directories are real, files are symlinks to the pinned checkout.  A commit's patched files
are written in place of the links and the links are put back afterwards.
"""
import os


class Overlay:
    def __init__(self, base, root):
        self.base = base
        self.root = root
        self._undo = []
        os.makedirs(root, exist_ok=True)
        for name in os.listdir(base):
            if name == ".git":
                continue
            dst = os.path.join(root, name)
            if not os.path.lexists(dst):
                os.symlink(os.path.join(base, name), dst)

    def _explode(self, rel_dir):
        """Make `rel_dir` a real directory of symlinks (recursively for its parents)."""
        if rel_dir in ("", "."):
            return
        self._explode(os.path.dirname(rel_dir))
        dst = os.path.join(self.root, rel_dir)
        if os.path.islink(dst):
            src = os.path.join(self.base, rel_dir)
            os.unlink(dst)
            os.mkdir(dst)
            for name in os.listdir(src):
                os.symlink(os.path.join(src, name), os.path.join(dst, name))
        elif not os.path.isdir(dst):
            os.makedirs(dst)
            self._undo.append(("rmdir", dst))

    def set_file(self, rel, data):
        """Install `data` (bytes) at rel, or remove rel when data is None."""
        self._explode(os.path.dirname(rel))
        dst = os.path.join(self.root, rel)
        src = os.path.join(self.base, rel)
        self._undo.append(("file", dst, src))  # (first: a failed write is undone as well)
        if os.path.lexists(dst):
            os.unlink(dst)
        if data is not None:
            with open(dst, "wb") as fh:
                fh.write(data)

    def restore(self):
        for item in reversed(self._undo):
            if item[0] == "file":
                _, dst, src = item
                if os.path.lexists(dst):
                    os.unlink(dst)
                if os.path.lexists(src):
                    os.symlink(src, dst)
            else:
                try:
                    os.rmdir(item[1])
                except OSError:
                    pass
        self._undo = []
