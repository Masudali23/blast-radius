"""Zygote process: keeps a warm interpreter and forks one child per job to run sqlglot tests.

    python -m br.worker <mode> <overlay_root> <base_root>

mode "full": sqlglot, every dialect and every test module are imported once; a job's child
             hot-swaps the code objects of changed functions and runs tests immediately.
mode "bare": only third-party libraries are imported; a job's child imports sqlglot and the
             tests fresh from the overlay tree, so import-time changes take effect.

Protocol (line oriented, on the pipes given by the parent):
  parent -> zygote : one JSON object per line (a job), or "quit"
  child  -> parent : "T <index> <P|F|S> <cpu_ms>" per finished test, "X <reason>" if the job
                     cannot be honoured, and the zygote ends each job with "END <exit status>"
"""
import gc
import importlib
import json
import os
import signal
import sys
import time
import types
import unittest

from br import infect, memo, tables, variants

FAILLOG = os.environ.get("BR_FAILLOG")
SETUP_LIMIT = 60.0  # seconds allowed for importing a test module and setting up its class
IMPORT_LIMIT = 30.0  # seconds allowed for importing one patched module on its own
GC_YOUNG = int(os.environ.get("BR_GC_YOUNG", "100000"))  # 0 keeps the interpreter's defaults


class TestTimeout(KeyboardInterrupt):
    """Raised by the per-test alarm.  unittest re-raises KeyboardInterrupt instead of recording it,
    so a runaway test (or subtest loop) is abandoned at once."""


def preload_modules(root):
    """Everything the full zygote imports up front (shared with the import-time tracer)."""
    mods = []
    for d, _, files in os.walk(os.path.join(root, "tests")):
        if "fixtures" in d or "__pycache__" in d:
            continue
        for f in sorted(files):
            if f.startswith("test_") and f.endswith(".py"):
                rel = os.path.relpath(os.path.join(d, f), root)
                mods.append(rel[:-3].replace(os.sep, "."))
    return sorted(mods)


CORE_PACKAGES = ("sqlglot.expressions", "sqlglot.optimizer", "sqlglot.executor")
CORE_MODULES = ("sqlglot.lineage", "sqlglot.planner", "sqlglot.diff", "sqlglot.serde", "sqlglot.anonymize", "sqlglot.jsonpath", "sqlglot.transforms", "sqlglot.schema", "sqlglot.time", "sqlglot.trie")


def preload_core(root):
    """sqlglot without any dialect: what the "core" zygote holds.  A patch that only touches
    files this does not load can be checked from here, importing just the dialects it needs."""
    import pkgutil

    try:
        importlib.import_module("sqlglot")
    except BaseException:
        return
    for pkg in CORE_PACKAGES:
        try:
            m = importlib.import_module(pkg)
            for info in pkgutil.iter_modules(m.__path__, pkg + "."):
                try:
                    importlib.import_module(info.name)
                except BaseException:
                    pass
        except BaseException:
            pass
    for name in CORE_MODULES:
        try:
            importlib.import_module(name)
        except BaseException:
            pass


def loaded_files(root):
    prefix = root + os.sep
    out = []
    for m in list(sys.modules.values()):
        f = getattr(m, "__file__", None)
        if f and f.startswith(prefix):
            out.append(f[len(prefix):])
    return sorted(out)


def preload(root, skip=()):
    for m in preload_modules(root):
        if m in skip:
            continue
        try:
            variants.wrap_module(importlib.import_module(m))
        except BaseException:
            pass
    for f in sorted(os.listdir(os.path.join(root, "sqlglot", "dialects"))):
        if f.endswith(".py") and not f.startswith("_"):
            try:
                importlib.import_module("sqlglot.dialects." + f[:-3])
            except BaseException:
                pass
    prerun_class_setup()


# --------------------------------------------------------------------------- hot swap
PROBED = [0]  # number of files instrumented for differential evaluation in this child
FUNCS = {}  # source path -> live function objects defined there (built once, before forking)


LOADED = set()  # source paths of modules already imported in the zygote


