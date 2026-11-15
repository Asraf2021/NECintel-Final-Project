"""Canlı hadisə yayımı (Server-Sent Events).

Kanallar:
  public — bütün vətəndaş müştərilərinə (sığınacaq dolulugu, yol dəyişikliyi);
  ops    — yalnız dispetçer panelinə (SOS, həyəcan, briqada);
  citizen_id verilmiş hadisə yalnız həmin vətəndaşa çatdırılır (fərdi yenidən marşrut).
"""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field

QUEUE_SIZE = 200
HEARTBEAT_S = 15


@dataclass(eq=False)
class Subscriber:
    channel: str
    citizen_id: str | None = None
    queue: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(maxsize=QUEUE_SIZE))


class EventBus:
    def __init__(self) -> None:
        self._subs: set[Subscriber] = set()
        self.published = 0
        self.dropped = 0

    @property
    def subscriber_count(self) -> int:
        return len(self._subs)

    def publish(self, event: str, data: dict | None = None, channel: str = "public",
                citizen_id: str | None = None) -> None:
        payload = {"event": event, "data": data or {}, "ts": time.time()}
        self.published += 1
        for sub in list(self._subs):
            if citizen_id is not None:
                deliver = sub.citizen_id == citizen_id
            elif channel == "ops":
                deliver = sub.channel == "ops"
            else:
                deliver = True
            if not deliver:
                continue
            try:
                sub.queue.put_nowait(payload)
            except asyncio.QueueFull:
                # Yavaş müştəri serveri bloklamamalıdır: hadisə atılır, müştəri polling ilə bərpa edir
                self.dropped += 1

    async def stream(self, channel: str, citizen_id: str | None = None):
        sub = Subscriber(channel=channel, citizen_id=citizen_id)
        self._subs.add(sub)
        try:
            yield "retry: 3000\n\n"
            yield _format({"event": "hello", "data": {"channel": channel}, "ts": time.time()})
            while True:
                try:
                    item = await asyncio.wait_for(sub.queue.get(), timeout=HEARTBEAT_S)
                    yield _format(item)
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
        finally:
            self._subs.discard(sub)


def _format(item: dict) -> str:
    return f"event: {item['event']}\ndata: {json.dumps(item, ensure_ascii=False)}\n\n"
