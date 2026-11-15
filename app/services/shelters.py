"""Toplanış məntəqələri: doluluq, status və ondan yaranan hadisələr."""
from __future__ import annotations

import time

from ..config import settings
from ..context import AppContext


def decorate(s: dict) -> dict:
    """Doluluq faizi və səviyyəsini əlavə edir."""
    # Hədlər yuvarlaqlaşdırılmamış dəqiq faizlə müqayisə olunur; yuvarlaqlaşdırma yalnız göstəriş üçündür
    raw = s["occupancy"] * 100 / s["capacity"] if s["capacity"] else 100.0
    if raw > settings.shelter_critical_pct:
        level = "critical"
    elif raw >= settings.shelter_reroute_pct:
        level = "full"
    elif raw >= 70:
        level = "high"
    elif raw >= 40:
        level = "medium"
    else:
        level = "low"
    return {**s, "accessible": bool(s.get("accessible", 1)), "occupancy_pct": round(raw, 1), "level": level,
            "over_critical": raw > settings.shelter_critical_pct,
            "accepting": s["status"] == "open" and raw < settings.shelter_reroute_pct}


def list_shelters(ctx: AppContext, include_reserve: bool = False) -> list[dict]:
    sql = "SELECT * FROM shelters" + ("" if include_reserve else " WHERE status='open'") + " ORDER BY id"
    return [decorate(s) for s in ctx.db.query(sql)]


def get_shelter(ctx: AppContext, shelter_id: str) -> dict | None:
    s = ctx.db.one("SELECT * FROM shelters WHERE id=?", (shelter_id,))
    return decorate(s) if s else None


def update_shelter(ctx: AppContext, shelter_id: str, occupancy: int | None = None,
                   status: str | None = None, delta: int | None = None) -> dict | None:
    """Doluluğu/statusu dəyişir və canlı hadisə yayır. Doluluq [0, capacity] aralığında saxlanılır."""
    with ctx.db.transaction():
        s = ctx.db.one("SELECT * FROM shelters WHERE id=?", (shelter_id,))
        if not s:
            return None
        new_occ = s["occupancy"]
        if occupancy is not None:
            new_occ = occupancy
        if delta is not None:
            new_occ = s["occupancy"] + delta
        new_occ = max(0, min(new_occ, s["capacity"]))
        new_status = status or s["status"]
        ctx.db.execute("UPDATE shelters SET occupancy=?, status=?, updated_at=? WHERE id=?",
                       (new_occ, new_status, time.time(), shelter_id))
        if new_occ != s["occupancy"]:
            # Doluluq tarixçəsi — Time-to-Capacity proqnozu üçün
            ctx.db.execute("INSERT INTO shelter_occupancy_log (shelter_id, ts, occupancy) VALUES (?,?,?)",
                           (shelter_id, time.time(), new_occ))
    updated = get_shelter(ctx, shelter_id)
    from . import forecast
    forecast.attach(ctx, [updated])
    ctx.bus.publish("shelter_update", {"shelter": updated}, channel="public")
    return updated
