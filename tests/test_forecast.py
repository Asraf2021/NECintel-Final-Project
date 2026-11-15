"""Time-to-Capacity (doluluq proqnozu) testləri.

Sənəddəki nümunə: tutum 1000, doluluq 780, xalis axın 4 nəfər/dəq -> T90 = 30 dəq, T100 = 55 dəq.
"""
import time

from app.services import forecast
from app.services.forecast import compute

NOW = 100_000.0


def series(rate_per_min, n, step_s, end_occ):
    """Sabit sürətlə artan (ts, doluluq) nöqtələri; sonuncu nöqtə NOW-dadır."""
    return [(NOW - (n - 1 - i) * step_s, round(end_occ - rate_per_min * (n - 1 - i) * step_s / 60)) for i in range(n)]


def test_document_worked_example():
    fc = compute(1000, 780, series(4, 6, 60, 780), NOW)
    assert fc["rate_per_min"] == 4.0
    assert fc["t90_min"] == 30.0 and fc["t100_min"] == 55.0
    assert fc["trend"] == "rising"
    lo, hi = fc["t90_range"]
    assert lo < 30 < hi


def test_confidence_levels():
    assert compute(1000, 780, series(4, 2, 10, 780), NOW)["confidence"] == "low"
    assert compute(1000, 780, series(4, 3, 40, 780), NOW)["confidence"] == "medium"
    assert compute(1000, 780, series(4, 6, 60, 780), NOW)["confidence"] == "high"


def test_high_confidence_has_narrower_interval():
    med = compute(1000, 780, series(4, 3, 40, 780), NOW)
    high = compute(1000, 780, series(4, 6, 60, 780), NOW)
    assert (high["t90_range"][1] - high["t90_range"][0]) < (med["t90_range"][1] - med["t90_range"][0])


def test_no_growth_gives_no_eta():
    fc = compute(1000, 500, [(NOW - 300, 500), (NOW - 120, 500), (NOW, 500)], NOW)
    assert fc["trend"] == "stable" and fc["t90_min"] is None and fc["t100_min"] is None


def test_falling_gives_no_eta():
    fc = compute(1000, 500, [(NOW - 300, 560), (NOW - 120, 530), (NOW, 500)], NOW)
    assert fc["trend"] == "falling" and fc["t90_min"] is None


def test_insufficient_data():
    assert compute(1000, 500, [], NOW)["trend"] == "unknown"
    assert compute(1000, 500, [(NOW, 500)], NOW)["t90_min"] is None


def test_points_outside_window_ignored():
    old = [(NOW - forecast.WINDOW_S - 100, 0)]
    fc = compute(1000, 780, old + series(4, 6, 60, 780), NOW)
    assert fc["rate_per_min"] == 4.0


def test_full_and_above_90():
    assert compute(1000, 1000, series(4, 6, 60, 1000), NOW)["trend"] == "full"
    fc = compute(1000, 950, series(4, 6, 60, 950), NOW)
    assert fc["t90_min"] == 0.0 and fc["t100_min"] == 12.5


def test_reliable_helper():
    assert forecast.reliable(compute(1000, 780, series(4, 6, 60, 780), NOW))
    assert not forecast.reliable(compute(1000, 780, series(4, 2, 10, 780), NOW))
    assert not forecast.reliable(None)


def test_occupancy_changes_are_logged_and_attached(client, ctx, auth_headers):
    client.patch("/api/v1/dispatch/shelters/sh05", headers=auth_headers, json={"occupancy": 400})
    client.patch("/api/v1/dispatch/shelters/sh05", headers=auth_headers, json={"occupancy": 450})
    n = ctx.db.scalar("SELECT COUNT(*) FROM shelter_occupancy_log WHERE shelter_id='sh05'")
    assert n == 2
    ov = client.get("/api/v1/dispatch/overview", headers=auth_headers).json()
    sh = next(s for s in ov["shelters"] if s["id"] == "sh05")
    assert "forecast" in sh and sh["forecast"]["samples"] == 2


def _seed_growth(ctx, shelter_id, capacity, start_pct, rate_per_min):
    """Son 5 dəqiqə üçün sabit artım tarixçəsi yazır və cari doluluğu yeniləyir."""
    now = time.time()
    start = int(capacity * start_pct)
    for i in range(6):
        occ = start + round(rate_per_min * i)
        ctx.db.execute("INSERT INTO shelter_occupancy_log (shelter_id, ts, occupancy) VALUES (?,?,?)",
                       (shelter_id, now - (5 - i) * 60, occ))
    ctx.db.execute("UPDATE shelters SET occupancy=? WHERE id=?", (start + round(rate_per_min * 5), shelter_id))


def test_recommendation_avoids_shelter_that_fills_before_arrival(client, ctx):
    base = client.get("/api/v1/evacuation", params={"district_id": "nasimi"}).json()
    rec = base["recommended"]
    sh = next(s for s in base["shelters"] if s["id"] == rec["shelter_id"])
    eta = rec["route"]["duration_min"]
    # Hazırda <90%, amma sürətli axınla vətəndaş çatana qədər 90%-i keçəcək
    gap = 0.9 * sh["capacity"] - sh["capacity"] * 0.5
    rate = gap / max(1.0, eta / 3) / 1.0
    _seed_growth(ctx, sh["id"], sh["capacity"], 0.5 - rate * 5 / sh["capacity"], rate)
    plan = client.get("/api/v1/evacuation", params={"district_id": "nasimi"}).json()
    target = next(s for s in plan["shelters"] if s["id"] == sh["id"])
    assert target["accepting"] is True
    assert target["fills_before_arrival"] is True
    assert plan["recommended"]["shelter_id"] != sh["id"]
    assert plan["recommended"]["reason"] == "forecast_full"


def test_slow_growth_does_not_change_recommendation(client, ctx):
    base = client.get("/api/v1/evacuation", params={"district_id": "nasimi"}).json()
    sid = base["recommended"]["shelter_id"]
    sh = next(s for s in base["shelters"] if s["id"] == sid)
    _seed_growth(ctx, sid, sh["capacity"], 0.2, 0.2)  # çox yavaş axın
    plan = client.get("/api/v1/evacuation", params={"district_id": "nasimi"}).json()
    assert plan["recommended"]["shelter_id"] == sid
