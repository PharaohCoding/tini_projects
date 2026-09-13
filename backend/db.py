from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any

import aiosqlite

from backend.errors import ApiError
from backend.models import WorkerRow, worker_from_row
from backend.timeutil import utcnow

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    goal TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    attachment_ids TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    FOREIGN KEY (project_id) REFERENCES projects(id)
);

CREATE TABLE IF NOT EXISTS workers (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    title TEXT NOT NULL,
    assignment TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL,
    environment TEXT NOT NULL DEFAULT 'cloud',
    priority TEXT NOT NULL DEFAULT 'normal',
    enqueue_seq INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',
    schedule_reason TEXT,
    report_path TEXT,
    result_summary TEXT,
    error TEXT,
    workdir TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    FOREIGN KEY (project_id) REFERENCES projects(id)
);

CREATE TABLE IF NOT EXISTS attachments (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    source TEXT NOT NULL,
    filename TEXT NOT NULL,
    mime TEXT,
    stored_path TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (project_id) REFERENCES projects(id)
);

CREATE INDEX IF NOT EXISTS idx_messages_project ON messages(project_id, created_at, id);
CREATE INDEX IF NOT EXISTS idx_workers_project_status ON workers(project_id, status, enqueue_seq);
CREATE INDEX IF NOT EXISTS idx_attachments_project ON attachments(project_id, created_at);
"""


class Database:
    _WORKER_UPDATE_FIELDS = {
        "title",
        "assignment",
        "status",
        "schedule_reason",
        "report_path",
        "result_summary",
        "error",
        "workdir",
        "started_at",
        "finished_at",
    }

    def __init__(self, path: str | Any) -> None:
        self._path = str(path)
        self._conn: aiosqlite.Connection | None = None
        self._write_lock = asyncio.Lock()

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("database is not initialized")
        return self._conn

    async def init(self) -> None:
        self._conn = await aiosqlite.connect(self._path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA foreign_keys = ON")
        await self._conn.execute("PRAGMA journal_mode = WAL")
        await self._conn.executescript(SCHEMA)
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    async def create_project(self, name: str, goal: str = "") -> dict[str, Any]:
        pid = str(uuid.uuid4())
        now = utcnow()
        await self.conn.execute(
            "INSERT INTO projects (id, name, goal, status, created_at, updated_at) VALUES (?, ?, ?, 'active', ?, ?)",
            (pid, name, goal, now, now),
        )
        await self.conn.commit()
        row = await self.get_project(pid)
        assert row is not None
        return row

    async def get_project(self, project_id: str) -> dict[str, Any] | None:
        cur = await self.conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,))
        row = await cur.fetchone()
        return dict(row) if row else None

    async def require_project(self, project_id: str) -> dict[str, Any]:
        row = await self.get_project(project_id)
        if row is None:
            raise ApiError("project not found", 404)
        return row

    async def list_projects(self) -> list[dict[str, Any]]:
        cur = await self.conn.execute(
            """
            SELECT p.*,
                   (SELECT COUNT(*) FROM workers w
                    WHERE w.project_id = p.id AND w.status IN ('queued', 'running'))
                   AS unfinished_workers
            FROM projects p
            ORDER BY p.updated_at DESC
            """
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def update_project(
        self,
        project_id: str,
        *,
        name: str | None = None,
        goal: str | None = None,
        status: str | None = None,
    ) -> dict[str, Any]:
        row = await self.require_project(project_id)
        new_name = name if name is not None else row["name"]
        new_goal = goal if goal is not None else row["goal"]
        new_status = status if status is not None else row["status"]
        now = utcnow()
        await self.conn.execute(
            "UPDATE projects SET name = ?, goal = ?, status = ?, updated_at = ? WHERE id = ?",
            (new_name, new_goal, new_status, now, project_id),
        )
        await self.conn.commit()
        updated = await self.get_project(project_id)
        assert updated is not None
        return updated

    async def touch_project(self, project_id: str) -> None:
        await self.conn.execute(
            "UPDATE projects SET updated_at = ? WHERE id = ?",
            (utcnow(), project_id),
        )
        await self.conn.commit()

    async def unfinished_count(self, project_id: str) -> int:
        cur = await self.conn.execute(
            "SELECT COUNT(*) AS n FROM workers WHERE project_id = ? AND status IN ('queued', 'running')",
            (project_id,),
        )
        row = await cur.fetchone()
        return int(row["n"] if row else 0)

    async def add_message(
        self,
        project_id: str,
        role: str,
        content: str,
        attachment_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        mid = str(uuid.uuid4())
        now = utcnow()
        ids = json.dumps(attachment_ids or [])
        await self.conn.execute(
            "INSERT INTO messages (id, project_id, role, content, attachment_ids, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (mid, project_id, role, content, ids, now),
        )
        await self.touch_project(project_id)
        return {
            "id": mid,
            "project_id": project_id,
            "role": role,
            "content": content,
            "attachment_ids": attachment_ids or [],
            "created_at": now,
        }

    async def get_message(self, message_id: str) -> dict[str, Any] | None:
        cur = await self.conn.execute("SELECT * FROM messages WHERE id = ?", (message_id,))
        row = await cur.fetchone()
        return self._message_dict(row) if row else None

    async def list_messages(self, project_id: str, after: str | None = None) -> list[dict[str, Any]]:
        if after:
            cursor = await self.get_message(after)
            if cursor is None or cursor["project_id"] != project_id:
                raise ApiError("after cursor not found", 400)
            cur = await self.conn.execute(
                """
                SELECT * FROM messages
                WHERE project_id = ?
                  AND (created_at > ? OR (created_at = ? AND id > ?))
                ORDER BY created_at ASC, id ASC
                """,
                (project_id, cursor["created_at"], cursor["created_at"], after),
            )
        else:
            cur = await self.conn.execute(
                "SELECT * FROM messages WHERE project_id = ? ORDER BY created_at ASC, id ASC",
                (project_id,),
            )
        rows = await cur.fetchall()
        return [self._message_dict(r) for r in rows]

    async def user_message_count(self, project_id: str) -> int:
        cur = await self.conn.execute(
            "SELECT COUNT(*) AS n FROM messages WHERE project_id = ? AND role = 'user'",
            (project_id,),
        )
        row = await cur.fetchone()
        return int(row["n"] if row else 0)

    def _message_dict(self, row: aiosqlite.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "project_id": row["project_id"],
            "role": row["role"],
            "content": row["content"],
            "attachment_ids": json.loads(row["attachment_ids"] or "[]"),
            "created_at": row["created_at"],
        }

    async def next_enqueue_seq(self, project_id: str) -> int:
        cur = await self.conn.execute(
            "SELECT COALESCE(MAX(enqueue_seq), 0) + 1 AS n FROM workers WHERE project_id = ?",
            (project_id,),
        )
        row = await cur.fetchone()
        return int(row["n"] if row else 1)

    async def insert_worker(
        self,
        project_id: str,
        *,
        title: str,
        assignment: str,
        kind: str,
        priority: str = "normal",
        environment: str = "cloud",
        workdir: str | None = None,
        worker_id: str | None = None,
    ) -> WorkerRow:
        wid = worker_id or str(uuid.uuid4())
        now = utcnow()
        async with self._write_lock:
            seq = await self.next_enqueue_seq(project_id)
            await self.conn.execute(
                """
                INSERT INTO workers (
                    id, project_id, title, assignment, kind, environment, priority,
                    enqueue_seq, status, schedule_reason, report_path, result_summary,
                    error, workdir, created_at, started_at, finished_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'queued', NULL, NULL, NULL, NULL, ?, ?, NULL, NULL)
                """,
                (wid, project_id, title, assignment, kind, environment, priority, seq, workdir, now),
            )
            await self.conn.commit()
        worker = await self.get_worker(wid)
        assert worker is not None
        return worker

    async def get_worker(self, worker_id: str) -> WorkerRow | None:
        cur = await self.conn.execute("SELECT * FROM workers WHERE id = ?", (worker_id,))
        row = await cur.fetchone()
        return worker_from_row(row) if row else None

    async def require_worker(self, project_id: str, worker_id: str) -> WorkerRow:
        worker = await self.get_worker(worker_id)
        if worker is None or worker.project_id != project_id:
            raise ApiError("worker not found", 404)
        return worker

    async def list_workers(self, project_id: str) -> list[WorkerRow]:
        cur = await self.conn.execute(
            "SELECT * FROM workers WHERE project_id = ? ORDER BY enqueue_seq ASC",
            (project_id,),
        )
        rows = await cur.fetchall()
        return [worker_from_row(r) for r in rows]

    async def workers_by_status(self, project_id: str, status: str) -> list[WorkerRow]:
        cur = await self.conn.execute(
            "SELECT * FROM workers WHERE project_id = ? AND status = ? ORDER BY enqueue_seq ASC",
            (project_id, status),
        )
        rows = await cur.fetchall()
        return [worker_from_row(r) for r in rows]

    async def mark_running(self, worker_id: str, schedule_reason: str, started_at: str) -> bool:
        async with self._write_lock:
            cur = await self.conn.execute(
                """
                UPDATE workers SET status = 'running', schedule_reason = ?, started_at = ?
                WHERE id = ? AND status = 'queued'
                """,
                (schedule_reason, started_at, worker_id),
            )
            await self.conn.commit()
            return cur.rowcount > 0

    async def update_worker(self, worker_id: str, **fields: Any) -> WorkerRow:
        unknown = set(fields) - self._WORKER_UPDATE_FIELDS
        if unknown:
            raise ValueError(f"unsupported worker fields: {unknown}")
        if not fields:
            worker = await self.get_worker(worker_id)
            assert worker is not None
            return worker
        cols = ", ".join(f"{k} = ?" for k in fields)
        values = list(fields.values()) + [worker_id]
        async with self._write_lock:
            await self.conn.execute(f"UPDATE workers SET {cols} WHERE id = ?", values)
            await self.conn.commit()
        worker = await self.get_worker(worker_id)
        assert worker is not None
        return worker

    async def add_attachment(
        self,
        project_id: str,
        *,
        source: str,
        filename: str,
        mime: str | None,
        stored_path: str,
        attachment_id: str | None = None,
    ) -> dict[str, Any]:
        aid = attachment_id or str(uuid.uuid4())
        now = utcnow()
        await self.conn.execute(
            """
            INSERT INTO attachments (id, project_id, source, filename, mime, stored_path, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (aid, project_id, source, filename, mime, stored_path, now),
        )
        await self.conn.commit()
        return {
            "id": aid,
            "project_id": project_id,
            "source": source,
            "filename": filename,
            "mime": mime,
            "stored_path": stored_path,
            "created_at": now,
        }

    async def get_attachment(self, attachment_id: str) -> dict[str, Any] | None:
        cur = await self.conn.execute("SELECT * FROM attachments WHERE id = ?", (attachment_id,))
        row = await cur.fetchone()
        return dict(row) if row else None

    async def list_attachments(self, project_id: str) -> list[dict[str, Any]]:
        cur = await self.conn.execute(
            "SELECT * FROM attachments WHERE project_id = ? ORDER BY created_at ASC",
            (project_id,),
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]
