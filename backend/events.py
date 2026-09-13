from __future__ import annotations

import asyncio
from collections import defaultdict
from typing import Any


class EventHub:
    """In-process SSE fan-out. No persistence."""

    def __init__(self) -> None:
        self._subs: dict[str, list[asyncio.Queue]] = defaultdict(list)

    def subscribe(self, project_id: str) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=256)
        self._subs[project_id].append(queue)
        return queue

    def unsubscribe(self, project_id: str, queue: asyncio.Queue) -> None:
        buckets = self._subs.get(project_id)
        if not buckets:
            return
        try:
            buckets.remove(queue)
        except ValueError:
            return
        if not buckets:
            self._subs.pop(project_id, None)

    async def publish(self, project_id: str, event_type: str, data: dict[str, Any]) -> None:
        payload = {"type": event_type, "data": data}
        for queue in list(self._subs.get(project_id, [])):
            try:
                queue.put_nowait(payload)
            except asyncio.QueueFull:
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                try:
                    queue.put_nowait(payload)
                except asyncio.QueueFull:
                    pass
