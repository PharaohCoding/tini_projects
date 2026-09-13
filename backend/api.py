from __future__ import annotations

import asyncio
import json
import mimetypes
import re
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from backend.errors import ApiError
from backend.models import PROJECT_STATUSES

router = APIRouter(prefix="/api")

SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1)
    goal: str = ""


class ProjectPatch(BaseModel):
    name: str | None = None
    goal: str | None = None
    status: str | None = None


class MessageCreate(BaseModel):
    content: str
    attachment_ids: list[str] | None = None


class FilePut(BaseModel):
    content: str


class AttachedChatIn(BaseModel):
    content: str | None = None
    transcript: str | None = None
    title: str | None = None
    filename: str | None = None


def _state(request: Request) -> Any:
    return request.app.state


def safe_filename(name: str | None) -> str:
    base = Path(name or "file").name
    cleaned = SAFE_NAME.sub("_", base).strip("._")
    return (cleaned or "file")[:180]


async def _project_payload(db: Any, project: dict[str, Any]) -> dict[str, Any]:
    unfinished = await db.unfinished_count(project["id"])
    return {**project, "unfinished_workers": unfinished}


@router.post("/projects")
async def create_project(payload: ProjectCreate, request: Request) -> dict[str, Any]:
    state = _state(request)
    name = payload.name.strip()
    if not name:
        raise ApiError("name is required", 400)
    project = await state.db.create_project(name, payload.goal)
    state.context.init_project(project["id"])
    return await _project_payload(state.db, project)


@router.get("/projects")
async def list_projects(request: Request) -> dict[str, Any]:
    projects = await _state(request).db.list_projects()
    return {"projects": projects}


@router.get("/projects/{project_id}")
async def get_project(project_id: str, request: Request) -> dict[str, Any]:
    state = _state(request)
    project = await state.db.require_project(project_id)
    return await _project_payload(state.db, project)


@router.patch("/projects/{project_id}")
async def patch_project(project_id: str, payload: ProjectPatch, request: Request) -> dict[str, Any]:
    if payload.status is not None and payload.status not in PROJECT_STATUSES:
        raise ApiError("invalid status", 400)
    name = payload.name.strip() if payload.name is not None else None
    if name is not None and not name:
        raise ApiError("name is required", 400)
    project = await _state(request).db.update_project(
        project_id, name=name, goal=payload.goal, status=payload.status
    )
    return await _project_payload(_state(request).db, project)


@router.get("/projects/{project_id}/messages")
async def list_messages(
    project_id: str,
    request: Request,
    after: str | None = None,
) -> dict[str, Any]:
    state = _state(request)
    await state.db.require_project(project_id)
    messages = await state.db.list_messages(project_id, after=after)
    return {"messages": messages}


@router.post("/projects/{project_id}/messages")
async def post_message(project_id: str, payload: MessageCreate, request: Request) -> dict[str, Any]:
    state = _state(request)
    return await state.coordinator.handle_user_message(
        project_id, payload.content, payload.attachment_ids
    )


@router.get("/projects/{project_id}/workers")
async def list_workers(project_id: str, request: Request) -> dict[str, Any]:
    state = _state(request)
    await state.db.require_project(project_id)
    workers = await state.db.list_workers(project_id)
    return {"workers": [w.to_dict() for w in workers]}


@router.get("/projects/{project_id}/workers/{worker_id}")
async def get_worker(project_id: str, worker_id: str, request: Request) -> dict[str, Any]:
    worker = await _state(request).db.require_worker(project_id, worker_id)
    return worker.to_dict()


@router.get("/projects/{project_id}/scheduler")
async def get_scheduler(project_id: str, request: Request) -> dict[str, Any]:
    state = _state(request)
    await state.db.require_project(project_id)
    return await state.scheduler.snapshot(project_id)


@router.post("/projects/{project_id}/workers/{worker_id}/cancel")
async def cancel_worker(project_id: str, worker_id: str, request: Request) -> dict[str, Any]:
    state = _state(request)
    await state.db.require_project(project_id)
    worker = await state.workers.cancel(project_id, worker_id)
    snapshot = await state.scheduler.snapshot(project_id)
    return {"worker": worker.to_dict(), "scheduler": snapshot}


