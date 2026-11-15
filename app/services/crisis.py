"""Kritik Böhran Alqoritmi — "Qırmızı Həyəcan".

Qayda A (SOS klasteri): eyni zonadan (rayon) VƏ YA 500 m radiusda son 5 dəqiqədə
          ən azı 4 fərqli vətəndaşdan SOS siqnalı daxil olubsa.
Qayda B (doluluq):      toplanış məntəqəsinin doluluğu 95%-i keçibsə.

Hər iki halda operator panelində xəbərdarlıq yaranır:
"Qırmızı Həyəcan: Əlavə Xilasetmə Briqadası Göndər və Yeni Toplanış Nöqtəsi Aç".
Operator qərarı bir kliklə təsdiqləyir -> ən yaxın boş briqada göndərilir və ən yaxın
ehtiyat məntəqə açılır (confirm_alert).
"""
from __future__ import annotations

import json
import time

from ..config import settings
from ..context import AppContext
from ..core.geo import haversine_m
from . import shelters as shelter_svc

# SOS növü -> üstünlük verilən briqada növü
PREFERRED_BRIGADE = {"fire": "fire", "medical": "medical", "collapse": "rescue", "flood": "rescue"}


def _alert_out(a: dict, ctx: AppContext) -> dict:
    a = dict(a)
    a["details"] = json.loads(a["details"])
    a["actions"] = json.loads(a["actions"])
    a["district_name"] = ctx.district_name(a["district_id"])
    return a


def list_alerts(ctx: AppContext, limit: int = 50) -> list[dict]:
    rows = ctx.db.query(
        "SELECT * FROM alerts ORDER BY (status='pending') DESC, created_at DESC LIMIT ?", (limit,))
    return [_alert_out(a, ctx) for a in rows]


def _recent_decision(ctx: AppContext, kind: str, column: str, value: str) -> bool:
    """Eyni obyekt üçün yaxın vaxtda qərar verilibsə, təkrar həyəcan yaradılmır (cooldown)."""
    since = time.time() - settings.alert_cooldown_s
    return ctx.db.scalar(
        f"SELECT COUNT(*) FROM alerts WHERE kind=? AND {column}=? AND status!='pending' AND decided_at>=?",
        (kind, value, since)) > 0


def evaluate_sos_cluster(ctx: AppContext, sos: dict) -> dict | None:
    """Qayda A. Yeni SOS daxil olanda çağırılır. Həyəcan yaranıbsa/yenilənibsə qaytarır."""
    now = time.time()
    since = now - settings.sos_cluster_window_s
    recent = ctx.db.query("SELECT * FROM sos_signals WHERE created_at>=?", (since,))

    zone = [s for s in recent if s["district_id"] == sos["district_id"]]
    radius = [s for s in recent
              if haversine_m(sos["lat"], sos["lon"], s["lat"], s["lon"]) <= settings.sos_cluster_radius_m]
    zone_n = len({s["citizen_id"] for s in zone})
    radius_n = len({s["citizen_id"] for s in radius})
    need = settings.sos_cluster_count
    if zone_n < need and radius_n < need:
        return None

    trigger = "radius" if radius_n >= need else "zone"
    cluster = radius if trigger == "radius" else zone
    by_type: dict[str, int] = {}
    for s in cluster:
        by_type[s["type"]] = by_type.get(s["type"], 0) + 1
    details = {
        "trigger": trigger, "zone_count": zone_n, "radius_count": radius_n,
        "window_s": settings.sos_cluster_window_s, "radius_m": settings.sos_cluster_radius_m,
        "sos_ids": [s["id"] for s in cluster], "by_type": by_type,
        "dominant_type": max(by_type, key=by_type.get),
    }
    lat = sum(s["lat"] for s in cluster) / len(cluster)
    lon = sum(s["lon"] for s in cluster) / len(cluster)

    with ctx.db.transaction():
        pending = ctx.db.one(
            "SELECT * FROM alerts WHERE kind='sos_cluster' AND district_id=? AND status='pending'",
            (sos["district_id"],))
        if not pending and trigger == "radius":
            # Rayon sərhədindəki klaster: eyni 500 m-lik ərazi üçün artıq gözləyən həyəcan varsa, onu yenilə
            for a in ctx.db.query("SELECT * FROM alerts WHERE kind='sos_cluster' AND status='pending'"):
                if a["lat"] is not None and haversine_m(lat, lon, a["lat"], a["lon"]) <= settings.sos_cluster_radius_m:
                    pending = a
                    break
        if pending:
            ctx.db.execute("UPDATE alerts SET details=?, lat=?, lon=? WHERE id=?",
                           (json.dumps(details), lat, lon, pending["id"]))
            alert_id, event = pending["id"], "alert_update"
        else:
            if _recent_decision(ctx, "sos_cluster", "district_id", sos["district_id"]):
                return None
            cur = ctx.db.execute(
                "INSERT INTO alerts (kind,level,district_id,lat,lon,details,created_at) VALUES ('sos_cluster','red',?,?,?,?,?)",
                (sos["district_id"], lat, lon, json.dumps(details), now))
            alert_id, event = cur.lastrowid, "alert_new"
    alert = _alert_out(ctx.db.one("SELECT * FROM alerts WHERE id=?", (alert_id,)), ctx)
    ctx.bus.publish(event, {"alert": alert}, channel="ops")
    return alert


