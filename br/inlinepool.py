"""A drop-in for concurrent.futures.ProcessPoolExecutor that runs submissions in this process.

sqlglot's optimizer and executor tests ship work to spawned workers.  Spawning costs seconds of
CPU and hides the work from tracing, so we run each submission inline instead, keeping the
pickle round trip of arguments and results that a real pool performs.
"""
import pickle
from concurrent.futures import Future


class InlinePool:
    def __init__(self, max_workers=None, mp_context=None, initializer=None, initargs=(), **kwargs):
        # Initializer arguments are fixture data (megabytes of table rows): they are handed over
        # as they are.  Submitted calls and their results still make the pickle round trip.
        if initializer is not None:
            initializer(*initargs)

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


class _Later:
    """What LazyPool.submit hands back: the call is made when the result is first asked for."""

    def __init__(self, payload):
        self._payload = payload
        self._outcome = None

    def result(self, timeout=None):
        if self._outcome is None:
            try:
                fn, args, kwargs = pickle.loads(self._payload)
                self._outcome = (pickle.loads(pickle.dumps(fn(*args, **kwargs))), None)
            except Exception as e:  # noqa: BLE001 - a real pool also ships the exception back
                try:
                    e = pickle.loads(pickle.dumps(e))
                except Exception as pe:  # unpicklable exception: a real pool would break
                    e = pe
                self._outcome = (None, e)
            self._payload = None
        value, error = self._outcome
        if error is not None:
            raise error
        return value

    def exception(self, timeout=None):
        try:
            self.result()
        except Exception as e:  # noqa: BLE001
            return e
        return None

    def done(self):
        return self._outcome is not None

    def cancel(self):
        return False


class LazyPool(InlinePool):
    """An InlinePool whose submissions run when their result is asked for, not when they are
    submitted.  The arguments are pickled at submission, as a real pool would do; the tasks of
    a pool are independent of one another, so the order they run in is not theirs to rely on.
    Used where it pays to look at each result before starting the next task (br/memo.py)."""

    def submit(self, fn, /, *args, **kwargs):
        return _Later(pickle.dumps((fn, args, kwargs)))


def install():
    import concurrent.futures as cf
    import concurrent.futures.process as cfp

    cf.ProcessPoolExecutor = InlinePool
    cfp.ProcessPoolExecutor = InlinePool
