"""Modul 2 — Fövqəladə Hallar Dispetçer API-si (yalnız səlahiyyətli rollar)."""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse

from ..config import settings
from ..context import AppContext, get_ctx
from ..core.security import require
from ..schemas import BrigadeUpdate, RoadCloseRequest, ScenarioRequest, ShelterUpdate, SosAssign
from ..services import analytics, crisis, demo, evacuation, shelters as shelter_svc, sos as sos_svc

router = APIRouter(prefix="/api/v1/dispatch", tags=["dispatcher"])


@router.get("/overview")
def overview(user: dict = Depends(require("ops:read")), ctx: AppContext = Depends(get_ctx)):
    """Canlı panel üçün tam icmal: KPI, zonalar, SOS, məntəqələr, briqadalar, həyəcanlar."""
    return analytics.overview(ctx)


@router.get("/stream")
async def stream(user: dict = Depends(require("ops:read")), ctx: AppContext = Depends(get_ctx)):
    return StreamingResponse(ctx.bus.stream("ops"), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ----- SOS -----
@router.get("/sos")
def sos_list(status: str | None = Query(default=None, pattern="^(active|assigned|resolved|safe)$"),
             user: dict = Depends(require("ops:read")), ctx: AppContext = Depends(get_ctx)):
    return {"sos": sos_svc.list_sos(ctx, status)}


@router.post("/sos/{sos_id}/assign")
def sos_assign(sos_id: int, body: SosAssign, user: dict = Depends(require("sos:manage")),
               ctx: AppContext = Depends(get_ctx)):
    result = sos_svc.assign_brigade(ctx, sos_id, body.brigade_id, user)
    if result is None:
        raise HTTPException(404, "sos_not_found")
    if "error" in result:
        raise HTTPException(409, result["error"])
    analytics.invalidate()
    return result


@router.post("/sos/{sos_id}/resolve")
def sos_resolve(sos_id: int, user: dict = Depends(require("sos:manage")), ctx: AppContext = Depends(get_ctx)):
    result = sos_svc.resolve_sos(ctx, sos_id, user)
    if result is None:
        raise HTTPException(404, "sos_not_found")
    analytics.invalidate()
    return result


# ----- Qırmızı Həyəcan -----
@router.get("/alerts")
def alerts(user: dict = Depends(require("ops:read")), ctx: AppContext = Depends(get_ctx)):
    return {"alerts": crisis.list_alerts(ctx)}


@router.post("/alerts/{alert_id}/confirm")
def alert_confirm(alert_id: int, user: dict = Depends(require("alert:decide")), ctx: AppContext = Depends(get_ctx)):
    """Bir kliklə təsdiq: əlavə briqada göndərilir + yeni toplanış nöqtəsi açılır."""
    result = crisis.confirm_alert(ctx, alert_id, user)
    if result is None:
        raise HTTPException(404, "alert_not_found")
    analytics.invalidate()
    return result


@router.post("/alerts/{alert_id}/dismiss")
def alert_dismiss(alert_id: int, user: dict = Depends(require("alert:decide")), ctx: AppContext = Depends(get_ctx)):
    result = crisis.dismiss_alert(ctx, alert_id, user)
    if result is None:
        raise HTTPException(404, "alert_not_found")
    analytics.invalidate()
    return result


# ----- Toplanış məntəqələri -----
@router.patch("/shelters/{shelter_id}")
async def shelter_update(shelter_id: str, body: ShelterUpdate, user: dict = Depends(require("shelter:manage")),
                         ctx: AppContext = Depends(get_ctx)):
    updated = shelter_svc.update_shelter(ctx, shelter_id, occupancy=body.occupancy, status=body.status)
    if updated is None:
        raise HTTPException(404, "shelter_not_found")
    ctx.db.audit(user["sub"], user["role"], "shelter_update", {"shelter_id": shelter_id, **body.model_dump(exclude_none=True)})
    crisis.evaluate_shelter(ctx, updated)
    analytics.invalidate()
    if not updated["accepting"]:
        await ctx.replan.request("shelter_full")
    return updated


# ----- Briqadalar -----
@router.patch("/brigades/{brigade_id}")
def brigade_update(brigade_id: str, body: BrigadeUpdate, user: dict = Depends(require("brigade:manage")),
                   ctx: AppContext = Depends(get_ctx)):
    import time
    if not ctx.db.one("SELECT id FROM brigades WHERE id=?", (brigade_id,)):
        raise HTTPException(404, "brigade_not_found")
    if body.status == "available":
        ctx.db.execute("UPDATE brigades SET status='available', target_lat=NULL, target_lon=NULL,"
                       " assigned_alert_id=NULL, assigned_sos_id=NULL, updated_at=? WHERE id=?", (time.time(), brigade_id))
    else:
        ctx.db.execute("UPDATE brigades SET status=?, updated_at=? WHERE id=?", (body.status, time.time(), brigade_id))
    ctx.db.audit(user["sub"], user["role"], "brigade_update", {"brigade_id": brigade_id, "status": body.status})
    ctx.bus.publish("brigade_update", {"brigade_id": brigade_id}, channel="ops")
    analytics.invalidate()
    return ctx.db.one("SELECT * FROM brigades WHERE id=?", (brigade_id,))


# ----- Yollar -----
@router.post("/roads/{edge_id}/close")
async def road_close(edge_id: str, body: RoadCloseRequest, user: dict = Depends(require("road:manage")),
                     ctx: AppContext = Depends(get_ctx)):
    import time
    if edge_id not in ctx.graph.edges:
        raise HTTPException(404, "road_not_found")
    ctx.routing.set_manual_closure(edge_id, True, body.reason)
    ctx.db.execute("INSERT OR REPLACE INTO road_closures (edge_id,source,reason,created_at) VALUES (?,?,?,?)",
                   (edge_id, "dispatcher", body.reason, time.time()))
    ctx.db.audit(user["sub"], user["role"], "road_close", {"edge_id": edge_id, "reason": body.reason})
    ctx.bus.publish("road_update", {"closures": ctx.routing.closures_list()}, channel="public")
    analytics.invalidate()
    rerouted = await ctx.replan.request("road_closed")
    # Fon rejimində say sonradan `reroute_batch` hadisəsi ilə gəlir (None = planlaşdırılıb)
    return {"ok": True, "edge_id": edge_id, "rerouted_citizens": rerouted,
            "replan": "done" if rerouted is not None else "scheduled"}


@router.post("/roads/{edge_id}/open")
def road_open(edge_id: str, user: dict = Depends(require("road:manage")), ctx: AppContext = Depends(get_ctx)):
    if edge_id not in ctx.graph.edges:
        raise HTTPException(404, "road_not_found")
    ctx.routing.set_manual_closure(edge_id, False)
    ctx.db.execute("DELETE FROM road_closures WHERE edge_id=?", (edge_id,))
    ctx.db.audit(user["sub"], user["role"], "road_open", {"edge_id": edge_id})
    ctx.bus.publish("road_update", {"closures": ctx.routing.closures_list()}, channel="public")
    analytics.invalidate()
    return {"ok": True, "edge_id": edge_id}


# ----- Audit və demo -----
@router.get("/audit")
def audit(limit: int = Query(default=50, ge=1, le=500), user: dict = Depends(require("ops:read")),
          ctx: AppContext = Depends(get_ctx)):
    rows = ctx.db.query("SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,))
    for r in rows:
        r["details"] = json.loads(r["details"])
    return {"audit": rows}


@router.post("/demo/scenario")
async def scenario(body: ScenarioRequest, user: dict = Depends(require("demo:run")), ctx: AppContext = Depends(get_ctx)):
    if not settings.demo_mode:
        raise HTTPException(403, "demo_mode_disabled")
    result = await demo.run(ctx, body.name, body.district_id, body.shelter_id, body.edge_id)
    ctx.db.audit(user["sub"], user["role"], "demo_scenario", {"name": body.name})
    analytics.invalidate()
    if body.name in ("reset", "road_open_all"):
        ctx.bus.publish("road_update", {"closures": ctx.routing.closures_list()}, channel="public")
        ctx.bus.publish("reset", {}, channel="public")
    return result