def index_functions(root):
    prefix = root + os.sep
    for m in list(sys.modules.values()):
        f = getattr(m, "__file__", None)
        if f:
            LOADED.add(f)
    for obj in gc.get_objects():
        if type(obj) is types.FunctionType:
            fn = obj.__code__.co_filename
            if fn.startswith(prefix):
                FUNCS.setdefault(fn, []).append(obj)


def _children(code):
    return [c for c in code.co_consts if isinstance(c, types.CodeType)]


def _pair(old, new, out, inside_func):
    """Collect (old_code, new_code) for the outermost function-like code objects that differ."""
    if old == new:
        # equal code objects have equal bytecode, constants (recursively) and line tables
        return True
    is_func = bool(old.co_flags & 1)
    if is_func and not inside_func:
        if old.co_name != new.co_name or old.co_freevars != new.co_freevars:
            return False
        out.append((old, new))
        return True
    oc, nc = _children(old), _children(new)
    if len(oc) != len(nc):
        return False
    for a, b in zip(oc, nc):
        if a.co_name != b.co_name:
            return False
        if not _pair(a, b, out, inside_func or is_func):
            return False
    return True


def _own_differs(old, new):
    """True when module/class-level bytecode itself differs (not just nested functions)."""
    if bool(old.co_flags & 1):
        return False
    strip = lambda c: tuple("<code>" if isinstance(k, types.CodeType) else (type(k).__name__, repr(k)) for k in c.co_consts)
    if old.co_code != new.co_code or old.co_names != new.co_names or strip(old) != strip(new):
        return True
    oc, nc = _children(old), _children(new)
    if len(oc) != len(nc):
        return True
    return any(_own_differs(a, b) for a, b in zip(oc, nc))


def _cached_code(base_path):
    """The pinned module's code object from its .pyc (saves compiling the old source)."""
    try:
        import importlib.util
        import marshal

        pyc = importlib.util.cache_from_source(base_path)
        st = os.stat(base_path)
        with open(pyc, "rb") as fh:
            data = fh.read()
        if data[:4] != importlib.util.MAGIC_NUMBER or int.from_bytes(data[4:8], "little") & 1:
            return None
        if int.from_bytes(data[8:12], "little") != int(st.st_mtime) & 0xFFFFFFFF or int.from_bytes(data[12:16], "little") != st.st_size & 0xFFFFFFFF:
            return None
        return marshal.loads(data[16:])
    except Exception:
        return None


def hot_swap(path, old_src, new_src, probe=False, base_path=None):
    """Replace the code of every live function defined in `path` whose source changed.

    Returns None on success, or a string explaining why a fresh import is required.
    With probe=True the changed expression is instrumented (see br.infect); PROBED records
    whether that worked.
    """
    old_top = _cached_code(base_path) if base_path else None
    if old_top is None:
        try:
            old_top = compile(old_src, path, "exec", dont_inherit=True)
        except Exception as e:
            return "old does not compile: %r" % (e,)
    new_top = None
    if probe:
        new_top = infect.instrument(old_src, new_src, path)
        if new_top is not None:
            PROBED[0] += 1
    if new_top is None:
        try:
            new_top = compile(new_src, path, "exec", dont_inherit=True)
        except Exception as e:
            return "new does not compile: %r" % (e,)
    if _own_differs(old_top, new_top):
        return "module or class body changed"
    pairs = []
    if not _pair(old_top, new_top, pairs, False):
        return "structure changed"
    if not pairs:
        return None
    if path not in LOADED:
        return None  # not imported yet: the child will import the patched source itself
    wanted = {}
    for old, new in pairs:
        wanted.setdefault((old.co_name, old.co_firstlineno), []).append((old, new))
    found = {id(old): 0 for old, _ in pairs}
    for obj in FUNCS.get(path, ()):
        c = obj.__code__
        cands = wanted.get((c.co_name, c.co_firstlineno))
        if not cands:
            continue
        for old, new in cands:
            if c.co_code == old.co_code and c.co_qualname == old.co_qualname and c.co_consts == old.co_consts:
                try:
                    obj.__code__ = new
                except Exception as e:
                    return "cannot assign code: %r" % (e,)
                found[id(old)] += 1
                break
    for old, new in pairs:
        if not found[id(old)]:
            return "no live function for %s:%d" % (old.co_qualname, old.co_firstlineno)
    return None


