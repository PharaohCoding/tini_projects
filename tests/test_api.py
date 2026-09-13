from __future__ import annotations

import time
from pathlib import Path

from fastapi.testclient import TestClient

from backend.main import create_app
from backend.settings import Settings
from tests.conftest import create_project


def _wait_until(predicate, timeout: float = 3.0):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = predicate()
        if last:
            return last
        time.sleep(0.05)
    raise AssertionError(f"timeout waiting for condition; last={last!r}")


def test_demo_schedule_enqueues_four_two_running_two_queued(client):
    project = create_project(client)
    pid = project["id"]
    response = client.post(f"/api/projects/{pid}/messages", json={"content": "/demo-schedule"})
    assert response.status_code == 200, response.text
    body = response.json()
    snap = body["scheduler"]
    assert snap["policy"] == "fair-kind"
    assert snap["max_running"] == 2
    assert [w["kind"] for w in snap["running"]] == ["research", "implement"]
    assert [w["kind"] for w in snap["queued"]] == ["test", "generic"]
    assert [w["position"] for w in snap["queued"]] == [1, 2]
    assert all(w["schedule_reason"] == "fifo" for w in snap["running"])
    workers = client.get(f"/api/projects/{pid}/workers").json()["workers"]
    assert len(workers) == 4
    assert [w["status"] for w in workers] == ["running", "running", "queued", "queued"]
    assert [w["enqueue_seq"] for w in workers] == [1, 2, 3, 4]
    text = body["coordinator"]["content"]
    assert "2/2" in text
    fetched = client.get(f"/api/projects/{pid}/scheduler").json()
    assert fetched == snap


def test_demo_fairness_two_research_running(client):
    project = create_project(client)
    pid = project["id"]
    response = client.post(f"/api/projects/{pid}/messages", json={"content": "/demo-fairness"})
    assert response.status_code == 200, response.text
    snap = response.json()["scheduler"]
    assert [w["kind"] for w in snap["running"]] == ["research", "research"]
    assert [w["kind"] for w in snap["queued"]] == ["research", "test"]


def test_project_crud_and_messages_persist(client):
    created = create_project(client, name="alpha", goal="keep chatting")
    pid = created["id"]
    client.post(f"/api/projects/{pid}/messages", json={"content": "hello coordinator"})
    client.post(f"/api/projects/{pid}/messages", json={"content": "second turn"})
    listed = client.get("/api/projects").json()["projects"]
    assert listed[0]["id"] == pid
    assert listed[0]["unfinished_workers"] == 0
    detail = client.get(f"/api/projects/{pid}").json()
    assert detail["name"] == "alpha"
    patched = client.patch(f"/api/projects/{pid}", json={"status": "paused"}).json()
    assert patched["status"] == "paused"
    messages = client.get(f"/api/projects/{pid}/messages").json()["messages"]
    roles = [m["role"] for m in messages]
    assert roles.count("user") == 2
    assert "coordinator" in roles
    after = messages[0]["id"]
    rest = client.get(f"/api/projects/{pid}/messages", params={"after": after}).json()["messages"]
    assert rest[0]["id"] == messages[1]["id"]


def test_missing_project_error_shape(client):
    response = client.get("/api/projects/does-not-exist")
    assert response.status_code == 404
    assert response.json() == {"error": "project not found"}


def test_upload_and_attached_chats(client):
    project = create_project(client)
    pid = project["id"]
    upload = client.post(
        f"/api/projects/{pid}/attachments",
        files={"file": ("hello.txt", b"hello-bytes", "text/plain")},
    )
    assert upload.status_code == 200, upload.text
    attachment = upload.json()
    assert attachment["source"] == "upload"
    assert attachment["stored_path"].startswith("media/")
    tree = client.get(f"/api/projects/{pid}/context").json()["tree"]
    paths = _flatten_paths(tree)
    assert attachment["stored_path"] in paths
    fetched = client.get(
        f"/api/projects/{pid}/context/file",
        params={"path": attachment["stored_path"]},
    )
    assert fetched.status_code == 200
    assert fetched.json()["content"] == "hello-bytes"

    imported = client.post(
        f"/api/projects/{pid}/attached-chats",
        json={"title": "old chat", "content": "user: hi\nassistant: yo"},
    )
    assert imported.status_code == 200, imported.text
    chat = imported.json()
    assert chat["source"] == "chat_import"
    assert chat["stored_path"].startswith("internal/attached-chats/")
    chat_file = client.get(
        f"/api/projects/{pid}/context/file",
        params={"path": chat["stored_path"]},
    )
    assert "user: hi" in chat_file.json()["content"]
    messages = client.post(
        f"/api/projects/{pid}/messages",
        json={"content": "please look", "attachment_ids": [attachment["id"]]},
    )
    assert messages.status_code == 200


def test_put_and_get_notes(client):
    project = create_project(client)
    pid = project["id"]
    put = client.put(
        f"/api/projects/{pid}/context/file",
        params={"path": "notes.md"},
        json={"content": "# notes\n\nedited by user\n"},
    )
    assert put.status_code == 200
    got = client.get(f"/api/projects/{pid}/context/file", params={"path": "notes.md"})
    assert "edited by user" in got.json()["content"]


