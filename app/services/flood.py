"""SOS daşqını (flood / spam hücumu) detektoru — Challenge Drop 3.

Müdafiə qatları (SOS endpoint-i):
  1. Rate limit — IP üzrə və vətəndaş ID-si üzrə (app/routers/citizen.py) -> 429;
  2. Dəqiq dublikat — eyni vətəndaş + eyni növ, 30 s (sos.create_sos);
  3. Daşqın rejimi (bu modul) — anonim SOS axını həddi aşanda (botnet: çox IP, təsadüfi ID-lər):
       * yaxın dublikatlar (eyni növ, ≤ 30 m, ≤ 60 s) bir kartda birləşdirilir ("+N oxşar");
       * yeni anonim SOS "karantin"ə (status = 'suspect') düşür: dispetçer panelində tək-tək kart kimi
         göstərilmir, Qırmızı Həyəcan alqoritminə daxil edilmir, briqada göndərilmir;
       * myGov ilə təsdiqlənmiş / qeydiyyatlı vətəndaşların SOS-u karantinə düşmür;
       * dispetçer karantini bir kliklə "real kimi qəbul et" və ya "spam kimi sil" edə bilər.
  Daşqın bitəndən (axın həddin yarısından aşağı, 30 s) sonra rejim avtomatik sönür (histerezis).
"""
from __future__ import annotations

import time
from collections import deque

from ..config import settings


class FloodGuard:
    def __init__(self) -> None:
        self._events: deque[float] = deque()
        self.active = False
        self.since: float | None = None
        self._calm_since: float | None = None
        self.quarantined = 0
        self.merged = 0
        self.peak_rate = 0
        self.activations = 0
        self._last_publish = 0.0

    def _trim(self, now: float) -> None:
        window = settings.sos_flood_window_s
        while self._events and self._events[0] <= now - window:
            self._events.popleft()

    def record(self, now: float | None = None) -> None:
        """Anonim SOS cəhdini qeyd edir və rejimi yeniləyir."""
        now = now or time.time()
        self._events.append(now)
        self.update(now)

    def update(self, now: float | None = None) -> bool:
        now = now or time.time()
        self._trim(now)
        rate = len(self._events)
        self.peak_rate = max(self.peak_rate, rate)
        threshold = settings.sos_flood_threshold
        if rate >= threshold:
            if not self.active:
                self.active, self.since = True, now
                self.activations += 1
            self._calm_since = None
        elif self.active and rate < threshold / 2:
            if self._calm_since is None:
                self._calm_since = now
            elif now - self._calm_since >= settings.sos_flood_cooldown_s:
                self.active, self.since, self._calm_since = False, None, None
        return self.active

    def should_publish(self, min_interval_s: float = 1.0) -> bool:
        """Daşqın zamanı hadisələr seyrəldilir (dispetçer panelini SSE ilə boğmamaq üçün)."""
        now = time.time()
        if now - self._last_publish >= min_interval_s:
            self._last_publish = now
            return True
        return False

    def status(self) -> dict:
        self.update()
        return {"active": self.active, "since": self.since, "rate": len(self._events),
                "window_s": settings.sos_flood_window_s, "threshold": settings.sos_flood_threshold,
                "quarantined_total": self.quarantined, "merged_total": self.merged,
                "peak_rate": self.peak_rate, "activations": self.activations}

    def reset(self) -> None:
        self.__init__()
