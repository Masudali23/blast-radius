"""Blast Radius selector.

For every patch: work out which tests can be affected (coverage maps built from the pinned
commit), run those tests against the patched code in a warm interpreter, and select exactly the
ones that fail.  Whatever cannot be checked cheaply is selected rather than skipped.
"""
import glob
import json
import os
import sys
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
CAL = float(os.environ.get("BR_CAL", "1.0"))
# a candidate set is "broad" when running it all costs more than this share of selecting it all
BROAD_FRAC = float(os.environ.get("BR_BROAD_FRAC", "0.75"))
BROAD_MIN = float(os.environ.get("BR_BROAD_MIN", "2.0"))
SCALED_SHARE = 0.03
# tests up to this estimated cost are simply run; costlier ones only while nothing has failed yet
CHEAP = float(os.environ.get("BR_CHEAP", "0.4"))
# a test this slow that reads an edited fixture file is selected without being run
LIKELY = float(os.environ.get("BR_LIKELY", "0.25"))
# chance that a not-yet-run candidate passes, when nothing / something has failed so far
P_PASS_CLEAN = float(os.environ.get("BR_P_PASS_CLEAN", "0.9"))
P_PASS_FAILED = float(os.environ.get("BR_P_PASS_FAILED", "0.7"))
# CPU seconds one commit may spend running tests, whatever was planned
COMMIT_CPU = float(os.environ.get("BR_COMMIT_CPU", "25"))
# seconds of a slow sliced test to sample before deciding about it
SLICE_PROBE = float(os.environ.get("BR_SLICE_PROBE", "1.0"))
# slow tests still unverified after a clean sample: "keep" them selected, or "drop" them
HEAVY_CLEAN = os.environ.get("BR_HEAVY_CLEAN", "drop")
# slow sliced tests left unverified when other tests fail / in a broad set: "keep" them
# selected, or "probe" (sample them and drop the ones whose sample passes)
HEAVY_FAILED = os.environ.get("BR_HEAVY_FAILED", "keep")
HEAVY_BROAD = os.environ.get("BR_HEAVY_BROAD", "keep")
# ... provided at least this many other candidates actually ran and passed
MIN_CLEAN = int(os.environ.get("BR_MIN_CLEAN", "5"))
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
        self.lane = None
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

    def get_lane(self):
        if self.lane is None:
            self.lane = Lane(BASE, TMP, APP, log)
        return self.lane

    # ------------------------------------------------------------------ one commit
    def decide(self, commit, data):
        plan = planner.make_plan(commit, data, BASE, self.maps)
        dbg = {"kinds": sorted(plan.kinds), "notes": plan.notes, "fresh": plan.fresh}
        self.debug[commit] = dbg
        if plan.unknown:
            dbg["how"] = "all"
            return list(self.all_numbers)
        cand = set(self.numbers(plan.cand)) | set(self.unmapped) if (plan.cand or plan.files) else set()
        if not plan.cand:
            cand = set()
        sure = set(self.numbers(plan.sure))
        cand -= sure
        dbg["ncand"] = len(cand)
        if not cand:
            dbg["how"] = "none" if not sure else "sure"
            return sorted(sure)
        if time.monotonic() > self.stop_at:
            dbg["how"] = "out-of-time"
            return sorted(cand | sure)
        selected, how = self.check(plan, cand, dbg)
        dbg["how"] = how
        return sorted(set(selected) | sure)

    def variant_for(self, plan, n):
        """How to run test n for this plan: (variant or None, share of its recorded time)."""
        tid = self.id_of[n]
        idx = self.idx_of.get(n)
        if idx is None or tid.split("::")[0] in plan.files:
            return None, 1.0  # unknown to the maps, or the test file itself is patched
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
            return {"changed": files}, min(1.0, share + 0.02)
        for kind, rel, lo, hi, mask in plan.parts:
            if not (mask >> idx) & 1:
                continue
            if kind != "func":
                return None, 1.0
            table = sm["lines"].get(rel, {})
            for line, m in self.maps.func_lines(rel, lo, hi):
                if (m >> idx) & 1:
                    sl = table.get(line, 0)
                    if not sl:
                        return None, 1.0  # runs outside the fixture loop (class set-up, ...)
                    allow |= sl
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
        total_est = sum(e for _, _, e in cheap) + sum(e for _, _, e in costly)
        dbg["est_ms"] = int(total_est)
        # "broad": running every candidate would cost about as much CPU as just selecting them all
        cost_all = None
        if total_est > BROAD_MIN * 1000:
            cost_all = split([n for n, _, _ in cheap + costly + deferred], ms)[1]
        broad = cost_all is not None and total_est > BROAD_FRAC * cost_all
        if broad:
            # Running a sample proves nothing about the tests left out, so select the candidates
            # unseen.  Only tests whose own weight in the bill exceeds the cost of checking them
            # are run.
            return self.broad_blind(plan, cheap + costly + deferred, likely, dbg)
        if not cheap and not costly and not deferred:
            return likely, "unseen"

        lane = self.get_lane()
        st = {"mode": "bare" if plan.fresh else "full", "open": False, "evals": 0, "differ": 0, "probed": False, "cpu": 0, "ran": 0, "cut": None, "planned": 0.0}

        def install():
            lane.ensure(st["mode"])
            for rel, content in plan.files.items():
                lane.overlay.set_file(rel, content)

        def run(items, stop_on_fail):
            if st["cut"]:
                return [], [n for n, _, _ in items]  # the child gave up earlier: do not run more
            jobs = [(self.id_of[n], max(1.5, 5.0 * e / 1000.0 + 0.5), v) for n, v, e in items]
            # total CPU this commit may burn: generous against the estimate, but bounded
            st["planned"] += sum(e for _, _, e in items) / 1000.0
            cpu_budget = min(COMMIT_CPU, 3.0 + 2.5 * st["planned"])
            if not st["open"]:
                budget = 30.0 + 4.0 * (total_est + sum(e for _, _, e in deferred)) / 1000.0
                budget = max(10.0, min(budget, self.stop_at + 30.0 - time.monotonic()))
                full = st["mode"] == "full"
                results, err = lane.run(st["mode"], jobs, swap=plan.swaps if full else (), probe=plan.probe if full else (), stop_on_fail=stop_on_fail, deadline=budget, stages=True, cpu_budget=cpu_budget)
                if err and full and not results:
                    # the warm worker cannot take this change: start from a fresh import instead
                    dbg["fallback"] = err
                    lane.finish()
                    lane.overlay.restore()
                    st["mode"] = "bare"
                    install()
                    results, err = lane.run("bare", jobs, stop_on_fail=stop_on_fail, deadline=budget, stages=True, cpu_budget=cpu_budget)
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
                else:
                    st["ran"] += 1
                    st["cpu"] += r[1]
                    st["evals"] += r[2]
                    st["differ"] += r[3]
                    if r[0] == "F":
                        failed.append(n)
                        dbg.setdefault("failed", []).append(self.id_of[n])
            return failed, unseen

        def cost_of(numbers):
            return split(numbers, ms)[1]

        selected, how, unverified = [], None, []
        try:
            install()
            failed = []
            if cheap:
                failed, unseen = run(cheap, False)
                failed = failed + unseen
            later = costly + deferred
            if later:
                # for each remaining test: is running it cheaper than just selecting it?
                p_pass = P_PASS_FAILED if failed else P_PASS_CLEAN
                base = list(failed)
                run_list, keep = [], []
                for item in sorted(later, key=lambda it: it[2]):
                    n, v, est = item
                    keep_cost = cost_of(base + keep + [n]) - cost_of(base + keep)
                    if est < p_pass * keep_cost:
                        run_list.append(item)
                    else:
                        keep.append(n)
                if run_list:
                    run_list.sort(key=lambda it: it[0])
                    failed2, unseen2 = run(run_list, False)
                    failed = failed + failed2 + unseen2
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
                    elif HEAVY_CLEAN == "drop" and len(probes) == len(keep) and st["ran"] - len(probes) >= MIN_CLEAN:
                        # every cheaper candidate passes and so does a sample of each slow one:
                        # a slow test failing on its own in that situation has not been observed
                        unverified = keep
                        keep = []
                        how = "heavy-dropped"
                    else:
                        unverified = keep
                        how = "heavy-kept"
                elif keep and failed and HEAVY_FAILED == "probe":
                    # something else fails: sample the remaining slow tests as well, and drop
                    # the ones whose sample is clean
                    keep, hit = self.probe_slow(plan, keep, run, dbg)
                    failed = failed + hit
                selected = failed + [n for n in keep if n not in failed]
                if failed:
                    unverified = [n for n in keep if n not in failed]
            else:
                selected = failed
            if how is None:
                how = "fail" if failed else "clean"
        finally:
            try:
                lane.finish()
            finally:
                lane.overlay.restore()
        dbg["mode"] = st["mode"]
        dbg["ran"] = st["ran"]
        dbg["cpu_ms"] = st["cpu"]
        dbg["unverified"] = unverified
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
        dbg["broad"] = len(everything)
        probes = []
        if HEAVY_BROAD == "probe":
            for n in sorted(keep, key=lambda n: -ms[n])[:8]:
                if by_n[n][2] > HEAVY * 1000:
                    pn, pv, pe = self.slice_probe(plan, n)
                    if pv is not None:
                        probes.append((pn, pv, pe))
        dbg["unverified"] = sorted(keep)
        if not to_run and not probes:
            return sorted(keep) + likely, "broad-blind"
        lane = self.get_lane()
        mode = "bare" if plan.fresh else "full"
        back = []
        lane.ensure(mode)
        try:
            for rel, content in plan.files.items():
                lane.overlay.set_file(rel, content)
            to_run = sorted(to_run + probes, key=lambda it: it[0])
            jobs = [(self.id_of[n], max(1.5, 5.0 * e / 1000.0 + 0.5), v) for n, v, e in to_run]
            full = mode == "full"
            results, err = lane.run(mode, jobs, swap=plan.swaps if full else (), deadline=60.0, cpu_budget=COMMIT_CPU)
            if err and full and not results:
                dbg["fallback"] = err
                lane.overlay.restore()
                mode = "bare"
                lane.ensure(mode)
                for rel, content in plan.files.items():
                    lane.overlay.set_file(rel, content)
                results, err = lane.run(mode, jobs, deadline=60.0, cpu_budget=COMMIT_CPU)
            probed = {n for n, _, _ in probes}
            for i, (n, v, e) in enumerate(to_run):
                r = results.get(i)
                if r is None or r[0] in ("F", "U"):
                    back.append(n)  # fails, or could not be checked: stays selected
                elif n in probed:
                    keep.discard(n)  # a clean sample of a slow test: drop it
                    dbg.setdefault("probe_dropped", []).append(n)
            dbg["ran"] = len(results)
            dbg["cpu_ms"] = sum(r[1] for r in results.values())
            dbg["mode"] = mode
        finally:
            lane.overlay.restore()
        return sorted(keep) + back + likely, "broad-blind"

    def probe_slow(self, plan, keep, run, dbg):
        """Sample each sliced slow test in `keep`.  Returns (tests still kept, tests seen failing)."""
        probes = [(n, pv, pe) for n, pv, pe in (self.slice_probe(plan, n) for n in keep) if pv is not None]
        if not probes:
            return keep, []
        hit, unseen = run(probes, False)
        hit = hit + unseen
        probed = {n for n, _, _ in probes}
        dbg["probe_hit"] = hit
        dbg["probe_dropped"] = sorted(probed - set(hit))
        return [n for n in keep if n not in probed], hit

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
        keys, spent = [], 0.0
        budget = SLICE_PROBE * 1000.0 / max(1.0, self.cpu[n])
        for i in order:
            k = sm["keys"][i]
            if allowed is not None and k not in allowed:
                continue
            if sm["share"][i] <= 0.0:
                continue  # entries the test skips tell us nothing
            if keys and spent + sm["share"][i] > budget:
                break
            keys.append(k)
            spent += sm["share"][i]
            if len(keys) >= 6:
                break
        if not keys:
            return n, None, 0.0
        return n, {"allow": keys}, self.cpu[n] * (spent + 0.02)

    def run(self):
        patches = sorted(glob.glob(os.path.join(DATA, "commits", "*.patch")))
        out = sys.stdout
        # the batch has 3 minutes (4.5 in the final, with about 1.5x the commits): stop running
        # tests well before that and select candidates unseen for whatever is left
        hard = 180.0 + 90.0 * min(1.0, max(0.0, (len(patches) - 40) / 20.0))
        self.stop_at = T0 + float(os.environ.get("BR_SOFT", str(0.55 * hard)))
        for path in patches:
            commit = os.path.basename(path)[: -len(".patch")]
            try:
                with open(path, "rb") as fh:
                    data = fh.read()
                selected = self.decide(commit, data)
            except Exception as e:  # never lose a commit: fall back to running everything
                log("commit %s: %r -> selecting all" % (commit, e))
                selected = list(self.all_numbers)
                self.debug.setdefault(commit, {})["crash"] = repr(e)
            workers, cost = split(selected, self.ms)
            self.debug.setdefault(commit, {})["cost_ms"] = cost
            self.debug[commit]["selected"] = len(selected)
            out.write(json.dumps({"commit": commit, "workers": workers}, separators=(",", ":")) + "\n")
        out.flush()
        if self.lane is not None:
            self.lane.close()
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
