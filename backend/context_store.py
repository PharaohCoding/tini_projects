from __future__ import annotations

from pathlib import Path
from typing import Any

from backend.errors import ApiError
from backend.models import TEXT_SUFFIXES
from backend.settings import Settings

NOTES_SEED = """# notes.md

（Coordinator 维护）打开本 Project 先看这里。

## 进度

- 项目已创建。

## 待办

- （暂无）

## 阻塞

- 无
"""

PREFERENCES_SEED = """# preferences.md

- 文风：简洁、短句
- MVP 使用 Mock Agent，不调用真实 LLM
- Coordinator 只规划与汇总，不改业务代码
"""


class ContextStore:
    """Project-scoped file I/O with a hard path sandbox."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def project_root(self, project_id: str) -> Path:
        return (self.settings.data_dir / "projects" / project_id).resolve()

    def context_root(self, project_id: str) -> Path:
        return self.project_root(project_id) / "context"

    def sandbox_root(self, project_id: str) -> Path:
        return self.project_root(project_id) / "sandbox"

    def uploads_root(self, project_id: str) -> Path:
        return self.project_root(project_id) / "uploads"

    def init_project(self, project_id: str) -> None:
        ctx = self.context_root(project_id)
        for rel in (
            "docs",
            "plans",
            "media",
            "internal",
            "internal/inbox",
            "internal/attached-chats",
        ):
            (ctx / rel).mkdir(parents=True, exist_ok=True)
        self.sandbox_root(project_id).mkdir(parents=True, exist_ok=True)
        self.uploads_root(project_id).mkdir(parents=True, exist_ok=True)
        notes = ctx / "notes.md"
        prefs = ctx / "preferences.md"
        if not notes.exists():
            notes.write_text(NOTES_SEED, encoding="utf-8")
        if not prefs.exists():
            prefs.write_text(PREFERENCES_SEED, encoding="utf-8")

    def resolve_context(self, project_id: str, relpath: str) -> Path:
        return self._safe_resolve(self.context_root(project_id), relpath)

    def resolve_sandbox(self, project_id: str, relpath: str) -> Path:
        return self._safe_resolve(self.sandbox_root(project_id), relpath)

    def _safe_resolve(self, base: Path, relpath: str) -> Path:
        if relpath is None or str(relpath).strip() == "":
            raise ApiError("path is required", 400)
        raw = str(relpath).replace("\\", "/")
        if "\x00" in raw:
            raise ApiError("path escapes sandbox", 400)
        if raw.startswith("/") or raw.startswith("~") or (len(raw) >= 2 and raw[1] == ":"):
            raise ApiError("path escapes sandbox", 400)
        parts: list[str] = []
        for piece in raw.split("/"):
            if piece in ("", "."):
                continue
            if piece == "..":
                raise ApiError("path escapes sandbox", 400)
            parts.append(piece)
        if not parts:
            raise ApiError("path is required", 400)
        base_resolved = base.resolve()
        target = (base_resolved.joinpath(*parts)).resolve()
        try:
            target.relative_to(base_resolved)
        except ValueError as exc:
            raise ApiError("path escapes sandbox", 400) from exc
        return target

    def rel_from_context(self, project_id: str, path: Path) -> str:
        return path.resolve().relative_to(self.context_root(project_id).resolve()).as_posix()

    def tree(self, project_id: str) -> list[dict[str, Any]]:
        root = self.context_root(project_id)
        if not root.exists():
            return []
        return self._walk(root, root)

    def _walk(self, root: Path, current: Path) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        try:
            children = sorted(current.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
        except FileNotFoundError:
            return items
        for child in children:
            rel = child.relative_to(root).as_posix()
            if child.is_dir():
                items.append(
                    {
                        "path": rel,
                        "type": "dir",
                        "children": self._walk(root, child),
                    }
                )
            else:
                items.append({"path": rel, "type": "file"})
        return items

    def read_text(self, project_id: str, relpath: str) -> str:
        path = self.resolve_context(project_id, relpath)
        if not path.exists() or not path.is_file():
            raise ApiError("file not found", 404)
        return path.read_text(encoding="utf-8")

    def read_bytes(self, project_id: str, relpath: str) -> bytes:
        path = self.resolve_context(project_id, relpath)
        if not path.exists() or not path.is_file():
            raise ApiError("file not found", 404)
        return path.read_bytes()

    def is_text_file(self, relpath: str) -> bool:
        suffix = Path(relpath).suffix.lower()
        return suffix in TEXT_SUFFIXES or suffix == ""

    def write_text(
        self,
        project_id: str,
        relpath: str,
        content: str,
        *,
        role: str = "user",
        worker_id: str | None = None,
    ) -> str:
        if role == "worker":
            self._assert_worker_path(relpath, worker_id)
        path = self.resolve_context(project_id, relpath)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return self.rel_from_context(project_id, path)

    def write_bytes(self, project_id: str, relpath: str, data: bytes) -> str:
        path = self.resolve_context(project_id, relpath)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return self.rel_from_context(project_id, path)

    def write_sandbox_text(self, project_id: str, relpath: str, content: str) -> str:
        path = self.resolve_sandbox(project_id, relpath)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return Path(relpath).as_posix()

    def _assert_worker_path(self, relpath: str, worker_id: str | None) -> None:
        if not worker_id:
            raise ApiError("worker writes require worker_id", 400)
        normalized = str(relpath).replace("\\", "/").lstrip("/")
        prefix = f"internal/{worker_id}-"
        if not (normalized.startswith(prefix) and normalized.endswith(".md") and "/" not in normalized[len("internal/") :]):
            raise ApiError("worker can only write internal/<worker-id>-*.md", 400)

    def notes_path(self, project_id: str) -> Path:
        return self.context_root(project_id) / "notes.md"

    def read_notes(self, project_id: str) -> str:
        path = self.notes_path(project_id)
        if not path.exists():
            return ""
        return path.read_text(encoding="utf-8")

    def read_preferences(self, project_id: str) -> str:
        path = self.context_root(project_id) / "preferences.md"
        if not path.exists():
            return ""
        return path.read_text(encoding="utf-8")
