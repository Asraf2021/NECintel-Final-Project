"""Marşrutlaşdırma xidməti: Xarici API + avtomatik lokal ehtiyat (fallback).

Dayanıqlılıq nümunələri:
  * timeout + təkrar cəhd (retry);
  * circuit breaker — ardıcıl uğursuzluqdan sonra xarici API müvəqqəti çağırılmır;
  * graceful degradation — xarici API əlçatmaz olduqda keşlənmiş yol şəbəkəsi və
    son məlum bağlı yollar əsasında lokal Dijkstra ilə marşrut qurulur;
  * qısa ömürlü marşrut keşi — yüksək yük altında təkrar hesablamanı azaldır.
"""
from __future__ import annotations

import asyncio
import time

import httpx

from ..config import settings
from ..core.graph import RoadGraph


class CircuitBreaker:
    def __init__(self, fail_threshold: int, reset_s: float):
        self.fail_threshold = fail_threshold
        self.reset_s = reset_s
        self.failures = 0
        self.opened_at: float | None = None

    @property
    def state(self) -> str:
        if self.opened_at is None:
            return "closed"
        return "half_open" if time.time() - self.opened_at >= self.reset_s else "open"

    def allow(self) -> bool:
        return self.state != "open"

    def success(self) -> None:
        self.failures = 0
        self.opened_at = None

    def failure(self) -> None:
        self.failures += 1
        if self.failures >= self.fail_threshold:
            self.opened_at = time.time()


class RoutingService:
    def __init__(self, graph: RoadGraph, client: httpx.AsyncClient, api_key: str):
        self.graph = graph
        self.client = client
        self.headers = {"X-API-Key": api_key}
        self.breaker = CircuitBreaker(settings.breaker_fail_threshold, settings.breaker_reset_s)
        self.manual_closures: dict[str, dict] = {}     # dispetçerin bağladığı yollar
        self.external_closures: dict[str, dict] = {}   # xarici API-dən son məlum vəziyyət
        self.congestion: dict[str, float] = {}
        self.last_sync_ok: float | None = None
        self.last_error: str | None = None
        self.external_calls = 0
        self.fallback_calls = 0
        self.cache_hits = 0
        self._version = 0
        self._cache: dict[tuple, tuple[float, list[dict], str]] = {}

    # ----- Vəziyyət -----
    @property
    def closed_edges(self) -> set[str]:
        return set(self.manual_closures) | set(self.external_closures)

    def closures_list(self) -> list[dict]:
        out = []
        for source, items in (("dispatcher", self.manual_closures), ("external", self.external_closures)):
            for edge_id, info in items.items():
                e = self.graph.edges.get(edge_id)
                if not e:
                    continue
                a, b = self.graph.nodes[e.a], self.graph.nodes[e.b]
                out.append({"edge_id": edge_id, "name": e.name, "source": source,
                            "reason": info.get("reason", ""), "since": info.get("since"),
                            "points": [[a["lat"], a["lon"]], [b["lat"], b["lon"]]]})
        return out

    def status(self) -> dict:
        degraded = self.breaker.state != "closed" or self.last_sync_ok is None
        return {
            "provider": "fallback_local" if degraded else "external",
            "degraded": degraded,
            "breaker": self.breaker.state,
            "last_sync_ok": self.last_sync_ok,
            "last_error": self.last_error,
            "external_calls": self.external_calls,
            "fallback_calls": self.fallback_calls,
            "cache_hits": self.cache_hits,
        }

    def _bump(self) -> None:
        self._version += 1
        self._cache.clear()

    def set_manual_closure(self, edge_id: str, closed: bool, reason: str = "") -> None:
        if closed:
            self.manual_closures[edge_id] = {"reason": reason, "since": time.time()}
        else:
            self.manual_closures.pop(edge_id, None)
        self._bump()

    # ----- Xarici API çağırışı -----
    async def _call(self, method: str, path: str, **kwargs):
        if not self.breaker.allow():
            raise RuntimeError("circuit_open")
        last_exc: Exception | None = None
        for _ in range(settings.external_retries + 1):
            try:
                # wait_for: in-process (ASGI) transportda da sərt timeout təmin olunur
                resp = await asyncio.wait_for(
                    self.client.request(method, path, headers=self.headers, timeout=settings.external_timeout_s, **kwargs),
                    timeout=settings.external_timeout_s + 0.5)
                resp.raise_for_status()
                self.breaker.success()
                self.external_calls += 1
                return resp.json()
            except Exception as exc:  # şəbəkə xətası, timeout, 5xx
                last_exc = exc
        self.breaker.failure()
        self.last_error = f"{type(last_exc).__name__}: {str(last_exc)[:120]}"
        raise RuntimeError("external_unavailable") from last_exc

    async def sync_traffic(self) -> bool:
        """Xarici API-dən bağlı yolları/sıxlığı sinxronlaşdırır. Dəyişiklik varsa True."""
        try:
            data = await self._call("GET", "/v1/traffic")
        except RuntimeError:
            return False  # kəsinti: son məlum vəziyyət saxlanılır
        was_degraded = self.last_sync_ok is None
        self.last_sync_ok = time.time()
        self.last_error = None
        closures = {c["edge_id"]: {"reason": c.get("reason", ""), "since": c.get("since")}
                    for c in data.get("closures", [])}
        congestion = {k: float(v) for k, v in data.get("congestion", {}).items()}
        changed = set(closures) != set(self.external_closures) or congestion != self.congestion
        self.external_closures, self.congestion = closures, congestion
        if changed or was_degraded:
            self._bump()
        return changed

    # ----- Marşrut -----
    async def routes(self, origin: tuple[float, float], destinations: list[tuple[float, float]],
                     profile: str = "foot") -> tuple[list[dict], str]:
        """Bir başlanğıcdan bir neçə təyinata marşrutlar. Qaytarır: (marşrutlar, mənbə).

        profile="wheelchair" — yalnız əlçatan (panduslu, maneəsiz) yol seqmentləri istifadə olunur.
        """
        speed = settings.wheelchair_speed_kmh if profile == "wheelchair" else settings.walking_speed_kmh
        key = (profile, round(origin[0], 4), round(origin[1], 4),
               tuple((round(d[0], 4), round(d[1], 4)) for d in destinations), self._version)
        cached = self._cache.get(key)
        if cached and time.time() - cached[0] < settings.route_cache_ttl_s:
            self.cache_hits += 1
            return cached[1], cached[2]

        source = "external"
        try:
            data = await self._call("POST", "/v1/routes/batch", json={
                "origin": {"lat": origin[0], "lon": origin[1]},
                "destinations": [{"lat": d[0], "lon": d[1]} for d in destinations],
                "avoid": sorted(self.manual_closures),
                "speed_kmh": speed,
                "profile": profile,
            })
            routes = data["routes"]
            if len(routes) != len(destinations):
                raise RuntimeError("bad_external_response")
        except Exception:
            source = "fallback"
            self.fallback_calls += 1
            closed = self.closed_edges
            routes = [
                self.graph.shortest_path(origin, d, closed, self.congestion, speed, profile).to_dict()
                for d in destinations
            ]
        if len(self._cache) > 5000:
            self._cache.clear()
        self._cache[key] = (time.time(), routes, source)
        return routes, source

    # ----- Simulyasiya idarəsi (demo) -----
    async def sim(self, path: str, payload: dict | None = None) -> dict:
        resp = await self.client.post(f"/v1/sim/{path}", json=payload or {}, timeout=3.0)
        resp.raise_for_status()
        return resp.json()
