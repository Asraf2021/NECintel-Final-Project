"""Predictive Shelter Occupancy / Time-to-Capacity — doluluq proqnozu.

Məntəqənin 90% kritik həddinə və 100% tam tutuma nə vaxt çatacağını son zaman pəncərəsindəki
xalis insan axınına əsasən hesablayır (komandanın beynəlxalq araşdırma sənədi, bölmə 2.5 və 2.10):

    r    = (pəncərədə xalis artım) / (pəncərənin dəqiqə sayı)     — nəfər/dəqiqə
    T90  = (0.9 · C − N) / r
    T100 = (C − N) / r

r ≤ 0 olduqda vaxt göstərilmir ("artım müşahidə olunmur"). Az nümunə və ya qısa müşahidə müddəti
etibarlılığı aşağı salır; proqnoz interval və etibarlılıq səviyyəsi ilə qaytarılır.
Bu, sabit axın fərziyyəsinə əsaslanan sadə modeldir (MVP); gələcəkdə statistik / ML modeli ilə əvəz oluna bilər.
"""
from __future__ import annotations

import time

from ..config import settings
from ..context import AppContext

WINDOW_S = 600            # 10 dəqiqəlik pəncərə
MIN_SPAN_MEDIUM_S = 60    # orta etibarlılıq üçün minimum müşahidə müddəti
MIN_SPAN_HIGH_S = 180
SPREAD = {"low": 0.5, "medium": 0.25, "high": 0.15}  # intervalın nisbi eni


def log_occupancy(ctx: AppContext, shelter_id: str, occupancy: int, ts: float | None = None) -> None:
    ctx.db.execute("INSERT INTO shelter_occupancy_log (shelter_id, ts, occupancy) VALUES (?,?,?)",
                   (shelter_id, ts or time.time(), occupancy))


def compute(capacity: int, occupancy: int, points: list[tuple[float, int]], now: float | None = None) -> dict:
    """Saf funksiya: (ts, doluluq) nöqtələrindən proqnoz. Testlər üçün DB-dən asılı deyil."""
    now = now or time.time()
    pts = sorted(p for p in points if p[0] >= now - WINDOW_S)
    out = {"rate_per_min": None, "t90_min": None, "t100_min": None, "t90_range": None, "t100_range": None,
           "trend": "unknown", "confidence": "low", "samples": len(pts), "window_min": WINDOW_S // 60}
    if occupancy >= capacity:
        out.update(trend="full", confidence="high")
        return out
    if len(pts) < 2:
        return out
    span = pts[-1][0] - pts[0][0]
    if span <= 0:
        return out
    rate = (pts[-1][1] - pts[0][1]) / (span / 60)
    out["rate_per_min"] = round(rate, 2)
    if span >= MIN_SPAN_HIGH_S and len(pts) >= 6:
        out["confidence"] = "high"
    elif span >= MIN_SPAN_MEDIUM_S and len(pts) >= 3:
        out["confidence"] = "medium"
    if rate <= 0.01:
        out["trend"] = "falling" if rate < -0.01 else "stable"
        return out
    out["trend"] = "rising"
    spread = SPREAD[out["confidence"]]
    t90_target = settings.shelter_reroute_pct / 100 * capacity
    if occupancy < t90_target:
        t90 = (t90_target - occupancy) / rate
        out["t90_min"] = round(t90, 1)
        out["t90_range"] = [round(t90 * (1 - spread)), round(t90 * (1 + spread))]
    else:
        out["t90_min"] = 0.0
    t100 = (capacity - occupancy) / rate
    out["t100_min"] = round(t100, 1)
    out["t100_range"] = [round(t100 * (1 - spread)), round(t100 * (1 + spread))]
    return out


def attach(ctx: AppContext, shelters: list[dict]) -> list[dict]:
    """Məntəqə siyahısına proqnoz əlavə edir (bir sorğu ilə)."""
    if not shelters:
        return shelters
    now = time.time()
    ids = [s["id"] for s in shelters]
    marks = ",".join("?" * len(ids))
    rows = ctx.db.query(
        f"SELECT shelter_id, ts, occupancy FROM shelter_occupancy_log WHERE ts>=? AND shelter_id IN ({marks}) ORDER BY ts",
        (now - WINDOW_S, *ids))
    by_id: dict[str, list] = {}
    for r in rows:
        by_id.setdefault(r["shelter_id"], []).append((r["ts"], r["occupancy"]))
    for s in shelters:
        s["forecast"] = compute(s["capacity"], s["occupancy"], by_id.get(s["id"], []), now)
    return shelters


def reliable(fc: dict | None) -> bool:
    return bool(fc) and fc.get("confidence") in ("medium", "high") and fc.get("trend") == "rising"
