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
        # "NAME\tkey\tclass": entries read by function code while `class` was being built
        self.dict_derived = {}
        for entry in idx.get("dict_derived", ()):
            parts = entry.split("\t")
            self.dict_derived.setdefault((parts[0], parts[1]), set()).add(parts[2] if len(parts) > 2 else "")
        self.ancestry = idx.get("ancestry", {})
        self.dict_names = set(idx.get("dict_names", ()))
        self.dict_all_keys = idx.get("dict_all_keys", {})
        self._dict_users = None
        self._relsl = None
        self._index_of = None
        self.class_users = idx.get("class_users", {})  # "file:line" of a class -> tests instantiating it
        self.tracked = set(idx.get("tracked", ()))
        self.cpu_ms = idx.get("cpu_ms") or []
        self.scaled_ms = idx.get("scaled_ms", {})
        self.sliced = idx.get("sliced", {})  # test id -> blob with per-fixture-entry coverage
        self._slices = {}
        self._reads = None
        self.meta_reads = set()
        self.outer_reads = None  # names read while modules load (None: not recorded)
        self.outer_recv = None  # name -> classes it is read through while loading, to feed something else
        self.class_anc = {}  # class name -> names of its ancestors
        self.classlike = {}  # name -> {file: {line: what the class is known to be}}, read off a class object
        self.derives = {}  # name -> names a class-creation hook stores from it
        self._relations = None
        self._memo = None
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
                self.meta_reads = set(d.get("meta_reads", ()))
                self.outer_reads = set(d["outer_reads"]) if "outer_reads" in d else None
                self.outer_recv = {n: set(r) for n, r in d["outer_recv"].items()} if "outer_recv" in d else None
                self.class_anc = {c: set(a) for c, a in d.get("class_anc", {}).items()}
                self.classlike = d.get("classlike", {})
                self.derives = d.get("derives", {})
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
            d["attr_tracked"] = set(d.get("attr_tracked", ()))
            d["attr_derived"] = set(d.get("attr_derived", ()))
            d.setdefault("attr_readers", {})
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

    def derived(self, name, key, cls_keys):
        """Was entry `key` of dict `name` read at import time in the class `cls_keys` or a subclass?"""
        owners = self.dict_derived.get((name, key))
        if not owners:
            return False
        want = set(cls_keys)
        for owner in owners:
            if not owner:
                return True  # recorded without its class: assume the worst
            if want & set(self.ancestry.get(owner, (owner,))):
                return True
        return False

    def relation(self, rel, key):
        """For the comparison at `key` in file `rel` (see br/relations.py): the tests in which
        the operator and its partner ever disagree, and whether they disagreed while the warm
        worker loaded.  (None, False) when that comparison was not observed."""
        if self._relations is None:
            path = os.path.join(self.dir, "relations.pkl")
            d = {"sites": {}, "hits": {}, "always": 0, "warm": ()}
            if os.path.exists(path):
                with open(path, "rb") as fh:
                    d = pickle.loads(zlib.decompress(fh.read()))
            d["sites"] = {f: frozenset(keys) for f, keys in d["sites"].items()}
            d["warm"] = frozenset(d["warm"])
            if "formulas" in d:
                d["formulas"] = {f: frozenset(keys) for f, keys in d["formulas"].items()}
                d["warm_aborted"] = frozenset(d["warm_aborted"])
                per = {}
                for site, kind, vec in d["warm_vectors"]:
                    per.setdefault(site, []).append((kind, vec))
                d["warm_vectors"] = per
            self._relations = d
        d = self._relations
        if key not in d["sites"].get(rel, ()) or "seen" not in d:
            return None, False
        site = rel + "|" + key
        # A test the tracer never saw evaluate the comparison is not vouched for: whatever
        # made the traced run skip it (a value cached by the tracer's own extra work, say)
        # need not hold in a real run.
        unseen = self.all & ~d["seen"].get(site, 0)
        return d["hits"].get(site, 0) | d["always"] | unseen, site in d["warm"]

    def formula(self, rel, key):
        """Recorded evaluations of the condition at `key` (see br/boolrel.py):
        ({(kind, vector): tests}, tests to keep regardless, evaluations made while the warm
        worker loaded), or None when it was not observed or one of those did not finish."""
        self.relation("", "")
        d = self._relations
        if key not in d.get("formulas", {}).get(rel, ()):
            return None
        site = rel + "|" + key
        if site in d["warm_aborted"]:
            return None
        vectors = d["vectors"].get(site, {})
        observed = d["aborted"].get(site, 0)
        for tests in vectors.values():
            observed |= tests
        # (tests the tracer never saw evaluate the condition are not vouched for)
        return vectors, d["aborted"].get(site, 0) | d["always"] | (self.all & ~observed), d["warm_vectors"].get(site, ())

    def _relslices(self):
        if self._relsl is None:
            path = os.path.join(self.dir, "relslices.pkl")
            self._relsl = {}
            if os.path.exists(path):
                with open(path, "rb") as fh:
                    self._relsl = pickle.loads(zlib.decompress(fh.read()))
        return self._relsl

    def entry_relation(self, test_id, rel, key):
        """For a sliced test: the fixture entries (bit mask, slice-map order) in which the
        comparison at `key` can behave differently under its partner operator: those in which
        the two disagreed, and those the tracer did not see evaluate it at all.  None when the
        test's entries were not recorded.  (tools/trace_rel_slices_one.py)"""
        d = self._relslices().get(test_id)
        if d is None:
            return None
        site = rel + "|" + key
        if site in d["pre_hits"]:
            return None  # disagreed while the class was set up: every entry builds on that
        return d["hits"].get(site, 0) | (((1 << d["n"]) - 1) & ~d["seen"].get(site, 0))

    def entry_formula(self, test_id, rel, key):
        """For a sliced test: ([(kind, vector, entries)], entries to keep regardless, [(kind,
        vector)] evaluated before any entry ran) for the condition at `key`, or None when the
        test's entries were not recorded or an evaluation before them did not finish."""
        d = self._relslices().get(test_id)
        if d is None:
            return None
        site = rel + "|" + key
        if site in d["pre_aborted"]:
            return None
        vectors = d["vectors"].get(site, ())
        observed = d["aborted"].get(site, 0)
        for _, _, entries in vectors:
            observed |= entries
        return vectors, d["aborted"].get(site, 0) | (((1 << d["n"]) - 1) & ~observed), d["pre_vectors"].get(site, ())

    def entry_use(self, test_id, field, items):
        """For a sliced test: the fixture entries that use any of `items`, where `field` says
        how ("classes": instantiate the class at that "file:line"; "attr_reads": read the
        constant "class\tNAME"; "dict_keys": look up "NAME\tkey"; "dict_calls": invoke the
        callable stored there).  None when the test's entries were not recorded."""
        d = self._relslices().get(test_id)
        use = d.get("use") if d else None
        if not use:
            return None
        table = use[field]
        mask = 0
        for item in items:
            mask |= table.get(item, 0)
        return mask

    def entry_lines(self, test_id, rel, lo, hi, loading=True):
        """For a sliced test: the fixture entries that run some function line in [lo, hi] of a
        file, or None when the test runs one of those lines outside its fixture loop (or is not
        sliced).  With `loading`, a line in the range that the test runs while modules load
        also means None; a caller that only asks who enters a function turns that off (its
        `def` line always runs then)."""
        sm = self.slices(test_id)
        d = self._relslices().get(test_id)
        if sm is None or d is None or "setup_lines" not in d:
            return None
        if self._index_of is None:
            self._index_of = {t: i for i, t in enumerate(self.ids)}
        idx = self._index_of.get(test_id)
        if idx is None:
            return None
        if loading and (self.mod(rel, lo, hi) >> idx) & 1:
            return None  # the test runs one of them while modules load
        table = sm["lines"].get(rel, {})
        during_setup = d["setup_lines"].get(rel, ())
        mask = 0
        for line, tests in self.func_lines(rel, lo, hi):
            if (tests >> idx) & 1:
                entries = table.get(line, 0)
                if not entries or line in during_setup:
                    return None  # runs outside the fixture loop: every entry builds on it
                mask |= entries
        return mask

    def entry_line_users(self, test_id, pairs):
        """entry_lines over a list of (file, line): the entries that run any of them; None as
        soon as one of them cannot be pinned to entries."""
        mask = 0
        for f, line in pairs:
            m = self.entry_lines(test_id, f, line, line)
            if m is None:
                return None
            mask |= m
        return mask

    def setup_runs(self, test_id, rel, line):
        """Does the class set-up of a sliced test run this function line?  (False when the
        set-up was not recorded.)"""
        d = self._relslices().get(test_id)
        return bool(d) and line in d.get("setup_lines", {}).get(rel, ())

    def memo(self, test_id):
        """Hand-over fingerprints of an executor test (see br/memo.py), or None."""
        if self._memo is None:
            path = os.path.join(self.dir, "memo.pkl")
            self._memo = {}
            if os.path.exists(path):
                with open(path, "rb") as fh:
                    self._memo = pickle.loads(zlib.decompress(fh.read()))
                for d in self._memo.values():
                    d["back"] = {rel: frozenset(lines) for rel, lines in d["back"].items()}
        return self._memo.get(test_id)

    def feeds_elsewhere(self, name, cls):
        """Is class `cls`'s attribute `name` read while modules load, other than to build the
        attribute of the same name?  Then something stored elsewhere holds (a piece of) its
        value and readers of `name` do not tell who is affected."""
        self.reads("")
        if self.outer_recv is None:
            return True
        for r in self.outer_recv.get(name, ()):
            if r == "*" or r == cls or cls in self.class_anc.get(r, ()):
                return True
        return False

    def class_object_readers(self, names, cls):
        """Tests that run a line reading one of `names` off a class object that can be `cls` (or
        a class holding `cls`'s value of it), no instance needed; None when it cannot be told.

        A name that a class-creation hook reads (`__init_subclass__`, a metaclass) lives on in
        whatever the hook stored from it, so readers of those names count as well, off any
        class: the hook may store on a class other than the one being created."""
        lines = self.class_object_lines(names, cls)
        if lines is None:
            return None
        mask = 0
        for f, line in lines:
            mask |= self.func(f, line, line)
        return mask

    def class_object_lines(self, names, cls):
        """The (file, line) pairs behind class_object_readers, or None."""
        self.reads("")
        todo = [(n, True) for n in names]
        seen = set(names)
        out = []
        while todo:
            name, own = todo.pop()
            for d in self.derives.get(name, ()):
                if d == "*":
                    return None
                if d not in seen:
                    seen.add(d)
                    todo.append((d, False))
            for f, lines in self.classlike.get(name, {}).items():
                for line, recvs in lines.items():
                    if not own or any(self._can_be(r, cls) for r in recvs):
                        out.append((f, line))
        return out

    def _can_be(self, recv, cls):
        """Can a read through `recv` (see tools/build_reads.py) return class `cls`'s own value?"""
        kind, other = recv[:1], recv[1:]
        if kind == "=":
            # the named class, which may inherit the value from cls
            return other == cls or cls in self.class_anc.get(other, ())
        if kind == "<":
            # some class at or below `other`: cls itself, a subclass of cls, or one inheriting from cls
            return other == cls or other in self.class_anc.get(cls, ()) or cls in self.class_anc.get(other, ())
        return True

    def attr_readers(self, cls_keys, name):
        """Tests that read constant `name` from the definition in one of `cls_keys`.

        None when those reads were not observed, or when something was derived from the value
        while classes were being built (by a metaclass, or by any function reading it then)."""
        d = self._dicts()
        self.reads("")  # loads meta_reads
        if name in self.meta_reads or self.outer_reads is None or name in self.outer_reads:
            # read while classes are built or modules load: copies of the value exist that
            # the recorded reads of this definition know nothing about
            return None
        mask, seen = 0, False
        for ck in cls_keys:
            k = "%s\t%s" % (ck, name)
            if k in d["attr_derived"]:
                return None
            if k in d["attr_tracked"]:
                seen = True
                mask |= d["attr_readers"].get(k, 0)
        return mask if seen else None
