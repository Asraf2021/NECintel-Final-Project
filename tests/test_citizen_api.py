"""Modul 1 — Vətəndaş API-si: meta, məntəqələr, təxliyə planı, avtomatik yenidən marşrut, SOS, status."""
from __future__ import annotations

import math

import pytest

from app.config import settings
from app.services import evacuation

from .helpers import DISPATCH, EVAC, SOS, STATUS, all_route_edges, cid, plan_with_edges, send_sos, shelter_row


# ---------------------------------------------------------------------------
# /meta və /shelters
# ---------------------------------------------------------------------------
def test_meta(client):
    r = client.get("/api/v1/meta")
    assert r.status_code == 200
    body = r.json()
    assert len(body["districts"]) == 8
    assert set(body["sos_types"]) == {"fire", "collapse", "flood", "medical"}
    assert body["thresholds"] == {"reroute_pct": 90.0, "critical_pct": 95.0}
    assert body["city"]["name"] == "Bakı"


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
    assert r.json()["routing"]["provider"] == "external"


def test_shelters_only_open_with_occupancy_pct(client, ctx):
    r = client.get("/api/v1/shelters")
    assert r.status_code == 200
    items = r.json()["shelters"]
    open_count = ctx.db.scalar("SELECT COUNT(*) FROM shelters WHERE status='open'")
    assert len(items) == open_count > 0
    for s in items:
        assert s["status"] == "open"
        assert s["occupancy_pct"] == pytest.approx(s["occupancy"] * 100 / s["capacity"], abs=0.05)
        assert s["level"] in {"low", "medium", "high", "full", "critical"}
        assert isinstance(s["accepting"], bool)


def test_shelters_filter_by_district(client):
    items = client.get("/api/v1/shelters", params={"district_id": "khatai"}).json()["shelters"]
    assert items and all(s["district_id"] == "khatai" for s in items)


def test_shelters_unknown_district_404(client):
    assert client.get("/api/v1/shelters", params={"district_id": "atlantis"}).status_code == 404


def test_shelters_sorted_by_distance(client):
    items = client.get("/api/v1/shelters", params={"lat": 40.383, "lon": 49.9}).json()["shelters"]
    dist = [s["distance_m"] for s in items]
    assert dist == sorted(dist)
    assert items[0]["id"] == "sh10"  # Xətai mərkəzinə ən yaxın


def test_shelters_limit_and_validation(client):
    assert len(client.get("/api/v1/shelters", params={"limit": 3}).json()["shelters"]) == 3
    assert client.get("/api/v1/shelters", params={"lat": 123, "lon": 49.9}).status_code == 422
    assert client.get("/api/v1/shelters", params={"limit": 0}).status_code == 422


def test_reserve_shelter_appears_after_opening(client, auth_headers):
    ids = {s["id"] for s in client.get("/api/v1/shelters").json()["shelters"]}
    assert "sh18" not in ids
    r = client.patch(f"{DISPATCH}/shelters/sh18", json={"status": "open"}, headers=auth_headers)
    assert r.status_code == 200
    ids = {s["id"] for s in client.get("/api/v1/shelters").json()["shelters"]}
    assert "sh18" in ids


# ---------------------------------------------------------------------------
# /evacuation
# ---------------------------------------------------------------------------
def test_evacuation_by_district(client):
    r = client.get(EVAC, params={"district_id": "khatai"})
    assert r.status_code == 200
    plan = r.json()
    assert plan["origin"]["district_id"] == "khatai"
    assert 1 <= len(plan["shelters"]) <= settings.nearest_shelters_limit
    for s in plan["shelters"]:
        assert "occupancy_pct" in s and "distance_m" in s
        assert s["status"] == "open"
    dist = [s["distance_m"] for s in plan["shelters"]]
    assert dist == sorted(dist)
    rec = plan["recommended"]
    assert rec["shelter_id"] in {s["id"] for s in plan["shelters"]}
    assert rec["route"]["found"] is True
    assert rec["route"]["duration_min"] > 0 and len(rec["route"]["points"]) >= 2
    assert {k: plan["routing"][k] for k in ("source", "degraded")} == {"source": "external", "degraded": False}
    assert plan["rerouted"] is False


