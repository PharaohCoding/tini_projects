from __future__ import annotations

import asyncio

import pytest

from backend.events import EventHub


@pytest.mark.asyncio
async def test_event_hub_delivers():
    hub = EventHub()
    queue = hub.subscribe("p")
    await hub.publish("p", "worker.queued", {"id": "w1"})
    event = await asyncio.wait_for(queue.get(), timeout=1)
    assert event["type"] == "worker.queued"
    assert event["data"]["id"] == "w1"
    hub.unsubscribe("p", queue)
