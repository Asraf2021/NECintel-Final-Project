"""Dayanıqlılıq: xarici API kəsintisi, circuit breaker, lokal ehtiyat (fallback) marşrutlaşdırma."""
from __future__ import annotations

import pytest

from app.config import settings
from app.services import routing as routing_mod
from app.services.routing import CircuitBreaker

from .helpers import DISPATCH, EVAC, all_route_edges, cid, plan_with_edges

POINTS = [(40.383, 49.900), (40.405, 49.870), (40.435, 49.830), (40.364, 49.835), (40.425, 49.930), (40.39, 49.84)]


@pytest.fixture(autouse=True)
def _no_route_cache(monkeypatch):
    """Marşrut keşi söndürülür ki, hər sorğu xarici API-yə getsin."""
    monkeypatch.setattr(settings, "route_cache_ttl_s", 0.0)


def _plan(client, i: int = 0, **extra):
    lat, lon = POINTS[i % len(POINTS)]
    r = client.get(EVAC, params={"lat": lat, "lon": lon, **extra})
    assert r.status_code == 200, r.text
    return r.json()


def _scenario(client, headers, name: str):
    r = client.post(f"{DISPATCH}/demo/scenario", json={"name": name}, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


# ---------------------------------------------------------------------------
# Kəsinti zamanı graceful degradation
# ---------------------------------------------------------------------------
def test_outage_falls_back_to_local_routing(client, ctx, auth_headers, sim):
    assert _plan(client)["routing"] == {"source": "external", "degraded": False}
    _scenario(client, auth_headers, "api_outage")
    assert sim["outage"] == "error"

    plan = _plan(client)
    assert plan["routing"] == {"source": "fallback", "degraded": True}
    assert plan["recommended"]["route"]["found"] is True
    assert plan["recommended"]["route"]["duration_min"] > 0
    assert len(plan["shelters"]) >= 1 and all("occupancy_pct" in s for s in plan["shelters"])
    assert ctx.routing.fallback_calls == 1
    assert "503" in ctx.routing.last_error or "HTTPStatusError" in ctx.routing.last_error


def test_fallback_route_equals_external_route(client, sim):
    """Eyni alqoritm, iki icraçı: kəsinti zamanı marşrut dəyişməməlidir."""
    plan, (lat, lon) = plan_with_edges(client)
    sim["outage"] = "error"
    fb = client.get(EVAC, params={"lat": lat, "lon": lon}).json()
    assert fb["routing"]["source"] == "fallback"
    assert fb["recommended"]["shelter_id"] == plan["recommended"]["shelter_id"]
    assert fb["recommended"]["route"] == plan["recommended"]["route"]


def test_breaker_opens_after_threshold_failures(client, ctx, sim, monkeypatch):
    threshold = settings.breaker_fail_threshold
    assert ctx.routing.breaker.fail_threshold == threshold
    sim["outage"] = "error"

    calls = []
    original = ctx.routing.client.request

    async def counting(method, path, **kw):
        calls.append(path)
        return await original(method, path, **kw)

    monkeypatch.setattr(ctx.routing.client, "request", counting)

    for i in range(threshold - 1):
        assert _plan(client, i)["routing"]["source"] == "fallback"
    assert ctx.routing.status()["breaker"] == "closed"            # hələ həddə çatmayıb
    assert _plan(client, threshold)["routing"]["source"] == "fallback"

    st = ctx.routing.status()
    assert st["breaker"] == "open" and st["degraded"] is True and st["provider"] == "fallback_local"
    assert st["fallback_calls"] == threshold
    assert len(calls) == threshold * (settings.external_retries + 1)   # hər uğursuzluq təkrar cəhdlə

    # breaker açıqdır -> xarici API daha çağırılmır, cavab dərhal lokal qurulur
    before = len(calls)
    plan = _plan(client, threshold + 1)
    assert plan["routing"]["source"] == "fallback" and plan["recommended"]["route"]["found"]
    assert len(calls) == before
    health = client.get("/health").json()
    assert health["status"] == "ok" and health["routing"]["breaker"] == "open"


def test_breaker_stays_open_until_reset_time(client, ctx, auth_headers, sim):
    sim["outage"] = "error"
    for i in range(settings.breaker_fail_threshold):
        _plan(client, i)
    _scenario(client, auth_headers, "api_restore")
    assert sim["outage"] == "off"
    # API bərpa olunub, amma reset vaxtı hələ keçməyib
    assert ctx.routing.status()["breaker"] == "open"
    assert _plan(client, 5)["routing"]["source"] == "fallback"


def test_recovery_after_restore_and_reset(client, ctx, auth_headers, sim):
    sim["outage"] = "error"
    for i in range(settings.breaker_fail_threshold):
        _plan(client, i)
    assert ctx.routing.status()["breaker"] == "open"

    _scenario(client, auth_headers, "api_restore")
    ctx.routing.breaker.reset_s = 0                                # "kifayət qədər vaxt keçdi"
    assert ctx.routing.status()["breaker"] == "half_open"

    plan = _plan(client, 0)
    assert plan["routing"] == {"source": "external", "degraded": False}
    st = ctx.routing.status()
    assert st["breaker"] == "closed" and st["provider"] == "external" and st["degraded"] is False
    assert ctx.routing.breaker.failures == 0


def test_half_open_failure_reopens_breaker(client, ctx, sim):
    sim["outage"] = "error"
    for i in range(settings.breaker_fail_threshold):
        _plan(client, i)
    ctx.routing.breaker.reset_s = 0
    assert ctx.routing.breaker.state == "half_open"
    ctx.routing.breaker.reset_s = 60
    ctx.routing.breaker.opened_at -= 61                            # sınaq sorğusuna icazə verilir
    assert ctx.routing.breaker.allow() is True
    assert _plan(client, 4)["routing"]["source"] == "fallback"     # sınaq uğursuzdur
    assert ctx.routing.breaker.state == "open"


def test_known_closures_respected_during_outage(client, ctx, auth_headers, sim):
    """Kəsintidən əvvəl məlum olan bağlı yollar (xarici + dispetçer) fallback marşrutunda da nəzərə alınır."""
    plan, (lat, lon) = plan_with_edges(client)
    ext_edge = plan["recommended"]["route"]["edge_ids"][0]
    client.post(f"{DISPATCH}/demo/scenario", json={"name": "road_close", "edge_id": ext_edge}, headers=auth_headers)
    assert client.portal.call(ctx.routing.sync_traffic) is True

    mid = client.get(EVAC, params={"lat": lat, "lon": lon}).json()
    assert mid["routing"]["source"] == "external" and ext_edge not in all_route_edges(mid)
    manual_edge = next(iter(mid["recommended"]["route"]["edge_ids"]), None)
    if manual_edge:
        client.post(f"{DISPATCH}/roads/{manual_edge}/close", json={}, headers=auth_headers)

    _scenario(client, auth_headers, "api_outage")
    assert client.portal.call(ctx.routing.sync_traffic) is False   # kəsinti: son məlum vəziyyət saxlanılır
    assert ext_edge in ctx.routing.external_closures

    fb = client.get(EVAC, params={"lat": lat, "lon": lon}).json()
    assert fb["routing"]["source"] == "fallback"
    assert fb["recommended"]["route"]["found"] is True
    edges = all_route_edges(fb)
    assert ext_edge not in edges
    if manual_edge:
        assert manual_edge not in edges
    assert fb["closed_roads"] == (2 if manual_edge else 1)


def test_sos_and_status_work_during_outage(client, ctx, sim):
    sim["outage"] = "error"
    r = client.post("/api/v1/sos", json={"citizen_id": cid(1), "type": "fire", "lat": 40.383, "lon": 49.9})
    assert r.status_code == 201
    r = client.post("/api/v1/status", json={"citizen_id": cid(1), "status": "safe", "shelter_id": "sh10"})
    assert r.status_code == 200 and r.json()["resolved_sos"] == 1
    assert client.get("/api/v1/shelters").status_code == 200


def test_reroute_still_works_during_outage(client, ctx, auth_headers, sim, events):
    me = cid(3)
    plan, _ = plan_with_edges(client, me)
    edge = plan["recommended"]["route"]["edge_ids"][0]
    sim["outage"] = "error"
    r = client.post(f"{DISPATCH}/roads/{edge}/close", json={}, headers=auth_headers)
    assert r.status_code == 200 and r.json()["rerouted_citizens"] >= 1
    pushed = [e for e in events if e["event"] == "reroute" and e["citizen_id"] == me][0]["data"]["plan"]
    assert pushed["routing"]["source"] == "fallback" and edge not in all_route_edges(pushed)


def test_invalid_api_key_degrades_gracefully(client, ctx):
    ctx.routing.headers = {"X-API-Key": "wrong-key"}               # 401 -> xəta kimi sayılır
    plan = _plan(client)
    assert plan["routing"]["source"] == "fallback" and plan["recommended"]["route"]["found"]
    assert ctx.routing.breaker.failures == 1


def test_bad_external_response_falls_back(client, ctx, monkeypatch):
    async def short_answer(method, path, **kw):
        return {"routes": []}                                      # təyinat sayı ilə uyğun gəlmir

    monkeypatch.setattr(ctx.routing, "_call", short_answer)
    plan = _plan(client)
    assert plan["routing"]["source"] == "fallback" and plan["recommended"]["route"]["found"]


def test_external_exception_falls_back(client, ctx, monkeypatch):
    async def explode(method, path, **kw):
        raise ConnectionError("network down")

    monkeypatch.setattr(ctx.routing.client, "request", explode)
    plan = _plan(client)
    assert plan["routing"]["source"] == "fallback"
    assert "ConnectionError" in ctx.routing.last_error
    assert client.portal.call(ctx.routing.sync_traffic) is False


def test_route_cache_hits(client, ctx, monkeypatch):
    monkeypatch.setattr(settings, "route_cache_ttl_s", 20.0)
    calls_before = ctx.routing.external_calls
    a = _plan(client, 0)
    b = _plan(client, 0)
    assert ctx.routing.cache_hits == 1 and ctx.routing.external_calls == calls_before + 1
    assert a["recommended"] == b["recommended"]
    # yol bağlananda keş etibarsız olur
    ctx.routing.set_manual_closure("e01", True)
    _plan(client, 0)
    assert ctx.routing.external_calls == calls_before + 2


def test_startup_during_outage_reports_degraded(monkeypatch):
    """Tətbiq xarici API əlçatmaz ikən də işə düşür (ilk sinxronizasiya uğursuz olsa belə)."""
    from fastapi.testclient import TestClient
    from external_api.main import create_external_app
    import httpx
    import app.main as main_mod

    def broken_client():
        ext = create_external_app()
        ext.state.sim["outage"] = "error"
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=ext), base_url="http://external-api")

    monkeypatch.setattr(main_mod, "_external_client", broken_client)
    app = main_mod.create_app(db_path=":memory:", start_background=False)
    with TestClient(app) as c:
        st = c.get("/health").json()["routing"]
        assert st["degraded"] is True and st["provider"] == "fallback_local" and st["last_sync_ok"] is None
        plan = c.get(EVAC, params={"district_id": "khatai"}).json()
        assert plan["routing"]["source"] == "fallback" and plan["recommended"]["route"]["found"]


