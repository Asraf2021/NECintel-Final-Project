"""Modul 2 — Dispetçer API-si: icmal, SOS idarəsi, briqadalar, yollar, audit, demo ssenarilər."""
from __future__ import annotations

from app.services import analytics

from .helpers import DISPATCH, STATUS, alerts, cid, district_point, insert_sos, send_sos, shelter_row


def _overview(client, headers):
    r = client.get(f"{DISPATCH}/overview", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def test_overview_structure(client, ctx, auth_headers):
    ov = _overview(client, auth_headers)
    for key in ("kpi", "zones", "sos", "shelters", "brigades", "alerts", "closures", "routing", "timeline",
                "by_type", "thresholds", "generated_at"):
        assert key in ov
    assert len(ov["zones"]) == 8
    assert len(ov["timeline"]) == 30 and sum(ov["timeline"]) == 0
    assert len(ov["shelters"]) == 25            # ehtiyat məntəqələr də daxildir
    assert len(ov["brigades"]) == 8
    assert ov["alerts"] == []
    assert ov["routing"]["provider"] == "external" and ov["routing"]["breaker"] == "closed"
    kpi = ov["kpi"]
    assert kpi["sos_active"] == 0 and kpi["alerts_pending"] == 0
    assert kpi["shelters_open"] == 17 and kpi["shelters_reserve"] == 8
    assert kpi["brigades_total"] == 8 and kpi["brigades_available"] == 7
    assert kpi["roads_closed"] == 0 and kpi["citizens_safe"] == 0
    assert kpi["total_capacity"] == ctx.db.scalar("SELECT SUM(capacity) FROM shelters WHERE status='open'")
    assert kpi["total_occupancy"] == ctx.db.scalar("SELECT SUM(occupancy) FROM shelters WHERE status='open'")
    assert ov["thresholds"] == {"reroute_pct": 90.0, "critical_pct": 95.0, "cluster_count": 4,
                                "cluster_window_s": 300, "cluster_radius_m": 500.0}
    for z in ov["zones"]:
        assert z["risk"] in {"low", "medium", "high", "critical"}
        assert {"district_id", "name", "sos_active", "sos_window", "occupancy_pct", "brigades_available"} <= set(z)


def test_overview_reacts_to_new_sos(client, ctx, auth_headers):
    before = _overview(client, auth_headers)
    lat, lon = district_point(ctx, "nizami")
    sos = send_sos(client, cid(1), "medical", lat, lon).json()["sos"]
    after = _overview(client, auth_headers)

    assert after["kpi"]["sos_active"] == before["kpi"]["sos_active"] + 1
    assert after["kpi"]["sos_window"] == 1
    assert sum(after["timeline"]) == 1 and after["timeline"][-1] == 1      # son dəqiqənin səbəti
    assert after["by_type"]["medical"] == 1
    assert after["sos"][0]["id"] == sos["id"]
    zone = next(z for z in after["zones"] if z["district_id"] == "nizami")
    assert zone["sos_active"] == 1 and zone["by_type"] == {"medical": 1} and zone["risk"] == "medium"
    others = [z for z in after["zones"] if z["district_id"] != "nizami"]
    assert all(z["sos_active"] == 0 for z in others)


def test_overview_zone_risk_escalates_with_cluster(client, ctx, auth_headers):
    for n in (1, 2):
        send_sos(client, cid(n), "fire", *district_point(ctx, "nizami", n))
    zone = next(z for z in _overview(client, auth_headers)["zones"] if z["district_id"] == "nizami")
    assert zone["risk"] == "high" and zone["sos_window"] == 2
    for n in (3, 4):
        send_sos(client, cid(n), "fire", *district_point(ctx, "nizami", n))
    ov = _overview(client, auth_headers)
    zone = next(z for z in ov["zones"] if z["district_id"] == "nizami")
    assert zone["risk"] == "critical"
    assert ov["kpi"]["alerts_pending"] == 1 and ov["alerts"][0]["kind"] == "sos_cluster"


def test_overview_reacts_to_shelter_road_and_safe_status(client, ctx, auth_headers):
    client.patch(f"{DISPATCH}/shelters/sh07", json={"occupancy": 700}, headers=auth_headers)
    client.post(f"{DISPATCH}/roads/e01/close", json={"reason": "Təmir"}, headers=auth_headers)
    client.post(STATUS, json={"citizen_id": cid(1), "status": "safe", "shelter_id": "sh09"})
    ov = _overview(client, auth_headers)
    assert ov["kpi"]["shelters_full"] == 1
    assert ov["kpi"]["roads_closed"] == 1 and ov["closures"][0]["edge_id"] == "e01"
    assert ov["kpi"]["citizens_safe"] == 1
    assert ov["kpi"]["alerts_pending"] == 1


def test_overview_timeline_buckets_old_signals(client, ctx, auth_headers):
    insert_sos(ctx, cid(1), 40.383, 49.9, age_s=10 * 60 + 5)     # 10 dəq əvvəl
    insert_sos(ctx, cid(2), 40.383, 49.9, age_s=31 * 60)         # 30 dəq pəncərədən kənar
    analytics.invalidate()
    tl = _overview(client, auth_headers)["timeline"]
    assert sum(tl) == 1 and tl[19] == 1


def test_overview_accessible_to_admin(client, admin_headers):
    assert client.get(f"{DISPATCH}/overview", headers=admin_headers).status_code == 200


# ----- SOS idarəsi -----
def test_sos_list_and_status_filter(client, auth_headers):
    send_sos(client, cid(1), "fire")
    send_sos(client, cid(2), "flood")
    client.post(STATUS, json={"citizen_id": cid(2), "status": "safe"})
    all_ = client.get(f"{DISPATCH}/sos", headers=auth_headers).json()["sos"]
    assert len(all_) == 2
    active = client.get(f"{DISPATCH}/sos", params={"status": "active"}, headers=auth_headers).json()["sos"]
    assert [s["citizen_id"] for s in active] == [cid(1)]
    safe = client.get(f"{DISPATCH}/sos", params={"status": "safe"}, headers=auth_headers).json()["sos"]
    assert [s["citizen_id"] for s in safe] == [cid(2)]
    assert client.get(f"{DISPATCH}/sos", params={"status": "bogus"}, headers=auth_headers).status_code == 422


def test_sos_assign_nearest_preferred_brigade_and_resolve(client, ctx, auth_headers, events):
    sos = send_sos(client, cid(1), "medical", 40.404, 49.865).json()["sos"]     # br03 (tibbi) yanında
    r = client.post(f"{DISPATCH}/sos/{sos['id']}/assign", json={}, headers=auth_headers)
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "assigned" and body["brigade_id"] == "br03"
    assert body["brigade_name"] and body["brigade_distance_m"] < 100
    assert ctx.db.one("SELECT * FROM brigades WHERE id='br03'")["status"] == "dispatched"
    assigned = [e for e in events if e["event"] == "sos_assigned"]
    assert len(assigned) == 1 and assigned[0]["citizen_id"] == cid(1)
    state = client.get(f"/api/v1/citizens/{cid(1)}").json()
    assert state["active_sos"][0]["brigade_name"] == body["brigade_name"]

    r = client.post(f"{DISPATCH}/sos/{sos['id']}/resolve", headers=auth_headers)
    assert r.status_code == 200 and r.json()["status"] == "resolved"
    b = ctx.db.one("SELECT * FROM brigades WHERE id='br03'")
    assert b["status"] == "available" and b["assigned_sos_id"] is None
    actions = [a["action"] for a in ctx.db.query("SELECT action FROM audit_log ORDER BY id")]
    assert "sos_assign" in actions and "sos_resolve" in actions


def test_sos_assign_specific_and_unavailable_brigade(client, ctx, auth_headers):
    sos = send_sos(client, cid(1), "fire").json()["sos"]
    r = client.post(f"{DISPATCH}/sos/{sos['id']}/assign", json={"brigade_id": "br07"}, headers=auth_headers)
    assert r.status_code == 409                                  # br07 məşğuldur
    r = client.post(f"{DISPATCH}/sos/{sos['id']}/assign", json={"brigade_id": "br05"}, headers=auth_headers)
    assert r.status_code == 200 and r.json()["brigade_id"] == "br05"


def test_sos_assign_and_resolve_unknown_404(client, auth_headers):
    assert client.post(f"{DISPATCH}/sos/777/assign", json={}, headers=auth_headers).status_code == 404
    assert client.post(f"{DISPATCH}/sos/777/resolve", headers=auth_headers).status_code == 404


# ----- Məntəqələr, briqadalar, yollar -----
def test_shelter_update_validation_and_audit(client, ctx, auth_headers):
    assert client.patch(f"{DISPATCH}/shelters/nope", json={"occupancy": 1}, headers=auth_headers).status_code == 404
    assert client.patch(f"{DISPATCH}/shelters/sh07", json={"status": "exploded"}, headers=auth_headers).status_code == 422
    r = client.patch(f"{DISPATCH}/shelters/sh07", json={"occupancy": 100}, headers=auth_headers)
    assert r.status_code == 200 and shelter_row(ctx, "sh07")["occupancy"] == 100
    assert ctx.db.scalar("SELECT COUNT(*) FROM audit_log WHERE action='shelter_update'") == 1


def test_brigade_update(client, ctx, auth_headers):
    r = client.patch(f"{DISPATCH}/brigades/br01", json={"status": "on_scene"}, headers=auth_headers)
    assert r.status_code == 200 and r.json()["status"] == "on_scene"
    r = client.patch(f"{DISPATCH}/brigades/br01", json={"status": "available"}, headers=auth_headers)
    assert r.json()["status"] == "available" and r.json()["target_lat"] is None
    assert client.patch(f"{DISPATCH}/brigades/br01", json={"status": "flying"}, headers=auth_headers).status_code == 422
    assert client.patch(f"{DISPATCH}/brigades/br99", json={"status": "busy"}, headers=auth_headers).status_code == 404
    assert ctx.db.scalar("SELECT COUNT(*) FROM audit_log WHERE action='brigade_update'") == 2


def test_road_close_open_unknown_and_persistence(client, ctx, auth_headers):
    assert client.post(f"{DISPATCH}/roads/e999/close", json={}, headers=auth_headers).status_code == 404
    assert client.post(f"{DISPATCH}/roads/e999/open", headers=auth_headers).status_code == 404
    assert client.post(f"{DISPATCH}/roads/e01/close", json={"reason": "x" * 121}, headers=auth_headers).status_code == 422
    client.post(f"{DISPATCH}/roads/e01/close", json={"reason": "Su basıb"}, headers=auth_headers)
    row = ctx.db.one("SELECT * FROM road_closures WHERE edge_id='e01'")
    assert row["source"] == "dispatcher" and row["reason"] == "Su basıb"
    assert "e01" in ctx.routing.closed_edges
    client.post(f"{DISPATCH}/roads/e01/open", headers=auth_headers)
    assert ctx.db.scalar("SELECT COUNT(*) FROM road_closures") == 0
    assert "e01" not in ctx.routing.closed_edges


def test_audit_log_endpoint(client, auth_headers):
    client.post(f"{DISPATCH}/roads/e01/close", json={"reason": "Test"}, headers=auth_headers)
    rows = client.get(f"{DISPATCH}/audit", headers=auth_headers).json()["audit"]
    assert rows[0]["action"] == "road_close" and rows[0]["actor"] == "dispatcher"
    assert rows[0]["details"] == {"edge_id": "e01", "reason": "Test"}
    assert rows[-1]["action"] == "login"
    assert client.get(f"{DISPATCH}/audit", params={"limit": 0}, headers=auth_headers).status_code == 422


def test_auth_me(client, auth_headers):
    me = client.get("/api/v1/auth/me", headers=auth_headers).json()
    assert me["username"] == "dispatcher" and me["role"] == "dispatcher"
    assert "alert:decide" in me["permissions"]


# ----- Demo ssenarilər -----
def test_demo_sos_cluster_scenario_creates_alert(client, ctx, auth_headers):
    r = client.post(f"{DISPATCH}/demo/scenario", json={"name": "sos_cluster", "district_id": "nizami"},
                    headers=auth_headers)
    assert r.status_code == 200
    body = r.json()
    assert len(body["sos_ids"]) == 4 and body["alert_id"]
    row = alerts(ctx)
    assert len(row) == 1 and row[0]["id"] == body["alert_id"] and row[0]["kind"] == "sos_cluster"


def test_demo_shelter_fill_scenario(client, ctx, auth_headers):
    r = client.post(f"{DISPATCH}/demo/scenario", json={"name": "shelter_fill", "shelter_id": "sh09"},
                    headers=auth_headers)
    assert r.status_code == 200 and r.json()["occupancy_pct"] > 95 and r.json()["alert_id"]
    assert alerts(ctx)[0]["shelter_id"] == "sh09"


def test_demo_reset_scenario_restores_seed(client, ctx, auth_headers):
    send_sos(client, cid(1), "fire")
    client.patch(f"{DISPATCH}/shelters/sh07", json={"occupancy": 700}, headers=auth_headers)
    client.post(f"{DISPATCH}/roads/e01/close", json={}, headers=auth_headers)
    r = client.post(f"{DISPATCH}/demo/scenario", json={"name": "reset"}, headers=auth_headers)
    assert r.status_code == 200
    assert ctx.db.scalar("SELECT COUNT(*) FROM sos_signals") == 0
    assert ctx.db.scalar("SELECT COUNT(*) FROM alerts") == 0
    assert shelter_row(ctx, "sh07")["occupancy"] == 220
    assert ctx.routing.closed_edges == set()
    # istifadəçilər saxlanılır -> token hələ də işləyir
    assert client.get(f"{DISPATCH}/overview", headers=auth_headers).status_code == 200


def test_demo_scenario_validation_and_demo_mode(client, auth_headers, monkeypatch):
    from app.config import settings
    assert client.post(f"{DISPATCH}/demo/scenario", json={"name": "nuke"}, headers=auth_headers).status_code == 422
    monkeypatch.setattr(settings, "demo_mode", False)
    assert client.post(f"{DISPATCH}/demo/scenario", json={"name": "reset"}, headers=auth_headers).status_code == 403
