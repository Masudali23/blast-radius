"""Run tools/trace_rel_slices_one.py for every sliced test.

Usage: trace_rel_slices_all.py <repo_root> <maps_dir> <out_dir> [jobs]

One process per test (each forks a child per fixture entry); <out_dir>/<map index>.pkl each.
"""
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

root, maps_dir, out_dir = sys.argv[1:4]
jobs = int(sys.argv[4]) if len(sys.argv) > 4 else 2
here = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(here))
from br import maps as maps_mod  # noqa: E402

os.makedirs(out_dir, exist_ok=True)
env = dict(os.environ, BR_REL_CACHE=os.path.join(out_dir, "cache"))
todo = sorted(maps_mod.Maps(maps_dir).sliced)


def one(item):
    i, tid = item
    out = os.path.join(out_dir, "%d.pkl" % i)
    if not os.path.exists(out):
        subprocess.run([sys.executable, os.path.join(here, "trace_rel_slices_one.py"), root, maps_dir, tid, out], env=env, stdin=subprocess.DEVNULL)
    return tid


with ThreadPoolExecutor(jobs) as pool:
    for tid in pool.map(one, enumerate(todo)):
        print("done", tid, flush=True)
