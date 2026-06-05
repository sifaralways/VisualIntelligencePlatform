from __future__ import annotations

import threading
import time

_lock = threading.Lock()
_last_user_activity_monotonic = time.monotonic()


def mark_user_activity() -> None:
    global _last_user_activity_monotonic
    with _lock:
        _last_user_activity_monotonic = time.monotonic()


def seconds_since_user_activity() -> float:
    with _lock:
        return max(0.0, time.monotonic() - _last_user_activity_monotonic)