def test_evacuation_by_coordinates(client):
    r = client.get(EVAC, params={"lat": 40.41, "lon": 49.90})
    assert r.status_code == 200
    plan = r.json()
    assert plan["origin"]["lat"] == 40.41 and plan["origin"]["lon"] == 49.90
    assert plan["recommended"]["route"]["found"] is True
    # tövsiyə olunan — qəbul edən məntəqələr arasında ən qısa müddətlidir
    accepting = [s for s in plan["shelters"] if s["accepting"] and s["route"]]
    best = min(accepting, key=lambda s: s["route"]["duration_min"])
    assert plan["recommended"]["route"]["duration_min"] <= best["route"]["duration_min"]


def test_evacuation_route_has_road_segments(client):
    plan, _ = plan_with_edges(client)
    route = plan["recommended"]["route"]
    assert route["edge_ids"] and route["steps"]
    assert route["distance_m"] > 250


@pytest.mark.parametrize("params", [
    {},
    {"lat": 40.4},
    {"lon": 49.9},
    {"district_id": "atlantis"},
    {"lat": 95, "lon": 49.9},
    {"lat": 40.4, "lon": 181},
    {"lat": "abc", "lon": 49.9},
    {"district_id": "khatai", "citizen_id": "short"},
    {"district_id": "khatai", "citizen_id": "bad id with spaces!"},
])
def test_evacuation_invalid_input_422(client, params):
    assert client.get(EVAC, params=params).status_code == 422


def test_evacuation_stores_plan_for_citizen(client, ctx):
    client.get(EVAC, params={"district_id": "khatai", "citizen_id": cid(1)})
    row = ctx.db.one("SELECT * FROM evacuation_plans WHERE citizen_id=?", (cid(1),))
    assert row and row["shelter_id"] == "sh10"
    # citizen_id olmadan plan saxlanılmır
    client.get(EVAC, params={"district_id": "nizami"})
    assert ctx.db.scalar("SELECT COUNT(*) FROM evacuation_plans") == 1


def test_recommended_never_full_when_alternatives_exist(client, ctx, auth_headers):
    """Hər rayonda ən yaxın məntəqə 90%-ə doldurulur -> tövsiyə başqa (< 90%) məntəqə olmalıdır."""
    for d in ctx.districts:
        nearest = client.get(EVAC, params={"district_id": d["id"]}).json()["shelters"][0]
        cap = shelter_row(ctx, nearest["id"])["capacity"]
        r = client.patch(f"{DISPATCH}/shelters/{nearest['id']}",
                         json={"occupancy": math.ceil(cap * 0.90)}, headers=auth_headers)
        assert r.status_code == 200 and r.json()["accepting"] is False
        plan = client.get(EVAC, params={"district_id": d["id"]}).json()
        rec = plan["recommended"]
        assert rec["shelter_id"] != nearest["id"]
        chosen = shelter_row(ctx, rec["shelter_id"])
        assert chosen["occupancy"] * 100 / chosen["capacity"] < 90
        assert rec["reason"] == "nearest_full"
        assert rec["skipped"][0]["shelter_id"] == nearest["id"]
        assert rec["skipped"][0]["reason"] == "shelter_full"


def test_just_below_90_pct_is_still_recommended(client, ctx, auth_headers):
    # sh10: 2200 yer -> 1979 = 89.95% (< 90) hələ qəbul edir
    r = client.patch(f"{DISPATCH}/shelters/sh10", json={"occupancy": 1970}, headers=auth_headers)  # 89.5%
    assert r.json()["accepting"] is True
    assert client.get(EVAC, params={"district_id": "khatai"}).json()["recommended"]["shelter_id"] == "sh10"


def test_all_shelters_full_recommends_least_occupied(client, ctx):
    ctx.db.execute("UPDATE shelters SET occupancy=CAST(capacity*0.93 AS INTEGER) WHERE status='open'")
    ctx.db.execute("UPDATE shelters SET occupancy=CAST(capacity*0.91 AS INTEGER) WHERE id='sh09'")
    plan = client.get(EVAC, params={"district_id": "khatai"}).json()
    assert plan["recommended"]["reason"] == "all_full"
    assert plan["recommended"]["shelter_id"] == "sh09"


def test_reroute_threshold_not_triggered_below_90_pct(client, auth_headers):
    # sh01: 6000 yer, 5398 nəfər = 89.97% (< 90%) -> hələ qəbul etməlidir
    r = client.patch(f"{DISPATCH}/shelters/sh01", json={"occupancy": 5398}, headers=auth_headers)
    assert 5398 * 100 / 6000 < 90
    assert r.json()["accepting"] is True


