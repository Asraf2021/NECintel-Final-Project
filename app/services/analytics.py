"""Dispetçer paneli üçün icmal və analitika."""
from __future__ import annotations

import time

from ..config import settings
from ..context import AppContext
from . import crisis, shelters as shelter_svc, sos as sos_svc

_cache: dict = {"ts": 0.0, "data": None}
OVERVIEW_TTL_S = 1.0  # mikro-keş: yüksək yük altında eyni saniyədə təkrar hesablanmır


def invalidate() -> None:
    _cache["ts"] = 0.0


def list_brigades(ctx: AppContext) -> list[dict]:
    rows = ctx.db.query("SELECT * FROM brigades ORDER BY id")
    for b in rows:
        b["district_name"] = ctx.district_name(b["district_id"])
    return rows


def overview(ctx: AppContext) -> dict:
    now = time.time()
    if _cache["data"] is not None and now - _cache["ts"] < OVERVIEW_TTL_S:
        return _cache["data"]

    window = settings.sos_cluster_window_s
    shelters = shelter_svc.list_shelters(ctx, include_reserve=True)
    open_sh = [s for s in shelters if s["status"] == "open"]
    brigades = list_brigades(ctx)
    sos_list = sos_svc.list_sos(ctx, limit=300)
    active = [s for s in sos_list if s["status"] in ("active", "assigned")]
    recent = [s for s in sos_list if s["created_at"] >= now - window]
    alerts = crisis.list_alerts(ctx)
    pending = [a for a in alerts if a["status"] == "pending"]
    total_cap = sum(s["capacity"] for s in open_sh)
    total_occ = sum(s["occupancy"] for s in open_sh)

    zones = []
    for d in ctx.districts:
        z_active = [s for s in active if s["district_id"] == d["id"]]
        z_recent = {s["citizen_id"] for s in recent if s["district_id"] == d["id"]}
        z_sh = [s for s in open_sh if s["district_id"] == d["id"]]
        cap = sum(s["capacity"] for s in z_sh)
        occ = sum(s["occupancy"] for s in z_sh)
        pct = round(occ * 100 / cap, 1) if cap else 0.0
        by_type: dict[str, int] = {}
        for s in z_active:
            by_type[s["type"]] = by_type.get(s["type"], 0) + 1
        has_alert = any(a["district_id"] == d["id"] for a in pending)
        if has_alert or len(z_recent) >= settings.sos_cluster_count:
            risk = "critical"
        elif len(z_recent) >= 2 or pct >= settings.shelter_reroute_pct:
            risk = "high"
        elif z_active or pct >= 70:
            risk = "medium"
        else:
            risk = "low"
        zones.append({
            "district_id": d["id"], "name": d["name"], "lat": d["lat"], "lon": d["lon"],
            "sos_active": len(z_active), "sos_window": len(z_recent), "by_type": by_type,
            "shelters_open": len(z_sh), "occupancy_pct": pct,
            "brigades_available": sum(1 for b in brigades if b["district_id"] == d["id"] and b["status"] == "available"),
            "risk": risk,
        })

    # Son 30 dəqiqə üzrə dəqiqəlik SOS axını
    start = now - 30 * 60
    buckets = [0] * 30
    for row in ctx.db.query("SELECT created_at FROM sos_signals WHERE created_at>=?", (start,)):
        idx = min(29, int((row["created_at"] - start) // 60))
        buckets[idx] += 1
    by_type_all: dict[str, int] = {"fire": 0, "collapse": 0, "flood": 0, "medical": 0}
    for s in active:
        by_type_all[s["type"]] = by_type_all.get(s["type"], 0) + 1

    data = {
        "generated_at": now,
        "kpi": {
            "sos_active": len(active),
            "sos_window": len(recent),
            "alerts_pending": len(pending),
            "shelters_open": len(open_sh),
            "shelters_reserve": sum(1 for s in shelters if s["status"] == "reserve"),
            "shelters_full": sum(1 for s in open_sh if not s["accepting"]),
            "occupancy_pct": round(total_occ * 100 / total_cap, 1) if total_cap else 0.0,
            "total_capacity": total_cap,
            "total_occupancy": total_occ,
            "brigades_available": sum(1 for b in brigades if b["status"] == "available"),
            "brigades_total": len(brigades),
            "roads_closed": len(ctx.routing.closed_edges),
            "citizens_safe": ctx.db.scalar("SELECT COUNT(*) FROM citizen_status WHERE status='safe'"),
            "live_clients": ctx.bus.subscriber_count,
        },
        "zones": zones,
        "sos": sos_list[:120],
        "shelters": shelters,
        "brigades": brigades,
        "alerts": alerts,
        "closures": ctx.routing.closures_list(),
        "routing": ctx.routing.status(),
        "timeline": buckets,
        "by_type": by_type_all,
        "thresholds": {
            "reroute_pct": settings.shelter_reroute_pct, "critical_pct": settings.shelter_critical_pct,
            "cluster_count": settings.sos_cluster_count, "cluster_window_s": window,
            "cluster_radius_m": settings.sos_cluster_radius_m,
        },
    }
    _cache["ts"], _cache["data"] = now, data
    return data
