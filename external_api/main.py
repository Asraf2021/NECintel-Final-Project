"""Simulyasiya olunmuş Xarici Xəritə və Marşrutlaşdırma API-si.

Real provayderi (Google Maps / OSRM / şəhər nəqliyyat mərkəzi) əvəz edir:
  * yol şəbəkəsi, bağlı yollar və sıxlıq (congestion) məlumatı;
  * marşrut hesablanması (tək və toplu);
  * /v1/sim/* — yol bağlanması və API kəsintisini (outage) simulyasiya etmək üçün.

Müstəqil servis kimi (uvicorn external_api.main:app --port 8001) və ya əsas
tətbiqin daxilində (in-process) işləyə bilir.
"""
from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.core.graph import RoadGraph

CITY_FILE = os.getenv("UCE_CITY_FILE", str(Path(__file__).resolve().parent.parent / "app" / "data" / "city.json"))
API_KEY = os.getenv("UCE_EXTERNAL_API_KEY", "demo-external-key")


class Point(BaseModel):
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)


class BatchRouteRequest(BaseModel):
    origin: Point
    destinations: list[Point] = Field(max_length=50)
    avoid: list[str] = Field(default_factory=list, max_length=200)
    speed_kmh: float = Field(default=4.5, gt=0, le=130)
    profile: str = Field(default="foot", pattern="^(foot|wheelchair)$")


class CloseRequest(BaseModel):
    reason: str = Field(default="incident", max_length=120)


class OutageRequest(BaseModel):
    mode: str = Field(pattern="^(off|error|timeout|slow)$")


def create_external_app() -> FastAPI:
    app = FastAPI(title="External Map & Routing API (simulated)", version="1.0.0")
    graph = RoadGraph.from_file(CITY_FILE)
    state = {
        "closures": {},      # edge_id -> {"reason":..., "since":...}
        "congestion": {},    # edge_id -> əmsal
        "outage": "off",
        "version": 1,
    }
    app.state.sim = state

    @app.middleware("http")
    async def outage_and_auth(request: Request, call_next):
        path = request.url.path
        if path.startswith("/v1/") and not path.startswith("/v1/sim/"):
            if request.headers.get("x-api-key") != API_KEY:
                return JSONResponse({"detail": "invalid api key"}, status_code=401)
            mode = state["outage"]
            if mode == "error":
                return JSONResponse({"detail": "service unavailable (simulated outage)"}, status_code=503)
            if mode == "timeout":
                await asyncio.sleep(30)
            elif mode == "slow":
                await asyncio.sleep(0.8)
        return await call_next(request)

    def _route(origin: Point, dest: Point, avoid: list[str], speed: float, profile: str = "foot") -> dict:
        closed = set(state["closures"]) | set(avoid)
        return graph.shortest_path(
            (origin.lat, origin.lon), (dest.lat, dest.lon), closed, state["congestion"], speed, profile
        ).to_dict()

    @app.get("/v1/health")
    async def health():
        return {"status": "ok", "version": state["version"]}

    @app.get("/v1/network")
    async def network():
        return {
            "nodes": list(graph.nodes.values()),
            "edges": [
                {"id": e.id, "a": e.a, "b": e.b, "name": e.name, "length_m": round(e.length_m),
                 "accessible": e.accessible, "barrier": e.barrier}
                for e in graph.edges.values()
            ],
        }

    @app.get("/v1/traffic")
    async def traffic():
        return {
            "version": state["version"],
            "closures": [{"edge_id": k, **v} for k, v in state["closures"].items()],
            "congestion": state["congestion"],
        }

    # Sinxron (def) endpoint-lər: FastAPI onları thread-pool-da işlədir — CPU-ağır Dijkstra event loop-u bloklamır
    @app.get("/v1/route")
    def route(from_lat: float, from_lon: float, to_lat: float, to_lon: float,
                    avoid: str = "", speed_kmh: float = 4.5, profile: str = "foot"):
        return _route(Point(lat=from_lat, lon=from_lon), Point(lat=to_lat, lon=to_lon),
                      [a for a in avoid.split(",") if a], speed_kmh, profile)

    @app.post("/v1/routes/batch")
    def routes_batch(req: BatchRouteRequest):
        return {"routes": [_route(req.origin, d, req.avoid, req.speed_kmh, req.profile) for d in req.destinations]}

    # ----- Simulyasiya idarəsi (real provayderdə olmur) -----
    @app.post("/v1/sim/roads/{edge_id}/close")
    async def close_road(edge_id: str, req: CloseRequest):
        if edge_id not in graph.edges:
            raise HTTPException(404, "unknown road")
        state["closures"][edge_id] = {"reason": req.reason, "since": time.time()}
        state["version"] += 1
        return {"ok": True, "edge_id": edge_id}

    @app.post("/v1/sim/roads/{edge_id}/open")
    async def open_road(edge_id: str):
        state["closures"].pop(edge_id, None)
        state["version"] += 1
        return {"ok": True, "edge_id": edge_id}

    @app.post("/v1/sim/congestion/{edge_id}")
    async def set_congestion(edge_id: str, factor: float = 2.0):
        if edge_id not in graph.edges:
            raise HTTPException(404, "unknown road")
        state["congestion"][edge_id] = max(1.0, min(factor, 5.0))
        state["version"] += 1
        return {"ok": True}

    @app.post("/v1/sim/outage")
    async def outage(req: OutageRequest):
        state["outage"] = req.mode
        return {"ok": True, "mode": req.mode}

    @app.post("/v1/sim/reset")
    async def reset():
        state["closures"].clear()
        state["congestion"].clear()
        state["outage"] = "off"
        state["version"] += 1
        return {"ok": True}

    return app


app = create_external_app()