@router.get("/projects/{project_id}/context")
async def get_context_tree(project_id: str, request: Request) -> dict[str, Any]:
    state = _state(request)
    await state.db.require_project(project_id)
    return {"tree": state.context.tree(project_id)}


@router.get("/projects/{project_id}/context/file")
async def get_context_file(
    project_id: str,
    request: Request,
    path: str = Query(..., min_length=1),
) -> Any:
    state = _state(request)
    await state.db.require_project(project_id)
    resolved = state.context.resolve_context(project_id, path)
    if not resolved.exists() or not resolved.is_file():
        raise ApiError("file not found", 404)
    if state.context.is_text_file(path):
        return {
            "path": state.context.rel_from_context(project_id, resolved),
            "content": resolved.read_text(encoding="utf-8"),
            "mime": mimetypes.guess_type(path)[0] or "text/plain",
        }
    mime = mimetypes.guess_type(path)[0] or "application/octet-stream"
    return FileResponse(resolved, media_type=mime, filename=resolved.name)


@router.put("/projects/{project_id}/context/file")
async def put_context_file(
    project_id: str,
    payload: FilePut,
    request: Request,
    path: str = Query(..., min_length=1),
) -> dict[str, Any]:
    state = _state(request)
    await state.db.require_project(project_id)
    if not state.context.is_text_file(path):
        raise ApiError("only text files can be edited", 400)
    rel = state.context.write_text(project_id, path, payload.content, role="user")
    await state.events.publish(project_id, "context.updated", {"path": rel})
    return {"path": rel, "content": payload.content}


@router.post("/projects/{project_id}/attachments")
async def upload_attachment(
    project_id: str,
    request: Request,
    file: UploadFile = File(...),
) -> dict[str, Any]:
    state = _state(request)
    await state.db.require_project(project_id)
    data = await file.read()
    aid = str(uuid.uuid4())
    filename = safe_filename(file.filename)
    raw_name = f"{aid}_{filename}"
    uploads = state.context.uploads_root(project_id)
    uploads.mkdir(parents=True, exist_ok=True)
    raw_path = uploads / raw_name
    raw_path.write_bytes(data)
    rel = f"media/{raw_name}"
    state.context.write_bytes(project_id, rel, data)
    mime = file.content_type or mimetypes.guess_type(filename)[0]
    attachment = await state.db.add_attachment(
        project_id,
        source="upload",
        filename=filename,
        mime=mime,
        stored_path=rel,
        attachment_id=aid,
    )
    await state.events.publish(project_id, "context.updated", {"path": rel})
    return attachment


@router.post("/projects/{project_id}/attached-chats")
async def import_attached_chat(
    project_id: str,
    payload: AttachedChatIn,
    request: Request,
) -> dict[str, Any]:
    state = _state(request)
    await state.db.require_project(project_id)
    text = payload.content if payload.content is not None else payload.transcript
    if text is None or str(text).strip() == "":
        raise ApiError("content is required", 400)
    aid = str(uuid.uuid4())
    title = (payload.title or payload.filename or "attached-chat").strip() or "attached-chat"
    filename = safe_filename(payload.filename or f"{aid}.md")
    if not filename.endswith(".md"):
        filename = f"{filename}.md"
    rel = f"internal/attached-chats/{aid}.md"
    body = f"# {title}\n\n{text.rstrip()}\n"
    state.context.write_text(project_id, rel, body, role="user")
    attachment = await state.db.add_attachment(
        project_id,
        source="chat_import",
        filename=filename,
        mime="text/markdown",
        stored_path=rel,
        attachment_id=aid,
    )
    await state.events.publish(project_id, "context.updated", {"path": rel})
    return attachment


@router.get("/projects/{project_id}/events")
async def project_events(project_id: str, request: Request) -> StreamingResponse:
    state = _state(request)
    await state.db.require_project(project_id)
    hub = state.events

    async def generate():
        queue = hub.subscribe(project_id)
        try:
            hello = {"project_id": project_id}
            yield f"event: scheduler.updated\ndata: {json.dumps(hello, ensure_ascii=False)}\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
                    continue
                payload = json.dumps(event["data"], ensure_ascii=False)
                yield f"event: {event['type']}\ndata: {payload}\n\n"
        finally:
            hub.unsubscribe(project_id, queue)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