# ---------------------------------------------------------------------------
# Avtomatik yenidən marşrutlaşdırma
# ---------------------------------------------------------------------------
def test_reroute_when_recommended_shelter_fills(client, ctx, auth_headers, events):
    me, other = cid(1), cid(2)
    first = client.get(EVAC, params={"district_id": "khatai", "citizen_id": me}).json()
    client.get(EVAC, params={"district_id": "binagadi", "citizen_id": other})  # təsirlənməyən vətəndaş
    target = first["recommended"]["shelter_id"]
    cap = shelter_row(ctx, target)["capacity"]

    r = client.patch(f"{DISPATCH}/shelters/{target}", json={"occupancy": math.ceil(cap * 0.90)}, headers=auth_headers)
    assert r.status_code == 200
    assert r.json()["occupancy_pct"] >= 90 and r.json()["accepting"] is False

    # hadisə yalnız təsirlənən vətəndaşa göndərilir
    reroutes = [e for e in events if e["event"] == "reroute"]
    assert [e["citizen_id"] for e in reroutes] == [me]
    pushed = reroutes[0]["data"]["plan"]
    assert pushed["rerouted"] is True and pushed["reroute_reason"] == "shelter_full"
    assert pushed["previous_shelter"]["id"] == target
    assert pushed["recommended"]["shelter_id"] != target
    assert any(e["event"] == "reroute_batch" and e["channel"] == "ops" and e["data"]["count"] == 1 for e in events)

    # növbəti plan fərqli məntəqə tövsiyə edir
    second = client.get(EVAC, params={"district_id": "khatai", "citizen_id": me}).json()
    assert second["recommended"]["shelter_id"] != target
    assert second["recommended"]["shelter_id"] == pushed["recommended"]["shelter_id"]
    assert ctx.db.one("SELECT shelter_id FROM evacuation_plans WHERE citizen_id=?", (me,))["shelter_id"] != target


def test_no_reroute_below_90_pct(client, ctx, auth_headers, events):
    first = client.get(EVAC, params={"district_id": "khatai", "citizen_id": cid(1)}).json()
    target = first["recommended"]["shelter_id"]
    cap = shelter_row(ctx, target)["capacity"]
    client.patch(f"{DISPATCH}/shelters/{target}", json={"occupancy": int(cap * 0.85)}, headers=auth_headers)
    assert not [e for e in events if e["event"] == "reroute"]
    assert client.get(EVAC, params={"district_id": "khatai"}).json()["recommended"]["shelter_id"] == target


def test_reroute_when_road_on_route_is_closed(client, ctx, auth_headers, events):
    me = cid(7)
    plan, (lat, lon) = plan_with_edges(client, me)
    edge = plan["recommended"]["route"]["edge_ids"][0]

    r = client.post(f"{DISPATCH}/roads/{edge}/close", json={"reason": "Qəza"}, headers=auth_headers)
    assert r.status_code == 200
    assert r.json()["ok"] is True and r.json()["rerouted_citizens"] >= 1

    reroutes = [e for e in events if e["event"] == "reroute" and e["citizen_id"] == me]
    assert len(reroutes) == 1
    assert reroutes[0]["data"]["plan"]["reroute_reason"] == "road_closed"
    assert edge not in all_route_edges(reroutes[0]["data"]["plan"])
    assert any(e["event"] == "road_update" and e["channel"] == "public" for e in events)

    new = client.get(EVAC, params={"lat": lat, "lon": lon, "citizen_id": me}).json()
    assert new["recommended"]["route"]["found"] is True
    assert edge not in new["recommended"]["route"]["edge_ids"]
    assert edge not in all_route_edges(new)
    assert new["closed_roads"] == 1

    roads = client.get("/api/v1/roads").json()
    assert [c["edge_id"] for c in roads["closures"]] == [edge]
    assert roads["closures"][0]["source"] == "dispatcher" and roads["closures"][0]["reason"] == "Qəza"


def test_road_close_without_affected_plans_reroutes_nobody(client, ctx, auth_headers):
    plan, _ = plan_with_edges(client, cid(7))
    used = set(plan["recommended"]["route"]["edge_ids"])
    unrelated = next(e for e in ctx.graph.edges if e not in used)
    r = client.post(f"{DISPATCH}/roads/{unrelated}/close", json={}, headers=auth_headers)
    assert r.json()["rerouted_citizens"] == 0


