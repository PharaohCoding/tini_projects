from __future__ import annotations

import pytest

from backend.db import Database
from backend.events import EventHub
from backend.models import QueueItem
from backend.scheduler import Scheduler, select_next
from backend.settings import Settings


def _item(wid: str, kind: str, seq: int, priority: str = "normal") -> QueueItem:
    return QueueItem(id=wid, kind=kind, priority=priority, enqueue_seq=seq)


def test_select_next_fifo_same_priority():
    queued = [_item("a", "research", 1), _item("b", "implement", 2)]
    picked, reason = select_next([], queued)
    assert picked.id == "a"
    assert reason == "fifo"


def test_select_next_high_priority_jumps_fifo():
    queued = [
        _item("a", "research", 1, "normal"),
        _item("b", "test", 2, "high"),
        _item("c", "generic", 3, "normal"),
    ]
    picked, reason = select_next([], queued)
    assert picked.id == "b"
    assert picked.kind == "test"
    assert reason == "priority"


def test_select_next_high_priority_still_fifo_within_band():
    queued = [
        _item("a", "research", 1, "high"),
        _item("b", "test", 2, "high"),
    ]
    picked, reason = select_next([], queued)
    assert picked.id == "a"
    assert reason == "priority"


def test_select_next_kind_fairness_skips_same_kind_head():
    running = [_item("r1", "research", 1)]
    queued = [_item("r2", "research", 2), _item("t1", "test", 3)]
    picked, reason = select_next(running, queued)
    assert picked.id == "t1"
    assert picked.kind == "test"
    assert reason == "fair-kind: 避免 research 占满双槽"


def test_select_next_kind_fairness_falls_back_when_all_same_kind():
    running = [_item("r1", "research", 1)]
    queued = [_item("r2", "research", 2), _item("r3", "research", 3)]
    picked, reason = select_next(running, queued)
    assert picked.id == "r2"
    assert reason == "fifo"


def test_select_next_no_fairness_when_running_kinds_already_mixed():
    running = [_item("r1", "research", 1), _item("i1", "implement", 2)]
    queued = [_item("r2", "research", 3), _item("t1", "test", 4)]
    picked, reason = select_next(running, queued)
    assert picked.id == "r2"
    assert reason == "fifo"


def test_select_next_empty_queue():
    assert select_next([], []) is None


async def _harness(tmp_path):
    settings = Settings(data_dir=tmp_path / "data", max_running=2)
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    db = Database(settings.db_path)
    await db.init()
    events = EventHub()
    started: list[str] = []

    def starter(worker):
        started.append(worker.id)

    scheduler = Scheduler(db=db, events=events, settings=settings)
    scheduler.set_starter(starter)
    project = await db.create_project("sched")
    return db, scheduler, started, project["id"]


@pytest.mark.asyncio
async def test_kick_enqueue_four_cap_two_fifo(tmp_path):
    db, scheduler, started, pid = await _harness(tmp_path)
    try:
        for kind in ("research", "implement", "test", "generic"):
            await db.insert_worker(pid, title=kind, assignment=kind, kind=kind)
        started_ids = await scheduler.kick(pid)
        assert len(started_ids) == 2
        assert started == started_ids
        running = await db.workers_by_status(pid, "running")
        queued = await db.workers_by_status(pid, "queued")
        assert [w.kind for w in running] == ["research", "implement"]
        assert [w.kind for w in queued] == ["test", "generic"]
        assert all(w.schedule_reason == "fifo" for w in running)
        snap = await scheduler.snapshot(pid)
        assert snap["policy"] == "fair-kind"
        assert snap["max_running"] == 2
        assert [q["kind"] for q in snap["queued"]] == ["test", "generic"]
        assert [q["position"] for q in snap["queued"]] == [1, 2]
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_kick_after_done_starts_fifo_head(tmp_path):
    db, scheduler, started, pid = await _harness(tmp_path)
    try:
        for kind in ("research", "implement", "test", "generic"):
            await db.insert_worker(pid, title=kind, assignment=kind, kind=kind)
        await scheduler.kick(pid)
        running = await db.workers_by_status(pid, "running")
        done = running[0]
        assert done.kind == "research"
        await db.update_worker(done.id, status="done", finished_at="2026-01-01T00:00:00Z")
        started.clear()
        started_ids = await scheduler.kick(pid)
        assert len(started_ids) == 1
        nxt = await db.get_worker(started_ids[0])
        assert nxt is not None
        assert nxt.kind == "test"
        assert nxt.schedule_reason == "fifo"
        queued = await db.workers_by_status(pid, "queued")
        assert [w.kind for w in queued] == ["generic"]
        running = await db.workers_by_status(pid, "running")
        assert sorted(w.kind for w in running) == ["implement", "test"]
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_kick_kind_fairness_after_slot_frees(tmp_path):
    db, scheduler, started, pid = await _harness(tmp_path)
    try:
        for title in ("r1", "r2", "r3"):
            await db.insert_worker(pid, title=title, assignment=title, kind="research")
        await db.insert_worker(pid, title="t1", assignment="t1", kind="test")
        await scheduler.kick(pid)
        running = await db.workers_by_status(pid, "running")
        assert [w.kind for w in running] == ["research", "research"]
        queued = await db.workers_by_status(pid, "queued")
        assert [w.kind for w in queued] == ["research", "test"]
        await db.update_worker(running[0].id, status="done", finished_at="2026-01-01T00:00:00Z")
        started.clear()
        started_ids = await scheduler.kick(pid)
        assert len(started_ids) == 1
        nxt = await db.get_worker(started_ids[0])
        assert nxt is not None
        assert nxt.kind == "test"
        assert nxt.schedule_reason == "fair-kind: 避免 research 占满双槽"
        leftover = await db.workers_by_status(pid, "queued")
        assert [w.kind for w in leftover] == ["research"]
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_kick_high_priority_before_older_normal(tmp_path):
    db, scheduler, started, pid = await _harness(tmp_path)
    try:
        for kind in ("research", "implement", "test", "generic"):
            await db.insert_worker(pid, title=kind, assignment=kind, kind=kind)
        await scheduler.kick(pid)
        await db.insert_worker(
            pid, title="hot", assignment="hot", kind="generic", priority="high"
        )
        running = await db.workers_by_status(pid, "running")
        await db.update_worker(running[0].id, status="done", finished_at="2026-01-01T00:00:00Z")
        started.clear()
        started_ids = await scheduler.kick(pid)
        nxt = await db.get_worker(started_ids[0])
        assert nxt is not None
        assert nxt.title == "hot"
        assert nxt.schedule_reason == "priority"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_kick_does_not_exceed_cap(tmp_path):
    db, scheduler, started, pid = await _harness(tmp_path)
    try:
        for i in range(6):
            await db.insert_worker(pid, title=str(i), assignment=str(i), kind="generic")
        await scheduler.kick(pid)
        await scheduler.kick(pid)
        running = await db.workers_by_status(pid, "running")
        queued = await db.workers_by_status(pid, "queued")
        assert len(running) == 2
        assert len(queued) == 4
        assert len(started) == 2
    finally:
        await db.close()
