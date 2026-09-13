from __future__ import annotations

import ast
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "backend"


def test_create_task_only_in_worker_manager_start():
    hits: list[str] = []
    for path in BACKEND.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = None
            if isinstance(func, ast.Attribute) and func.attr == "create_task":
                name = func.attr
            elif isinstance(func, ast.Name) and func.id == "create_task":
                name = func.id
            if name != "create_task":
                continue
            hits.append(f"{path.name}:{node.lineno}")
            parent_fn = _enclosing_function(tree, node)
            assert path.name == "worker_manager.py", hits
            assert parent_fn == "start", f"create_task in {path.name}:{parent_fn}"
    assert hits, "expected asyncio.create_task in WorkerManager.start"


def test_no_openai_import():
    for path in BACKEND.rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        assert "import openai" not in source
        assert "from openai" not in source


def test_coordinator_never_starts_runners():
    source = (BACKEND / "coordinator.py").read_text(encoding="utf-8")
    assert "create_task" not in source
    assert "workers.start" not in source
    assert "WorkerManager.start" not in source


def _enclosing_function(tree: ast.AST, target: ast.AST) -> str | None:
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and _contains(node, target):
            return node.name
    return None


def _contains(node: ast.AST, target: ast.AST) -> bool:
    return any(child is target for child in ast.walk(node))
