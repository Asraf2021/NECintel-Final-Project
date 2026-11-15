"""Marşrutlaşdırma xidməti: Xarici API + avtomatik çoxsəviyyəli ehtiyat (fallback).

Dayanıqlılıq nümunələri (Challenge Drop 2 — Xarici API 503 qaytarır və ya 5 saniyədən çox cavab vermir):
  * sərt timeout + təkrar cəhd (retry): asılı sorğu heç vaxt 5 s-dən çox gözlənilmir;
  * circuit breaker — ardıcıl uğursuzluqdan sonra xarici API müvəqqəti çağırılmır (hər sorğu timeout gözləmir);
  * graceful degradation — ehtiyat səviyyələri (fallback_tier):
      1. "local_graph"  — keşlənmiş yol şəbəkəsi + son məlum bağlı yollar üzərində lokal Dijkstra;
      2. "cached_route" — son keşlənmiş marşrutlar (TTL-dən asılı olmayaraq, kəsinti zamanı);
      3. "static_matrix" — statik koordinat matrisi (məntəqələrə düz xətt məsafəsi × yol əmsalı):
                           qraf belə əlçatmaz olsa, məsafə/müddət qiymətləndirməsi verilir;
  * heç bir halda istisna (unhandled exception) istifadəçiyə qalxmır — həmişə cavab var;
  * qısa ömürlü marşrut keşi — yüksək yük altında təkrar hesablamanı azaldır.
"""
from __future__ import annotations

import asyncio
import time

import httpx

from ..config import settings
from ..core.geo import haversine_m
from ..core.graph import ACCESS_FACTOR, ROAD_FACTOR, RoadGraph


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
        # Son məlum marşrutlar (kəsinti üçün): açar versiyasız, TTL-siz; yalnız ehtiyat səviyyə 2-də istifadə olunur
        self._last_known: dict[tuple, tuple[float, list[dict]]] = {}
        self.tier_calls = {"local_graph": 0, "cached_route": 0, "static_matrix": 0}
        self.degraded_since: float | None = None
        self.consecutive_errors = 0

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
        if degraded and self.degraded_since is None:
            self.degraded_since = time.time()
        if not degraded:
            self.degraded_since = None
        return {
            "provider": "fallback_local" if degraded else "external",
            "degraded": degraded,
            "mode": "offline_fallback" if degraded else "online",
            "breaker": self.breaker.state,
            "degraded_since": self.degraded_since,
            "last_sync_ok": self.last_sync_ok,
            "last_error": self.last_error,
            "external_calls": self.external_calls,
            "fallback_calls": self.fallback_calls,
            "tier_calls": dict(self.tier_calls),
            "cache_hits": self.cache_hits,
            "last_known_routes": len(self._last_known),
            "timeout_s": settings.external_timeout_s,
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
            except asyncio.TimeoutError as exc:  # cavab müddəti həddi keçdi (Drop 2: > 5 s)
                last_exc = exc
                self.last_error = f"Timeout: cavab {settings.external_timeout_s:g} s ərzində gəlmədi"
                break  # timeout-da təkrar cəhd etmirik — istifadəçi gözləməsin
            except Exception as exc:  # şəbəkə xətası, 5xx (503 Service Unavailable)
                last_exc = exc
        self.breaker.failure()
        self.consecutive_errors += 1
        if not isinstance(last_exc, asyncio.TimeoutError):
            status = getattr(getattr(last_exc, "response", None), "status_code", None)
            self.last_error = f"HTTP {status}" if status else f"{type(last_exc).__name__}: {str(last_exc)[:120]}"
        raise RuntimeError("external_unavailable") from last_exc

    async def sync_traffic(self) -> bool:
        """Xarici API-dən bağlı yolları/sıxlığı sinxronlaşdırır. Dəyişiklik varsa True."""
        try:
            data = await self._call("GET", "/v1/traffic")
        except RuntimeError:
            return False  # kəsinti: son məlum vəziyyət saxlanılır
        was_degraded = self.last_sync_ok is None
        recovered = self.consecutive_errors > 0   # kəsintidən sonra ilk uğurlu cavab
        self.last_sync_ok = time.time()
        self.last_error = None
        self.consecutive_errors = 0
        closures = {c["edge_id"]: {"reason": c.get("reason", ""), "since": c.get("since")}
                    for c in data.get("closures", [])}
        congestion = {k: float(v) for k, v in data.get("congestion", {}).items()}
        changed = set(closures) != set(self.external_closures) or congestion != self.congestion
        self.external_closures, self.congestion = closures, congestion
        if changed or was_degraded or recovered:
            self._bump()   # bərpadan sonra ehtiyat nəticələri keşdə qalmasın
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
        lk_key = (profile, round(origin[0], 4), round(origin[1], 4),
                  tuple((round(d[0], 4), round(d[1], 4)) for d in destinations))

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
            routes = await self._fallback_routes(origin, destinations, speed, profile, lk_key)
        if source == "external":  # ehtiyat cavablar keşlənmir: API bərpa olunan kimi təzə marşrut
            if len(self._cache) > 5000:
                self._cache.clear()
            self._cache[key] = (time.time(), routes, source)
        if source == "external" or all(r.get("tier") == "local_graph" for r in routes):
            if len(self._last_known) > 20000:
                self._last_known.clear()
            self._last_known[lk_key] = (time.time(), routes)
        return routes, source

    async def _fallback_routes(self, origin, destinations, speed, profile, lk_key) -> list[dict]:
        """Ehtiyat səviyyələri: lokal qraf → son keşlənmiş marşrut → statik matris. Heç vaxt istisna atmır."""
        try:
            # 1) CPU-ağır hesablama event loop-u bloklamasın (texniki borc E1)
            routes = await asyncio.to_thread(self._local_routes, origin, destinations, speed, profile)
            self.tier_calls["local_graph"] += 1
            for r in routes:
                r["tier"] = "local_graph"
            return routes
        except Exception:
            pass
        known = self._last_known.get(lk_key)
        if known:
            self.tier_calls["cached_route"] += 1
            age = round(time.time() - known[0])
            return [{**r, "tier": "cached_route", "cached_age_s": age} for r in known[1]]
        self.tier_calls["static_matrix"] += 1
        return [self.static_estimate(origin, d, speed) for d in destinations]

    @staticmethod
    def static_estimate(origin, dest, speed_kmh: float) -> dict:
        """Statik koordinat matrisi: düz xətt məsafəsi × yol əmsalı. Qraf əlçatmaz olduqda son ehtiyat."""
        d = haversine_m(origin[0], origin[1], dest[0], dest[1]) * ROAD_FACTOR * ACCESS_FACTOR
        return {"found": True, "distance_m": round(d), "duration_min": round(d / (speed_kmh * 1000 / 60), 1),
                "points": [list(origin), list(dest)], "edge_ids": [], "steps": [], "tier": "static_matrix",
                "estimate": True}

    def _local_routes(self, origin, destinations, speed: float, profile: str) -> list[dict]:
        """Lokal Dijkstra (ehtiyat). Thread-də işləyir: qraf yalnız oxunur, vəziyyət lokal dəyişənlərdədir."""
        closed = self.closed_edges
        congestion = dict(self.congestion)
        return [self.graph.shortest_path(origin, d, closed, congestion, speed, profile).to_dict() for d in destinations]

    # ----- Simulyasiya idarəsi (demo) -----
    async def sim(self, path: str, payload: dict | None = None) -> dict:
        resp = await self.client.post(f"/v1/sim/{path}", json=payload or {}, timeout=3.0)
        resp.raise_for_status()
        return resp.json()
