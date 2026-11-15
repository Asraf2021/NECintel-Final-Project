"""Demo ssenariləri: münsiflərə sistemi canlı göstərmək üçün hadisələri simulyasiya edir."""
from __future__ import annotations

import asyncio
import json
import logging
import math
import random
import time
import uuid

from ..config import settings
from ..context import AppContext
from ..core.security import hash_password
from . import crisis, evacuation, shelters as shelter_svc, sos as sos_svc


INFLOW_TICK_S = 3.0
log = logging.getLogger("uce.demo")


async def _inflow_loop(ctx: AppContext, targets: list[str]) -> None:
    """Canlı insan axını: seçilmiş məntəqələrə hər 3 saniyədən bir gəliş (Time-to-Capacity proqnozunu göstərmək üçün)."""
    from . import analytics
    tick = 0
    while True:
        await asyncio.sleep(INFLOW_TICK_S)
        tick += 1
        try:
            changed_full = False
            for sid in targets:
                s = shelter_svc.get_shelter(ctx, sid)
                if not s or s["status"] != "open" or s["occupancy"] >= s["capacity"]:
                    continue
                # tutumun ~0.15–0.3%-i hər addımda (bir az təsadüfi) — 90%-ə bir neçə dəqiqəyə çatır
                inc = max(1, round(s["capacity"] * random.uniform(0.0015, 0.003)))
                updated = shelter_svc.update_shelter(ctx, sid, delta=inc)
                crisis.evaluate_shelter(ctx, updated)
                if s["accepting"] and not updated["accepting"]:
                    changed_full = True
            analytics.invalidate()
            if changed_full or tick % 5 == 0:
                await ctx.replan.request("inflow")
        except Exception:  # simulyator heç vaxt tətbiqi dayandırmamalıdır
            log.exception("inflow tick failed")


def stop_inflow(ctx: AppContext) -> bool:
    task = getattr(ctx, "inflow_task", None)
    if task and not task.done():
        task.cancel()
        ctx.inflow_task = None
        return True
    return False


async def _sync_now(ctx: AppContext) -> int:
    """Xarici API-dən yol vəziyyətini dərhal çəkir; dəyişiklik varsa vətəndaşları yenidən marşrutlaşdırır."""
    if await ctx.routing.sync_traffic():
        ctx.bus.publish("road_update", {"closures": ctx.routing.closures_list()}, channel="public")
        return await ctx.replan.request("traffic")
    return 0


def default_users() -> list[dict]:
    return [
        {"username": "dispatcher", "password_hash": hash_password(settings.dispatcher_password),
         "role": "dispatcher", "display_name": "Dispetçer 01"},
        {"username": "admin", "password_hash": hash_password(settings.admin_password),
         "role": "admin", "display_name": "Administrator"},
        {"username": "operator", "password_hash": hash_password(settings.operator_password),
         "role": "operator", "display_name": "Operator (yalnız baxış)"},
    ]