# ---------------------------------------------------------------------------
# CircuitBreaker — vahid test
# ---------------------------------------------------------------------------
def test_circuit_breaker_state_machine(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(routing_mod.time, "time", lambda: now[0])
    cb = CircuitBreaker(fail_threshold=3, reset_s=15)
    assert cb.state == "closed" and cb.allow()

    cb.failure(); cb.failure()
    assert cb.state == "closed" and cb.allow()                     # hədd - 1
    cb.failure()
    assert cb.state == "open" and not cb.allow()                   # həddə çatdı

    now[0] += 14.9
    assert cb.state == "open" and not cb.allow()
    now[0] += 0.2
    assert cb.state == "half_open" and cb.allow()                  # sınaq sorğusu

    cb.failure()                                                   # sınaq uğursuz -> yenidən açıq
    assert cb.state == "open"
    now[0] += 15.1
    assert cb.state == "half_open"
    cb.success()                                                   # sınaq uğurlu -> bağlı
    assert cb.state == "closed" and cb.failures == 0 and cb.opened_at is None


def test_circuit_breaker_success_resets_failure_count():
    cb = CircuitBreaker(fail_threshold=3, reset_s=15)
    cb.failure(); cb.failure()
    cb.success()
    cb.failure(); cb.failure()
    assert cb.state == "closed"                                    # ardıcıl olmayan xətalar breaker-i açmır
