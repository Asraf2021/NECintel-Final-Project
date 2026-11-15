"""Fon yenidən marşrutlaşdırma planlayıcısı (texniki borc E2) və thread-pool marşrutlaşdırma (E1)."""
import asyncio
import threading

import pytest

from app.services import evacuation
from app.services.replan import ReplanScheduler


class FakeCtx:
    pass


def run(coro):
    return asyncio.run(coro)


def test_inline_mode_runs_synchronously(monkeypatch):
    calls = []

    async def fake(ctx, max_plans=500):
        calls.append(1)
        return 3

    monkeypatch.setattr(evacuation, "reevaluate_plans", fake)

    async def go():
        s = ReplanScheduler(FakeCtx(), inline=True)
        assert await s.request("x") == 3
        return s
    s = run(go())
    assert calls == [1] and s.runs == 1 and s.last_rerouted == 3 and s.status()["mode"] == "inline"


def test_background_mode_returns_immediately_and_coalesces(monkeypatch):
    calls = []

    async def fake(ctx, max_plans=500):
        calls.append(1)
        await asyncio.sleep(0.01)
        return 5

    monkeypatch.setattr(evacuation, "reevaluate_plans", fake)

    async def go():
        s = ReplanScheduler(FakeCtx(), inline=False, debounce_s=0.05)
        s.start()
        results = [await s.request(f"r{i}") for i in range(10)]  # 10 ardıcıl siqnal
        assert results == [None] * 10                              # sorğu gözləmir
        assert calls == []                                         # hələ hesablanmayıb
        await asyncio.sleep(0.2)
        await s.stop()
        return s
    s = run(go())
    assert len(calls) == 1          # 10 siqnal -> 1 hesablama
    assert s.requests == 10 and s.runs == 1 and s.last_rerouted == 5


def test_signal_during_run_triggers_one_more_run(monkeypatch):
    calls = []

    async def fake(ctx, max_plans=500):
        calls.append(1)
        await asyncio.sleep(0.1)
        return 0

    monkeypatch.setattr(evacuation, "reevaluate_plans", fake)

    async def go():
        s = ReplanScheduler(FakeCtx(), inline=False, debounce_s=0.01)
        s.start()
        await s.request("a")
        await asyncio.sleep(0.05)       # hesablama gedir
        await s.request("b")            # bu dəyişiklik itməməlidir
        await asyncio.sleep(0.35)
        await s.stop()
    run(go())
    assert len(calls) == 2


def test_background_worker_survives_errors(monkeypatch):
    calls = []

    async def fake(ctx, max_plans=500):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("boom")
        return 1

    monkeypatch.setattr(evacuation, "reevaluate_plans", fake)

    async def go():
        s = ReplanScheduler(FakeCtx(), inline=False, debounce_s=0.01)
        s.start()
        await s.request("a")
        await asyncio.sleep(0.1)
        await s.request("b")
        await asyncio.sleep(0.1)
        await s.stop()
        return s
    s = run(go())
    assert len(calls) == 2 and s.last_rerouted == 1


def test_health_exposes_replan_status(client):
    h = client.get("/health").json()
    assert h["replan"]["mode"] == "inline"   # testlərdə fon söndürülüb -> sinxron


def test_road_close_reports_done_in_inline_mode(client, auth_headers):
    r = client.post("/api/v1/dispatch/roads/e01/close", headers=auth_headers, json={"reason": "t"}).json()
    assert r["replan"] == "done" and isinstance(r["rerouted_citizens"], int)


def test_fallback_routing_runs_off_event_loop_thread(ctx, client, monkeypatch):
    """Xarici API kəsildikdə lokal Dijkstra event loop thread-ində yox, işçi thread-də işləyir."""
    seen = []
    original = ctx.graph.shortest_path

    def spy(*a, **kw):
        seen.append(threading.current_thread().name)
        return original(*a, **kw)

    monkeypatch.setattr(ctx.graph, "shortest_path", spy)
    ctx.routing.client._transport.app.state.sim["outage"] = "error"
    plan = client.get("/api/v1/evacuation", params={"district_id": "khatai"}).json()
    assert plan["routing"]["source"] == "fallback"
    loop_thread = client.portal.call(lambda: threading.current_thread().name)
    assert seen and all(name != loop_thread for name in seen)


def test_replan_uses_bounded_concurrency(ctx, client, monkeypatch):
    """Eyni anda işləyən build_plan sayı settings.replan_concurrency-dən çox olmur."""
    from app.config import settings
    monkeypatch.setattr(settings, "replan_concurrency", 2)
    for i in range(12):
        client.get("/api/v1/evacuation", params={"lat": 40.39 + i * 0.001, "lon": 49.84, "citizen_id": f"conc-test-{i:04d}"})
    active, peak = 0, 0
    original = evacuation.build_plan

    async def tracked(*a, **kw):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        try:
            await asyncio.sleep(0.005)
            return await original(*a, **kw)
        finally:
            active -= 1

    monkeypatch.setattr(evacuation, "build_plan", tracked)
    ctx.db.execute("UPDATE shelters SET occupancy=capacity")  # hamısı dolu -> hamı təsirlənir
    client.portal.call(evacuation.reevaluate_plans, ctx)
    assert 1 <= peak <= 2
