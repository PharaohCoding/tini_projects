from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


KINDS = ("research", "implement", "test", "generic")
PRIORITIES = ("normal", "high")
ENVIRONMENTS = ("cloud", "local")
WORKER_STATUSES = ("queued", "running", "done", "failed", "canceled")
PROJECT_STATUSES = ("active", "paused", "archived")
MESSAGE_ROLES = ("user", "coordinator", "system")
ATTACHMENT_SOURCES = ("upload", "chat_import", "worker")
TEXT_SUFFIXES = {
    ".md",
    ".txt",
    ".json",
    ".csv",
    ".py",
    ".yml",
    ".yaml",
    ".toml",
    ".ini",
    ".xml",
    ".html",
    ".css",
    ".js",
    ".ts",
    ".rst",
    ".log",
}


@dataclass
class ToolCall:
    name: str
    args: dict[str, Any] = field(default_factory=dict)


@dataclass
class AgentResult:
    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)


@dataclass
class QueueItem:
    id: str
    kind: str
    priority: str
    enqueue_seq: int
    schedule_reason: str | None = None
    started_at: str | None = None
    title: str = ""
    status: str = "queued"


@dataclass
class WorkerRow:
    id: str
    project_id: str
    title: str
    assignment: str
    kind: str
    environment: str
    priority: str
    enqueue_seq: int
    status: str
    schedule_reason: str | None
    report_path: str | None
    result_summary: str | None
    error: str | None
    workdir: str | None
    created_at: str
    started_at: str | None
    finished_at: str | None

    def as_queue_item(self) -> QueueItem:
        return QueueItem(
            id=self.id,
            kind=self.kind,
            priority=self.priority,
            enqueue_seq=self.enqueue_seq,
            schedule_reason=self.schedule_reason,
            started_at=self.started_at,
            title=self.title,
            status=self.status,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "project_id": self.project_id,
            "title": self.title,
            "assignment": self.assignment,
            "kind": self.kind,
            "environment": self.environment,
            "priority": self.priority,
            "enqueue_seq": self.enqueue_seq,
            "status": self.status,
            "schedule_reason": self.schedule_reason,
            "report_path": self.report_path,
            "result_summary": self.result_summary,
            "error": self.error,
            "workdir": self.workdir,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }


def worker_from_row(row: Any) -> WorkerRow:
    return WorkerRow(
        id=row["id"],
        project_id=row["project_id"],
        title=row["title"],
        assignment=row["assignment"],
        kind=row["kind"],
        environment=row["environment"],
        priority=row["priority"],
        enqueue_seq=row["enqueue_seq"],
        status=row["status"],
        schedule_reason=row["schedule_reason"],
        report_path=row["report_path"],
        result_summary=row["result_summary"],
        error=row["error"],
        workdir=row["workdir"],
        created_at=row["created_at"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
    )
