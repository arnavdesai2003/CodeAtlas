"""Bounded process-local work sharing; never a cache or a distributed lock."""

from concurrent.futures import Future
from threading import Lock


class MissFlights:
    def __init__(self, *, max_keys: int = 128, wait_seconds: float = 1.0):
        self.max_keys = max_keys
        self.wait_seconds = wait_seconds
        self._lock = Lock()
        self._pending: dict[tuple[str, int, str], Future] = {}

    def acquire(self, key):
        with self._lock:
            if key in self._pending:
                return self._pending[key], False
            if len(self._pending) >= self.max_keys:
                return None, False
            future = Future()
            self._pending[key] = future
            return future, True

    def release(self, key, future):
        with self._lock:
            if self._pending.get(key) is future:
                del self._pending[key]
