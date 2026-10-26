"""Request limits, shared process slots, and bounded evaluation scheduling."""

from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path
import fcntl
import threading
import time


def model_concurrency(value):
    if type(value) is not int or value < 1:
        raise ValueError("model_concurrency must be a positive integer")
    return value


class RequestLimiter:
    """Share across all model clients in one run, including their retries."""

    def __init__(self, limit):
        self.limit = model_concurrency(limit)
        self._slots = threading.BoundedSemaphore(self.limit)

    def __enter__(self):
        self._slots.acquire()
        return self

    def __exit__(self, *_exc):
        self._slots.release()


class FileSlots:
    """An OS lock per slot; process exit releases held slots automatically."""
    def __init__(self, directory, limit, *, busy=None):
        self.directory = Path(directory)
        self.limit = model_concurrency(limit)
        self.busy = busy
        self._held = threading.local()

    def __enter__(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        while True:
            for index in range(self.limit):
                stream = (self.directory / f"slot-{index}.lock").open("a")
                try:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    stream.close()
                    continue
                except BaseException:
                    stream.close()
                    raise
                if not hasattr(self._held, "stack"):
                    self._held.stack = []
                self._held.stack.append(stream)
                return self
            if self.busy is not None:
                raise self.busy
            time.sleep(0.05)

    def __exit__(self, *_exc):
        stream = self._held.stack.pop()
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        finally:
            stream.close()


def run_bounded(worker, items, *, max_workers, on_result):
    """Deliver results on the caller thread; drain running work before raising.

    Iterate inputs on the caller thread before dispatch, so persisted attack
    draws cannot depend on worker scheduling. At most max_workers tasks are
    submitted at once. A failure stops refill and cancels work not yet started.
    """
    limit = model_concurrency(max_workers)
    iterator = iter(enumerate(items))
    pending = {}
    error = None

    with ThreadPoolExecutor(max_workers=limit, thread_name_prefix="gepa-eval") as pool:
        def fill():
            while len(pending) < limit:
                try:
                    index, item = next(iterator)
                except StopIteration:
                    break
                pending[pool.submit(worker, item)] = index

        def deliver(future):
            index = pending.pop(future)
            if not future.cancelled():
                on_result(index, future.result())

        try:
            fill()
            while pending:
                done, _ = wait(pending, return_when=FIRST_COMPLETED)
                for future in sorted(done, key=pending.get):
                    try:
                        deliver(future)
                    except BaseException as exc:
                        if error is None:
                            error = exc
                if error is not None:
                    break
                fill()
        except BaseException as exc:
            error = exc
        finally:
            if error is not None:
                for future in pending:
                    future.cancel()
                for future in list(pending):
                    try:
                        deliver(future)
                    except BaseException:
                        # Preserve the initiating error; workers persist their
                        # own successful observations and failure diagnostics.
                        pass
    if error is not None:
        raise error
