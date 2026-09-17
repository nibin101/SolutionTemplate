"""In-process pub/sub used to push capture events to the chair-side UI (SSE).

Ingest runs on FastAPI's worker threads while SSE subscribers live on the event
loop, so publishing has to hop back onto the loop thread - that is what
`bind_loop` + `call_soon_threadsafe` below are for.
"""

from __future__ import annotations

import asyncio
import json
import threading
from typing import Any


class EventBroker:
    def __init__(self, max_queue: int = 256) -> None:
        self._subscribers: set[asyncio.Queue[str]] = set()
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._max_queue = max_queue

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def subscribe(self) -> asyncio.Queue[str]:
        queue: asyncio.Queue[str] = asyncio.Queue(maxsize=self._max_queue)
        with self._lock:
            self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[str]) -> None:
        with self._lock:
            self._subscribers.discard(queue)

    def publish(self, event_type: str, data: dict[str, Any]) -> None:
        payload = json.dumps({"type": event_type, "data": data})
        loop = self._loop
        if loop is None:
            return
        with self._lock:
            queues = list(self._subscribers)
        for queue in queues:
            # A browser tab that stopped reading must never block ingest, so a
            # full queue drops the event rather than applying back-pressure.
            loop.call_soon_threadsafe(self._offer, queue, payload)

    @staticmethod
    def _offer(queue: asyncio.Queue[str], payload: str) -> None:
        try:
            queue.put_nowait(payload)
        except asyncio.QueueFull:
            pass


broker = EventBroker()