# --------------------------------------------------------------------------- running tests
class Runner:
    def __init__(self, out_fd):
        self.out = out_fd
        self.classes = dict(PRESET)
        self.modules = {}
        self.result = unittest.TestResult()
        self.result._testRunEntered = True
        # a test with one failing subtest has failed: no need to finish its loop
        self.result.failfast = True

    def emit(self, text):
        os.write(self.out, (text + "\n").encode())

    def _module(self, name):
        if name not in self.modules:
            try:
                self.modules[name] = importlib.import_module(name)
                variants.wrap_module(self.modules[name])
            except TestTimeout:
                raise
            except BaseException as e:
                self.modules[name] = e
        return self.modules[name]

    def _class(self, cls):
        if cls not in self.classes:
            ok = True
            try:
                setup = getattr(cls, "setUpClass", None)
                if setup is not None:
                    _class_setup(cls, setup)
            except unittest.SkipTest:
                ok = "skip"
            except TestTimeout:
                raise
            except BaseException:
                ok = False
                try:
                    cls.doClassCleanups()
                except BaseException:
                    pass
            self.classes[cls] = ok
        return self.classes[cls]

    def prepare(self, test_id):
        """Import the test's module and set up its class (not charged to the test's time limit)."""
        path, cls_name, meth = test_id.split("::")
        mod = self._module(path[:-3].replace("/", "."))
        if isinstance(mod, BaseException):
            return
        cls = getattr(mod, cls_name, None)
        if cls is not None and not getattr(cls, "__unittest_skip__", False):
            self._class(cls)

    def run_one(self, test_id, variant=None):
        """Return 'P', 'F', 'S', or 'U' when the requested variant cannot be built."""
        path, cls_name, meth = test_id.split("::")
        mod = self._module(path[:-3].replace("/", "."))
        if isinstance(mod, BaseException):
            return "F"  # the test's file cannot be imported
        cls = getattr(mod, cls_name, None)
        if cls is None or not hasattr(cls, meth):
            return "F"  # pytest would not find it: treat as failing, the caller selects it
        if getattr(cls, "__unittest_skip__", False):
            return "S"
        state = self._class(cls)
        if state == "skip":
            return "S"
        if state is False:
            return "F"
        res = self.result
        before = (len(res.failures), len(res.errors), len(res.unexpectedSuccesses), len(res.skipped))
        test = cls(meth)
        allow = None
        if variant:
            if variant.get("scaled"):
                scaled = variants.scaled_method(test, mod, cls_name, meth, test_id)
                if scaled is None:
                    return "U"
                setattr(test, meth, scaled)
            if "allow" in variant:
                allow = set(variant["allow"])
                variants.apply_slices(test, cls, allow)
            if "changed" in variant:
                variants.SL.changed = set(variant["changed"])
                variants.apply_slices(test, cls, None, variants.SL.changed)
        session = None
        if variant and variant.get("memo"):
            attr = memo.TESTS.get(test_id)
            if attr is None:
                return "U"
            session = memo.Session(mod, cls, attr, known=variant["memo"], strict=True, costs=variant.get("memo_costs"), budget=variant.get("memo_budget", 0.0))
        variants.SL.active = allow
        try:
            if session is not None:
                session.__enter__()
            test.run(res)
        finally:
            if session is not None:
                session.__exit__(None, None, None)
            variants.SL.active = None
            variants.SL.changed = None
            res.shouldStop = False
        after = (len(res.failures), len(res.errors), len(res.unexpectedSuccesses), len(res.skipped))
        if session is not None and session.gave_up and after[:3] == before[:3]:
            return "U"  # some entry is handed something new: nothing was decided
        if after[:3] != before[:3]:
            if FAILLOG:
                with open(FAILLOG, "a") as fh:
                    for _, tb in (res.failures + res.errors)[:2]:
                        fh.write("=== %s\n%s\n" % (test_id, tb[-3000:]))
            # keep the result object small
            del res.failures[:], res.errors[:], res.unexpectedSuccesses[:]
            return "F"
        if after[3] != before[3]:
            return "S"
        return "P"

    def finish(self):
        for cls, ok in list(self.classes.items()):
            if ok is True:
                try:
                    td = getattr(cls, "tearDownClass", None)
                    if td is not None:
                        td()
                    cls.doClassCleanups()
                except BaseException:
                    pass


