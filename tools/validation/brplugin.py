"""pytest plugin: write the set of failing test ids (and files that failed to import) as JSON."""
import json
import os

_failed = set()
_collect_failed = set()
_dur = {}


def pytest_runtest_logreport(report):
    if report.failed:
        _failed.add(report.nodeid)
    if report.when == "call":
        _dur[report.nodeid] = _dur.get(report.nodeid, 0.0) + report.duration


def pytest_collectreport(report):
    if report.failed:
        _collect_failed.add(report.nodeid)


def pytest_sessionfinish(session, exitstatus):
    out = os.environ.get("BR_OUT")
    if out:
        with open(out, "w") as fh:
            json.dump({"failed": sorted(_failed), "collect_failed": sorted(_collect_failed), "exit": int(exitstatus), "dur": _dur if os.environ.get("BR_DUR") else {}}, fh)
