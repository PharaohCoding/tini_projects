from __future__ import annotations

import pytest

from backend.context_store import ContextStore
from backend.errors import ApiError
from backend.settings import Settings
from tests.conftest import create_project


def test_path_sandbox_rejects_dotdot(client):
    project = create_project(client)
    pid = project["id"]
    for bad in ("../secret.md", "../../etc/passwd", "/etc/passwd", "docs/../../notes.md"):
        response = client.get(f"/api/projects/{pid}/context/file", params={"path": bad})
        assert response.status_code == 400, bad
        assert response.json()["error"] == "path escapes sandbox"
        put = client.put(
            f"/api/projects/{pid}/context/file",
            params={"path": bad},
            json={"content": "nope"},
        )
        assert put.status_code == 400
        assert put.json()["error"] == "path escapes sandbox"


def test_path_sandbox_rejects_empty(client):
    project = create_project(client)
    pid = project["id"]
    response = client.get(f"/api/projects/{pid}/context/file", params={"path": ""})
    assert response.status_code in {400, 422}
    assert "error" in response.json()


def test_worker_cannot_write_docs(tmp_path):
    settings = Settings(data_dir=tmp_path / "data")
    store = ContextStore(settings)
    store.init_project("p1")
    with pytest.raises(ApiError, match="worker can only write"):
        store.write_text("p1", "docs/plan.md", "nope", role="worker", worker_id="abc")


def test_worker_can_write_own_report(tmp_path):
    settings = Settings(data_dir=tmp_path / "data")
    store = ContextStore(settings)
    store.init_project("p1")
    rel = store.write_text(
        "p1",
        "internal/wid-report.md",
        "ok",
        role="worker",
        worker_id="wid",
    )
    assert rel == "internal/wid-report.md"
    assert store.read_text("p1", rel) == "ok"


def test_context_skeleton_on_create(client):
    project = create_project(client)
    pid = project["id"]
    tree = client.get(f"/api/projects/{pid}/context").json()["tree"]
    paths = set()

    def walk(nodes):
        for node in nodes:
            paths.add(node["path"])
            if node.get("children"):
                walk(node["children"])

    walk(tree)
    for required in ("notes.md", "preferences.md", "docs", "plans", "media", "internal"):
        assert required in paths
