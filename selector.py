"""Blast Radius selector.

For every patch: work out which tests can be affected (coverage maps built from the pinned
commit), run those tests against the patched code in a warm interpreter, and select exactly the
ones that fail.  Whatever cannot be checked cheaply is selected rather than skipped.
"""
import ast
import glob
import json
import os
import sys
import threading
import time

APP = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, APP)

from br import plan as planner  # noqa: E402
from br import variants  # noqa: E402
from br.engine import Lane  # noqa: E402
from br.maps import Maps  # noqa: E402

DATA = os.environ.get("BR_DATA", "/data")
BASE = os.environ.get("BR_REPO", "/app/repo")
MAPS = os.environ.get("BR_MAPS", "/app/maps")
TMP = os.environ.get("BR_TMP", "/tmp/br")
DEBUG = os.environ.get("BR_DEBUG")

FIXTURES = "tests/fixtures/"
CLASS_FIXTURES = set(variants.CLASS_LISTS.values())
WORKER_MS = 2000
MAX_WORKERS = 8
# tests slower than this (recorded seconds) are never run blindly by us
HEAVY = float(os.environ.get("BR_HEAVY", "2.6"))
# machine factor: scoring-machine CPU per CPU ms measured when the maps were built
CAL = float(os.environ.get("BR_CAL", "1.25"))
# a candidate set is "broad" when running it all costs more than this share of selecting it all
BROAD_FRAC = float(os.environ.get("BR_BROAD_FRAC", "0.75"))
BROAD_MIN = float(os.environ.get("BR_BROAD_MIN", "2.0"))
BROAD_COST = float(os.environ.get("BR_BROAD_COST", "6.0"))
SCALED_SHARE = 0.03
# tests up to this estimated cost are simply run; costlier ones only while nothing has failed yet
CHEAP = float(os.environ.get("BR_CHEAP", "0.4"))
# a test this slow that reads an edited fixture file is selected without being run
LIKELY = float(os.environ.get("BR_LIKELY", "0.25"))
# chance that a not-yet-run candidate passes, when nothing / something has failed so far
P_PASS_CLEAN = float(os.environ.get("BR_P_PASS_CLEAN", "0.9"))
P_PASS_FAILED = float(os.environ.get("BR_P_PASS_FAILED", "0.7"))
# ... for the cheap candidates left when the first one fails (one by one, and all together)
P_PASS_AGAIN = float(os.environ.get("BR_P_PASS_AGAIN", str(P_PASS_FAILED)))
P_PASS_REST = float(os.environ.get("BR_P_PASS_REST", "0"))
# ... a test lighter than this many recorded seconds is not run on its own after a failure
AGAIN_MIN = float(os.environ.get("BR_AGAIN_MIN", "1.0"))
# ... and the same two when at least this many tests passed before the first failure
MIXED_AFTER = int(os.environ.get("BR_MIXED_AFTER", "1000000"))
P_PASS_AGAIN_MIXED = float(os.environ.get("BR_P_PASS_AGAIN_MIXED", str(P_PASS_AGAIN)))
P_PASS_REST_MIXED = float(os.environ.get("BR_P_PASS_REST_MIXED", str(P_PASS_REST)))
# ... and for the sample of a slow fixture-driven test next to real failures
P_PASS_SAMPLE = float(os.environ.get("BR_P_PASS_SAMPLE", "0.7"))
# CPU seconds one commit may spend running tests, whatever was planned
COMMIT_CPU = float(os.environ.get("BR_COMMIT_CPU", "25"))
# seconds of a slow sliced test to sample before deciding about it
SLICE_PROBE = float(os.environ.get("BR_SLICE_PROBE", "0.4"))
# slow tests still unverified after a clean sample: "keep" them selected, or "drop" them
HEAVY_CLEAN = os.environ.get("BR_HEAVY_CLEAN", "drop")
# a slow test left unverified while other tests fail: "keep" it selected, or decide from its
# "related" tests (dropped only when enough of them ran and all passed, few tests fail overall,
# and a sample of the slow test itself passes)
HEAVY_FAILED = os.environ.get("BR_HEAVY_FAILED", "related")
MIN_RELATED = int(os.environ.get("BR_MIN_RELATED", "5"))
# related tests left unrun are run for this purpose when they cost at most this share of what
# the slow test weighs in the selection
RELATED_RUN = float(os.environ.get("BR_RELATED_RUN", "0.5"))
MANY_FAILED = int(os.environ.get("BR_MANY_FAILED", "20"))
# ... and never more than this many seconds of them: what they can save is the price of the
# slow test's sample, not the slow test itself
RELATED_CAP = float(os.environ.get("BR_RELATED_CAP", "1000"))
# the first part of a slow test's sample is run before its related tests are
FIRST_LOOK = os.environ.get("BR_FIRST_LOOK", "1") == "1"
# measuring only: let the slow test go as soon as that first part has passed
FIRST_ONLY = os.environ.get("BR_FIRST_ONLY", "0") == "1"
# seconds to wait for the stand-alone import check of a patched module
IMPORT_WAIT = 45.0
# recognise unchanged hand-overs to the slow half of the executor's TPC-DS test
MEMO = os.environ.get("BR_MEMO", "1") == "1"
# ... and entries whose hand-over changed are run for real while their estimated CPU stays
# under this share of the test's recorded time
MEMO_BUDGET = float(os.environ.get("BR_MEMO_BUDGET", "0.7"))
# reference policy: run every candidate, keep nothing on an estimate (see verify_all)
EXACT = os.environ.get("BR_EXACT", "0") == "1"
EXACT_CAP = float(os.environ.get("BR_EXACT_CAP", "15"))
# stop the cheap candidates at the first failure and weigh the rest: run or leave selected
STOP_FIRST = os.environ.get("BR_STOP_FIRST", "1") == "1"
# order of the first stage: "collection" (the suite's own), "cost" (least expensive first) or
# "name" (tests named after a patched file first, then least expensive)
ORDER = os.environ.get("BR_ORDER", "name")
# a slow test whose sample passes is only let go when keeping it would add at least this many
# seconds to the bill; a lighter one stays selected
DROP_MIN = float(os.environ.get("BR_DROP_MIN", "8"))
# The slowest test spends over half its time waiting for DuckDB, whose answers come from the
# store when the image has them (br/tables.py): about this share of its calibrated CPU is left.
ANSWER_TESTS = ("tests/test_executor.py::TestExecutor::test_execute_tpcds",)
ANSWER_SHARE = float(os.environ.get("BR_ANSWER_SHARE", "0.5"))
# A test that does nothing but ask DuckDB, about texts whose answers are all stored: a small
# share of its calibrated CPU is left.  Its time limits stay those of a run that asks DuckDB
# (which is what happens when the patch touches what the stored answers depend on).
STORED_TESTS = {"tests/test_executor.py::TestExecutor::test_optimized_tpch": float(os.environ.get("BR_STORED_SHARE", "0.06"))}
# with nothing failing, a slow test that can be sampled is run in full only up to this many
# seconds of CPU; past that it is sampled
CLEAN_RUN = float(os.environ.get("BR_CLEAN_RUN", "8"))
# narrow sliced tests to the fixture entries a swapped operator / regrouped condition can reach
ENTRY_NARROW = os.environ.get("BR_ENTRY_NARROW", "1") == "1"
# use the executor shortcut (br/memo.py) for every run of its test, not only for samples
SHORT_RUNS = os.environ.get("BR_SHORT_RUNS", "1") == "1"
# a test that an edited class fixture reaches only through its class set-up is settled by
# running that set-up
SETUP_ONLY = os.environ.get("BR_SETUP_ONLY", "1") == "1"
# share of a slow sliced test's cost that is run as a sample when the test is still selected
# unchecked next to real failures (0: never sample, keep it)
SAMPLE_SHARE = float(os.environ.get("BR_SAMPLE_SHARE", "0.2"))
SAMPLE_ORDER = os.environ.get("BR_SAMPLE_ORDER", "cost")
SAMPLE_PARTS = int(os.environ.get("BR_SAMPLE_PARTS", "4"))
SAMPLE_EXTRA = frozenset(x.strip() for x in os.environ.get("BR_SAMPLE_EXTRA", "").split(",") if x.strip())
# the sample looks at every entry's quick half first (br/memo.py): when few entries are handed
# something new, all of those are run and the sample is the whole test
SETTLE = os.environ.get("BR_SETTLE", "1") == "1"
# ... as long as running them costs no more than this share of the test
WEIGHED_SHARE = float(os.environ.get("BR_WEIGHED_SHARE", "0.3"))
WEIGH_MIXED = os.environ.get("BR_WEIGH_MIXED", "0") == "1"
# for measuring only: select nothing that was not seen to fail
ONLY_SEEN = os.environ.get("BR_ONLY_SEEN", "0") == "1"
# ... provided at least this many other candidates actually ran and passed
MIN_CLEAN = int(os.environ.get("BR_MIN_CLEAN", "5"))
DUMP = os.environ.get("BR_DUMP", "0") == "1"
# Not exact (off by default).  The first stage takes the lighter candidates in the order of
# ORDER, up to this many estimated seconds; if none of them fails the commit is taken to
# break nothing and the candidates left are not selected.  A slow fixture-driven test is
# still settled by its own sample.
CLEAN_STOP = float(os.environ.get("BR_CLEAN_STOP", "0"))
# ... but never fewer than this share of what all of them are estimated to cost, nor fewer
# than this many seconds
CLEAN_FRAC = float(os.environ.get("BR_CLEAN_FRAC", "0.5"))
CLEAN_MIN = float(os.environ.get("BR_CLEAN_MIN", "0.4"))
# ... and only for a change inside function bodies, or of a class-level value whose readers are
# known (on labelled patches the other kinds have little to save and fail late more often)
CLEAN_ANY = os.environ.get("BR_CLEAN_ANY", "0") == "1"
T0 = time.monotonic()


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def split(numbers, ms):
    """Best assignment of tests to 1..8 workers: longest first onto the lightest worker."""
    if not numbers:
        return [], 0
    items = sorted(numbers, key=lambda n: -ms[n])
    best = None
    for k in range(1, MAX_WORKERS + 1):
        loads = [0] * k
        workers = [[] for _ in range(k)]
        for n in items:
            i = loads.index(min(loads))
            loads[i] += ms[n]
            workers[i].append(n)
        cost = max(loads) + WORKER_MS * k
        if best is None or cost < best[0]:
            best = (cost, workers)
        if k >= len(items):
            break
    return [w for w in best[1] if w], best[0]


