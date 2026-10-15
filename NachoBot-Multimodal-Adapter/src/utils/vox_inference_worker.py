"""Keep Vox model construction and iterator operations on one owner thread."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
import threading


def _advance(iterator):
    try:
        return False, next(iterator)
    except StopIteration:
        return True, None


def _close(iterator):
    close = getattr(iterator, "close", None)
    if close is not None:
        close()


class InferenceWorker:
    def __init__(self):
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="vox-inference")
        self._owner = None
        self._session_lock = asyncio.Lock()

    def _invoke(self, function, *args, **kwargs):
        identity = threading.get_ident()
        if self._owner is None:
            self._owner = identity
        elif self._owner != identity:
            raise RuntimeError("inference_owner_changed")
        return function(*args, **kwargs)

    def _submit(self, function, *args, **kwargs):
        return self._executor.submit(self._invoke, function, *args, **kwargs)

    @property
    def is_owner_thread(self):
        return threading.get_ident() == self._owner

    def initialize(self, function, *args, **kwargs):
        """Finish initial model construction before starting the HTTP loop."""
        return self._submit(function, *args, **kwargs).result()

    async def generate(self, function, *args, **kwargs):
        async with self._session_lock:
            return await asyncio.wrap_future(self._submit(function, *args, **kwargs))

    def _close_created_iterator(self, completed):
        if completed.cancelled():
            return
        try:
            iterator = completed.result()
        except BaseException:
            return
        if threading.get_ident() == self._owner:
            # A running constructor completes its callback on the owner thread,
            # before that thread can begin the next queued request.
            _close(iterator)
        else:
            self._submit(_close, iterator)

    async def stream(self, factory, *args, **kwargs):
        async with self._session_lock:
            created = self._submit(factory, *args, **kwargs)
            iterator = None
            try:
                iterator = await asyncio.wrap_future(created)
                while True:
                    exhausted, value = await asyncio.wrap_future(self._submit(_advance, iterator))
                    if exhausted:
                        return
                    yield value
            finally:
                if iterator is None:
                    created.add_done_callback(self._close_created_iterator)
                else:
                    # Submit closure before releasing the session lock. Even if
                    # the consumer is cancelled, later inference stays behind
                    # closure in the single worker's FIFO queue.
                    closed = self._submit(_close, iterator)
                    try:
                        await asyncio.shield(asyncio.wrap_future(closed))
                    except asyncio.CancelledError:
                        pass

    def shutdown(self):
        self._executor.shutdown(wait=True)