def evaluate_shelter(ctx: AppContext, shelter: dict) -> dict | None:
    """Qayda B. Doluluq dəyişəndə çağırılır."""
    if shelter["status"] != "open" or not shelter["over_critical"]:
        return None
    details = {"trigger": "occupancy", "occupancy_pct": shelter["occupancy_pct"],
               "occupancy": shelter["occupancy"], "capacity": shelter["capacity"],
               "shelter_name": shelter["name"], "threshold_pct": settings.shelter_critical_pct}
    with ctx.db.transaction():
        pending = ctx.db.one(
            "SELECT * FROM alerts WHERE kind='shelter_overflow' AND shelter_id=? AND status='pending'", (shelter["id"],))
        if pending:
            ctx.db.execute("UPDATE alerts SET details=? WHERE id=?", (json.dumps(details, ensure_ascii=False), pending["id"]))
            alert_id, event = pending["id"], "alert_update"
        else:
            if _recent_decision(ctx, "shelter_overflow", "shelter_id", shelter["id"]):
                return None
            cur = ctx.db.execute(
                "INSERT INTO alerts (kind,level,district_id,shelter_id,lat,lon,details,created_at)"
                " VALUES ('shelter_overflow','red',?,?,?,?,?,?)",
                (shelter["district_id"], shelter["id"], shelter["lat"], shelter["lon"],
                 json.dumps(details, ensure_ascii=False), time.time()))
            alert_id, event = cur.lastrowid, "alert_new"
    alert = _alert_out(ctx.db.one("SELECT * FROM alerts WHERE id=?", (alert_id,)), ctx)
    ctx.bus.publish(event, {"alert": alert}, channel="ops")
    return alert


def nearest_available_brigade(ctx: AppContext, lat: float, lon: float, preferred_type: str | None = None) -> dict | None:
    available = ctx.db.query("SELECT * FROM brigades WHERE status='available'")
    if not available:
        return None
    preferred = [b for b in available if b["type"] == preferred_type]
    pool = preferred or available
    return min(pool, key=lambda b: haversine_m(lat, lon, b["lat"], b["lon"]))


def confirm_alert(ctx: AppContext, alert_id: int, actor: dict) -> dict | None:
    """Bir kliklə təsdiq: (1) əlavə briqada göndər, (2) yeni toplanış nöqtəsi aç. Atomar icra olunur."""
    opened_shelter = None
    with ctx.db.transaction():
        alert = ctx.db.one("SELECT * FROM alerts WHERE id=?", (alert_id,))
        if not alert:
            return None
        if alert["status"] != "pending":
            return _alert_out(alert, ctx)  # idempotent: ikinci klik heç nəyi təkrarlamır
        details = json.loads(alert["details"])
        lat, lon = alert["lat"], alert["lon"]
        actions: dict = {"brigade": None, "new_shelter": None}

        brigade = nearest_available_brigade(ctx, lat, lon, PREFERRED_BRIGADE.get(details.get("dominant_type", ""), "rescue"))
        if brigade:
            ctx.db.execute(
                "UPDATE brigades SET status='dispatched', target_lat=?, target_lon=?, assigned_alert_id=?, updated_at=? WHERE id=?",
                (lat, lon, alert_id, time.time(), brigade["id"]))
            actions["brigade"] = {"id": brigade["id"], "name": brigade["name"], "type": brigade["type"],
                                  "distance_m": round(haversine_m(lat, lon, brigade["lat"], brigade["lon"]))}
            sos_ids = details.get("sos_ids") or []
            if sos_ids:
                marks = ",".join("?" * len(sos_ids))
                ctx.db.execute(
                    f"UPDATE sos_signals SET status='assigned', brigade_id=?, updated_at=? WHERE id IN ({marks}) AND status='active'",
                    (brigade["id"], time.time(), *sos_ids))

        reserves = ctx.db.query("SELECT * FROM shelters WHERE status='reserve'")
        if reserves:
            new = min(reserves, key=lambda s: haversine_m(lat, lon, s["lat"], s["lon"]))
            ctx.db.execute("UPDATE shelters SET status='open', updated_at=? WHERE id=?", (time.time(), new["id"]))
            opened_shelter = new["id"]
            actions["new_shelter"] = {"id": new["id"], "name": new["name"], "capacity": new["capacity"],
                                      "distance_m": round(haversine_m(lat, lon, new["lat"], new["lon"]))}

        ctx.db.execute("UPDATE alerts SET status='confirmed', actions=?, decided_at=?, decided_by=? WHERE id=?",
                       (json.dumps(actions, ensure_ascii=False), time.time(), actor["sub"], alert_id))
        ctx.db.audit(actor["sub"], actor["role"], "alert_confirm", {"alert_id": alert_id, **actions})

    result = _alert_out(ctx.db.one("SELECT * FROM alerts WHERE id=?", (alert_id,)), ctx)
    ctx.bus.publish("alert_update", {"alert": result}, channel="ops")
    if result["actions"].get("brigade"):
        ctx.bus.publish("brigade_update", {"brigade_id": result["actions"]["brigade"]["id"]}, channel="ops")
    if opened_shelter:
        ctx.bus.publish("shelter_update", {"shelter": shelter_svc.get_shelter(ctx, opened_shelter), "opened": True},
                        channel="public")
    return result


def dismiss_alert(ctx: AppContext, alert_id: int, actor: dict) -> dict | None:
    with ctx.db.transaction():
        alert = ctx.db.one("SELECT * FROM alerts WHERE id=?", (alert_id,))
        if not alert:
            return None
        if alert["status"] == "pending":
            ctx.db.execute("UPDATE alerts SET status='dismissed', decided_at=?, decided_by=? WHERE id=?",
                           (time.time(), actor["sub"], alert_id))
            ctx.db.audit(actor["sub"], actor["role"], "alert_dismiss", {"alert_id": alert_id})
    result = _alert_out(ctx.db.one("SELECT * FROM alerts WHERE id=?", (alert_id,)), ctx)
    ctx.bus.publish("alert_update", {"alert": result}, channel="ops")
    return result
