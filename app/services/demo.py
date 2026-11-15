"""Demo ssenariləri: münsiflərə sistemi canlı göstərmək üçün hadisələri simulyasiya edir."""
from __future__ import annotations

import json
import math
import random
import uuid

from ..config import settings
from ..context import AppContext
from ..core.security import hash_password
from . import crisis, evacuation, shelters as shelter_svc, sos as sos_svc


async def _sync_now(ctx: AppContext) -> int:
    """Xarici API-dən yol vəziyyətini dərhal çəkir; dəyişiklik varsa vətəndaşları yenidən marşrutlaşdırır."""
    if await ctx.routing.sync_traffic():
        ctx.bus.publish("road_update", {"closures": ctx.routing.closures_list()}, channel="public")
        return await evacuation.reevaluate_plans(ctx)
    return 0


def default_users() -> list[dict]:
    return [
        {"username": "dispatcher", "password_hash": hash_password(settings.dispatcher_password),
         "role": "dispatcher", "display_name": "Dispetçer 01"},
        {"username": "admin", "password_hash": hash_password(settings.admin_password),
         "role": "admin", "display_name": "Administrator"},
    ]


async def run(ctx: AppContext, name: str, district_id: str | None, shelter_id: str | None, edge_id: str | None) -> dict:
    if name == "sos_cluster":
        d = ctx.district(district_id) if district_id else None
        d = d or random.choice(ctx.districts)
        sos_type = random.choice(["fire", "collapse", "flood", "medical"])
        created, alert = [], None
        for _ in range(settings.sos_cluster_count):
            # ~300 m radiusda təsadüfi nöqtələr, hər biri fərqli vətəndaşdan
            ang, r = random.uniform(0, 2 * math.pi), random.uniform(30, 300)
            lat = d["lat"] + (r * math.cos(ang)) / 111_320
            lon = d["lon"] + (r * math.sin(ang)) / (111_320 * math.cos(math.radians(d["lat"])))
            res = sos_svc.create_sos(ctx, f"demo-{uuid.uuid4().hex[:12]}", sos_type, lat, lon, "Demo ssenari")
            created.append(res["sos"]["id"])
            alert = res["alert"] or alert
        return {"scenario": name, "district": d["name"], "sos_ids": created, "alert_id": alert["id"] if alert else None}

    if name == "shelter_fill":
        target = shelter_svc.get_shelter(ctx, shelter_id) if shelter_id else None
        if not target:
            # Ən çox vətəndaşa tövsiyə olunan məntəqə doldurulur ki, avtomatik yenidən marşrut görünsün
            row = ctx.db.one(
                "SELECT p.shelter_id, COUNT(*) n FROM evacuation_plans p JOIN shelters s ON s.id=p.shelter_id"
                " WHERE s.status='open' GROUP BY p.shelter_id ORDER BY n DESC, MAX(p.updated_at) DESC LIMIT 1")
            target = shelter_svc.get_shelter(ctx, row["shelter_id"]) if row else None
        if not target or not target["accepting"]:
            open_sh = [s for s in shelter_svc.list_shelters(ctx) if s["accepting"]]
            target = max(open_sh, key=lambda s: s["occupancy_pct"]) if open_sh else None
        if not target:
            return {"scenario": name, "error": "no_shelter"}
        updated = shelter_svc.update_shelter(ctx, target["id"], occupancy=math.ceil(target["capacity"] * 0.97))
        alert = crisis.evaluate_shelter(ctx, updated)
        rerouted = await evacuation.reevaluate_plans(ctx)
        return {"scenario": name, "shelter": updated["name"], "occupancy_pct": updated["occupancy_pct"],
                "alert_id": alert["id"] if alert else None, "rerouted": rerouted}

    if name == "crowd":
        changed = 0
        for s in shelter_svc.list_shelters(ctx):
            inc = random.randint(0, max(1, s["capacity"] // 12))
            updated = shelter_svc.update_shelter(ctx, s["id"], delta=inc)
            crisis.evaluate_shelter(ctx, updated)
            changed += 1
        rerouted = await evacuation.reevaluate_plans(ctx)
        return {"scenario": name, "shelters_changed": changed, "rerouted": rerouted}

    if name == "road_close":
        closed = ctx.routing.closed_edges
        target = edge_id if edge_id in ctx.graph.edges else None
        if not target:
            # Aktiv təxliyə marşrutlarında olan yol seçilir ki, yenidən marşrutlaşdırma görünsün
            used: dict[str, int] = {}
            for p in ctx.db.query("SELECT edge_ids FROM evacuation_plans ORDER BY updated_at DESC LIMIT 200"):
                for e in json.loads(p["edge_ids"]):
                    if e not in closed:
                        used[e] = used.get(e, 0) + 1
            target = max(used, key=used.get) if used else random.choice([e for e in ctx.graph.edges if e not in closed])
        await ctx.routing.sim(f"roads/{target}/close", {"reason": "Demo: yol qəzası"})
        rerouted = await _sync_now(ctx)
        return {"scenario": name, "edge_id": target, "road": ctx.graph.edges[target].name, "rerouted": rerouted}

    if name == "road_open_all":
        await ctx.routing.sim("reset")
        for e in list(ctx.routing.manual_closures):
            ctx.routing.set_manual_closure(e, False)
        ctx.db.execute("DELETE FROM road_closures")
        await _sync_now(ctx)
        return {"scenario": name}

    if name == "api_outage":
        await ctx.routing.sim("outage", {"mode": "error"})
        # Kəsintini dərhal aşkarla (növbəti fon sinxronunu gözləmədən): circuit breaker açılır
        for _ in range(ctx.routing.breaker.fail_threshold):
            if not ctx.routing.breaker.allow():
                break
            await ctx.routing.sync_traffic()
        ctx.bus.publish("routing_status", ctx.routing.status(), channel="public")
        return {"scenario": name, "mode": "error", "routing": ctx.routing.status()}

    if name == "api_restore":
        await ctx.routing.sim("outage", {"mode": "off"})
        ctx.routing.breaker.success()
        await _sync_now(ctx)
        ctx.bus.publish("routing_status", ctx.routing.status(), channel="public")
        return {"scenario": name, "mode": "off", "routing": ctx.routing.status()}

    if name == "reset":
        await ctx.routing.sim("reset")
        ctx.routing.manual_closures.clear()
        ctx.routing.breaker.success()
        ctx.db.seed(ctx.city, default_users(), force=True)
        await ctx.routing.sync_traffic()
        ctx.routing._bump()
        return {"scenario": name}

    return {"scenario": name, "error": "unknown"}
