"""A drop-in for concurrent.futures.ProcessPoolExecutor that runs submissions in this process.

sqlglot's optimizer and executor tests ship work to spawned workers.  Spawning costs seconds of
CPU and hides the work from tracing, so we run each submission inline instead, keeping the
pickle round trip of arguments and results that a real pool performs.
"""
import pickle
from concurrent.futures import Future


class InlinePool:
    def __init__(self, max_workers=None, mp_context=None, initializer=None, initargs=(), **kwargs):
        if initializer is not None:
            initializer(*pickle.loads(pickle.dumps(initargs)))

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def submit(self, fn, /, *args, **kwargs):
        future = Future()
        future.set_running_or_notify_cancel()
        try:
            fn2, args2, kwargs2 = pickle.loads(pickle.dumps((fn, args, kwargs)))
            result = fn2(*args2, **kwargs2)
            future.set_result(pickle.loads(pickle.dumps(result)))
        except Exception as e:  # noqa: BLE001 - a real pool also ships the exception back
            try:
                e = pickle.loads(pickle.dumps(e))
            except Exception as pe:  # unpicklable exception: a real pool would break
                e = pe
            future.set_exception(e)
        return future

    def map(self, fn, *iterables, timeout=None, chunksize=1):
        return [self.submit(fn, *a).result() for a in zip(*iterables)]

    def shutdown(self, wait=True, cancel_futures=False):
        pass


def install():
    import concurrent.futures as cf
    import concurrent.futures.process as cfp

    cf.ProcessPoolExecutor = InlinePool
    cfp.ProcessPoolExecutor = InlinePool
