"""Record which function-level lines run while the full zygote preloads (no test executed).

Usage: trace_import.py <repo_root> <out_json>
A changed function that already ran at import time cannot be hot-swapped safely.
"""
import json
import os
import sys

root = os.path.realpath(sys.argv[1])
out = os.path.abspath(sys.argv[2])
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(root)
sys.path.insert(0, root)
os.environ.setdefault("SKIP_INTEGRATION", "0")
prefix = root + os.sep
func = {}
mon = sys.monitoring
tool = mon.COVERAGE_ID
mon.use_tool_id(tool, "blast")


def on_line(code, line):
    fn = code.co_filename
    if fn.startswith(prefix) and code.co_flags & 1:
        func.setdefault(fn, set()).add(line)
    return mon.DISABLE


opened = set()


def audit(event, args):
    if event == "open":
        p = args[0]
        if isinstance(p, bytes):
            p = os.fsdecode(p)
        if isinstance(p, str):
            if not p.startswith(prefix):
                p = os.path.realpath(p)
            if p.startswith(prefix) and "__pycache__" not in p:
                opened.add(p[len(prefix):])


sys.addaudithook(audit)
mon.register_callback(tool, mon.events.LINE, on_line)
mon.set_events(tool, mon.events.LINE)
from br import inlinepool, worker

inlinepool.install()
worker._limit_duckdb_threads()
worker.preload(root)
mon.set_events(tool, 0)
loaded = {getattr(m, "__file__", None) for m in list(sys.modules.values())}
data_files = sorted(f for f in opened if os.path.join(root, f) not in loaded)
res = {k[len(prefix):]: sorted(v) for k, v in func.items()}
res["__files__"] = data_files
json.dump(res, open(out, "w"))
print("files read at preload:", len(data_files))
print("import-time function lines:", sum(len(v) for v in func.values()), "files", len(func))
