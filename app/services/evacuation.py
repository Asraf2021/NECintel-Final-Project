"""Təxliyə planı: ən yaxın toplanış məntəqələri, tövsiyə olunan marşrut və avtomatik yenidən marşrutlaşdırma."""
from __future__ import annotations

import json
import time

from ..config import settings
from ..context import AppContext
from ..core.geo import haversine_m
from ..core.roles import citizen_role, role_policy
from . import shelters as shelter_svc

CANDIDATES = 8  # marşrutu hesablanan ən yaxın məntəqə sayı


async def build_plan(ctx: AppContext, lat: float, lon: float, citizen_id: str | None = None,
                     role: str | None = None) -> dict:
    """Vətəndaş üçün təxliyə planı qurur.

    Tövsiyə qaydası: doluluğu 90%-dən az olan və marşrutu mövcud olan məntəqələr arasında
    ən qısa piyada müddəti. Ən yaxın məntəqə dolu (>=90%) və ya yolu bağlıdırsa — alternativ.
    Rol siyasəti (app/core/roles.py): "priority_citizen" üçün yalnız əlçatan məntəqələr və
    yalnız əlçatan (panduslu, maneəsiz) yol seqmentləri ilə marşrut.
    """
    role = role or citizen_role(ctx.db, citizen_id)
    policy = role_policy(role)
    profile = policy["route_profile"]
    open_shelters = shelter_svc.list_shelters(ctx)
    if policy["accessible_shelters_only"]:
        open_shelters = [s for s in open_shelters if s["accessible"]]
    for s in open_shelters:
        s["distance_m"] = round(haversine_m(lat, lon, s["lat"], s["lon"]))
    open_shelters.sort(key=lambda s: s["distance_m"])
    candidates = open_shelters[:CANDIDATES]

    # Yaxındakıların hamısı doludursa, daha uzaqdakı boş məntəqələri də əlavə et
    if candidates and not any(s["accepting"] for s in candidates):
        candidates += [s for s in open_shelters[CANDIDATES:] if s["accepting"]][:4]

    routes, source = await ctx.routing.routes((lat, lon), [(s["lat"], s["lon"]) for s in candidates], profile)
    for s, r in zip(candidates, routes):
        s["route"] = r if r.get("found") else None

    reachable = [s for s in candidates if s["route"]]
    accepting = [s for s in reachable if s["accepting"]]
    skipped = []
    recommended = None
    reason = "nearest"

    if accepting:
        recommended = min(accepting, key=lambda s: s["route"]["duration_min"])
    elif reachable:
        recommended = min(reachable, key=lambda s: s["occupancy_pct"])
        reason = "all_full"

    nearest = candidates[0] if candidates else None
    if recommended and nearest and recommended["id"] != nearest["id"]:
        if not nearest["accepting"]:
            reason = "nearest_full" if reason == "nearest" else reason
            skipped.append({"shelter_id": nearest["id"], "name": nearest["name"], "reason": "shelter_full",
                            "occupancy_pct": nearest["occupancy_pct"]})
        elif not nearest["route"]:
            reason = "road_closed"
            skipped.append({"shelter_id": nearest["id"], "name": nearest["name"], "reason": "no_route"})

    shown = candidates[: settings.nearest_shelters_limit]
    if recommended and recommended not in shown:
        shown = shown[:-1] + [recommended]  # tövsiyə olunan məntəqə həmişə siyahıda görünür

    plan = {
        "origin": {"lat": lat, "lon": lon, "district_id": ctx.nearest_district(lat, lon)},
        "shelters": shown,
        "recommended": None,
        "rerouted": False,
        "reroute_reason": None,
        "routing": {"source": source, "degraded": source != "external"},
        "role": role,
        "profile": profile,
        "sos_priority": policy["sos_priority"],
        "closed_roads": len(ctx.routing.closed_edges),
        "generated_at": time.time(),
    }
    if recommended:
        plan["recommended"] = {"shelter_id": recommended["id"], "name": recommended["name"],
                               "name_en": recommended.get("name_en", ""), "accessible": recommended["accessible"],
                               "route": recommended["route"], "reason": reason, "skipped": skipped}

    if citizen_id and recommended:
        _compare_and_store(ctx, citizen_id, lat, lon, recommended, plan, profile)
    return plan


def _compare_and_store(ctx: AppContext, citizen_id: str, lat: float, lon: float, recommended: dict, plan: dict,
                       profile: str = "foot") -> None:
    """Əvvəlki planla müqayisə edir: dəyişiklik varsa səbəbini qeyd edir, yeni planı saxlayır."""
    prev = ctx.db.one("SELECT * FROM evacuation_plans WHERE citizen_id=?", (citizen_id,))
    new_edges = recommended["route"]["edge_ids"]
    if prev and haversine_m(lat, lon, prev["origin_lat"], prev["origin_lon"]) < 150:
        prev_edges = json.loads(prev["edge_ids"])
        closed = ctx.routing.closed_edges
        if set(prev_edges) & closed:
            plan["rerouted"], plan["reroute_reason"] = True, "road_closed"
        elif prev["shelter_id"] != recommended["id"]:
            old = shelter_svc.get_shelter(ctx, prev["shelter_id"])
            if old and not old["accepting"]:
                plan["rerouted"], plan["reroute_reason"] = True, "shelter_full"
                plan["previous_shelter"] = {"id": old["id"], "name": old["name"], "name_en": old.get("name_en", ""),
                                            "occupancy_pct": old["occupancy_pct"]}
    ctx.db.execute(
        "INSERT INTO evacuation_plans (citizen_id,origin_lat,origin_lon,shelter_id,edge_ids,profile,updated_at)"
        " VALUES (?,?,?,?,?,?,?) ON CONFLICT(citizen_id) DO UPDATE SET origin_lat=excluded.origin_lat,"
        " origin_lon=excluded.origin_lon, shelter_id=excluded.shelter_id, edge_ids=excluded.edge_ids,"
        " profile=excluded.profile, updated_at=excluded.updated_at",
        (citizen_id, lat, lon, recommended["id"], json.dumps(new_edges), profile, time.time()),
    )


async def reevaluate_plans(ctx: AppContext, max_plans: int = 500) -> int:
    """Yol bağlandıqda / məntəqə 90% dolduqda təsirlənən vətəndaşlara avtomatik alternativ marşrut göndərir."""
    closed = ctx.routing.closed_edges
    full = {s["id"] for s in shelter_svc.list_shelters(ctx, include_reserve=True) if not s["accepting"]}
    plans = ctx.db.query("SELECT * FROM evacuation_plans ORDER BY updated_at DESC LIMIT ?", (max_plans,))
    sent = 0
    for p in plans:
        affected = p["shelter_id"] in full or bool(set(json.loads(p["edge_ids"])) & closed)
        if not affected:
            continue
        plan = await build_plan(ctx, p["origin_lat"], p["origin_lon"], p["citizen_id"])
        if plan["rerouted"]:
            ctx.bus.publish("reroute", {"plan": plan}, citizen_id=p["citizen_id"])
            sent += 1
    if sent:
        ctx.bus.publish("reroute_batch", {"count": sent}, channel="ops")
    return sent
