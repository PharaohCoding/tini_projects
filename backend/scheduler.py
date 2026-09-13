from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Sequence

from backend.db import Database
from backend.events import EventHub
from backend.models import QueueItem, WorkerRow
from backend.settings import Settings
from backend.timeutil import utcnow

PRIORITY_RANK = {"high": 0, "normal": 1}

Starter = Callable[[WorkerRow], None]


def queue_order_key(item: QueueItem) -> tuple[int, int]:
    return (PRIORITY_RANK.get(item.priority, 1), item.enqueue_seq)


def order_queued(queued: Sequence[QueueItem]) -> list[QueueItem]:
    return sorted(queued, key=queue_order_key)


def select_next(
    running: Sequence[QueueItem],
    queued: Sequence[QueueItem],
) -> tuple[QueueItem, str] | None:
    """Pick the next queued worker and a UI-visible schedule_reason.

    Policy: fair-kind / cap is applied by the caller. This function only
    chooses among queued items given the current running set.

    - Same priority: FIFO via enqueue_seq.
    - high jumps ahead of all normal; same priority remains FIFO.
    - Kind fairness: if every running worker shares one kind, the queue
      head is that same kind, and another kind is waiting, skip the head
      and admit the first different kind.
    """
    if not queued:
        return None
    ordered = order_queued(queued)
    peek = ordered[0]
    running_kinds = {w.kind for w in running}
    if (
        len(running_kinds) == 1
        and peek.kind in running_kinds
        and any(q.kind != peek.kind for q in ordered)
    ):
        pick = next(q for q in ordered if q.kind != peek.kind)
        occupied = next(iter(running_kinds))
        return pick, f"fair-kind: 避免 {occupied} 占满双槽"
    reason = "priority" if peek.priority == "high" else "fifo"
    return peek, reason


class Scheduler:
    """Sole admission controller. Never writes Context or talks to the user."""

    def __init__(self, db: Database, events: EventHub, settings: Settings) -> None:
        self.db = db
        self.events = events
        self.settings = settings
        self._starter: Starter | None = None
        self._locks: dict[str, asyncio.Lock] = {}
        self._locks_guard = asyncio.Lock()

    def set_starter(self, starter: Starter) -> None:
        self._starter = starter

    async def _project_lock(self, project_id: str) -> asyncio.Lock:
        async with self._locks_guard:
            lock = self._locks.get(project_id)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[project_id] = lock
            return lock

    async def kick(self, project_id: str) -> list[str]:
        if self._starter is None:
            raise RuntimeError("Scheduler starter is not bound; WorkerManager.start must be set")
        started_ids: list[str] = []
        lock = await self._project_lock(project_id)
        to_start: list[WorkerRow] = []
        async with lock:
            while True:
                running_rows = await self.db.workers_by_status(project_id, "running")
                if len(running_rows) >= self.settings.max_running:
                    break
                queued_rows = await self.db.workers_by_status(project_id, "queued")
                picked = select_next(
                    [w.as_queue_item() for w in running_rows],
                    [w.as_queue_item() for w in queued_rows],
                )
                if picked is None:
                    break
                item, reason = picked
                admitted = await self.db.mark_running(item.id, reason, utcnow())
                if not admitted:
                    continue
                worker = await self.db.get_worker(item.id)
                if worker is None:
                    break
                to_start.append(worker)
        for worker in to_start:
            self._starter(worker)
            started_ids.append(worker.id)
            await self.events.publish(
                project_id,
                "worker.started",
                {
                    "id": worker.id,
                    "kind": worker.kind,
                    "schedule_reason": worker.schedule_reason,
                    "started_at": worker.started_at,
                },
            )
        if to_start:
            await self.events.publish(project_id, "scheduler.updated", {"project_id": project_id})
        return started_ids

    async def snapshot(self, project_id: str) -> dict:
        running_rows = await self.db.workers_by_status(project_id, "running")
        queued_rows = await self.db.workers_by_status(project_id, "queued")
        queued_items = order_queued([w.as_queue_item() for w in queued_rows])
        running_sorted = sorted(
            running_rows,
            key=lambda w: (w.started_at or "", w.enqueue_seq),
        )
        return {
            "policy": self.settings.policy,
            "max_running": self.settings.max_running,
            "running": [
                {
                    "id": w.id,
                    "kind": w.kind,
                    "schedule_reason": w.schedule_reason,
                    "started_at": w.started_at,
                }
                for w in running_sorted
            ],
            "queued": [
                {
                    "id": item.id,
                    "kind": item.kind,
                    "priority": item.priority,
                    "enqueue_seq": item.enqueue_seq,
                    "position": index,
                }
                for index, item in enumerate(queued_items, start=1)
            ],
        }
