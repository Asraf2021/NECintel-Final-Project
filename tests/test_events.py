"""Hadisə şini (EventBus) — kanallar, fərdi çatdırılma, dolu növbə."""
from __future__ import annotations

import asyncio
import json

from app.core.events import QUEUE_SIZE, EventBus, Subscriber


def _subscribe(bus: EventBus, channel: str, citizen_id: str | None = None) -> Subscriber:
    sub = Subscriber(channel=channel, citizen_id=citizen_id)
    bus._subs.add(sub)
    return sub


def _drain(sub: Subscriber) -> list[dict]:
    items = []
    while not sub.queue.empty():
        items.append(sub.queue.get_nowait())
    return items


def test_public_event_reaches_public_and_ops():
    async def scenario():
        bus = EventBus()
        public, ops = _subscribe(bus, "public"), _subscribe(bus, "ops")
        citizen = _subscribe(bus, "public", "citizen-0001")
        bus.publish("shelter_update", {"id": "sh01"}, channel="public")
        for sub in (public, ops, citizen):
            got = _drain(sub)
            assert len(got) == 1
            assert got[0]["event"] == "shelter_update" and got[0]["data"] == {"id": "sh01"} and got[0]["ts"] > 0
        assert bus.published == 1 and bus.dropped == 0

    asyncio.run(scenario())


def test_ops_event_does_not_reach_public():
    async def scenario():
        bus = EventBus()
        public, ops = _subscribe(bus, "public"), _subscribe(bus, "ops")
        citizen = _subscribe(bus, "public", "citizen-0001")
        bus.publish("sos_new", {"sos": {"id": 1}}, channel="ops")
        assert _drain(public) == [] and _drain(citizen) == []
        assert [e["event"] for e in _drain(ops)] == ["sos_new"]

    asyncio.run(scenario())


def test_citizen_targeted_event_reaches_only_that_citizen():
    async def scenario():
        bus = EventBus()
        public, ops = _subscribe(bus, "public"), _subscribe(bus, "ops")
        me = _subscribe(bus, "public", "citizen-0001")
        me_second_tab = _subscribe(bus, "public", "citizen-0001")
        other = _subscribe(bus, "public", "citizen-0002")
        bus.publish("reroute", {"plan": {}}, citizen_id="citizen-0001")
        assert [e["event"] for e in _drain(me)] == ["reroute"]
        assert [e["event"] for e in _drain(me_second_tab)] == ["reroute"]
        assert _drain(other) == [] and _drain(public) == [] and _drain(ops) == []

    asyncio.run(scenario())


def test_full_queue_drops_instead_of_blocking():
    async def scenario():
        bus = EventBus()
        slow, healthy = _subscribe(bus, "public"), _subscribe(bus, "public")
        for i in range(QUEUE_SIZE):
            bus.publish("tick", {"i": i})
        _drain(healthy)                                   # sağlam müştəri oxuyur, yavaş müştəri oxumur
        # publish sinxrondur: dolu növbə gözləmə yaratmamalıdır
        await asyncio.wait_for(asyncio.to_thread(lambda: [bus.publish("tick", {"i": -1}) for _ in range(5)]), 1.0)
        assert bus.dropped == 5
        assert bus.published == QUEUE_SIZE + 5
        assert slow.queue.qsize() == QUEUE_SIZE
        assert [e["data"]["i"] for e in _drain(slow)][:3] == [0, 1, 2]   # köhnə hadisələr saxlanılır
        assert len(_drain(healthy)) == 5                  # sağlam müştəri heç nə itirmir

    asyncio.run(scenario())


def test_publish_without_subscribers_and_default_data():
    bus = EventBus()
    bus.publish("noop")
    assert bus.published == 1 and bus.dropped == 0 and bus.subscriber_count == 0
    sub = _subscribe(bus, "public")
    bus.publish("empty")
    assert _drain(sub)[0]["data"] == {}


def test_stream_generator_lifecycle_and_sse_format():
    async def scenario():
        bus = EventBus()
        gen = bus.stream("public", "citizen-0001")
        assert await gen.__anext__() == "retry: 3000\n\n"
        hello = await gen.__anext__()
        assert hello.startswith("event: hello\ndata: ") and hello.endswith("\n\n")
        assert bus.subscriber_count == 1

        bus.publish("shelter_update", {"name": "Xətai sığınacağı"})
        bus.publish("sos_new", {}, channel="ops")                 # public axına düşməməlidir
        bus.publish("reroute", {"plan": 1}, citizen_id="citizen-0001")
        first = await asyncio.wait_for(gen.__anext__(), 1.0)
        second = await asyncio.wait_for(gen.__anext__(), 1.0)
        assert first.startswith("event: shelter_update\n")
        payload = json.loads(first.split("data: ", 1)[1])
        assert payload["data"] == {"name": "Xətai sığınacağı"}
        assert "Xətai" in first                                   # UTF-8 escape olunmur
        assert second.startswith("event: reroute\n")

        await gen.aclose()
        assert bus.subscriber_count == 0                          # bağlananda abunə silinir

    asyncio.run(scenario())