def _unpatched(rel):
    """Is `rel` (a file, or a directory when it ends with "/") the pinned one in this child?"""
    overlay, base = PLACE["overlay"], PLACE["base"]
    if not overlay or not base:
        return False
    here = os.path.join(overlay, rel.rstrip("/"))
    there = os.path.realpath(os.path.join(base, rel.rstrip("/")))
    if os.path.realpath(here) == there:
        return True  # reached through links only: nothing was written in its place
    if not rel.endswith("/") or not os.path.isdir(here) or os.path.islink(here):
        return False
    try:
        names = sorted(os.listdir(here))
        if names != sorted(os.listdir(there)):
            return False
        return all(os.path.realpath(os.path.join(here, n)) == os.path.realpath(os.path.join(there, n)) for n in names)
    except OSError:
        return False


def _class_setup(cls, setup):
    """Run a class set-up; the executor class takes its tables from the store and opens DuckDB
    only when a test first asks for it, provided the patch leaves alone everything the set-up
    reads.  (Its CREATE VIEW statements are plain text over the fixture files: they cannot
    start failing because of a patch to sqlglot.)"""
    if tables.usable(cls) and all(_unpatched(rel) for rel in tables.DEPENDS):
        LAZY["on"] = True
        try:
            tables.setup(cls)
            return
        except tables.Mismatch:
            pass
        finally:
            LAZY["on"] = False
    setup()


def _on_alarm(signum, frame):
    raise TestTimeout()


def arm(cpu_seconds):
    """Limit the code that follows by CPU time, so a busy machine cannot cause a timeout.
    A much longer wall-clock limit backs it up for code that blocks without using CPU."""
    signal.setitimer(signal.ITIMER_PROF, cpu_seconds, 0.2)
    signal.setitimer(signal.ITIMER_REAL, 8.0 * cpu_seconds + 20.0, 1.0)


def disarm():
    signal.setitimer(signal.ITIMER_PROF, 0)
    signal.setitimer(signal.ITIMER_REAL, 0)


def _read_line(fd):
    """Read one line from a pipe without buffering past it."""
    chunks = []
    while True:
        b = os.read(fd, 65536)
        if not b:
            break
        chunks.append(b)
        if b.endswith(b"\n"):
            break
    return b"".join(chunks)


