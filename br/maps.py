"""Loader for the coverage maps built from the pinned commit (see tools/build_maps.py)."""
import os
import pickle
import zlib


class Maps:
    def __init__(self, directory):
        self.dir = directory
        with open(os.path.join(directory, "index.pkl"), "rb") as fh:
            idx = pickle.loads(zlib.decompress(fh.read()))
        self.ids = idx["ids"]
        self.files = idx["files"]
        self.opened = idx["opened"]
        self.imports = idx["imports"]
        self.spawn = idx["spawn"]
        self.import_func = {k: set(v) for k, v in idx["import_func"].items()}
        self.import_files = set(idx.get("import_files", ()))  # data read while the zygote warms up
        self.extra = idx.get("extra", {})
        self.dict_derived = set(idx.get("dict_derived", ()))
        self.dict_names = set(idx.get("dict_names", ()))
        self.dict_all_keys = idx.get("dict_all_keys", {})
        self._dict_users = None
        self.class_users = idx.get("class_users", {})  # "file:line" of a class -> tests instantiating it
        self.tracked = set(idx.get("tracked", ()))
        self.cpu_ms = idx.get("cpu_ms") or []
        self.scaled_ms = idx.get("scaled_ms", {})
        self.sliced = idx.get("sliced", {})  # test id -> blob with per-fixture-entry coverage
        self._slices = {}
        self._reads = None
        self._cache = {}
        self.all = (1 << len(self.ids)) - 1

    def _file(self, rel):
        m = self._cache.get(rel)
        if m is None:
            name = self.files.get(rel)
            if name is None:
                m = {"func": {}, "mod": {}}
            else:
                with open(os.path.join(self.dir, name), "rb") as fh:
                    m = pickle.loads(zlib.decompress(fh.read()))
            self._cache[rel] = m
        return m

    def known(self, rel):
        return rel in self.files

    def _range(self, table, lo, hi):
        mask = 0
        if hi - lo > 4 * len(table):
            for line, m in table.items():
                if lo <= line <= hi:
                    mask |= m
        else:
            get = table.get
            for line in range(lo, hi + 1):
                m = get(line)
                if m:
                    mask |= m
        return mask

    def func(self, rel, lo, hi):
        return self._range(self._file(rel)["func"], lo, hi)

    def mod(self, rel, lo, hi):
        return self._range(self._file(rel)["mod"], lo, hi)

    def any_line(self, rel):
        f = self._file(rel)
        return self._range(f["func"], 0, 10**9) | self._range(f["mod"], 0, 10**9)

    def ran_at_import(self, rel, lo, hi):
        lines = self.import_func.get(rel)
        if not lines:
            return False
        if hi - lo > len(lines):
            return any(lo <= l <= hi for l in lines)
        return any(l in lines for l in range(lo, hi + 1))

    def func_lines(self, rel, lo, hi):
        """(line, mask) for every line in [lo, hi] that some test executes at call time."""
        table = self._file(rel)["func"]
        if hi - lo > 4 * len(table):
            return [(l, m) for l, m in table.items() if lo <= l <= hi]
        return [(l, table[l]) for l in range(lo, hi + 1) if l in table]

    def slices(self, test_id):
        name = self.sliced.get(test_id)
        if name is None:
            return None
        s = self._slices.get(test_id)
        if s is None:
            with open(os.path.join(self.dir, name), "rb") as fh:
                s = pickle.loads(zlib.decompress(fh.read()))
            self._slices[test_id] = s
        return s

    def reads(self, name):
        """Lines that read a class/module level name: {file: [lines]}, or None if not indexed."""
        if self._reads is None:
            path = os.path.join(self.dir, "reads.pkl")
            if os.path.exists(path):
                with open(path, "rb") as fh:
                    d = pickle.loads(zlib.decompress(fh.read()))
                self._reads = (set(d["bound"]), d["reads"])
            else:
                self._reads = (set(), {})
        bound, table = self._reads
        if name not in bound:
            return None
        return table.get(name, {})

    def _dicts(self):
        if self._dict_users is None:
            path = os.path.join(self.dir, "dictkeys.pkl")
            d = {}
            if os.path.exists(path):
                with open(path, "rb") as fh:
                    d = pickle.loads(zlib.decompress(fh.read()))
            if "users" not in d:
                d = {"users": d, "calls": {}, "callable": []}
            d["callable"] = set(d["callable"])
            self._dict_users = d
        return self._dict_users

    def dict_users(self, name, key):
        """Tests that look `key` up (key "*": read the whole dict) in a class-level dict `name`."""
        return self._dicts()["users"].get("%s\t%s" % (name, key), 0)

    def dict_callers(self, name, key):
        """Tests that invoke the callable stored under `key`; None if that entry is not a callable."""
        d = self._dicts()
        k = "%s\t%s" % (name, key)
        if k not in d["callable"]:
            return None
        return d["calls"].get(k, 0)
