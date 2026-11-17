"""Thread-safe fan-out of live events to WebSocket subscribers."""

from __future__ import annotations

import queue
import threading
from typing import Any


class LiveHub:
    def __init__(self) -> None:
        self._subs: set[queue.Queue] = set()
        self._lock = threading.Lock()
        self.latest: dict[str, dict[str, Any]] = {}  # "pid:ecu" -> reading dict

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=2000)
        with self._lock:
            self._subs.add(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            self._subs.discard(q)

    def publish(self, event: dict[str, Any]) -> None:
        if event.get("type") == "reading":
            r = event["reading"]
            self.latest[f"{r['pid']}:{r.get('ecu')}"] = r
        with self._lock:
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait(event)
            except queue.Full:  # slow consumer: drop its oldest event, never block acquisition
                try:
                    q.get_nowait()
                    q.put_nowait(event)
                except (queue.Empty, queue.Full):
                    pass
