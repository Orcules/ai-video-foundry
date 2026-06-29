"""In-process server log capture for real-time debugging (SSE + recent tail).

Installed at API startup. Endpoints use the normal tenant API key (Bearer / ?token=) — no extra secret.

Env:
  SERVER_LOG_BUFFER_LINES   — ring buffer size (default 8000)

Logs are process-wide; they may include paths, URLs, and errors from any job on this server.
"""

from __future__ import annotations

import logging
import os
import queue
import threading
from collections import deque
from typing import Deque, List

_lock = threading.RLock()
_buffer: Deque[str] = deque(maxlen=max(100, int(os.environ.get("SERVER_LOG_BUFFER_LINES", "8000"))))
_subscribers: List[queue.Queue] = []
_handler_installed = False


class _RingBufferBroadcastHandler(logging.Handler):
    """Append formatted lines to a ring buffer and fan-out to subscriber queues."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
        except Exception:
            msg = record.getMessage()
        with _lock:
            _buffer.append(msg)
            for q in list(_subscribers):
                try:
                    q.put_nowait(msg)
                except queue.Full:
                    try:
                        q.get_nowait()
                    except queue.Empty:
                        pass
                    try:
                        q.put_nowait(msg)
                    except Exception:
                        pass


def install_server_log_capture() -> None:
    """Idempotent: attach handler to root logger (INFO+)."""
    global _handler_installed
    with _lock:
        if _handler_installed:
            return
        h = _RingBufferBroadcastHandler()
        h.setLevel(logging.INFO)
        h.setFormatter(logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s"))
        root = logging.getLogger()
        root.addHandler(h)
        # Uvicorn often logs on these; ensure they propagate (default True) — root already has handler.
        for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
            logging.getLogger(name).setLevel(logging.INFO)
        _handler_installed = True
        logging.getLogger(__name__).info(
            "Server log capture installed (buffer=%s lines). GET /api/admin/server-logs/recent and /stream (API key).",
            _buffer.maxlen,
        )


def get_recent_lines(limit: int = 500) -> List[str]:
    if limit <= 0:
        return []
    with _lock:
        lines = list(_buffer)
    return lines[-limit:]


def register_subscriber(maxsize: int = 2000) -> queue.Queue:
    q: queue.Queue = queue.Queue(maxsize=maxsize)
    with _lock:
        _subscribers.append(q)
    return q


def unregister_subscriber(q: queue.Queue) -> None:
    with _lock:
        try:
            _subscribers.remove(q)
        except ValueError:
            pass
