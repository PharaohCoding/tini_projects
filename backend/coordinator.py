from __future__ import annotations

import asyncio
from typing import Any

from backend.adapter import AgentAdapter
from backend.context_store import ContextStore
from backend.db import Database
from backend.errors import ApiError
from backend.events import EventHub
from backend.models import AgentResult
from backend.scheduler import Scheduler
from backend.worker_manager import WorkerManager


class CoordinatorService:
    """Plans, enqueues, and consolidates. Never starts runners."""

    def __init__(
        self,
        adapter: AgentAdapter,
        db: Database,
        context: ContextStore,
        workers: WorkerManager,
        scheduler: Scheduler,
        events: EventHub,
    ) -> None:
        self.adapter = adapter
        self.db = db
        self.context = context
        self.workers = workers
        self.scheduler = scheduler
        self.events = events
        self._locks: dict[str, asyncio.Lock] = {}
        self._locks_guard = asyncio.Lock()

    async def _lock(self, project_id: str) -> asyncio.Lock:
        async with self._locks_guard:
            lock = self._locks.get(project_id)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[project_id] = lock
            return lock

    async def handle_user_message(
        self,
        project_id: str,
        content: str,
        attachment_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        await self.db.require_project(project_id)
        await self._validate_attachments(project_id, attachment_ids or [])
        lock = await self._lock(project_id)
        async with lock:
            user = await self.db.add_message(
                project_id, "user", content, attachment_ids or []
            )
            await self.events.publish(project_id, "message.created", user)
            is_first = (await self.db.user_message_count(project_id)) == 1
            history = await self.db.list_messages(project_id)
            notes = self.context.read_notes(project_id)
            prefs = self.context.read_preferences(project_id)
            result = self.adapter.complete(
                history,
                ["read_context", "write_context", "spawn_worker", "attach_ref", "finish"],
                phase="user",
                user_text=content,
                is_first_user=is_first,
                notes=notes,
                preferences=prefs,
                attachment_ids=attachment_ids or [],
            )
            spawned = await self._apply_tools(project_id, result, allow_spawn=True)
            snapshot = await self.scheduler.snapshot(project_id)
            reply = result.text
            if spawned:
                reply = reply.rstrip() + "\n\n" + format_schedule_text(snapshot, spawned)
            coordinator = await self.db.add_message(project_id, "coordinator", reply)
            await self.events.publish(project_id, "message.created", coordinator)
            await self.events.publish(project_id, "scheduler.updated", {"project_id": project_id})
            return {
                "user": user,
                "coordinator": coordinator,
                "scheduler": snapshot,
            }

    async def run_summary(self, project_id: str) -> dict[str, Any] | None:
        lock = await self._lock(project_id)
        async with lock:
            running = await self.db.workers_by_status(project_id, "running")
            queued = await self.db.workers_by_status(project_id, "queued")
            if running or queued:
                return None
            workers = await self.db.list_workers(project_id)
            reports = []
            for worker in workers:
                if worker.status not in {"done", "failed"}:
                    continue
                report_text = ""
                if worker.report_path:
                    try:
                        report_text = self.context.read_text(project_id, worker.report_path)
                    except ApiError:
                        report_text = ""
                reports.append(
                    {
                        "id": worker.id,
                        "title": worker.title,
                        "kind": worker.kind,
                        "status": worker.status,
                        "result_summary": worker.result_summary,
                        "report": report_text,
                    }
                )
            notes = self.context.read_notes(project_id)
            prefs = self.context.read_preferences(project_id)
            result = self.adapter.complete(
                await self.db.list_messages(project_id),
                ["read_context", "write_context", "finish"],
                phase="summary",
                notes=notes,
                preferences=prefs,
                worker_reports=reports,
            )
            await self._apply_tools(project_id, result, allow_spawn=False)
            coordinator = await self.db.add_message(project_id, "coordinator", result.text)
            await self.events.publish(project_id, "message.created", coordinator)
            await self.events.publish(project_id, "context.updated", {"path": "notes.md"})
            return coordinator

    async def _apply_tools(
        self,
        project_id: str,
        result: AgentResult,
        *,
        allow_spawn: bool,
    ) -> list[dict[str, Any]]:
        spawned: list[dict[str, Any]] = []
        for call in result.tool_calls:
            name = call.name
            args = call.args or {}
            if name == "finish":
                break
            if name == "spawn_worker":
                if not allow_spawn:
                    continue
                worker = await self.workers.enqueue(
                    project_id,
                    title=str(args.get("title") or args.get("kind") or "worker"),
                    assignment=str(args.get("assignment") or ""),
                    kind=str(args.get("kind") or "generic"),
                    priority=str(args.get("priority") or "normal"),
                    environment=str(args.get("environment") or "cloud"),
                    workdir=args.get("workdir"),
                )
                spawned.append(worker.to_dict())
            elif name == "read_context":
                path = str(args.get("path") or "notes.md")
                try:
                    self.context.read_text(project_id, path)
                except ApiError:
                    continue
            elif name == "write_context":
                path = str(args.get("path") or "notes.md")
                if args.get("append_line"):
                    current = self.context.read_notes(project_id) if path == "notes.md" else self.context.read_text(project_id, path)
                    content = current.rstrip() + "\n" + str(args["append_line"]) + "\n"
                else:
                    content = str(args.get("content") or "")
                self.context.write_text(project_id, path, content, role="coordinator")
                await self.events.publish(project_id, "context.updated", {"path": path})
            elif name == "attach_ref":
                aid = str(args.get("attachment_id") or "")
                if aid:
                    row = await self.db.get_attachment(aid)
                    if row is None or row["project_id"] != project_id:
                        raise ApiError("attachment not found", 404)
            else:
                continue
        return spawned

    async def _validate_attachments(self, project_id: str, attachment_ids: list[str]) -> None:
        for aid in attachment_ids:
            row = await self.db.get_attachment(aid)
            if row is None or row["project_id"] != project_id:
                raise ApiError("attachment not found", 400)


def format_schedule_text(snapshot: dict[str, Any], spawned: list[dict[str, Any]]) -> str:
    running = ", ".join(item["kind"] for item in snapshot.get("running") or []) or "无"
    queued = ", ".join(
        f"#{item['position']} {item['kind']}" for item in snapshot.get("queued") or []
    ) or "无"
    kinds = ", ".join(item.get("kind", "?") for item in spawned)
    n_run = len(snapshot.get("running") or [])
    cap = snapshot.get("max_running", 2)
    return (
        f"已入队 {len(spawned)} 个：{kinds}。\n"
        f"运行中（{n_run}/{cap}）：{running}\n"
        f"排队：{queued}\n"
        "本项目最多 2 个并行；同 kind 占满双槽时让路给其他 kind。"
    )