class Selector:
    def __init__(self):
        with open(os.path.join(DATA, "tests.json"), "rb") as fh:
            tests = json.load(fh)["tests"]
        self.tests = tests
        self.ms = {t["number"]: int(t["seconds"] * 1000 + 0.5) for t in tests}
        self.id_of = {t["number"]: t["id"] for t in tests}
        self.all_numbers = [t["number"] for t in tests]
        self.maps = Maps(MAPS)
        num_by_id = {t["id"]: t["number"] for t in tests}
        # map index -> runtime test number (None when the run does not list that test)
        self.idx2num = [num_by_id.get(i) for i in self.maps.ids]
        self.idx_of = {n: i for i, n in enumerate(self.idx2num) if n is not None}
        known = set(self.maps.ids)
        # tests we have no map for are always in play
        self.unmapped = [t["number"] for t in tests if t["id"] not in known]
        # estimated CPU ms to run each test ourselves on the scoring machine
        self.cpu = {}
        for n, tid in self.id_of.items():
            idx = self.idx_of.get(n)
            cal = self.maps.cpu_ms[idx] if idx is not None and idx < len(self.maps.cpu_ms) else None
            self.cpu[n] = (cal * CAL) if cal is not None else self.ms[n] * 0.85
        self.slack = {}  # test -> factor from its estimate to the time a run may need at worst
        if self.stored_answers():
            for n, tid in self.id_of.items():
                if tid in ANSWER_TESTS:
                    self.cpu[n] *= ANSWER_SHARE
                share = STORED_TESTS.get(tid, 0.0)
                if 0.0 < share < 1.0:
                    self.cpu[n] *= share
                    self.slack[n] = 1.0 / share
        self.lane = None
        self.lane2 = None
        self._related = {}
        self._list_uses = {}
        self.debug = {}

    def numbers(self, mask):
        out = []
        idx2num = self.idx2num
        i = 0
        while mask:
            low = mask & -mask
            i = low.bit_length() - 1
            n = idx2num[i]
            if n is not None:
                out.append(n)
            mask ^= low
        return out

    def cheapest_first(self, patches):
        """Order commits by the estimated CPU of checking them, so that if time runs out it is
        the most expensive ones that fall back to being selected unseen."""
        cost = {}
        for path in patches:
            try:
                with open(path, "rb") as fh:
                    plan = planner.make_plan(os.path.basename(path), fh.read(), BASE, self.maps)
                cost[path] = 1e12 if plan.unknown else sum(min(self.cpu[n], EXACT_CAP * 1000) for n in self.numbers(plan.cand))
            except Exception:
                cost[path] = 1e12
        return sorted(patches, key=lambda p: (cost[p], p))

    def named_after(self, plan):
        """A function telling whether a test is named after one of the patched files (0) or
        not (1): its file or method name contains the file's name, as in
        sqlglot/dialects/bigquery.py and tests/dialects/test_bigquery.py."""
        stems = set()
        for rel in plan.files:
            head, tail = os.path.split(rel)
            stem = tail.rsplit(".", 1)[0].lower()
            if stem == "__init__":
                stem = os.path.basename(head).lower()
            if len(stem) >= 3 and stem not in ("core", "base", "helper", "helpers", "tests"):
                stems.add(stem)
        if not stems:
            return lambda n: 1
        ids = self.id_of

        def near(n):
            tid = ids[n].lower()
            return 0 if any(stem in tid for stem in stems) else 1

        return near

    @staticmethod
    def plain_kind(plan):
        """Is this a change inside function bodies only, or of a class-level value that the
        planner narrowed to the tests that use it?"""
        notes = " ".join(plan.notes)
        if "attr-narrowed" in notes or "class-narrowed" in notes or "default-narrowed" in notes or "key-narrowed" in notes:
            return True
        return sorted(plan.kinds) == ["func"]

    def get_lane(self):
        if self.lane is None:
            self.lane = Lane(BASE, TMP, APP, log)
        return self.lane

    def get_second_lane(self):
        if self.lane2 is None:
            self.lane2 = Lane(BASE, TMP + "_2", APP, log)
        return self.lane2

    # ------------------------------------------------------------------ one commit
    @staticmethod
    def stored_answers():
        """Does the table store hold DuckDB answers (tools/build_tables.py says so next to it)?"""
        try:
            with open(os.environ.get("BR_TABLES", "/app/tables.pkl") + ".info") as fh:
                words = fh.read().split()
            return len(words) == 2 and words[0] == "answers" and int(words[1]) > 0
        except (OSError, ValueError):
            return False

    def decide(self, commit, data):
        plan = planner.make_plan(commit, data, BASE, self.maps)
        dbg = {"kinds": sorted(plan.kinds), "notes": plan.notes, "fresh": plan.fresh}
        self.debug[commit] = dbg
        if plan.unknown:
            dbg["how"] = "all"
            return [] if ONLY_SEEN else list(self.all_numbers)
        cand = set(self.numbers(plan.cand)) | set(self.unmapped) if (plan.cand or plan.files) else set()
        if not plan.cand:
            cand = set()
        sure = set(self.numbers(plan.sure))
        cand -= sure
        dbg["ncand"] = len(cand)
        if plan.imports:
            # candidates were narrowed on the understanding that the patched module still
            # imports; if it does not, every test that imports it fails
            late = time.monotonic() > self.stop_at
            bad = None if late else self.unimportable(plan)
            if late or bad:
                dbg["how"] = "out-of-time" if late else "import-error"
                dbg["unimportable"] = bad
                return sorted(cand | sure | set(self.numbers(plan.wide)))
        if not cand:
            dbg["how"] = "none" if not sure else "sure"
            return sorted(sure)
        if time.monotonic() > self.stop_at:
            dbg["how"] = "out-of-time"
            return sorted(sure) if ONLY_SEEN else sorted(cand | sure)
        selected, how = self.verify_all(plan, cand, dbg) if EXACT else self.check(plan, cand, dbg)
        dbg["how"] = how
        if ONLY_SEEN:
            unchecked = set(dbg.get("unverified") or ()) | set(dbg.get("unseen") or ())
            selected = [n for n in selected if n not in unchecked]
        return sorted(set(selected) | sure)

    def verify_all(self, plan, cand, dbg):
        """The plain policy, for reference: run every candidate and select exactly what fails.

        Nothing is left selected on an estimate and nothing is let go on a sample.  Tests whose
        run would take longer than EXACT_CAP seconds are settled by their unchanged hand-over
        when that applies and stay selected otherwise.  Two workers run side by side."""
        items, keep = [], []
        for n in sorted(cand):
            v, share = self.plain_variant(plan, n)
            est = self.cpu[n] * share
            if est > EXACT_CAP * 1000:
                sv = self.short_variant(plan, n)
                if sv is None:
                    keep.append(n)
                    continue
                v, est = sv
            items.append((n, v, est))
        halves, loads = [[], []], [0.0, 0.0]
        for it in sorted(items, key=lambda it: -it[2]):
            i = 0 if loads[0] <= loads[1] else 1
            halves[i].append(it)
            loads[i] += it[2]
        halves = [sorted(h, key=lambda it: it[0]) for h in halves if h]
        lanes = [self.get_lane(), self.get_second_lane()][: len(halves)]
        mode = lanes[0].fresh_mode(plan.files) if (lanes and plan.fresh) else "full"
        outcomes = [None] * len(halves)

        def work(i):
            lane, part = lanes[i], halves[i]
            m = lane.fresh_mode(plan.files) if plan.fresh else "full"
            jobs = [(self.id_of[n], max(1.5, 5.0 * e * self.slack.get(n, 1.0) / 1000.0 + 0.5), v) for n, v, e in part]
            wait = max(10.0, self.stop_at + 30.0 - time.monotonic())
            cpu = max(30.0, 5.0 + 2.5 * sum(e * self.slack.get(n, 1.0) for n, _, e in part) / 1000.0)
            lane.ensure(m)
            try:
                for rel, content in plan.files.items():
                    lane.overlay.set_file(rel, content)
                full = m == "full"
                results, err = lane.run(m, jobs, swap=plan.swaps if full else (), consts=plan.consts if full else (), deadline=wait, cpu_budget=cpu)
                if err and full and not results:
                    lane.overlay.restore()
                    m = lane.fresh_mode(plan.files)
                    lane.ensure(m)
                    for rel, content in plan.files.items():
                        lane.overlay.set_file(rel, content)
                    results, err = lane.run(m, jobs, deadline=wait, cpu_budget=cpu)
                outcomes[i] = results
            except Exception as e:  # this half could not be checked: it stays selected
                log("verify_all: %r" % (e,))
                outcomes[i] = {}
            finally:
                lane.overlay.restore()

        threads = [threading.Thread(target=work, args=(i,)) for i in range(len(halves))]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        selected, ran, cpu_ms = list(keep), 0, 0
        for part, results in zip(halves, outcomes):
            for i, (n, v, e) in enumerate(part):
                r = (results or {}).get(i)
                if r is None or r[0] in ("F", "U"):
                    selected.append(n)
                if r is not None:
                    ran += 1
                    cpu_ms += r[1]
                    if DEBUG:
                        dbg.setdefault("tests", []).append((n, r[1], int(e), r[0], -1))
        dbg["mode"] = mode
        dbg["ran"] = ran
        dbg["cpu_ms"] = cpu_ms
        dbg["unverified"] = keep
        return selected, "verified"

    def unimportable(self, plan):
        """Import the patched modules on their own in a fresh child; return those that fail."""
        lane = self.get_lane()
        mode = lane.fresh_mode(plan.files)
        lane.ensure(mode)
        names = []
        for rel in plan.imports:
            name = rel[:-3].replace("/", ".")
            names.append(name[:-9] if name.endswith(".__init__") else name)
        try:
            for rel, content in plan.files.items():
                lane.overlay.set_file(rel, content)
            _, err = lane.run(mode, [], deadline=IMPORT_WAIT, imports=names)
            bad = list(lane.unimportable)
            if err:
                bad.append(err)  # the child died or could not start: treat as not importable
        finally:
            lane.overlay.restore()
        return bad

    def variant_for(self, plan, n):
        """How to run test n for this plan: (variant or None, share of its recorded time)."""
        v, share = self.plain_variant(plan, n)
        if SHORT_RUNS and MEMO and self.maps.memo(self.id_of[n]) is not None:
            short = self.short_variant(plan, n)
            if short is not None:
                # The executor's fixture test, settled entry by entry: an entry that is handed
                # exactly what the pinned commit handed over needs no slow half, every other
                # entry is run for real.  (The estimate stays that of the plain run.)
                v = dict(short[0])
                v["memo_budget"] = 1e12
        return v, share

    def short_variant(self, plan, n):
        """(variant, estimated CPU ms) for settling test n by recognising unchanged hand-overs
        to its slow half (br/memo.py), or None when that cannot be done for this patch."""
        tid = self.id_of[n]
        known = self.maps.memo(tid) if MEMO else None
        if known is None:
            return None
        v, share = self.plain_variant(plan, n)
        must = None if (v is not None and not v.get("allow")) else self.patched_slow_halves(plan, n, known)
        if must is None:
            return None
        frac = 1.0
        if v is not None:
            sm = self.maps.slices(tid)
            live = [k for k, s in zip(sm["keys"], sm["share"]) if s > 0.0]
            allowed = set(v["allow"])
            frac = len([k for k in live if k in allowed]) / max(1, len(live))
        v = dict(v or {})
        v["memo"] = known["entries"]
        # entries that are handed something new may still be run for real, up to this much
        v["memo_costs"] = {k: self.cpu[n] * share for k, share in known.get("costs", {}).items()}
        v["memo_budget"] = MEMO_BUDGET * self.ms[n]
        v["memo_must"] = sorted(must)
        # the quick halves, plus the entries whose slow half runs patched code
        return v, self.cpu[n] * (frac * known["share"] + 0.01 + sum(known.get("costs", {}).get(k, 0.0) for k in must))

    def patched_slow_halves(self, plan, n, known):
        """The entries of test n whose slow half runs code the patch changes (they are run for
        real whatever they are handed), when the patch changes only function bodies; None when
        nothing can be claimed (a module / class level change, edited data, or patched code in
        the slow half that cannot be pinned to entries)."""
        idx = self.idx_of.get(n)
        if idx is None or plan.unknown or plan.syntax or self.id_of[n].split("::")[0] in plan.files:
            return None
        back, by = known["back"], known.get("back_by") or {}
        must = set()
        for kind, rel, lo, hi, mask in plan.parts:
            if kind == "func":
                lines = back.get(rel)
                if lines and (any(lo <= line <= hi for line in lines) if hi - lo > len(lines) else any(line in lines for line in range(lo, hi + 1))):
                    hit = [key for key, per in by.items() if any(lo <= line <= hi for line in per.get(rel, ()))]
                    if not hit:
                        return None  # run by the slow half, but not by any one entry's replay
                    must.update(hit)
            elif kind == "data" and rel.endswith(".py") and not (mask >> idx) & 1:
                continue  # another test reads that source file as text
            elif kind == "mod" and plan.uses.get((rel, lo, hi)) and by and known.get("use_by"):
                # A class constant, dict entry or default value that nothing looked at while
                # modules loaded: a slow half notices it only by using it.
                needs = plan.uses[(rel, lo, hi)]
                use_by = known["use_by"]
                for key, lines in by.items():
                    use = use_by.get(key)
                    if use is None or all(self.uses_it(need, lines, use) for need in needs):
                        must.add(key)
            else:
                return None  # another module / class level change, or edited data: no claim
        return must

    @staticmethod
    def uses_it(alternatives, lines, use):
        """Does code that ran `lines` ({file: lines}) and used `use` ({kind: items}) meet one of
        the alternatives (see Plan.uses)?"""
        for kind, what in alternatives:
            if kind == "pairs":
                if any(line in lines.get(f, ()) for f, line in what):
                    return True
            elif kind == "range":
                f, lo, hi = what
                if any(lo <= line <= hi for line in lines.get(f, ())):
                    return True
            elif any(item in use.get(kind, ()) for item in what):
                return True
        return False

    def sample_order(self, tid, sm, live):
        """The order in which a sample takes fixture entries.  "cost": least expensive first.
        "cover": the entry that runs the most lines no earlier entry of the sample ran, per
        unit of cost, comes next (an entry that adds none counts by its cost alone), so that
        the sample sees as much of the code the whole test reaches as it can for its price."""
        share = sm["share"]
        if SAMPLE_ORDER != "cover":
            return sorted(live, key=lambda i: share[i])
        want = 0
        for i in live:
            want |= 1 << i
        weight = {}  # set of entries (as a mask) -> number of lines that exactly they run
        for table in sm["lines"].values():
            for mask in table.values():
                mask &= want
                if mask:
                    weight[mask] = weight.get(mask, 0) + 1
        groups = list(weight.items())
        of = {i: [] for i in live}
        gain = {i: 0 for i in live}
        for g, (mask, w) in enumerate(groups):
            m = mask
            while m:
                low = m & -m
                i = low.bit_length() - 1
                of[i].append(g)
                gain[i] += w
                m ^= low
        seen = [False] * len(groups)
        left = set(live)
        order = []
        while left:
            best = max(left, key=lambda i: ((gain[i] + 0.5) / max(share[i], 1e-9), -i))
            order.append(best)
            left.discard(best)
            for g in of[best]:
                if not seen[g]:
                    seen[g] = True
                    mask, w = groups[g]
                    m = mask
                    while m:
                        low = m & -m
                        gain[low.bit_length() - 1] -= w
                        m ^= low
        return order

    def sample_variant(self, plan, n):
        """([(variant, estimated CPU ms), ...], total ms) for running the cheapest entries of
        sliced test n, up to SAMPLE_SHARE of the cost of the entries the patch can reach, in a
        few parts of about equal cost; None when the test is not sliced or the sample would be
        all of it."""
        tid = self.id_of[n]
        sm = self.maps.slices(tid)
        if sm is None or tid.split("::")[0] in plan.files:
            return None
        v, _ = self.plain_variant(plan, n)
        if v is not None and "allow" not in v:
            return None
        allowed = set(v["allow"]) if v is not None else None
        # (an entry whose measured cost is nil is still parsed and handed to the pool by the
        # test, so it belongs to the sample like any other; it comes first, at no cost)
        live = [i for i, k in enumerate(sm["keys"]) if allowed is None or k in allowed]
        total = sum(sm["share"][i] for i in live)
        if not live or total <= 0.0:
            return None
        keys, spent = [], 0.0
        for i in self.sample_order(tid, sm, live):
            keys.append(sm["keys"][i])
            spent += sm["share"][i]
            if spent >= SAMPLE_SHARE * total:
                break
        if len(keys) >= len(live):
            return None
        short = self.short_variant(plan, n)
        share_of = dict(zip(sm["keys"], sm["share"]))

        def split_up(keys, count):
            total = sum(share_of[k] for k in keys)
            parts, chunk, load = [], [], 0.0
            for k in keys:
                chunk.append(k)
                load += share_of[k]
                if load >= total / count and len(parts) < count - 1:
                    parts.append((chunk, load))
                    chunk, load = [], 0.0
            if chunk:
                parts.append((chunk, load))
            return parts

        parts = split_up(keys, SAMPLE_PARTS)
        if SAMPLE_EXTRA:
            # ... and, right after the first part, the entries that most often fail on their
            # own when the optimizer or the engine is changed
            taken = set(keys)
            extra = [sm["keys"][i] for i in live if sm["keys"][i] not in taken and sm["keys"][i].rsplit("#", 1)[-1] in SAMPLE_EXTRA]
            if extra:
                load = sum(share_of[k] for k in extra)
                parts.insert(1, (extra, load))
                spent += load
        out = []
        for chunk, load in parts:
            variant = {"allow": chunk}
            if short is not None:
                # unchanged hand-overs among these entries need no slow half
                variant = dict(short[0])
                variant["allow"] = chunk
                variant["memo_budget"] = 1e12
                variant["memo_leash"] = 1
            out.append((variant, self.cpu[n] * (load + 0.02)))
        rest = None
        if SETTLE and short is not None:
            # For a patch that changes what only some entries hand over (the first part shows
            # it): all the other entries at once.  Their quick halves tell which of them are
            # handed something new.  If running those costs no more than the rest of the
            # sample would have, they are all run and the verdict is that of the whole test;
            # otherwise the least expensive of them are run, for the same price.
            first, load = parts[0]
            taken = set(first)
            others = [sm["keys"][i] for i in live if sm["keys"][i] not in taken]
            allowance = self.cpu[n] * max(0.0, spent - load)
            variant = dict(short[0])
            variant["allow"] = others
            variant["memo_budget"] = max(allowance, self.cpu[n] * WEIGHED_SHARE)
            variant["memo_sample"] = allowance
            variant["memo_leash"] = 1
            known = self.maps.memo(tid)
            quick = self.cpu[n] * known["share"] * len(others) / max(1, len(live))
            rest = (variant, quick + variant["memo_budget"] + 0.02 * self.cpu[n])
        return out, self.cpu[n] * (spent + 0.02), rest

    def class_list_uses(self, rel, name):
        """Line spans of the statements in test file `rel` (outside the class set-up) that
        mention a class-level list holding fixture file `name`; None when that cannot be told
        (the name shows up in another test file too, or the file looks attributes up by text)."""
        key = (rel, name)
        if key not in self._list_uses:
            spans = None
            attrs = [a for a, f in variants.CLASS_LISTS.items() if f == name]
            try:
                with open(os.path.join(BASE, rel), encoding="utf-8") as fh:
                    text = fh.read()
                elsewhere = False
                for path in glob.glob(os.path.join(BASE, "tests", "**", "*.py"), recursive=True):
                    if os.path.relpath(path, BASE) != rel:
                        with open(path, encoding="utf-8", errors="replace") as fh:
                            other = fh.read()
                        if any(a in other for a in attrs):
                            elsewhere = True
                            break
                tree = ast.parse(text)
                by_text = any(
                    (isinstance(node, ast.Name) and node.id in ("getattr", "setattr", "delattr", "vars", "dir", "eval", "exec", "globals", "locals"))
                    or (isinstance(node, ast.Attribute) and node.attr in ("__dict__", "__getattribute__"))
                    for node in ast.walk(tree)
                )
                if attrs and not elsewhere and not by_text:
                    spans = []

                    def visit(node, stmt, setup):
                        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in ("setUpClass", "tearDownClass"):
                            setup = True
                        if isinstance(node, ast.stmt):
                            stmt = node
                        mention = (
                            (isinstance(node, ast.Attribute) and node.attr in attrs)
                            or (isinstance(node, ast.Name) and node.id in attrs)
                            or (isinstance(node, ast.Constant) and isinstance(node.value, str) and any(a in node.value for a in attrs))
                        )
                        if mention and not setup and stmt is not None:
                            spans.append((stmt.lineno, stmt.end_lineno or stmt.lineno))
                        for child in ast.iter_child_nodes(node):
                            visit(child, stmt, setup)

                    visit(tree, None, False)
            except (OSError, SyntaxError, ValueError, RecursionError):
                spans = None
            self._list_uses[key] = spans
        return self._list_uses[key]

    def only_class_setup(self, plan, n, idx, mine):
        """Can the patch reach test n only by way of its class set-up?  True when all it
        touches for this test are fixture files that the set-up loads into class-level lists
        and the test, when traced, ran none of the statements that mention those lists: then
        the test passes exactly when the set-up still succeeds."""
        if not mine or not self.maps.known(self.id_of[n].split("::")[0]):
            return False
        rel = self.id_of[n].split("::")[0]
        for kind, path, _, _, _ in mine:
            if kind != "data" or not path.startswith(FIXTURES) or path[len(FIXTURES):] not in CLASS_FIXTURES:
                return False
            spans = self.class_list_uses(rel, path[len(FIXTURES):])
            if spans is None:
                return False
            for lo, hi in spans:
                if any((mask >> idx) & 1 for _, mask in self.maps.func_lines(rel, lo, hi)):
                    return False
        return True

    def plain_variant(self, plan, n):
        tid = self.id_of[n]
        idx = self.idx_of.get(n)
        if idx is None or tid.split("::")[0] in plan.files:
            return None, 1.0  # unknown to the maps, or the test file itself is patched
        if SETUP_ONLY and self.only_class_setup(plan, n, idx, [p for p in plan.parts if (p[4] >> idx) & 1]):
            return {"setup": 1}, 0.002
        if tid in variants.SCALED:
            scaled = self.maps.scaled_ms.get(tid)
            return {"scaled": 1}, (scaled * CAL / max(1.0, self.cpu[n])) if scaled is not None else SCALED_SHARE
        sm = self.maps.slices(tid)
        if sm is None:
            return None, 1.0
        nkeys = len(sm["keys"])
        full = (1 << nkeys) - 1
        allow = 0
        mine = [p for p in plan.parts if (p[4] >> idx) & 1]
        if mine and all(p[0] == "data" for p in mine):
            # only edited fixture files reach this test: run just the entries that changed
            files, share = [], 0.0
            for _, rel, _, _, _ in mine:
                if not rel.startswith(FIXTURES) or not rel.endswith(".sql"):
                    return None, 1.0
                name = rel[len(FIXTURES):]
                per = [sm["share"][i] for i, k in enumerate(sm["keys"]) if k.startswith(name + "#")]
                if not per:
                    if name in CLASS_FIXTURES:
                        # loaded by the class set-up for other tests; this one uses no entry of it
                        continue
                    return None, 1.0  # the test reads this file some other way
                files.append(name)
                share += 2.0 * sum(per) / len(per)  # an edit usually touches one entry
            if not files:
                return {"allow": []}, 0.02  # only the set-up can be affected: run the test empty
            # ... and no entry of the files that were not edited: nothing this test reaches
            # in them has changed
            return {"changed": files, "allow": []}, min(1.0, share + 0.02)
        for kind, rel, lo, hi, mask in plan.parts:
            if not (mask >> idx) & 1:
                continue
            if kind != "func":
                # a class constant or default value: of a sliced test, only the entries that
                # read it (when no entry is recorded as doing so, the test reaches it some
                # other way and runs whole)
                entries = plan.entry.get((rel, lo, hi)) if ENTRY_NARROW and kind == "mod" else None
                reach = entries(tid) if entries is not None else None
                if not reach:
                    explained = getattr(entries, "explained", None)
                    if reach == 0 and explained is not None and explained(tid):
                        # Several conditions, each met by some entries, but by no entry all at
                        # once: no entry can notice the edit.  What the test does outside its
                        # fixture loop still runs.
                        continue
                    return None, 1.0
                allow |= reach
                if allow == full:
                    return None, 1.0
                continue
            table = sm["lines"].get(rel, {})
            part = 0
            for line, m in self.maps.func_lines(rel, lo, hi):
                if (m >> idx) & 1:
                    sl = table.get(line, 0)
                    if not sl or self.maps.setup_runs(tid, rel, line):
                        return None, 1.0  # runs outside the fixture loop (class set-up, ...)
                    part |= sl
            entries = plan.entry.get((rel, lo, hi)) if ENTRY_NARROW else None
            if entries is not None and part:
                # a swapped operator or regrouped condition: of the entries that reach the
                # line, only those in which it can behave differently (when none is recorded
                # as such although the test is a candidate, the recording does not explain
                # the test and every entry that reaches the line is run)
                reach = entries(tid)
                if reach is not None and part & reach:
                    part &= reach
            allow |= part
            if allow == full:
                return None, 1.0
        share = sm["share"]
        keys = []
        frac = 0.0
        for i, k in enumerate(sm["keys"]):
            if (allow >> i) & 1:
                keys.append(k)
                frac += share[i]
        return {"allow": keys}, min(1.0, frac + 0.02)

    def check(self, plan, cand, dbg):
        """Run candidates in stages, cheapest evidence first; return (tests to select, how)."""
        ms = self.ms
        cheap, costly, deferred, likely = [], [], [], []
        data_mask = 0
        for kind, rel, lo, hi, mask in plan.parts:
            if kind == "data" and not rel.endswith(".py"):
                data_mask |= mask
        # collection order: the order the suite itself runs in, so tests see the same
        # leftovers from earlier tests as they do in a real run
        for n in sorted(cand):
            v, share = self.variant_for(plan, n)
            est = self.cpu[n] * share
            idx = self.idx_of.get(n)
            if v is None and est > LIKELY * 1000 and idx is not None and (data_mask >> idx) & 1:
                likely.append(n)  # reads an edited fixture and is slow: select it unseen
            elif est > HEAVY * 1000:
                deferred.append((n, v, est))
            elif est > CHEAP * 1000:
                costly.append((n, v, est))
            else:
                cheap.append((n, v, est))
        if ORDER != "collection" and STOP_FIRST:
            # The first stage ends at the first failure, so what matters is how soon one shows:
            # the tests named after a patched file come first, and within each group the
            # least expensive.  (Every test still runs on its own; only the order changes.)
            near = self.named_after(plan) if ORDER == "name" else (lambda n: 1)
            cheap.sort(key=lambda it: (near(it[0]), it[2], it[0]))
        total_est = sum(e for _, _, e in cheap) + sum(e for _, _, e in costly)
        dbg["est_ms"] = int(total_est)
        if DUMP:
            # for studying run orders off line: every candidate with its estimate, its class
            # and whether it is run through a part of its fixture entries
            near = self.named_after(plan)
            dbg["cands"] = [(n, int(e), k, near(n), 1 if (v and "allow" in v) else 0) for k, items in (("c", cheap), ("m", costly), ("d", deferred)) for n, v, e in items]
            dbg["likely"] = likely
            return sorted(cand), "dump"
        # "broad": running every candidate would cost about as much CPU as just selecting them all
        cost_all = None
        if total_est > BROAD_MIN * 1000:
            cost_all = split([n for n, _, _ in cheap + costly + deferred], ms)[1]
        # (a small set is always run: checking it costs about what selecting it would)
        broad = cost_all is not None and cost_all >= BROAD_COST * 1000 and total_est > BROAD_FRAC * cost_all
        if broad:
            # Running a sample proves nothing about the tests left out, so select the candidates
            # unseen.  Only tests whose own weight in the bill exceeds the cost of checking them
            # are run.
            return self.broad_blind(plan, cheap + costly + deferred, likely, dbg)
        dbg["unseen"] = likely
        if not cheap and not costly and not deferred:
            return likely, "unseen"

        lane = self.get_lane()
        st = {"mode": lane.fresh_mode(plan.files) if plan.fresh else "full", "open": False, "evals": 0, "differ": 0, "probed": False, "cpu": 0, "ran": 0, "cut": None, "planned": 0.0, "outcome": {}, "undecided": set(), "note": {}}

        def install():
            lane.ensure(st["mode"])
            for rel, content in plan.files.items():
                lane.overlay.set_file(rel, content)

        def run(items, stop_on_fail, patience=5.0):
            if st["cut"]:
                return [], [n for n, _, _ in items]  # the child gave up earlier: do not run more
            jobs = [(self.id_of[n], max(1.5, patience * e * self.slack.get(n, 1.0) / 1000.0 + 0.5), v) for n, v, e in items]
            # total CPU this commit may burn: generous against the estimate, but bounded
            st["planned"] += sum(e * self.slack.get(n, 1.0) for n, _, e in items) / 1000.0
            cpu_budget = min(COMMIT_CPU, 3.0 + 2.5 * st["planned"])
            if not st["open"]:
                budget = 30.0 + 4.0 * (total_est + sum(e for _, _, e in deferred)) / 1000.0
                budget = max(10.0, min(budget, self.stop_at + 30.0 - time.monotonic()))
                full = st["mode"] == "full"
                results, err = lane.run(st["mode"], jobs, swap=plan.swaps if full else (), consts=plan.consts if full else (), probe=plan.probe if full else (), stop_on_fail=stop_on_fail, deadline=budget, stages=True, cpu_budget=cpu_budget)
                if err and full and not results:
                    # the warm worker cannot take this change: start from a fresh import instead
                    dbg["fallback"] = err
                    lane.finish()
                    lane.overlay.restore()
                    st["mode"] = lane.fresh_mode(plan.files)
                    install()
                    results, err = lane.run(st["mode"], jobs, stop_on_fail=stop_on_fail, deadline=budget, stages=True, cpu_budget=cpu_budget)
                st["open"] = True
                st["probed"] = lane.probed and st["mode"] == "full"
            else:
                results, err = lane.more(jobs, stop_on_fail=stop_on_fail, cpu_budget=cpu_budget)
            if err:
                dbg["err"] = err
            if lane.cut:
                st["cut"] = lane.cut
                dbg["cut"] = lane.cut
            failed, unseen = [], []
            for i, (n, v, e) in enumerate(items):
                r = results.get(i)
                if r is None or r[0] == "U":
                    unseen.append(n)
                    if r is not None and v and "memo" in v:
                        st["undecided"].add(n)  # not a failure: simply not settled this way
                else:
                    st["ran"] += 1
                    st["cpu"] += r[1]
                    st["outcome"][n] = r[0]
                    st["note"][n] = r[4] if len(r) > 4 else ""
                    if DEBUG:
                        dbg.setdefault("tests", []).append((n, r[1], int(e), r[0], (len(v.get("allow", ())) if v and "allow" in v else -1), r[2], r[3]))
                    st["evals"] += r[2]
                    st["differ"] += r[3]
                    if r[0] == "F":
                        failed.append(n)
                        dbg.setdefault("failed", []).append(self.id_of[n])
            return failed, unseen

        def cost_of(numbers):
            return split(numbers, ms)[1]

        def settle(n, sv=None, weigh=True, first=None):
            """Run slow sliced test n in a few parts, its least expensive entries first, each
            part on a short leash: "F" when a part fails, "U" when one takes much longer than
            it should or the test cannot be run this way, "P" when every part passed.
            `first`: what the first part said, when it has been run (and passed) already."""
            sv = sv or self.sample_variant(plan, n)
            if sv is None:
                return "U"
            parts, rest = sv[0], sv[2] if weigh else None
            if first is None:
                # (the child's CPU allowance follows what is planned: plan all of it now)
                st["planned"] += sv[1] / 1000.0
            for k, (variant, est) in enumerate(parts):
                if k == 0 and first is not None:
                    note = first
                else:
                    f3, u3 = run([(n, variant, est)], False, patience=1.8)
                    if f3:
                        return "F"
                    if u3:
                        return "U"
                    note = st["note"].get(n, "")
                    if k == 0:
                        dbg.setdefault("first_part", []).append((n, note))
                recognised, _, new = note.partition("/")
                if k == 0 and rest is not None and recognised.isdigit() and int(recognised) > 0 and (new == "0" or WEIGH_MIXED):
                    # every entry of the first part is handed what it was always handed, so
                    # the patch reaches the test through few entries and a sample of the
                    # cheapest tells little: weigh the others instead
                    st["planned"] += rest[1] / 1000.0
                    f3, u3 = run([(n, rest[0], rest[1])], False, patience=1.8)
                    dbg.setdefault("weighed", []).append((n, "F" if f3 else "U" if u3 else "P", st["note"].get(n, "")))
                    return "F" if f3 else "U" if u3 else "P"
            return "P"

        selected, how, unverified = [], None, []
        try:
            install()
            failed = []
            kept = []  # cheap candidates left selected without being run
            first = cheap
            if CLEAN_STOP > 0 and STOP_FIRST and (cheap or costly) and (CLEAN_ANY or self.plain_kind(plan)):
                near = self.named_after(plan) if ORDER == "name" else (lambda n: 1)
                first, spent = [], 0.0
                budget = min(CLEAN_STOP * 1000, max(CLEAN_MIN * 1000, CLEAN_FRAC * total_est))
                for item in sorted(cheap + costly, key=lambda it: (near(it[0]), it[2], it[0])):
                    if spent >= budget:
                        break
                    first.append(item)
                    spent += item[2]
            if first:
                failed, rest = run(first, STOP_FIRST)
                if first is not cheap:
                    light = {it[0] for it in cheap}
                    if not failed and not rest and not st["cut"]:
                        # everything looked at passes: the others are let go unseen
                        dbg["stopped"] = len(cheap) + len(costly) - len(first)
                        costly = []
                    else:
                        taken = {it[0] for it in first}
                        rest = [n for n in rest if n in light] + [it[0] for it in cheap if it[0] not in taken]
                        costly = [it for it in costly if it[0] not in st["outcome"]]
                open_ = [n for n in rest if n in st["undecided"]]
                rest = [n for n in rest if n not in st["undecided"]]
                if rest and (st["cut"] or not failed or not STOP_FIRST):
                    failed = failed + rest  # could not be checked
                elif rest:
                    # One test fails, so the commit pays for a worker whatever the others do.
                    # From here a test is only run when that costs less CPU than the seconds
                    # it would add to the selection if left in unchecked.
                    by = {it[0]: it for it in cheap}
                    kept = list(rest)
                    again = []
                    current = cost_of(failed + kept)
                    # How likely the tests that are left are to pass shows in what came before
                    # the failure: when several tests passed first, the breakage is narrow and
                    # the others mostly pass; when the very first test failed, mostly not.
                    mixed = st["ran"] - len(failed) >= MIXED_AFTER
                    p_again = P_PASS_AGAIN_MIXED if mixed else P_PASS_AGAIN
                    p_rest = P_PASS_REST_MIXED if mixed else P_PASS_REST
                    for n in sorted(rest, key=lambda n: -ms[n])[:12]:
                        if ms[n] < AGAIN_MIN * 1000:
                            break  # too light to be worth a run of its own
                        without = cost_of(failed + [m for m in kept if m != n])
                        if by[n][2] < p_again * (current - without):
                            again.append(by[n])
                            kept.remove(n)
                            current = without
                    if kept and sum(by[n][2] for n in kept) < p_rest * (current - cost_of(failed)):
                        again.extend(by[n] for n in kept)
                        kept = []
                    if again:
                        again.sort(key=lambda it: it[0])
                        f2, u2 = run(again, False)
                        failed = failed + f2 + u2
                kept = open_ + kept
            later = costly + deferred
            if later:
                # for each remaining test: is running it cheaper than just selecting it?
                p_pass = P_PASS_FAILED if failed else P_PASS_CLEAN
                base = failed + kept
                run_list, keep = [], []
                for item in sorted(later, key=lambda it: it[2]):
                    n, v, est = item
                    keep_cost = cost_of(base + keep + [n]) - cost_of(base + keep)
                    if est < p_pass * keep_cost and (failed or est <= CLEAN_RUN * 1000 or self.slice_probe(plan, n)[1] is None):
                        run_list.append(item)
                    else:
                        keep.append(n)
                if run_list:
                    run_list.sort(key=lambda it: it[0])
                    failed2, unseen2 = run(run_list, False)
                    failed = failed + failed2 + [n for n in unseen2 if n not in st["undecided"]]
                    keep = keep + [n for n in unseen2 if n in st["undecided"]]
                keep = kept + keep
                looked = {}  # slow test -> (its sample, what the first part said) once that part passed
                if keep and failed and FIRST_LOOK and not st["cut"] and SAMPLE_SHARE > 0:
                    # A slow fixture-driven test that is still selected unchecked next to real
                    # failures: the first part of its sample comes before anything else that
                    # is done about it.  When such a test fails it mostly fails there, and
                    # then nothing more need be run on its account.
                    for n in list(keep):
                        if ms[n] <= HEAVY * 1000 or n in st["undecided"]:
                            continue
                        weight = cost_of(failed + keep) - cost_of(failed + [m for m in keep if m != n])
                        if weight < DROP_MIN * 1000:
                            continue
                        sv = self.sample_variant(plan, n)
                        if sv is None or sv[1] >= P_PASS_SAMPLE * weight:
                            continue
                        st["planned"] += sv[1] / 1000.0
                        f3, u3 = run([(n, sv[0][0][0], sv[0][0][1])], False, patience=1.8)
                        dbg.setdefault("first_part", []).append((n, st["note"].get(n, "")))
                        if f3:
                            failed.append(n)
                            keep = [m for m in keep if m != n]
                            dbg.setdefault("sampled", []).append((n, "F"))
                        elif u3:
                            looked[n] = None  # not settled this way: it stays
                            dbg.setdefault("sampled", []).append((n, "U"))
                        elif FIRST_ONLY:
                            keep = [m for m in keep if m != n]
                            dbg.setdefault("sampled", []).append((n, "P"))
                            dbg.setdefault("sample_dropped", []).append(n)
                        else:
                            looked[n] = (sv, st["note"].get(n, ""))
                if keep and not failed:
                    # nothing fails so far; look at a sample of each remaining slow test
                    probes = [(n, pv, pe) for n, pv, pe in (self.slice_probe(plan, n) for n in keep) if pv is not None]
                    hit = []
                    if probes:
                        hit, _ = run(probes, False)
                        dbg["probe_hit"] = hit
                    if hit:
                        failed = hit
                        how = "fail"
                    elif HEAVY_CLEAN == "drop" and len(probes) == len(keep) and (st["ran"] - len(probes) >= MIN_CLEAN or dbg.get("stopped")):
                        # every cheaper candidate passes and so does a sample of each slow one:
                        # the slow tests that weigh on the bill are let go; a sample is not the
                        # whole test, so the ones that are cheap to keep stay selected
                        unverified = list(keep)
                        for n in sorted(keep, key=lambda n: -ms[n]):
                            if cost_of(keep) - cost_of([m for m in keep if m != n]) >= DROP_MIN * 1000:
                                # (where unchanged hand-overs are recognised, a larger sample
                                # than the probe is cheap: take it before letting the test go)
                                sv = self.sample_variant(plan, n) if SETTLE else None
                                verdict = settle(n, sv, weigh=False) if sv is not None and sv[0][0][0].get("memo") else "P"
                                dbg.setdefault("settled", []).append((n, verdict))
                                if verdict == "F":
                                    failed = [n]
                                    how = "fail"
                                    break
                                if verdict == "P":
                                    keep = [m for m in keep if m != n]
                        if how is None:
                            how = "heavy-kept" if keep else "heavy-dropped"
                    else:
                        unverified = keep
                        how = "heavy-kept"
                elif keep and failed and HEAVY_FAILED == "related":
                    # Something else fails.  A slow test we could not afford to run stays
                    # selected unless the tests closest to it (same test file, or reading the
                    # same fixture file) were run and all passed, and the breakage is small.
                    slow = {it[0] for it in deferred} | st["undecided"]
                    by_n = {it[0]: it for it in cheap + later}
                    maybe = []
                    if len(failed) < MANY_FAILED:
                        for n in sorted((n for n in keep if n in slow), key=lambda n: -ms[n]):
                            if n in looked and looked[n] is None:
                                continue
                            rel_mask = self.related_mask(n)
                            pending = [m for m in keep if m != n and m not in slow and self.idx_of.get(m) is not None and (rel_mask >> self.idx_of[m]) & 1]
                            if pending:
                                # related tests we kept without running: run them first, if that
                                # costs clearly less than the slow test weighs
                                weight = cost_of(failed + keep) - cost_of(failed + [m for m in keep if m != n])
                                if sum(by_n[m][2] for m in pending) > min(RELATED_RUN * weight, RELATED_CAP * 1000):
                                    continue
                                items = sorted((by_n[m] for m in pending), key=lambda it: it[0])
                                f2, u2 = run(items, False)
                                bad = set(f2 + u2)
                                failed = failed + [m for m in f2 + u2 if m not in failed]
                                keep = [m for m in keep if m in bad or m not in pending]
                                if bad:
                                    continue
                            rel_pass, rel_fail = self.related_outcomes(n, st["outcome"])
                            unrun = [m for m in keep if m != n and self.idx_of.get(m) is not None and (rel_mask >> self.idx_of[m]) & 1 and m not in st["outcome"] and m not in slow]
                            if rel_fail == 0 and rel_pass >= MIN_RELATED and not unrun:
                                maybe.append(n)
                    # (a test whose first sample part has passed needs no probe: that part
                    # holds the probe's entries)
                    seen = [n for n in maybe if looked.get(n)]
                    probes = [(n, pv, pe) for n, pv, pe in (self.slice_probe(plan, n) for n in maybe if not looked.get(n)) if pv is not None]
                    if probes or seen:
                        hit, unseen = run(probes, False) if probes else ([], [])
                        hit = hit + unseen
                        # a sample that fails is a failure among the slow tests' own
                        # neighbours: then none of them is let go on the strength of a sample
                        dropped = [] if hit else seen + [n for n, _, _ in probes]
                        failed = failed + [m for m in hit if m not in failed]
                        dbg["related_dropped"] = dropped
                        keep = [n for n in keep if n not in dropped]
                if keep and failed and not st["cut"] and SAMPLE_SHARE > 0:
                    # A slow fixture-driven test is still selected unchecked next to real
                    # failures, and weighs heavily on the bill.  Run its cheapest entries, up
                    # to a share of its cost: when such a test fails it fails on most of its
                    # entries, so a failure shows at once; if every sampled entry passes, the
                    # test is let go.  (Entries whose slow half is handed exactly what it was
                    # handed before the patch are recognised without running it: br/memo.py.)
                    sampled = []
                    for n in keep:
                        if n in looked:
                            if looked[n] is not None:
                                sampled.append((n, looked[n][0], looked[n][1]))
                            continue
                        if ms[n] <= HEAVY * 1000 or n in st["undecided"]:
                            continue
                        weight = cost_of(failed + keep) - cost_of(failed + [m for m in keep if m != n])
                        if weight < DROP_MIN * 1000:
                            continue
                        sv = self.sample_variant(plan, n)
                        if sv is not None and sv[1] < P_PASS_SAMPLE * weight:
                            sampled.append((n, sv, None))
                    passed = []
                    for n, sv, first in sampled:
                        verdict = settle(n, sv, first=first)
                        dbg.setdefault("sampled", []).append((n, verdict))
                        if verdict == "F" and n not in failed:
                            failed.append(n)
                        elif verdict == "P":
                            passed.append(n)
                    if passed:
                        keep = [n for n in keep if n not in passed]
                        dbg["sample_dropped"] = sorted(passed)
                selected = failed + [n for n in keep if n not in failed]
                if failed:
                    unverified = [n for n in keep if n not in failed]
            else:
                selected = failed + kept
                unverified = kept
            if how is None:
                how = "fail" if failed else "clean"
        finally:
            try:
                lane.finish()
            finally:
                lane.overlay.restore()
        dbg["mode"] = st["mode"]
        dbg["probed"] = st["probed"]
        dbg["ran"] = st["ran"]
        dbg["cpu_ms"] = st["cpu"]
        dbg["unverified"] = unverified
        dbg["unseen"] = likely
        return selected + likely, how

    def broad_blind(self, plan, items, likely, dbg):
        ms = self.ms
        everything = [n for n, _, _ in items]
        keep = set(everything)
        by_n = {it[0]: it for it in items}
        to_run = []
        for n in sorted(everything, key=lambda n: -ms[n])[:12]:
            est = by_n[n][2]
            current = split(sorted(keep), ms)[1]
            without = split(sorted(keep - {n}), ms)[1]
            if est < P_PASS_CLEAN * (current - without):
                to_run.append(by_n[n])
                keep.discard(n)
        # A slow test that weighs on the bill can still be settled through the tests closest to
        # it (same test file / same fixture file): run those and a sample of the slow test.
        bundles = []  # (slow test, its related tests, its sample)
        if HEAVY_FAILED == "related":
            scheduled = {it[0] for it in to_run}
            for n in sorted(keep, key=lambda n: -ms[n])[:4]:
                if by_n[n][2] <= HEAVY * 1000:
                    continue
                gain = split(sorted(keep), ms)[1] - split(sorted(keep - {n}), ms)[1]
                rel_mask = self.related_mask(n)
                related = [by_n[m] for m in everything if m != n and by_n[m][2] <= HEAVY * 1000 and self.idx_of.get(m) is not None and (rel_mask >> self.idx_of[m]) & 1]
                pn, pv, pe = self.slice_probe(plan, n)
                if pv is None or len(related) < MIN_RELATED:
                    continue
                cost = pe + sum(e for m, _, e in related if m not in scheduled)
                if cost < 0.5 * gain:
                    bundles.append((n, [m for m, _, _ in related], (pn, pv, pe)))
                    for it in related:
                        if it[0] not in scheduled:
                            scheduled.add(it[0])
                            to_run.append(it)
                    to_run.append((pn, pv, pe))
        dbg["broad"] = len(everything)
        dbg["unverified"] = sorted(keep)
        if not to_run:
            return sorted(keep) + likely, "broad-blind"
        lane = self.get_lane()
        mode = lane.fresh_mode(plan.files) if plan.fresh else "full"
        back = []
        lane.ensure(mode)
        try:
            for rel, content in plan.files.items():
                lane.overlay.set_file(rel, content)
            to_run.sort(key=lambda it: it[0])
            jobs = [(self.id_of[n], max(1.5, 5.0 * e * self.slack.get(n, 1.0) / 1000.0 + 0.5), v) for n, v, e in to_run]
            full = mode == "full"
            results, err = lane.run(mode, jobs, swap=plan.swaps if full else (), consts=plan.consts if full else (), deadline=60.0, cpu_budget=COMMIT_CPU)
            if err and full and not results:
                dbg["fallback"] = err
                lane.overlay.restore()
                mode = lane.fresh_mode(plan.files)
                lane.ensure(mode)
                for rel, content in plan.files.items():
                    lane.overlay.set_file(rel, content)
                results, err = lane.run(mode, jobs, deadline=60.0, cpu_budget=COMMIT_CPU)
            outcome = {}
            sampled = {b[0] for b in bundles}
            for i, (n, v, e) in enumerate(to_run):
                r = results.get(i)
                bad = r is None or r[0] in ("F", "U")
                outcome[n] = "F" if bad else "P"
                if n in sampled:
                    continue  # a sample, not the whole test: settled with its bundle below
                if bad:
                    back.append(n)  # fails, or could not be checked: stays selected
                keep.discard(n)
            dropped = []
            sample_failed = any(outcome.get(n) != "P" for n, _, _ in bundles)
            for n, related, _ in bundles:
                # (one failing sample among the slow tests and none of them is let go)
                if not sample_failed and outcome.get(n) == "P" and all(outcome.get(m) == "P" for m in related):
                    keep.discard(n)
                    dropped.append(n)
            if dropped:
                dbg["related_dropped"] = dropped
            dbg["unverified"] = sorted(keep)
            dbg["ran"] = len(results)
            dbg["cpu_ms"] = sum(r[1] for r in results.values())
            dbg["mode"] = mode
        finally:
            lane.overlay.restore()
        return sorted(keep) + back + likely, "broad-blind"

    def related_mask(self, n):
        """Map-index mask of the tests close to test n (same test file, or a shared fixture)."""
        mask = self._related.get(n)
        if mask is None:
            mask = 0
            idx = self.idx_of.get(n)
            path = self.id_of[n].split("::")[0]
            for m, tid in self.id_of.items():
                if tid.startswith(path + "::"):
                    j = self.idx_of.get(m)
                    if j is not None:
                        mask |= 1 << j
            if idx is not None:
                for f, readers in self.maps.opened.items():
                    if f.startswith(FIXTURES) and (readers >> idx) & 1:
                        mask |= readers
            self._related[n] = mask
        return mask

    def related_outcomes(self, n, outcome):
        """(passed, failed) among the tests that ran and are close to test n: same test file,
        or reading one of the fixture files n reads."""
        if self.idx_of.get(n) is None:
            return 0, 0
        mask = self.related_mask(n)
        passed = failed = 0
        for m, o in outcome.items():
            j = self.idx_of.get(m)
            if m != n and j is not None and (mask >> j) & 1:
                if o == "F":
                    failed += 1
                elif o == "P":
                    passed += 1
        return passed, failed

    def slice_probe(self, plan, n):
        """A cheap sample of a sliced test: its least expensive fixture entries."""
        tid = self.id_of[n]
        if tid.split("::")[0] in plan.files:
            return n, None, 0.0
        sm = self.maps.slices(tid)
        if sm is None:
            return n, None, 0.0
        v, share = self.variant_for(plan, n)
        allowed = set(v["allow"]) if v and "allow" in v else None
        order = sorted(range(len(sm["keys"])), key=lambda i: sm["share"][i])
        keys, spent, free = [], 0.0, []
        budget = SLICE_PROBE * 1000.0 / max(1.0, self.cpu[n])
        for i in order:
            k = sm["keys"][i]
            if allowed is not None and k not in allowed:
                continue
            if sm["share"][i] <= 0.0:
                # nothing measurable, but the test still parses it: always part of the probe
                free.append(k)
                continue
            if keys and spent + sm["share"][i] > budget:
                break
            keys.append(k)
            spent += sm["share"][i]
            if len(keys) >= 6:
                break
        if not keys:
            return n, None, 0.0
        return n, {"allow": free + keys}, self.cpu[n] * (spent + 0.02)

    def run(self):
        patches = sorted(glob.glob(os.path.join(DATA, "commits", "*.patch")))
        out = sys.stdout
        # the batch has 3 minutes (4.5 in the final, with about 1.5x the commits): stop running
        # tests well before that and select candidates unseen for whatever is left
        hard = 180.0 + 90.0 * min(1.0, max(0.0, (len(patches) - 40) / 20.0))
        self.stop_at = T0 + float(os.environ.get("BR_SOFT", str((0.68 if EXACT else 0.55) * hard)))
        if EXACT:
            patches = self.cheapest_first(patches)
        for path in patches:
            commit = os.path.basename(path)[: -len(".patch")]
            try:
                with open(path, "rb") as fh:
                    data = fh.read()
                before = sum(lane.child_ms for lane in (self.lane, self.lane2) if lane is not None)
                selected = self.decide(commit, data)
                if DEBUG:
                    self.debug[commit]["child_ms"] = sum(lane.child_ms for lane in (self.lane, self.lane2) if lane is not None) - before
            except Exception as e:  # never lose a commit: fall back to running everything
                log("commit %s: %r -> selecting all" % (commit, e))
                selected = list(self.all_numbers)
                self.debug.setdefault(commit, {})["crash"] = repr(e)
            workers, cost = split(selected, self.ms)
            self.debug.setdefault(commit, {})["cost_ms"] = cost
            self.debug[commit]["selected"] = len(selected)
            out.write(json.dumps({"commit": commit, "workers": workers}, separators=(",", ":")) + "\n")
        out.flush()
        for lane in (self.lane, self.lane2):
            if lane is not None:
                lane.close()
        if DEBUG:
            with open(DEBUG, "w") as fh:
                json.dump(self.debug, fh, indent=1)
        try:
            import resource

            kids = resource.getrusage(resource.RUSAGE_CHILDREN)
            hows = {}
            for d in self.debug.values():
                h = hows.setdefault(d.get("how", "?"), [0, 0])
                h[0] += 1
                h[1] += d.get("cpu_ms", 0) or 0
            log("cpu: main %.2fs (tests %.2fs) | %s" % (
                time.process_time(),
                sum(h[1] for h in hows.values()) / 1000.0,
                " ".join("%s=%d/%.1fs" % (k, v[0], v[1] / 1000.0) for k, v in sorted(hows.items()))))
            top = sorted(self.debug.items(), key=lambda kv: -(kv[1].get("cpu_ms") or 0))[:6]
            log("slowest: " + " ".join("%s=%.1fs/%s/%d" % (c, (d.get("cpu_ms") or 0) / 1000.0, d.get("how"), d.get("ran") or 0) for c, d in top))
        except Exception:
            pass
        log("done %d commits in %.1fs wall" % (len(patches), time.monotonic() - T0))


if __name__ == "__main__":
    Selector().run()
    # everything is written: leave without tearing the interpreter down (maps, syntax trees)
    try:
        sys.stdout.flush()
        sys.stderr.flush()
    finally:
        os._exit(0)