async def run(ctx: AppContext, name: str, district_id: str | None, shelter_id: str | None, edge_id: str | None,
              mode: str | None = None) -> dict:
    if name == "sos_flood":
        # Botnet simulyasiyası: 600 saxta SOS, hər biri fərqli "vətəndaş ID"-si ilə (fərqli IP-lərdən gəlmiş kimi —
        # IP limiti bunu tutmur, buna görə daşqın detektoru və dublikat filtri işə düşür)
        started = time.perf_counter()
        hotspots = [(d["lat"], d["lon"]) for d in random.sample(ctx.districts, 3)]
        stats = {"created": 0, "quarantined": 0, "merged": 0, "duplicate": 0}
        for i in range(600):
            if i % 3 == 0:  # üçdə biri eyni nöqtələrdən (tipik bot davranışı)
                lat, lon = random.choice(hotspots)
            else:
                lat, lon = random.uniform(40.36, 40.45), random.uniform(49.80, 49.96)
            res = sos_svc.create_sos(ctx, f"bot-{uuid.uuid4().hex[:16]}", random.choice(["fire", "collapse", "flood", "medical"]),
                                     lat, lon, "")
            key = "merged" if res.get("merged") else "quarantined" if res.get("quarantined") else "duplicate" if res["duplicate"] else "created"
            stats[key] += 1
        ctx.bus.publish("flood_status", ctx.flood.status(), channel="ops")
        return {"scenario": name, **stats, "elapsed_ms": round((time.perf_counter() - started) * 1000),
                "avg_ms_per_sos": round((time.perf_counter() - started) * 1000 / 600, 2), "flood": ctx.flood.status()}

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

    if name == "priority_sos":
        # Hərəkət məhdudiyyətli vətəndaş: profil + SOS -> avtomatik Kritik Prioritet
        d = ctx.district(district_id) if district_id else None
        d = d or random.choice(ctx.districts)
        citizen = f"demo-p-{uuid.uuid4().hex[:12]}"
        sos_svc.set_profile(ctx, citizen, "priority_citizen")
        lat = d["lat"] + random.uniform(-0.002, 0.002)
        lon = d["lon"] + random.uniform(-0.002, 0.002)
        res = sos_svc.create_sos(ctx, citizen, random.choice(["medical", "collapse", "flood", "fire"]), lat, lon, "Demo")
        return {"scenario": name, "district": d["name"], "sos_id": res["sos"]["id"], "priority": res["sos"]["priority"]}

    if name == "inflow_start":
        stop_inflow(ctx)
        # Ən çox vətəndaşa tövsiyə olunan məntəqələr + ən dolu açıq məntəqələr
        rows = ctx.db.query(
            "SELECT p.shelter_id FROM evacuation_plans p JOIN shelters s ON s.id=p.shelter_id WHERE s.status='open'"
            " GROUP BY p.shelter_id ORDER BY COUNT(*) DESC LIMIT 2")
        targets = [r["shelter_id"] for r in rows]
        for s in sorted(shelter_svc.list_shelters(ctx), key=lambda x: -x["occupancy_pct"]):
            if len(targets) >= 4:
                break
            if s["accepting"] and s["id"] not in targets:
                targets.append(s["id"])
        for sid in targets:  # başlanğıc nöqtəsi
            sh = shelter_svc.get_shelter(ctx, sid)
            ctx.db.execute("INSERT INTO shelter_occupancy_log (shelter_id, ts, occupancy) VALUES (?,?,?)",
                           (sid, __import__("time").time(), sh["occupancy"]))
        ctx.inflow_task = asyncio.create_task(_inflow_loop(ctx, targets))
        return {"scenario": name, "shelters": [shelter_svc.get_shelter(ctx, t)["name"] for t in targets],
                "tick_s": INFLOW_TICK_S}

    if name == "inflow_stop":
        return {"scenario": name, "stopped": stop_inflow(ctx)}

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
        rerouted = await ctx.replan.request("shelter_full")
        return {"scenario": name, "shelter": updated["name"], "occupancy_pct": updated["occupancy_pct"],
                "alert_id": alert["id"] if alert else None, "rerouted": rerouted}

    if name == "crowd":
        changed = 0
        for s in shelter_svc.list_shelters(ctx):
            inc = random.randint(0, max(1, s["capacity"] // 12))
            updated = shelter_svc.update_shelter(ctx, s["id"], delta=inc)
            crisis.evaluate_shelter(ctx, updated)
            changed += 1
        rerouted = await ctx.replan.request("crowd")
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
        outage_mode = mode or "error"  # error -> HTTP 503; timeout -> cavab 5 s-dən çox gecikir
        await ctx.routing.sim("outage", {"mode": outage_mode})
        # Kəsintini dərhal aşkarla (növbəti fon sinxronunu gözləmədən): breaker həddi qədər paralel yoxlama —
        # timeout rejimində də ümumi gözləmə bir timeout qədərdir (≤ 2.5 s), 3 × 2.5 s deyil
        await asyncio.gather(*(ctx.routing.sync_traffic() for _ in range(ctx.routing.breaker.fail_threshold)))
        ctx.bus.publish("routing_status", ctx.routing.status(), channel="public")
        return {"scenario": name, "mode": outage_mode, "routing": ctx.routing.status()}

    if name == "api_restore":
        await ctx.routing.sim("outage", {"mode": "off"})
        ctx.routing.breaker.success()
        ctx.routing._bump()
        await _sync_now(ctx)
        ctx.bus.publish("routing_status", ctx.routing.status(), channel="public")
        return {"scenario": name, "mode": "off", "routing": ctx.routing.status()}

    if name == "reset":
        stop_inflow(ctx)
        if ctx.flood:
            ctx.flood.reset()
        await ctx.routing.sim("reset")
        ctx.routing.manual_closures.clear()
        ctx.routing.breaker.success()
        ctx.db.seed(ctx.city, default_users(), force=True)
        await ctx.routing.sync_traffic()
        ctx.routing._bump()
        return {"scenario": name}

    return {"scenario": name, "error": "unknown"}
