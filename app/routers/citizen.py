"""Modul 1 — Vətəndaş Təhlükəsizlik API-si (açıq, lakin limitli və validasiyalı)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from ..config import settings
from ..context import AppContext, get_ctx
from ..core.geo import haversine_m
from ..core.security import client_ip, limiter, rate_limit
from ..schemas import SOS_TYPES, SHELTER_TYPES, SosCreate, StatusUpdate
from ..services import analytics, evacuation, shelters as shelter_svc, sos as sos_svc

router = APIRouter(prefix="/api/v1", tags=["citizen"], dependencies=[rate_limit("general", "rate_limit_general_per_min")])

CITIZEN_ID_RE = r"^[A-Za-z0-9_\-]{8,64}$"


@router.get("/meta")
def meta(ctx: AppContext = Depends(get_ctx)):
    """Müştəri üçün sorğu kitabçası: rayonlar, növlər, hədlər."""
    return {
        "app": settings.app_name, "version": settings.version, "city": ctx.city["city"],
        "districts": ctx.districts, "sos_types": SOS_TYPES, "shelter_types": SHELTER_TYPES,
        "thresholds": {"reroute_pct": settings.shelter_reroute_pct, "critical_pct": settings.shelter_critical_pct},
        "demo_mode": settings.demo_mode,
    }


@router.get("/shelters")
def shelters(district_id: str | None = Query(default=None, max_length=32),
             lat: float | None = Query(default=None, ge=-90, le=90),
             lon: float | None = Query(default=None, ge=-180, le=180),
             limit: int = Query(default=50, ge=1, le=100),
             ctx: AppContext = Depends(get_ctx)):
    """Açıq toplanış məntəqələri və cari doluluq faizi. Koordinat verilibsə — məsafəyə görə sıralanır."""
    items = shelter_svc.list_shelters(ctx)
    if district_id:
        if not ctx.district(district_id):
            raise HTTPException(404, "unknown_district")
        items = [s for s in items if s["district_id"] == district_id]
    if lat is not None and lon is not None:
        for s in items:
            s["distance_m"] = round(haversine_m(lat, lon, s["lat"], s["lon"]))
        items.sort(key=lambda s: s["distance_m"])
    return {"shelters": items[:limit]}


@router.get("/evacuation")
async def evacuation_plan(lat: float | None = Query(default=None, ge=-90, le=90),
                          lon: float | None = Query(default=None, ge=-180, le=180),
                          district_id: str | None = Query(default=None, max_length=32),
                          citizen_id: str | None = Query(default=None, pattern=CITIZEN_ID_RE),
                          ctx: AppContext = Depends(get_ctx)):
    """Ən yaxın məntəqələr + tövsiyə olunan təxliyə marşrutu (rayon və ya koordinat üzrə)."""
    if lat is None or lon is None:
        d = ctx.district(district_id) if district_id else None
        if not d:
            raise HTTPException(422, "lat_lon_or_district_required")
        lat, lon = d["lat"], d["lon"]
    return await evacuation.build_plan(ctx, lat, lon, citizen_id)


@router.post("/sos", status_code=201)
def send_sos(body: SosCreate, request: Request, ctx: AppContext = Depends(get_ctx)):
    """Bir kliklə SOS. IP və vətəndaş üzrə limitlənir (spam / saxta həyəcana qarşı)."""
    limit = settings.rate_limit_sos_per_min
    if not limiter.allow(f"sos-ip:{client_ip(request)}", limit * 5) or not limiter.allow(f"sos-c:{body.citizen_id}", limit):
        raise HTTPException(429, "rate_limit_exceeded", headers={"Retry-After": "60"})
    result = sos_svc.create_sos(ctx, body.citizen_id, body.type, body.lat, body.lon, body.note)
    analytics.invalidate()
    return {"sos": result["sos"], "duplicate": result["duplicate"]}


@router.post("/status")
async def update_status(body: StatusUpdate, ctx: AppContext = Depends(get_ctx)):
    """"Təhlükəsizəm" statusu. Məntəqə seçilibsə, onun dolulugu yenilənir."""
    result = sos_svc.set_status(ctx, body.citizen_id, body.status, body.shelter_id, body.lat, body.lon)
    analytics.invalidate()
    if result["shelter"] and not result["shelter"]["accepting"]:
        await evacuation.reevaluate_plans(ctx)  # məntəqə 90%-ə çatdı -> başqalarına alternativ
    return result


@router.get("/citizens/{citizen_id}")
def citizen(citizen_id: str, ctx: AppContext = Depends(get_ctx)):
    import re
    if not re.match(CITIZEN_ID_RE, citizen_id):
        raise HTTPException(422, "invalid_citizen_id")
    return sos_svc.citizen_state(ctx, citizen_id)


@router.get("/roads")
def roads(ctx: AppContext = Depends(get_ctx)):
    """Yol şəbəkəsi (xəritədə çəkmək üçün) və bağlı yollar."""
    g = ctx.graph
    return {
        "edges": [{"id": e.id, "name": e.name,
                   "points": [[g.nodes[e.a]["lat"], g.nodes[e.a]["lon"]], [g.nodes[e.b]["lat"], g.nodes[e.b]["lon"]]]}
                  for e in g.edges.values()],
        "closures": ctx.routing.closures_list(),
        "routing": ctx.routing.status(),
    }


@router.get("/stream")
async def stream(citizen_id: str | None = Query(default=None, pattern=CITIZEN_ID_RE),
                 ctx: AppContext = Depends(get_ctx)):
    """Canlı hadisələr (SSE): doluluq, yol dəyişikliyi, fərdi yenidən marşrut."""
    return StreamingResponse(ctx.bus.stream("public", citizen_id), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
