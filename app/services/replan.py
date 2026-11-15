"""Yenidən marşrutlaşdırma planlayıcısı (texniki borc E2).

Problem: `reevaluate_plans` əvvəllər HTTP sorğusunun içində ardıcıl işləyirdi — "yolu bağla" və ya
"məntəqə doldu" sorğusu yüzlərlə vətəndaşın planı yenidən hesablanana qədər gözləyirdi.

Həll: sorğu yalnız "yenidən hesabla" siqnalı verir və dərhal cavab qaytarır. Fon işçisi siqnalları
qısa debounce ilə birləşdirir (coalescing): bir neçə dəyişiklik ardıcıl gəlsə, hesablama bir dəfə aparılır;
hesablama zamanı yeni siqnal gəlsə, bitdikdən sonra bir dəfə də təkrarlanır. Nəticə vətəndaşlara
SSE ilə (`reroute`), dispetçerə `reroute_batch` hadisəsi ilə çatdırılır.

`inline=True` rejimi (testlər və fon tapşırıqları söndürülmüş tətbiq) əvvəlki sinxron davranışı saxlayır.
"""
from __future__ import annotations

import asyncio
import logging
import time

log = logging.getLogger("uce.replan")


class ReplanScheduler:
    def __init__(self, ctx, inline: bool, debounce_s: float = 0.3):
        self.ctx = ctx
        self.inline = inline
        self.debounce_s = debounce_s
        self._event = asyncio.Event()
        self._task: asyncio.Task | None = None
        self.requests = 0
        self.runs = 0
        self.last_rerouted = 0
        self.last_duration_ms = 0.0
        self.last_reason = ""
        self.running = False

    def start(self) -> None:
        if not self.inline and self._task is None:
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    async def request(self, reason: str = "") -> int | None:
        """Yenidən hesablama istəyi. inline rejimdə nəticə sayını, fon rejimində None qaytarır."""
        self.requests += 1
        self.last_reason = reason
        if self.inline:
            return await self._run()
        self._event.set()
        return None

    async def _loop(self) -> None:
        while True:
            await self._event.wait()
            await asyncio.sleep(self.debounce_s)  # ardıcıl siqnalları bir hesablamaya birləşdir
            self._event.clear()
            try:
                await self._run()
            except asyncio.CancelledError:
                raise
            except Exception:  # fon işçisi heç vaxt dayanmamalıdır
                log.exception("replan failed")

    async def _run(self) -> int:
        from . import evacuation
        self.running = True
        started = time.perf_counter()
        try:
            count = await evacuation.reevaluate_plans(self.ctx)
        finally:
            self.running = False
        self.runs += 1
        self.last_rerouted = count
        self.last_duration_ms = round((time.perf_counter() - started) * 1000, 1)
        return count

    def status(self) -> dict:
        return {"mode": "inline" if self.inline else "background", "requests": self.requests, "runs": self.runs,
                "running": self.running, "last_rerouted": self.last_rerouted,
                "last_duration_ms": self.last_duration_ms, "last_reason": self.last_reason}