def test_road_reopen_restores_route(client, auth_headers):
    plan, (lat, lon) = plan_with_edges(client)
    edges = plan["recommended"]["route"]["edge_ids"]
    client.post(f"{DISPATCH}/roads/{edges[0]}/close", json={}, headers=auth_headers)
    r = client.post(f"{DISPATCH}/roads/{edges[0]}/open", headers=auth_headers)
    assert r.status_code == 200
    again = client.get(EVAC, params={"lat": lat, "lon": lon}).json()
    assert again["recommended"]["route"]["edge_ids"] == edges
    assert client.get("/api/v1/roads").json()["closures"] == []


def test_external_road_closure_is_synced_and_avoided(client, ctx, sim):
    """Xarici API yolu bağlayır -> sinxronizasiyadan sonra marşrut həmin yoldan keçmir."""
    me = cid(9)
    plan, (lat, lon) = plan_with_edges(client, me)
    edge = plan["recommended"]["route"]["edge_ids"][0]
    # fon dövrü yoxdur: xarici tərəfdə yolu bağlayıb sinxronizasiyanı özümüz çağırırıq
    client.portal.call(ctx.routing.sim, f"roads/{edge}/close", {"reason": "Qəza"})
    assert edge in sim["closures"]
    assert edge not in ctx.routing.closed_edges                      # hələ sinxronlaşmayıb

    assert client.portal.call(ctx.routing.sync_traffic) is True      # dəyişiklik aşkarlandı
    assert client.portal.call(ctx.routing.sync_traffic) is False     # ikinci dəfə dəyişiklik yoxdur
    assert edge in ctx.routing.closed_edges
    assert client.portal.call(evacuation.reevaluate_plans, ctx) >= 1

    new = client.get(EVAC, params={"lat": lat, "lon": lon, "citizen_id": me}).json()
    assert edge not in all_route_edges(new)
    closures = client.get("/api/v1/roads").json()["closures"]
    assert closures[0]["edge_id"] == edge and closures[0]["source"] == "external"
    assert closures[0]["reason"] == "Qəza"


def test_demo_road_close_scenario_reroutes_citizen(client, ctx, auth_headers, sim, events):
    """Demo ssenari ilə (xarici tərəfdə) bağlanan yol vətəndaşın marşrutundan çıxarılır."""
    me = cid(9)
    plan, (lat, lon) = plan_with_edges(client, me)
    edge = plan["recommended"]["route"]["edge_ids"][0]
    r = client.post(f"{DISPATCH}/demo/scenario", json={"name": "road_close", "edge_id": edge}, headers=auth_headers)
    assert r.status_code == 200 and r.json()["edge_id"] == edge
    assert edge in sim["closures"]
    # ssenari özü sinxronlaşdıra bilər; etməyibsə, burada edirik
    client.portal.call(ctx.routing.sync_traffic)
    rerouted = r.json().get("rerouted", 0) + client.portal.call(evacuation.reevaluate_plans, ctx)
    assert rerouted >= 1
    assert edge in ctx.routing.closed_edges
    assert [e["citizen_id"] for e in events if e["event"] == "reroute"] == [me]
    new = client.get(EVAC, params={"lat": lat, "lon": lon, "citizen_id": me}).json()
    assert edge not in all_route_edges(new)


# ---------------------------------------------------------------------------
# SOS
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("sos_type", ["fire", "collapse", "flood", "medical"])
def test_sos_created_for_each_type(client, ctx, sos_type):
    r = send_sos(client, cid(1), sos_type, 40.383, 49.9, "Kömək lazımdır")
    assert r.status_code == 201
    body = r.json()
    assert body["duplicate"] is False
    sos = body["sos"]
    assert sos["type"] == sos_type and sos["status"] == "active"
    assert sos["district_id"] == "khatai" and sos["district_name"] == "Xətai"
    assert sos["note"] == "Kömək lazımdır"
    assert ctx.db.scalar("SELECT COUNT(*) FROM sos_signals") == 1


