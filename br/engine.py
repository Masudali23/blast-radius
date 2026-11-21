"""Lanes: a writable overlay of the repo plus the warm worker processes that run tests in it."""
import json
import os
import select
import signal
import subprocess
import sys
import time

from .overlay import Overlay

THIRD_PARTY = ("numpy", "pandas", "duckdb", "dateutil", "pytz")
START_TIMEOUT = 90.0


class Chan:
    """One worker's pipe pair (as seen from here)."""

    def __init__(self, pid, w_fd, r_fd):
        self.pid = pid
        self.w = w_fd
        self.r = r_fd
        self.buf = bytearray()

    def send(self, data):
        view = memoryview(data)
        while view:
            n = os.write(self.w, view)
            view = view[n:]


class Lane:
    def __init__(self, base, tmp, app_dir, log=None):
        self.base = base
        self.app_dir = app_dir
        self.overlay = Overlay(base, os.path.join(tmp, "o"))
        self.z = {}
        self.proc = None
        self.core_loaded = None
        self.cut = None
        self._deadline = 60.0
        self.log = log or (lambda *a: None)

    def _start_base(self):
        """Start the bare worker.  The core and full workers are forked from it later, each over
        its own pipe pair created here."""
        env = dict(os.environ)
        env["PYTHONPATH"] = self.app_dir
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env.setdefault("SKIP_INTEGRATION", "0")
        child_fds, self._ends = [], {}
        for mode in ("core", "full"):
            to_r, to_w = os.pipe()
            from_r, from_w = os.pipe()
            child_fds += [to_r, from_w]
            self._ends[mode] = (to_w, from_r)
        args = [sys.executable, "-m", "br.worker", self.overlay.root, self.base] + [str(fd) for fd in child_fds] + list(THIRD_PARTY)
        p = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env, cwd=self.app_dir, bufsize=0, start_new_session=True, pass_fds=tuple(child_fds))
        for fd in child_fds:
            os.close(fd)
        self.proc = p
        bare = Chan(p.pid, p.stdin.fileno(), p.stdout.fileno())
        self._await(bare, "bare")
        self.z["bare"] = bare

    def _await(self, chan, mode):
        try:
            line = self._readline(chan, START_TIMEOUT)
        except TimeoutError:
            line = b""
        if not line.startswith(b"READY"):
            self._kill()
            raise RuntimeError("%s worker did not start: %r" % (mode, line[:80]))
        return line[5:].strip()

    def _start(self, mode):
        """Start `mode` and whatever precedes it in the chain bare -> core -> full."""
        if "bare" not in self.z:
            self._start_base()
        if mode in ("core", "full") and "core" not in self.z:
            self.z["bare"].send(b'{"fork":"core"}\n')
            core = Chan(self.proc.pid, *self._ends["core"])
            payload = self._await(core, "core")
            try:
                self.core_loaded = set(json.loads(payload)) if payload else set()
            except ValueError:
                self.core_loaded = None
            self.z["core"] = core
        if mode == "full" and "full" not in self.z:
            self.z["core"].send(b'{"fork":"full"}\n')
            full = Chan(self.proc.pid, *self._ends["full"])
            self._await(full, "full")
            self.z["full"] = full
        return self.z[mode]

    def fresh_mode(self, files):
        """Where a change that needs a fresh import can be checked from: the core worker when
        none of the changed python files is already loaded there, the bare one otherwise."""
        if "core" not in self.z:
            self._start("core")
        loaded = self.core_loaded
        if loaded is None:
            return "bare"
        for rel in files:
            if rel in loaded:
                return "bare"
            if rel.endswith(".py") and not rel.startswith(("sqlglot/", "tests/")):
                return "bare"
        return "core"

    def _readline(self, p, timeout):
        """One line from the worker, or TimeoutError.  b"" means the pipe closed."""
        buf = p.buf
        fd = p.r
        end = time.monotonic() + timeout
        while True:
            i = buf.find(b"\n")
            if i >= 0:
                line = bytes(buf[: i + 1])
                del buf[: i + 1]
                return line
            left = end - time.monotonic()
            if left <= 0:
                raise TimeoutError()
            ready, _, _ = select.select([fd], [], [], left)
            if not ready:
                raise TimeoutError()
            chunk = os.read(fd, 65536)
            if not chunk:
                return b""
            buf += chunk

    def _kill(self, p=None):
        """Kill every worker (they share one session) and forget them."""
        proc = self.proc
        self.z = {}
        self.proc = None
        if proc is None:
            return
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        try:
            proc.wait(timeout=5)
        except Exception:
            pass

    def ensure(self, mode):
        """Start the worker for `mode` now.  Must happen while the overlay is still pristine:
        the full worker imports sqlglot and the tests from it."""
        if mode not in self.z:
            self._start(mode)

    def _send(self, p, obj):
        p.send((json.dumps(obj, separators=(",", ":")) + "\n").encode())

    def _collect(self, p, mode):
        """Read until the child pauses ("S") or the job ends.  Returns (results, error, alive)."""
        results = {}
        error = None
        while True:
            try:
                line = self._readline(p, self._deadline + 15.0)
            except TimeoutError:
                line = b""
            if not line:
                self._kill()
                return results, "worker died", False
            if line.startswith(b"T "):
                parts = line.split()
                results[int(parts[1])] = (parts[2].decode(), int(parts[3]), int(parts[4]), int(parts[5]))
            elif line.startswith(b"I "):
                self.probed = True
            elif line.startswith(b"M "):
                self.unimportable.append(line[2:].decode("utf-8", "replace").strip())
            elif line.startswith(b"B "):
                self.cut = line[2:].decode().strip()  # the child stopped early (budget / timeout)
            elif line.startswith(b"S"):
                return results, error, True
            elif line.startswith(b"X "):
                error = line[2:].decode("utf-8", "replace").strip()
            elif line.startswith(b"END"):
                status = int(line.split()[1])
                if status != 0 and error is None:
                    error = "child exit status %d" % status
                return results, error, False

    def run(self, mode, tests, swap=(), stop_on_fail=False, deadline=60.0, probe=(), stages=False, cpu_budget=None, imports=()):
        """Run tests [(test_id, limit_seconds[, variant])] in a fresh child of the `mode` worker.

        Returns (results, error): results maps index -> (outcome, cpu_ms, evals, differ) for the
        tests that finished; error is None, or why the job could not be honoured / the child died.
        With stages=True the child then waits: call more() to run further tests in the same
        process and finish() to let it go.
        """
        p = self.z[mode]
        self.probed = False
        self._mode = mode
        self._open = False
        self._deadline = deadline
        self.cut = None
        self.unimportable = []
        self._send(p, {"tests": tests, "swap": list(swap), "stop_on_fail": stop_on_fail, "deadline": deadline, "probe": list(probe), "stages": stages, "cpu_budget": cpu_budget, "imports": list(imports)})
        results, error, alive = self._collect(p, mode)
        self._open = alive
        return results, error

    def more(self, tests, stop_on_fail=False, cpu_budget=None):
        if not self._open:
            return {}, "no child"
        p = self.z[self._mode]
        self._send(p, {"tests": tests, "stop_on_fail": stop_on_fail, "cpu_budget": cpu_budget})
        results, error, alive = self._collect(p, self._mode)
        self._open = alive
        return results, error

    def finish(self):
        if getattr(self, "_open", False):
            p = self.z.get(self._mode)
            self._open = False
            if p is not None:
                p.send(b"done\n")
                self._collect(p, self._mode)

    def close(self):
        for p in self.z.values():
            try:
                p.send(b"quit\n")
            except Exception:
                pass
        proc = self.proc
        if proc is not None:
            try:
                proc.wait(timeout=3)
            except Exception:
                pass
        self._kill()