def test_cancel_running_frees_slot(client):
    project = create_project(client)
    pid = project["id"]
    client.post(f"/api/projects/{pid}/messages", json={"content": "/demo-schedule"})
    snap = client.get(f"/api/projects/{pid}/scheduler").json()
    first = snap["running"][0]["id"]
    canceled = client.post(f"/api/projects/{pid}/workers/{first}/cancel")
    assert canceled.status_code == 200, canceled.text
    body = canceled.json()
    assert body["worker"]["status"] == "canceled"
    snap2 = body["scheduler"]
    assert len(snap2["running"]) == 2
    assert first not in [w["id"] for w in snap2["running"]]
    assert "test" in [w["kind"] for w in snap2["running"]]
    queued_kinds = [w["kind"] for w in snap2["queued"]]
    assert queued_kinds == ["generic"]


def test_cancel_queued_never_starts(client):
    project = create_project(client)
    pid = project["id"]
    client.post(f"/api/projects/{pid}/messages", json={"content": "/demo-schedule"})
    snap = client.get(f"/api/projects/{pid}/scheduler").json()
    queued_id = snap["queued"][0]["id"]
    response = client.post(f"/api/projects/{pid}/workers/{queued_id}/cancel")
    assert response.json()["worker"]["status"] == "canceled"
    snap2 = client.get(f"/api/projects/{pid}/scheduler").json()
    assert queued_id not in [w["id"] for w in snap2["queued"]]
    assert queued_id not in [w["id"] for w in snap2["running"]]
    assert len(snap2["running"]) == 2


def test_fail_path_and_kick(tmp_path):
    settings = Settings(
        data_dir=tmp_path / "data",
        duration_research=0.05,
        duration_implement=30.0,
        duration_test=30.0,
        duration_generic=30.0,
        auto_summary=False,
    )
    with TestClient(create_app(settings)) as client:
        project = create_project(client)
        pid = project["id"]
        client.post(f"/api/projects/{pid}/messages", json={"content": "请研究 [fail]"})
        worker = _wait_until(
            lambda: next(
                (
                    w
                    for w in client.get(f"/api/projects/{pid}/workers").json()["workers"]
                    if w["status"] == "failed"
                ),
                None,
            )
        )
        assert worker["error"]
        snap = client.get(f"/api/projects/{pid}/scheduler").json()
        assert snap["running"] == []
        assert snap["queued"] == []


def test_kick_after_done_via_api(tmp_path):
    settings = Settings(
        data_dir=tmp_path / "data",
        duration_research=0.05,
        duration_implement=30.0,
        duration_test=30.0,
        duration_generic=30.0,
        auto_summary=False,
    )
    with TestClient(create_app(settings)) as client:
        project = create_project(client)
        pid = project["id"]
        client.post(f"/api/projects/{pid}/messages", json={"content": "/demo-schedule"})

        def test_running():
            snap = client.get(f"/api/projects/{pid}/scheduler").json()
            kinds = [w["kind"] for w in snap["running"]]
            if "test" in kinds and "implement" in kinds:
                return snap
            return None

        snap = _wait_until(test_running, timeout=3.0)
        assert [w["kind"] for w in snap["queued"]] == ["generic"]
        workers = {w["kind"]: w for w in client.get(f"/api/projects/{pid}/workers").json()["workers"]}
        assert workers["research"]["status"] == "done"
        assert workers["research"]["report_path"] == f"internal/{workers['research']['id']}-report.md"
        report = client.get(
            f"/api/projects/{pid}/context/file",
            params={"path": workers["research"]["report_path"]},
        )
        assert report.status_code == 200
        assert workers["research"]["id"] in report.json()["content"]


def test_auto_summary_updates_notes(tmp_path):
    settings = Settings(
        data_dir=tmp_path / "data",
        duration_research=0.05,
        duration_implement=0.05,
        duration_test=0.05,
        duration_generic=0.05,
        auto_summary=True,
    )
    with TestClient(create_app(settings)) as client:
        project = create_project(client)
        pid = project["id"]
        client.post(f"/api/projects/{pid}/messages", json={"content": "/demo-schedule"})

        def notes_summarized():
            notes = client.get(
                f"/api/projects/{pid}/context/file", params={"path": "notes.md"}
            ).json()["content"]
            if "自动汇总" in notes:
                return notes
            return None

        notes = _wait_until(notes_summarized, timeout=4.0)
        assert "research" in notes
        messages = client.get(f"/api/projects/{pid}/messages").json()["messages"]
        assert any(m["role"] == "coordinator" and "汇总" in m["content"] for m in messages)
        workers = client.get(f"/api/projects/{pid}/workers").json()["workers"]
        assert all(w["status"] == "done" for w in workers)
        for worker in workers:
            path = Path(tmp_path / "data" / "projects" / pid / "context" / worker["report_path"])
            assert path.is_file()
        sandbox = tmp_path / "data" / "projects" / pid / "sandbox" / "output.txt"
        assert sandbox.is_file()


def test_first_message_demo_hint_spawns_four(client):
    project = create_project(client)
    pid = project["id"]
    response = client.post(f"/api/projects/{pid}/messages", json={"content": "请演示并行调度"})
    snap = response.json()["scheduler"]
    assert len(snap["running"]) == 2
    assert len(snap["queued"]) == 2


def _flatten_paths(tree) -> list[str]:
    out = []
    for node in tree:
        out.append(node["path"])
        if node.get("type") == "dir":
            out.extend(_flatten_paths(node.get("children") or []))
    return out