@pytest.mark.parametrize("payload", [
    {"citizen_id": "citizen-0001", "type": "earthquake", "lat": 40.4, "lon": 49.9},
    {"citizen_id": "citizen-0001", "type": "fire", "lat": 91, "lon": 49.9},
    {"citizen_id": "citizen-0001", "type": "fire", "lat": -90.5, "lon": 49.9},
    {"citizen_id": "citizen-0001", "type": "fire", "lat": 40.4, "lon": 180.1},
    {"citizen_id": "citizen-0001", "type": "fire", "lat": "north", "lon": 49.9},
    {"citizen_id": "short", "type": "fire", "lat": 40.4, "lon": 49.9},
    {"citizen_id": "bad id!!", "type": "fire", "lat": 40.4, "lon": 49.9},
    {"citizen_id": "x" * 65, "type": "fire", "lat": 40.4, "lon": 49.9},
    {"citizen_id": "citizen-0001", "type": "fire", "lat": 40.4, "lon": 49.9, "note": "x" * 281},
    {"citizen_id": "citizen-0001", "type": "fire"},
    {"type": "fire", "lat": 40.4, "lon": 49.9},
    {},
])
def test_sos_invalid_payload_422(client, ctx, payload):
    assert client.post(SOS, json=payload).status_code == 422
    assert ctx.db.scalar("SELECT COUNT(*) FROM sos_signals") == 0


def test_sos_malformed_json_422(client):
    r = client.post(SOS, content=b"{not json", headers={"Content-Type": "application/json"})
    assert r.status_code == 422


def test_sos_duplicate_click_is_idempotent(client, ctx):
    first = send_sos(client, cid(1), "fire").json()
    second = send_sos(client, cid(1), "fire")
    assert second.status_code == 201
    assert second.json()["duplicate"] is True
    assert second.json()["sos"]["id"] == first["sos"]["id"]
    assert ctx.db.scalar("SELECT COUNT(*) FROM sos_signals") == 1


def test_sos_different_type_or_citizen_is_not_duplicate(client, ctx):
    send_sos(client, cid(1), "fire")
    assert send_sos(client, cid(1), "flood").json()["duplicate"] is False
    assert send_sos(client, cid(2), "fire").json()["duplicate"] is False
    assert ctx.db.scalar("SELECT COUNT(*) FROM sos_signals") == 3


def test_sos_after_dedupe_window_creates_new_row(client, ctx):
    send_sos(client, cid(1), "fire")
    ctx.db.execute("UPDATE sos_signals SET created_at=created_at-31")  # 30 s pəncərədən köhnə
    assert send_sos(client, cid(1), "fire").json()["duplicate"] is False
    assert ctx.db.scalar("SELECT COUNT(*) FROM sos_signals") == 2


def test_sos_publishes_ops_event(client, events):
    send_sos(client, cid(1), "medical")
    ev = [e for e in events if e["event"] == "sos_new"]
    assert len(ev) == 1 and ev[0]["channel"] == "ops" and ev[0]["citizen_id"] is None


# ---------------------------------------------------------------------------
# "Təhlükəsizəm" statusu
# ---------------------------------------------------------------------------
def test_safe_status_increments_shelter_exactly_once(client, ctx):
    me = cid(1)
    send_sos(client, me, "fire")
    send_sos(client, me, "medical")
    before = shelter_row(ctx, "sh09")["occupancy"]

    r = client.post(STATUS, json={"citizen_id": me, "status": "safe", "shelter_id": "sh09"})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "safe" and body["resolved_sos"] == 2
    assert body["shelter"]["id"] == "sh09" and body["shelter"]["occupancy"] == before + 1
    assert shelter_row(ctx, "sh09")["occupancy"] == before + 1

    # təkrar klik doluluğu artırmır
    again = client.post(STATUS, json={"citizen_id": me, "status": "safe", "shelter_id": "sh09"})
    assert again.status_code == 200 and again.json()["shelter"] is None
    assert shelter_row(ctx, "sh09")["occupancy"] == before + 1

    # aktiv SOS-lar "safe" olur
    statuses = {s["status"] for s in ctx.db.query("SELECT status FROM sos_signals WHERE citizen_id=?", (me,))}
    assert statuses == {"safe"}

    state = client.get(f"/api/v1/citizens/{me}").json()
    assert state == {"citizen_id": me, "status": "safe", "shelter_id": "sh09", "active_sos": [], "role": "citizen"}