def run_job(job, mode, overlay, base, out_fd, in_fd=0):
    """Runs inside the forked child."""
    if GC_YOUNG:
        # A child lives for one commit.  With the default thresholds the collector runs every
        # 700 allocations and keeps re-scanning the syntax trees the tests build; collecting
        # less often changes when unreachable cycles are freed and nothing else.  (The tests
        # that measure memory or watch weak references call gc.collect() themselves.)
        gc.set_threshold(GC_YOUNG, 50, 100)
    runner = Runner(out_fd)
    # keep anything the tests print away from our channel
    devnull = os.open(os.devnull, os.O_RDWR)
    os.dup2(devnull, 1)
    os.dup2(devnull, 2)
    sys.stdout = open(os.devnull, "w")
    sys.stderr = sys.stdout
    signal.signal(signal.SIGALRM, _on_alarm)
    signal.signal(signal.SIGPROF, _on_alarm)
    variants.SL.base_fixtures = os.path.join(base, "tests", "fixtures")
    PLACE["overlay"], PLACE["base"] = overlay, base

    for name in job.get("imports", ()):
        # does the patched module still import?  (asked before its candidates were narrowed)
        arm(IMPORT_LIMIT)
        try:
            importlib.import_module(name)
        except BaseException as e:
            runner.emit("M %s %s" % (name, type(e).__name__))
        finally:
            disarm()

    if mode == "full":
        for rel in job.get("swap", ()):
            path = os.path.join(overlay, rel)
            try:
                with open(os.path.join(base, rel), "rb") as fh:
                    old_src = fh.read()
                with open(path, "rb") as fh:
                    new_src = fh.read()
            except OSError as e:
                runner.emit("X read %s: %r" % (rel, e))
                return
            why = hot_swap(path, old_src, new_src, probe=rel in job.get("probe", ()), base_path=os.path.join(base, rel))
            if why:
                runner.emit("X %s: %s" % (rel, why))
                return
        if PROBED[0]:
            infect.install()
            runner.emit("I %d" % PROBED[0])

    stage = job
    job_t0 = time.process_time()
    while True:
        stop_on_fail = stage.get("stop_on_fail", False)
        # CPU seconds this child may spend in total; past it the caller selects the rest unseen
        cpu_budget = stage.get("cpu_budget")
        for index, entry in enumerate(stage["tests"]):
            test_id, limit = entry[0], entry[1]
            variant = entry[2] if len(entry) > 2 else None
            if cpu_budget is not None and time.process_time() - job_t0 > cpu_budget:
                runner.emit("B budget")
                break
            e0, i0 = infect.STATS
            arm(SETUP_LIMIT)
            try:
                runner.prepare(test_id)
            except BaseException:
                pass
            t0 = time.process_time()  # the test itself; imports and class set-up are not its cost
            arm(limit)
            timed_out = False
            try:
                outcome = runner.run_one(test_id, variant)
            except TestTimeout:
                outcome = "F"
                timed_out = True
            except BaseException:
                outcome = "F"
            finally:
                disarm()
            runner.emit("T %d %s %d %d %d" % (index, outcome, int((time.process_time() - t0) * 1000), infect.STATS[0] - e0, infect.STATS[1] - i0))
            if timed_out:
                # a change that makes one test hang usually makes many hang: stop paying for it
                runner.emit("B timeout")
                break
            if outcome == "F" and stop_on_fail:
                break
        if not job.get("stages"):
            break
        # the caller decides what to run next from what it has just seen
        runner.emit("S")
        line = _read_line(in_fd)
        if not line or line.strip() == b"done":
            break
        stage = json.loads(line)
    arm(5.0)
    try:
        runner.finish()
    except BaseException:
        pass
    disarm()


class LazyConn:
    """Stands in for a DuckDB connection opened before the fork.

    While the zygote runs a class set-up it only records the statements; the forked child opens
    the real connection on first use and replays them.  (DuckDB connections do not survive fork.)
    """

    def __init__(self, factory, args, kwargs):
        self.__dict__["_lz"] = [factory, args, kwargs, [], None, os.getpid()]

    def _conn(self):
        st = self._lz
        if st[4] is None:
            st[4] = st[0](*st[1], **st[2])
            for a, k in st[3]:
                st[4].execute(*a, **k)
        return st[4]

    def execute(self, *args, **kwargs):
        st = self._lz
        if st[4] is None and (PRERUN["on"] or LAZY["on"]):
            st[3].append((args, kwargs))
            return self
        return self._conn().execute(*args, **kwargs)

    def close(self):
        st = self._lz
        if st[4] is not None:
            st[4].close()

    def __getattr__(self, name):
        return getattr(self._conn(), name)


# test modules the full zygote leaves for the child to import (heavy imports, rarely needed)
LAZY_MODULES = ("tests.test_sqlglotc",)
PRERUN = {"on": False}
# set while a class set-up runs in a child that may defer its DuckDB work (see _class_setup)
LAZY = {"on": False}
# where the patched tree and the pinned checkout are, for asking "is this file patched?"
PLACE = {"overlay": None, "base": None}
PRESET = {}  # test classes whose setUpClass already ran in the zygote
PRERUN_CLASSES = (("tests.test_executor", "TestExecutor"),)


def prerun_class_setup():
    """Run expensive, side-effect-free class set-ups once, before forking."""
    PRERUN["on"] = True
    try:
        for mod_name, cls_name in PRERUN_CLASSES:
            try:
                cls = getattr(sys.modules[mod_name], cls_name)
                if getattr(cls, "__unittest_skip__", False):
                    continue
                if tables.usable(cls):
                    try:
                        tables.setup(cls)
                    except tables.Mismatch:
                        cls.setUpClass()
                else:
                    cls.setUpClass()
                PRESET[cls] = True
            except BaseException:
                pass
    finally:
        PRERUN["on"] = False


