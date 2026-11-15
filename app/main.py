"""Urban Crisis & Crowd Evacuation Engine — tətbiqin giriş nöqtəsi.

İşə salma:  uvicorn app.main:app --port 8000
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .config import BASE_DIR, settings
from .context import AppContext, load_city
from .core.db import Database
from .core.events import EventBus
from .core.graph import RoadGraph
from .routers import auth, citizen, dispatcher
from .services import analytics, demo, evacuation
from .services.routing import RoutingService

log = logging.getLogger("uce")
STATIC_DIR = BASE_DIR / "static"
MAX_BODY_BYTES = 64 * 1024

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "geolocation=(self), camera=(), microphone=()",
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: https://*.basemaps.cartocdn.com https://*.tile.openstreetmap.org; "
        "connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
    ),
}


class BodyLimitMiddleware:
    """ASGI middleware: sorğu gövdəsi həddi aşarsa 413 qaytarır.

    Yalnız Content-Length başlığına güvənmir — chunked axında da baytları sayır.
    Gövdə kiçik olduğu üçün (≤ 64 KB) əvvəlcə tam oxunur, sonra tətbiqə ötürülür.
    """

    def __init__(self, app, limit: int):
        self.app = app
        self.limit = limit

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] not in ("POST", "PUT", "PATCH"):
            return await self.app(scope, receive, send)
        headers = dict(scope.get("headers") or [])
        length = headers.get(b"content-length", b"")
        if length.isdigit() and int(length) > self.limit:
            return await self._reject(send)
        chunks, total = [], 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            body = message.get("body", b"")
            total += len(body)
            if total > self.limit:
                return await self._reject(send)
            chunks.append(body)
            if not message.get("more_body", False):
                break
        replayed = False

        async def replay():
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": b"".join(chunks), "more_body": False}
            return await receive()

        await self.app(scope, replay, send)

    @staticmethod
    async def _reject(send):
        response = JSONResponse({"detail": "payload_too_large"}, status_code=413, headers=SECURITY_HEADERS)
        await response({"type": "http"}, None, send)


def _external_client() -> httpx.AsyncClient:
    """Xarici API müştərisi. URL verilməyibsə, simulyator eyni prosesdə (şəbəkəsiz) çağırılır."""
    if settings.external_api_url:
        return httpx.AsyncClient(base_url=settings.external_api_url)
    from external_api.main import create_external_app
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=create_external_app()), base_url="http://external-api")


async def _traffic_loop(ctx: AppContext) -> None:
    """Fon tapşırığı: xarici API-dən yol vəziyyətini izləyir; dəyişiklikdə vətəndaşları yenidən marşrutlaşdırır."""
    was_degraded = ctx.routing.status()["degraded"]
    while True:
        try:
            changed = await ctx.routing.sync_traffic()
            degraded = ctx.routing.status()["degraded"]
            if degraded != was_degraded:
                was_degraded = degraded
                analytics.invalidate()
                ctx.bus.publish("routing_status", ctx.routing.status(), channel="public")
            if changed:
                analytics.invalidate()
                ctx.bus.publish("road_update", {"closures": ctx.routing.closures_list()}, channel="public")
                await evacuation.reevaluate_plans(ctx)
        except Exception:  # fon dövrü heç vaxt dayanmamalıdır
            log.exception("traffic sync failed")
        await asyncio.sleep(settings.traffic_sync_interval_s)


def create_app(db_path: str | None = None, start_background: bool = True) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        city = load_city(settings.city_file)
        db = Database(db_path or settings.db_path)
        db.seed(city, demo.default_users())
        graph = RoadGraph(city["nodes"], city["edges"])
        client = _external_client()
        routing = RoutingService(graph, client, os.getenv("UCE_EXTERNAL_API_KEY", "demo-external-key"))
        for row in db.query("SELECT * FROM road_closures WHERE source='dispatcher'"):
            routing.manual_closures[row["edge_id"]] = {"reason": row["reason"], "since": row["created_at"]}
        ctx = AppContext(db=db, bus=EventBus(), graph=graph, routing=routing, city=city)
        app.state.ctx = ctx
        app.state.started_at = time.time()
        await routing.sync_traffic()
        task = asyncio.create_task(_traffic_loop(ctx)) if start_background else None
        try:
            yield
        finally:
            if task:
                task.cancel()
            demo.stop_inflow(ctx)
            await client.aclose()
            db.close()

    app = FastAPI(title=settings.app_name, version=settings.version, lifespan=lifespan)

    if settings.cors_origins:
        app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origins, allow_methods=["*"], allow_headers=["*"])

    @app.middleware("http")
    async def harden(request: Request, call_next):
        response = await call_next(request)
        is_api_docs = request.url.path in ("/docs", "/redoc", "/docs/oauth2-redirect")
        for k, v in SECURITY_HEADERS.items():
            if k == "Content-Security-Policy" and is_api_docs:
                continue  # Swagger UI öz skriptlərini CDN-dən yükləyir; sərt CSP yalnız tətbiq səhifələrinə tətbiq olunur
            response.headers.setdefault(k, v)
        if request.url.path.startswith("/api/"):
            response.headers.setdefault("Cache-Control", "no-store")
        return response

    # Ən xarici qat: gövdə ölçüsü həddi (Content-Length və chunked axın üçün eyni qayda)
    app.add_middleware(BodyLimitMiddleware, limit=MAX_BODY_BYTES)

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception):
        log.exception("unhandled error on %s", request.url.path)
        return JSONResponse({"detail": "internal_error"}, status_code=500)  # daxili detal sızdırılmır

    app.include_router(citizen.router)
    app.include_router(auth.router)
    app.include_router(dispatcher.router)

    @app.get("/health", tags=["system"])
    def health(request: Request):
        ctx: AppContext = request.app.state.ctx
        return {"status": "ok", "version": settings.version,
                "uptime_s": round(time.time() - request.app.state.started_at),
                "routing": ctx.routing.status(),
                "events": {"clients": ctx.bus.subscriber_count, "published": ctx.bus.published, "dropped": ctx.bus.dropped}}

    # ----- Frontend (statik) -----
    pages = {"/": "index.html", "/citizen": "citizen.html", "/dispatcher": "dispatcher.html"}
    for route, filename in pages.items():
        app.add_api_route(route, (lambda f=filename: FileResponse(STATIC_DIR / f)), include_in_schema=False)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app


app = create_app()
