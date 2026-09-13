from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from backend.adapter import FAIL_TOKEN
from backend.context_store import ContextStore
from backend.db import Database
from backend.errors import ApiError
from backend.events import EventHub
from backend.models import ENVIRONMENTS, KINDS, PRIORITIES, WorkerRow
from backend.scheduler import Scheduler
from backend.settings import Settings
from backend.timeutil import utcnow

if TYPE_CHECKING:
    from backend.coordinator import CoordinatorService


class WorkerManager:
    """State machine + mock runner. start() is Scheduler-only."""

    def __init__(
        self,
        db: Database,
        events: EventHub,
        context: ContextStore,
        scheduler: Scheduler,
        settings: Settings,
    ) -> None:
        self.db = db
        self.events = events
        self.context = context
        self.scheduler = scheduler
        self.settings = settings
        self._tasks: dict[str, asyncio.Task] = {}
        self._coordinator: CoordinatorService | None = None
        self._dirty_summary: set[str] = set()

    def bind_coordinator(self, coordinator: CoordinatorService) -> None:
        self._coordinator = coordinator

    def start(self, worker: WorkerRow) -> None:
        """Launch a mock runner. Scheduler.kick is the only caller."""
        existing = self._tasks.get(worker.id)
        if existing is not None and not existing.done():
            return
        # asyncio.create_task is allowed ONLY here, after Scheduler selected this worker.
        self._tasks[worker.id] = asyncio.create_task(
            self._run(worker.id),
            name=f"worker-{worker.id}",
        )

    async def enqueue(
        self,
        project_id: str,
        *,
        title: str,
        assignment: str,
        kind: str,
        priority: str = "normal",
        environment: str = "cloud",
        workdir: str | None = None,
    ) -> WorkerRow:
        if kind not in KINDS:
            raise ApiError(f"invalid kind: {kind}", 400)
        if priority not in PRIORITIES:
            raise ApiError(f"invalid priority: {priority}", 400)
        if environment not in ENVIRONMENTS:
            raise ApiError(f"invalid environment: {environment}", 400)
        local_workdir = workdir if environment == "local" else None
        worker = await self.db.insert_worker(
            project_id,
            title=title,
            assignment=assignment,
            kind=kind,
            priority=priority,
            environment=environment,
            workdir=local_workdir,
        )
        await self.events.publish(project_id, "worker.queued", worker.to_dict())
        await self.events.publish(project_id, "scheduler.updated", {"project_id": project_id})
        await self.scheduler.kick(project_id)
        latest = await self.db.get_worker(worker.id)
        assert latest is not None
        return latest

    async def cancel(self, project_id: str, worker_id: str) -> WorkerRow:
        worker = await self.db.require_worker(project_id, worker_id)
        if worker.status in {"done", "failed", "canceled"}:
            return worker
        if worker.status == "queued":
            updated = await self.db.update_worker(
                worker.id,
                status="canceled",
                finished_at=utcnow(),
                error="canceled",
            )
            await self.events.publish(project_id, "worker.canceled", updated.to_dict())
            await self.events.publish(project_id, "scheduler.updated", {"project_id": project_id})
            await self.scheduler.kick(project_id)
            return updated
        task = self._tasks.get(worker_id)
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        else:
            current = await self.db.get_worker(worker_id)
            if current is not None and current.status == "running":
                updated = await self.db.update_worker(
                    worker.id,
                    status="canceled",
                    finished_at=utcnow(),
                    error="canceled",
                )
                await self.events.publish(project_id, "worker.canceled", updated.to_dict())
                await self.events.publish(project_id, "scheduler.updated", {"project_id": project_id})
        await self.scheduler.kick(project_id)
        return await self.db.require_worker(project_id, worker_id)

    async def shutdown(self) -> None:
        tasks = [task for task in self._tasks.values() if not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _run(self, worker_id: str) -> None:
        worker = await self.db.get_worker(worker_id)
        if worker is None or worker.status != "running":
            return
        try:
            duration = self.settings.duration_for(worker.kind)
            await asyncio.sleep(duration)
            worker = await self.db.get_worker(worker_id)
            if worker is None or worker.status != "running":
                return
            if FAIL_TOKEN in (worker.assignment or ""):
                await self._finish(worker, status="failed", error="mock worker failed")
                return
            report_path = self._write_outputs(worker)
            summary = f"Mock {worker.kind} 完成：{worker.title}"
            await self._finish(
                worker,
                status="done",
                report_path=report_path,
                result_summary=summary,
            )
        except asyncio.CancelledError:
            current = await self.db.get_worker(worker_id)
            if current is not None and current.status == "running":
                updated = await self.db.update_worker(
                    worker_id,
                    status="canceled",
                    finished_at=utcnow(),
                    error="canceled",
                )
                await self.events.publish(current.project_id, "worker.canceled", updated.to_dict())
                await self.events.publish(
                    current.project_id, "scheduler.updated", {"project_id": current.project_id}
                )
            raise
        except Exception as exc:
            current = await self.db.get_worker(worker_id)
            if current is not None and current.status == "running":
                await self._finish(current, status="failed", error=str(exc))
        finally:
            self._tasks.pop(worker_id, None)

    def _write_outputs(self, worker: WorkerRow) -> str:
        rel = f"internal/{worker.id}-report.md"
        body = (
            f"---\n"
            f'worker_id: "{worker.id}"\n'
            f"kind: {worker.kind}\n"
            f"---\n\n"
            f"# {worker.title}\n\n"
            f"## Assignment\n\n"
            f"{worker.assignment}\n\n"
            f"Mock worker 已完成。kind={worker.kind} environment={worker.environment}。\n"
        )
        self.context.write_text(
            worker.project_id,
            rel,
            body,
            role="worker",
            worker_id=worker.id,
        )
        if worker.kind == "implement":
            self.context.write_sandbox_text(
                worker.project_id,
                "output.txt",
                f"sandbox output from {worker.id}\n",
            )
        return rel

    async def _finish(
        self,
        worker: WorkerRow,
        *,
        status: str,
        error: str | None = None,
        report_path: str | None = None,
        result_summary: str | None = None,
    ) -> None:
        current = await self.db.get_worker(worker.id)
        if current is None or current.status != "running":
            return
        fields: dict[str, str | None] = {"status": status, "finished_at": utcnow()}
        if error is not None:
            fields["error"] = error
        if report_path is not None:
            fields["report_path"] = report_path
        if result_summary is not None:
            fields["result_summary"] = result_summary
        updated = await self.db.update_worker(worker.id, **fields)
        event = "worker.done" if status == "done" else "worker.failed"
        await self.events.publish(worker.project_id, event, updated.to_dict())
        await self.events.publish(
            worker.project_id, "scheduler.updated", {"project_id": worker.project_id}
        )
        self._dirty_summary.add(worker.project_id)
        await self.scheduler.kick(worker.project_id)
        await self._maybe_summary(worker.project_id)

    async def _maybe_summary(self, project_id: str) -> None:
        if not self.settings.auto_summary:
            return
        if project_id not in self._dirty_summary:
            return
        running = await self.db.workers_by_status(project_id, "running")
        queued = await self.db.workers_by_status(project_id, "queued")
        if running or queued:
            return
        if self._coordinator is None:
            return
        await self._coordinator.run_summary(project_id)
        self._dirty_summary.discard(project_id)
