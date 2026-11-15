"""Challenge Drop 2: Xarici Xəritə API-si sıradan çıxır (HTTP 503 və ya cavab > 5 s).

Tələblər: sistem çökməməli (unhandled exception / 500 yoxdur), circuit breaker / fallback avtomatik işə düşməli,
marşrut lokal ehtiyat bazadan (lokal qraf → son keşlənmiş marşrut → statik koordinat matrisi) hesablanmalı,
istifadəçiyə "Oflayn / Ehtiyat marşrut rejimi" bildirilməli.
"""
import time

import pytest

from app.config import settings

EVAC = "/api/v1/evacuation"
SCEN = "/api/v1/dispatch/demo/scenario"


@pytest.fixture
def fast_timeout(monkeypatch):
    # Testi sürətli saxlamaq üçün: simulyator 8 s gecikir, bizim timeout 0.3 s
    monkeypatch.setattr(settings, "external_timeout_s", 0.3)


def test_timeout_over_5s_falls_back_fast_without_error(client, ctx, sim, fast_timeout):
    sim["outage"] = "timeout"
    started = time.perf_counter()
    r = client.get(EVAC, params={"district_id": "nizami"})
    elapsed = time.perf_counter() - started
    assert r.status_code == 200
    plan = r.json()
    assert plan["routing"]["source"] == "fallback" and plan["routing"]["mode"] == "offline_fallback"
    assert plan["routing"]["tier"] == "local_graph"
    assert plan["recommended"] and plan["recommended"]["route"]["found"]
    assert elapsed < 5.0                     # istifadəçi 5 s gözləmir
    assert "Timeout" in ctx.routing.last_error


def test_timeout_is_not_retried(client, ctx, sim, fast_timeout, monkeypatch):
    monkeypatch.setattr(settings, "external_retries", 3)
    sim["outage"] = "timeout"
    started = time.perf_counter()
    client.get(EVAC, params={"district_id": "khatai"})
    assert time.perf_counter() - started < 1.5   # 4 × 0.3 s deyil, bir timeout


def test_http_503_falls_back_and_reports_reason(client, ctx, sim):
    sim["outage"] = "error"
    plan = client.get(EVAC, params={"district_id": "sabail"}).json()
    assert plan["routing"]["source"] == "fallback"
    assert ctx.routing.last_error == "HTTP 503"


def test_breaker_opens_then_requests_skip_external(client, ctx, sim, fast_timeout):
    sim["outage"] = "timeout"
    for d in ("nizami", "khatai", "sabail", "yasamal"):
        client.get(EVAC, params={"district_id": d})
    assert ctx.routing.breaker.state == "open"
    calls_before = ctx.routing.external_calls
    started = time.perf_counter()
    plan = client.get(EVAC, params={"district_id": "binagadi"}).json()
    assert time.perf_counter() - started < 0.3   # breaker açıq: timeout gözlənilmir
    assert plan["routing"]["source"] == "fallback" and ctx.routing.external_calls == calls_before


@pytest.mark.parametrize("mode", ["error", "timeout"])
def test_no_endpoint_returns_500_during_outage(client, auth_headers, sim, fast_timeout, mode):
    sim["outage"] = mode
    calls = [
        client.get(EVAC, params={"lat": 40.41, "lon": 49.9}),
        client.get(EVAC, params={"district_id": "nasimi", "role": "priority_citizen"}),
        client.get("/api/v1/shelters", params={"lat": 40.4, "lon": 49.85}),
        client.get("/api/v1/roads"),
        client.post("/api/v1/sos", json={"citizen_id": "outage-test-01", "type": "fire", "lat": 40.39, "lon": 49.84}),
        client.post("/api/v1/status", json={"citizen_id": "outage-test-01", "status": "safe", "shelter_id": "sh03"}),
        client.get("/api/v1/dispatch/overview", headers=auth_headers),
        client.post("/api/v1/dispatch/roads/e01/close", headers=auth_headers, json={"reason": "t"}),
        client.get("/health"),
    ]
    assert all(r.status_code < 500 for r in calls), [r.status_code for r in calls]


def test_cached_route_tier_when_local_graph_also_fails(client, ctx, sim, monkeypatch):
    params = {"district_id": "nizami"}
    online = client.get(EVAC, params=params).json()     # son məlum marşrut yaddaşa düşür
    ctx.routing._cache.clear()                          # 20 s-lik təzə keşin vaxtı bitib
    sim["outage"] = "error"
    monkeypatch.setattr(ctx.graph, "shortest_path", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("graph down")))
    plan = client.get(EVAC, params=params).json()
    assert plan["routing"]["tier"] == "cached_route"
    assert plan["recommended"]["shelter_id"] == online["recommended"]["shelter_id"]
    assert plan["recommended"]["route"]["cached_age_s"] >= 0


def test_static_matrix_tier_as_last_resort(client, ctx, sim, monkeypatch):
    sim["outage"] = "error"
    monkeypatch.setattr(ctx.graph, "shortest_path", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("graph down")))
    plan = client.get(EVAC, params={"lat": 40.4321, "lon": 49.8123}).json()
    assert plan["routing"]["tier"] == "static_matrix"
    route = plan["recommended"]["route"]
    assert route["estimate"] is True and route["distance_m"] > 0 and route["duration_min"] > 0


def test_recovery_returns_to_online_and_drops_fallback_cache(client, ctx, sim, auth_headers):
    params = {"district_id": "nizami"}
    client.post(SCEN, headers=auth_headers, json={"name": "api_outage", "mode": "error"})
    assert client.get(EVAC, params=params).json()["routing"]["source"] == "fallback"
    client.post(SCEN, headers=auth_headers, json={"name": "api_restore"})
    plan = client.get(EVAC, params=params).json()
    assert plan["routing"]["source"] == "external" and plan["routing"]["mode"] == "online"


def test_demo_switch_modes_and_health(client, ctx, auth_headers, fast_timeout):
    r = client.post(SCEN, headers=auth_headers, json={"name": "api_outage", "mode": "timeout"}).json()
    assert r["mode"] == "timeout" and r["routing"]["breaker"] == "open" and r["routing"]["mode"] == "offline_fallback"
    h = client.get("/health").json()["routing"]
    assert h["degraded"] and h["degraded_since"] and "tier_calls" in h
    client.post(SCEN, headers=auth_headers, json={"name": "api_restore"})
    r = client.post(SCEN, headers=auth_headers, json={"name": "api_outage", "mode": "error"}).json()
    assert r["mode"] == "error" and r["routing"]["last_error"] == "HTTP 503"
    assert client.post(SCEN, headers=auth_headers, json={"name": "api_outage", "mode": "bogus"}).status_code == 422


def test_priority_citizen_still_gets_accessible_route_from_backup(client, ctx, sim, fast_timeout):
    sim["outage"] = "timeout"
    plan = client.get(EVAC, params={"district_id": "nasimi", "role": "priority_citizen"}).json()
    bad = {e for e, x in ctx.graph.edges.items() if not x.accessible}
    assert plan["routing"]["source"] == "fallback" and plan["recommended"]["accessible"]
    assert not set(plan["recommended"]["route"]["edge_ids"]) & bad