def test_safe_status_moves_between_shelters(client, ctx):
    me = cid(1)
    a0, b0 = shelter_row(ctx, "sh09")["occupancy"], shelter_row(ctx, "sh10")["occupancy"]
    client.post(STATUS, json={"citizen_id": me, "status": "safe", "shelter_id": "sh09"})
    client.post(STATUS, json={"citizen_id": me, "status": "safe", "shelter_id": "sh10"})
    assert shelter_row(ctx, "sh09")["occupancy"] == a0
    assert shelter_row(ctx, "sh10")["occupancy"] == b0 + 1


def test_safe_status_without_or_unknown_shelter(client, ctx):
    total = ctx.db.scalar("SELECT SUM(occupancy) FROM shelters")
    assert client.post(STATUS, json={"citizen_id": cid(1), "status": "safe"}).json()["shelter"] is None
    r = client.post(STATUS, json={"citizen_id": cid(2), "status": "safe", "shelter_id": "nope"})
    assert r.status_code == 200 and r.json()["shelter"] is None
    # ehtiyat (açılmamış) məntəqəyə giriş sayılmır
    r = client.post(STATUS, json={"citizen_id": cid(3), "status": "safe", "shelter_id": "sh18"})
    assert r.json()["shelter"] is None
    assert ctx.db.scalar("SELECT SUM(occupancy) FROM shelters") == total


def test_safe_status_removes_stored_plan(client, ctx):
    client.get(EVAC, params={"district_id": "khatai", "citizen_id": cid(1)})
    client.post(STATUS, json={"citizen_id": cid(1), "status": "safe", "shelter_id": "sh10"})
    assert ctx.db.scalar("SELECT COUNT(*) FROM evacuation_plans") == 0


def test_evacuating_status_does_not_touch_shelter(client, ctx):
    before = shelter_row(ctx, "sh09")["occupancy"]
    r = client.post(STATUS, json={"citizen_id": cid(1), "status": "evacuating", "shelter_id": "sh09"})
    assert r.status_code == 200
    assert shelter_row(ctx, "sh09")["occupancy"] == before
    assert client.get(f"/api/v1/citizens/{cid(1)}").json()["status"] == "evacuating"


@pytest.mark.parametrize("payload", [
    {"citizen_id": "citizen-0001", "status": "dead"},
    {"citizen_id": "bad", "status": "safe"},
    {"citizen_id": "citizen-0001", "status": "safe", "shelter_id": "x" * 17},
    {"citizen_id": "citizen-0001", "status": "safe", "lat": 100},
    {"status": "safe"},
])
def test_status_invalid_payload_422(client, payload):
    assert client.post(STATUS, json=payload).status_code == 422


def test_citizen_state(client):
    me = cid(5)
    assert client.get(f"/api/v1/citizens/{me}").json() == {
        "citizen_id": me, "status": "unknown", "shelter_id": None, "active_sos": [], "role": "citizen"}
    sos_id = send_sos(client, me, "flood").json()["sos"]["id"]
    state = client.get(f"/api/v1/citizens/{me}").json()
    assert state["status"] == "sos"
    assert [s["id"] for s in state["active_sos"]] == [sos_id]
    assert client.get("/api/v1/citizens/bad!id").status_code == 422


def test_safe_checkin_reaching_90_pct_reroutes_others(client, ctx, events):
    """Vətəndaşın girişi məntəqəni 90%-ə çatdırırsa, həmin məntəqəyə gedənlər yenidən marşrutlanır."""
    walker = cid(1)
    first = client.get(EVAC, params={"district_id": "khatai", "citizen_id": walker}).json()
    target = first["recommended"]["shelter_id"]
    cap = shelter_row(ctx, target)["capacity"]
    ctx.db.execute("UPDATE shelters SET occupancy=? WHERE id=?", (math.ceil(cap * 0.90) - 1, target))
    r = client.post(STATUS, json={"citizen_id": cid(2), "status": "safe", "shelter_id": target})
    assert r.json()["shelter"]["accepting"] is False
    assert [e["citizen_id"] for e in events if e["event"] == "reroute"] == [walker]


def test_safe_sos_safe_does_not_double_count(client, ctx):
    me = cid(1)
    before = shelter_row(ctx, "sh09")["occupancy"]
    client.post(STATUS, json={"citizen_id": me, "status": "safe", "shelter_id": "sh09"})
    send_sos(client, me, "medical")
    client.post(STATUS, json={"citizen_id": me, "status": "safe", "shelter_id": "sh09"})
    assert shelter_row(ctx, "sh09")["occupancy"] == before + 1
