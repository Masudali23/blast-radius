"""Record what br/memo.py needs for the executor tests it can shorten.

Usage: trace_memo.py <repo_root> <out_json>

For each test in memo.TESTS, run at the pinned commit:
  entries  {sql key: [fingerprint of (plan, tables), key of the SQL text given to DuckDB]}
  back     {source file: [lines]} executed by the slow half (PythonExecutor.execute and the
           comparison with DuckDB), i.e. the code a patch must leave alone for the fingerprints
           to decide anything
  front_ms CPU of a run in which every entry is recognised and its slow half skipped
  full_ms  CPU of the ordinary run
  costs    {sql key: CPU ms of that entry's slow half}

With a third argument (a maps directory) the result is also stored there as memo.pkl.
"""
import importlib
import json
import os
import sys
import time
import unittest


def main():
    root = os.path.realpath(sys.argv[1])
    out_path = os.path.abspath(sys.argv[2])
    os.chdir(root)
    sys.path.insert(0, root)
    os.environ.setdefault("SKIP_INTEGRATION", "0")
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from br import inlinepool, memo

    inlinepool.install()
    prefix = root + os.sep
    mon = sys.monitoring
    tool = mon.COVERAGE_ID
    mon.use_tool_id(tool, "memo")
    events = mon.events.LINE | mon.events.PY_START
    back = {}

    def note(code, line):
        fn = code.co_filename
        if fn.startswith(prefix):
            back.setdefault(fn[len(prefix):], set()).add(line)

    def on_line(code, line):
        note(code, line)
        return mon.DISABLE

    def on_start(code, offset):
        note(code, code.co_firstlineno)
        return mon.DISABLE

    mon.register_callback(tool, mon.events.LINE, on_line)
    mon.register_callback(tool, mon.events.PY_START, on_start)

    def phase(on):
        # events are only delivered while the slow half runs, so a line switched off after its
        # first report was first reached there
        mon.set_events(tool, events if on else 0)

    out = {}
    for tid in memo.TESTS:
        path, cls_name, meth = tid.split("::")
        module = importlib.import_module(path[:-3].replace("/", "."))
        cls = getattr(module, cls_name)
        cls.setUpClass()
        try:
            back.clear()
            mon.restart_events()
            record = {}
            result = unittest.TestResult()
            t0 = time.process_time()
            with memo.Session(module, cls, memo.TESTS[tid], record=record, phase=phase) as recording:
                cls(meth).run(result)
            full = time.process_time() - t0
            costs = dict(recording.cost_record)
            phase(False)
            ok = result.wasSuccessful() and result.testsRun == 1
            lines = {rel: sorted(ls) for rel, ls in back.items()}
            # how much is left when every entry is recognised
            result2 = unittest.TestResult()
            t0 = time.process_time()
            with memo.Session(module, cls, memo.TESTS[tid], known=record) as session:
                cls(meth).run(result2)
            front = time.process_time() - t0
            out[tid] = {
                "ok": ok and result2.wasSuccessful(),
                "entries": record,
                "back": lines,
                "front_ms": int(front * 1000),
                "full_ms": int(full * 1000),
                "costs": {k: round(v, 1) for k, v in costs.items()},
                "skipped": session.skipped,
                "ran": session.ran,
            }
            sys.stderr.write("%s ok=%s entries=%d back-lines=%d front=%.2fs skipped=%d ran=%d\n" % (tid, out[tid]["ok"], len(record), sum(len(v) for v in lines.values()), front, session.skipped, session.ran))
            for _, tb in (result.failures + result.errors + result2.failures + result2.errors)[:2]:
                sys.stderr.write(tb[-1500:] + "\n")
        finally:
            cls.tearDownClass()
    with open(out_path, "w") as fh:
        json.dump(out, fh)
    if len(sys.argv) > 3:
        import pickle
        import zlib

        packed = {
            tid: {
                "entries": d["entries"],
                "back": d["back"],
                "share": d["front_ms"] / max(1.0, d["full_ms"]),
                # each entry's slow half as a share of the whole test
                "costs": {k: v / max(1.0, d["full_ms"]) for k, v in d["costs"].items()},
            }
            for tid, d in out.items()
            if d["ok"] and d["entries"] and not d["ran"]
        }
        with open(os.path.join(sys.argv[3], "memo.pkl"), "wb") as fh:
            fh.write(zlib.compress(pickle.dumps(packed, protocol=5), 6))


main()
