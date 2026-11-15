"""Testlər üçün köməkçi funksiyalar."""
from __future__ import annotations

import time

from app.core.geo import haversine_m

EVAC = "/api/v1/evacuation"
SOS = "/api/v1/sos"
STATUS = "/api/v1/status"
DISPATCH = "/api/v1/dispatch"


def cid(n: int) -> str:
    """Etibarlı vətəndaş identifikatoru (>= 8 simvol)."""
    return f"citizen-{n:04d}"


def district_point(ctx, district_id: str, i: int = 0) -> tuple[float, float]:
    """Rayon mərkəzinin yaxınlığında nöqtə (hər i üçün ~20 m sürüşmə)."""
    d = ctx.district(district_id)
    return d["lat"] + i * 0.0002, d["lon"]


def send_sos(client, citizen_id: str, sos_type: str = "fire", lat: float = 40.383, lon: float = 49.9, note: str = ""):
    return client.post(SOS, json={"citizen_id": citizen_id, "type": sos_type, "lat": lat, "lon": lon, "note": note})


def insert_sos(ctx, citizen_id: str, lat: float, lon: float, age_s: float = 0.0, sos_type: str = "fire") -> dict:
    """SOS sətirini birbaşa bazaya yazır (köhnə created_at vermək üçün)."""
    ts = time.time() - age_s
    cur = ctx.db.execute(
        "INSERT INTO sos_signals (citizen_id,type,lat,lon,district_id,note,status,created_at,updated_at)"
        " VALUES (?,?,?,?,?,'','active',?,?)",
        (citizen_id, sos_type, lat, lon, ctx.nearest_district(lat, lon), ts, ts))
    return ctx.db.one("SELECT * FROM sos_signals WHERE id=?", (cur.lastrowid,))


def alerts(ctx, kind: str | None = None, status: str | None = None) -> list[dict]:
    rows = ctx.db.query("SELECT * FROM alerts ORDER BY id")
    return [a for a in rows if (kind is None or a["kind"] == kind) and (status is None or a["status"] == status)]


def shelter_row(ctx, shelter_id: str) -> dict:
    return ctx.db.one("SELECT * FROM shelters WHERE id=?", (shelter_id,))


def plan_with_edges(client, citizen_id: str | None = None) -> tuple[dict, tuple[float, float]]:
    """Tövsiyə olunan marşrutunda ən azı bir yol seqmenti olan başlanğıc nöqtəsini dinamik tapır."""
    candidates = [(40.4100, 49.9000)]
    candidates += [(40.36 + i * 0.01, 49.80 + j * 0.02) for i in range(10) for j in range(9)]
    for lat, lon in candidates:
        params = {"lat": lat, "lon": lon}
        if citizen_id:
            params["citizen_id"] = citizen_id
        r = client.get(EVAC, params=params)
        assert r.status_code == 200, r.text
        plan = r.json()
        rec = plan["recommended"]
        if rec and rec["route"]["edge_ids"]:
            return plan, (lat, lon)
    raise AssertionError("marşrutunda yol seqmenti olan başlanğıc tapılmadı")


def all_route_edges(plan: dict) -> set[str]:
    """Plandakı bütün marşrutların (tövsiyə + alternativlər) yol seqmentləri."""
    edges = set(plan["recommended"]["route"]["edge_ids"]) if plan["recommended"] else set()
    for s in plan["shelters"]:
        if s.get("route"):
            edges |= set(s["route"]["edge_ids"])
    return edges


def border_points(ctx, offset_m: float = 60.0):
    """İki qonşu rayonun sərhədində, bir-birindən < 500 m aralı, fərqli rayonlara düşən iki nöqtə."""
    ds = ctx.districts
    for a in ds:
        for b in ds:
            if a["id"] >= b["id"]:
                continue
            mlat, mlon = (a["lat"] + b["lat"]) / 2, (a["lon"] + b["lon"]) / 2
            half = haversine_m(a["lat"], a["lon"], mlat, mlon)
            t = offset_m / half
            pa = (mlat + (a["lat"] - mlat) * t, mlon + (a["lon"] - mlon) * t)
            pb = (mlat + (b["lat"] - mlat) * t, mlon + (b["lon"] - mlon) * t)
            if ctx.nearest_district(*pa) == a["id"] and ctx.nearest_district(*pb) == b["id"]:
                return (a["id"], pa), (b["id"], pb)
    raise AssertionError("sərhəd nöqtələri tapılmadı")