def _limit_duckdb_threads():
    """One DuckDB thread per connection: the container has 2 cores and a process limit, while
    DuckDB sizes its pool from the host.  Results do not depend on the thread count."""
    try:
        import duckdb
    except Exception:
        return
    original = duckdb.connect

    def connect(*args, **kwargs):
        config = dict(kwargs.get("config") or {})
        config.setdefault("threads", 1)
        kwargs["config"] = config
        if PRERUN["on"] or LAZY["on"]:
            return LazyConn(original, args, kwargs)
        return original(*args, **kwargs)

    duckdb.connect = connect


def serve(mode, overlay, base, in_fd, out_fd, spares):
    """The zygote loop: one forked child per job.

    Zygotes form a chain, each forked from the previous one so nothing is imported twice:
      bare  third-party libraries only
      core  + sqlglot without dialects
      full  + every dialect, every test module, expensive class set-ups
    `spares` maps the next mode down the chain to its pipe pair.
    """
    ready = b"READY"
    if mode == "core":
        preload_core(overlay)
        ready += b" " + json.dumps(loaded_files(overlay)).encode()
    elif mode == "full":
        preload(overlay, skip=LAZY_MODULES)
        gc.collect()
        index_functions(overlay)
    gc.collect()
    gc.freeze()
    os.write(out_fd, ready + b"\n")
    while True:
        line = _read_line(in_fd)
        if not line or line.strip() == b"quit":
            break
        job = json.loads(line)
        nxt = job.get("fork")
        if nxt:
            fds = spares.get(nxt)
            if fds is None:
                continue
            pid = os.fork()
            if pid == 0:
                try:
                    os.close(in_fd)
                    os.close(out_fd)
                    gc.unfreeze()
                    serve(nxt, overlay, base, fds[0], fds[1], {k: v for k, v in spares.items() if k != nxt})
                finally:
                    os._exit(0)
            continue
        pid = os.fork()
        if pid == 0:
            code = 0
            try:
                run_job(job, mode, overlay, base, out_fd, in_fd)
            except BaseException:
                code = 3
            finally:
                os._exit(code)
        killed = []

        def _kill(signum, frame, pid=pid):
            killed.append(1)
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass

        signal.signal(signal.SIGALRM, _kill)
        signal.setitimer(signal.ITIMER_REAL, job.get("deadline", 60.0), 1.0)
        try:
            _, status = os.waitpid(pid, 0)
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
        os.write(out_fd, ("END %d\n" % (-9 if killed else status)).encode())


def main():
    # usage: worker.py <overlay> <base> <core_in> <core_out> <full_in> <full_out> [modules...]
    overlay, base = sys.argv[1], sys.argv[2]
    spares = {"core": (int(sys.argv[3]), int(sys.argv[4])), "full": (int(sys.argv[5]), int(sys.argv[6]))}
    out_fd = os.dup(1)
    in_fd = os.dup(0)
    # nothing imported below may write into the protocol channel
    devnull = os.open(os.devnull, os.O_RDWR)
    os.dup2(devnull, 1)
    sys.stdout = open(os.devnull, "w")
    os.environ.setdefault("SKIP_INTEGRATION", "0")
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    for var in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
        os.environ[var] = "1"
    sys.dont_write_bytecode = True
    os.chdir(overlay)
    sys.path.insert(0, overlay)
    for name in ("unittest.mock", "json", "decimal", "datetime", "logging", "typing", "concurrent.futures", "multiprocessing"):
        try:
            importlib.import_module(name)
        except Exception:
            pass
    from br import inlinepool

    inlinepool.install()
    for name in sys.argv[7:]:
        try:
            importlib.import_module(name)
        except Exception:
            pass
    _limit_duckdb_threads()
    signal.signal(signal.SIGCHLD, signal.SIG_DFL)
    serve("bare", overlay, base, in_fd, out_fd, spares)


if __name__ == "__main__":
    main()
