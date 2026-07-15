"""In-memory ring buffer of recent log records, for the dashboard's Logs tab.

Attaches an extra logging.Handler to the root logger (alongside the existing
StreamHandler from logging.basicConfig) so recent formatted log lines can be served
over HTTP/SSE without reading a log file — the container's stdout isn't otherwise
accessible from inside the process. Installed lazily on first use so importing this
module has no side effects.
"""
import logging
import threading
from collections import deque

MAX_LINES = 2000


class RingBufferHandler(logging.Handler):
    """Thread-safe bounded buffer of (seq, formatted line) tuples."""

    def __init__(self, maxlen: int = MAX_LINES):
        super().__init__()
        self._buf = deque(maxlen=maxlen)
        self._cond = threading.Condition()
        self._seq = 0

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
        except Exception:
            return
        with self._cond:
            self._seq += 1
            self._buf.append((self._seq, msg))
            self._cond.notify_all()

    def snapshot(self) -> list:
        """All buffered (seq, line) tuples, oldest first."""
        with self._cond:
            return list(self._buf)

    def wait_for_more(self, after_seq: int, timeout: float = 15.0) -> list:
        """Block until at least one line newer than after_seq exists (or timeout),
        then return every buffered line newer than after_seq."""
        with self._cond:
            self._cond.wait_for(lambda: bool(self._buf) and self._buf[-1][0] > after_seq,
                                timeout=timeout)
            return [item for item in self._buf if item[0] > after_seq]


_handler = None
_install_lock = threading.Lock()


def install() -> RingBufferHandler:
    """Idempotent: attaches the handler to the root logger on first call only."""
    global _handler
    with _install_lock:
        if _handler is not None:
            return _handler
        handler = RingBufferHandler()
        handler.setFormatter(logging.Formatter(
            fmt="%(asctime)s cerbomoticzGx: %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
        logging.getLogger().addHandler(handler)
        _handler = handler
        return handler


def get_handler() -> RingBufferHandler:
    return _handler or install()
