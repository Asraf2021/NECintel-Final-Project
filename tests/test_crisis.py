"""Kritik Böhran Alqoritmi ("Qırmızı Həyəcan") — hədlərin dəqiq yoxlanması."""
from __future__ import annotations

import pytest

from app.config import settings
from app.core.geo import haversine_m
from app.services import crisis

from .helpers import DISPATCH, STATUS, alerts, border_points, cid, district_point, insert_sos, send_sos, shelter_row

ZONE = "khatai"


def _sos_from(client, ctx, n: int, district: str = ZONE, sos_type: str = "fire"):
    """n nömrəli vətəndaşdan rayon mərkəzi yaxınlığında SOS göndərir."""
    lat, lon = district_point(ctx, district, n)
    assert ctx.nearest_district(lat, lon) == district
    r = send_sos(client, cid(n), sos_type, lat, lon)
    assert r.status_code == 201, r.text
    return r.json()["sos"]


def _confirm(client, headers, alert_id):
    return client.post(f"{DISPATCH}/alerts/{alert_id}/confirm", headers=headers)


# ---------------------------------------------------------------------------
# Qayda A — SOS klasteri
# ---------------------------------------------------------------------------
def test_three_citizens_no_alert_fourth_triggers_exactly_one(client, ctx, events):
    for n in (1, 2, 3):
        _sos_from(client, ctx, n)
    assert alerts(ctx) == []                       # 3 siqnal: həyəcan YOXDUR
    assert not [e for e in events if e["event"].startswith("alert")]

    _sos_from(client, ctx, 4)                      # 4-cü fərqli vətəndaş
    rows = alerts(ctx)
    assert len(rows) == 1
    a = rows[0]
    assert a["kind"] == "sos_cluster" and a["status"] == "pending" and a["level"] == "red"
    assert a["district_id"] == ZONE
    new = [e for e in events if e["event"] == "alert_new"]
    assert len(new) == 1 and new[0]["channel"] == "ops"
    details = new[0]["data"]["alert"]["details"]
    assert details["zone_count"] == 4 and len(details["sos_ids"]) == 4
    assert details["dominant_type"] == "fire" and details["window_s"] == 300


def test_four_sos_from_same_citizen_no_alert(client, ctx):
    lat, lon = district_point(ctx, ZONE)
    for t in ("fire", "collapse", "flood", "medical"):   # növlər fərqlidir ki, dedupe işləməsin
        assert send_sos(client, cid(1), t, lat, lon).json()["duplicate"] is False
    assert ctx.db.scalar("SELECT COUNT(*) FROM sos_signals") == 4
    assert alerts(ctx) == []


def test_three_citizens_plus_repeat_from_one_no_alert(client, ctx):
    for n in (1, 2, 3):
        _sos_from(client, ctx, n, sos_type="fire")
    _sos_from(client, ctx, 3, sos_type="medical")        # 4-cü siqnal, amma 3 fərqli vətəndaş
    assert ctx.db.scalar("SELECT COUNT(*) FROM sos_signals") == 4
    assert alerts(ctx) == []


def test_four_citizens_in_different_zones_no_alert(client, ctx):
    for n, district in enumerate(["sabail", "binagadi", "nizami", "khatai"], start=1):
        _sos_from(client, ctx, n, district)
    assert alerts(ctx) == []


def test_fifth_sos_updates_existing_alert(client, ctx, events):
    for n in range(1, 5):
        _sos_from(client, ctx, n)
    first = alerts(ctx)[0]
    _sos_from(client, ctx, 5, sos_type="medical")
    rows = alerts(ctx)
    assert len(rows) == 1 and rows[0]["id"] == first["id"]      # ikinci həyəcan yaranmır
    assert len([e for e in events if e["event"] == "alert_new"]) == 1
    upd = [e for e in events if e["event"] == "alert_update"]
    assert len(upd) == 1
    details = upd[0]["data"]["alert"]["details"]
    assert details["zone_count"] == 5 and len(details["sos_ids"]) == 5
    assert details["by_type"] == {"fire": 4, "medical": 1}


def test_sos_older_than_window_do_not_count(client, ctx):
    lat, lon = district_point(ctx, ZONE)
    for n in (1, 2, 3):
        insert_sos(ctx, cid(n), lat, lon, age_s=settings.sos_cluster_window_s + 1)   # 5 dəq + 1 s
    _sos_from(client, ctx, 4)
    assert alerts(ctx) == []
    # pəncərə daxilində daha 3 təzə siqnal -> indi 4 təzə var
    for n in (5, 6):
        _sos_from(client, ctx, n)
    assert alerts(ctx) == []
    _sos_from(client, ctx, 7)
    assert len(alerts(ctx, kind="sos_cluster", status="pending")) == 1


def test_sos_just_inside_window_count(client, ctx):
    lat, lon = district_point(ctx, ZONE)
    for n in (1, 2, 3):
        insert_sos(ctx, cid(n), lat, lon, age_s=settings.sos_cluster_window_s - 10)  # 4 dəq 50 s
    _sos_from(client, ctx, 4)
    assert len(alerts(ctx, kind="sos_cluster", status="pending")) == 1


def test_window_is_configurable(client, ctx, monkeypatch):
    monkeypatch.setattr(settings, "sos_cluster_window_s", 60)
    lat, lon = district_point(ctx, ZONE)
    for n in (1, 2, 3):
        insert_sos(ctx, cid(n), lat, lon, age_s=90)
    _sos_from(client, ctx, 4)
    assert alerts(ctx) == []


def test_zone_trigger_for_points_far_apart_in_same_district(client, ctx):
    """Eyni rayonda, amma bir-birindən > 500 m aralı 4 vətəndaş -> 'zone' tetikləyicisi."""
    chosen = None
    for d in ctx.districts:
        pts = [(d["lat"] + 0.006, d["lon"]), (d["lat"] - 0.006, d["lon"]),
               (d["lat"], d["lon"] + 0.008), (d["lat"], d["lon"] - 0.008)]
        if all(ctx.nearest_district(*p) == d["id"] for p in pts):
            chosen = (d["id"], pts)
            break
    assert chosen, "uyğun rayon tapılmadı"
    district, pts = chosen
    assert all(haversine_m(*pts[3], *p) > 500 for p in pts[:3])
    for n, (lat, lon) in enumerate(pts, start=1):
        assert send_sos(client, cid(n), "flood", lat, lon).status_code == 201
    row = alerts(ctx, kind="sos_cluster", status="pending")
    assert len(row) == 1 and row[0]["district_id"] == district
    import json
    details = json.loads(row[0]["details"])
    assert details["trigger"] == "zone" and details["zone_count"] == 4 and details["radius_count"] == 1


def test_radius_trigger_across_district_border(client, ctx):
    """Fərqli rayonlara düşən, amma < 500 m aralı 4 vətəndaş -> 'radius' tetikləyicisi (unit)."""
    (da, pa), (db_, pb) = border_points(ctx)
    assert da != db_ and haversine_m(*pa, *pb) < 500
    insert_sos(ctx, cid(1), *pa)
    insert_sos(ctx, cid(2), pa[0] + 0.0001, pa[1])
    insert_sos(ctx, cid(3), *pb)
    assert crisis.evaluate_sos_cluster(ctx, insert_sos(ctx, cid(9), *pb, age_s=400)) is None  # köhnə siqnal sayılmır
    ctx.db.execute("DELETE FROM sos_signals WHERE citizen_id=?", (cid(9),))
    assert alerts(ctx) == []

    last = insert_sos(ctx, cid(4), pb[0], pb[1] + 0.0001)
    alert = crisis.evaluate_sos_cluster(ctx, last)
    assert alert is not None
    assert alert["kind"] == "sos_cluster" and alert["status"] == "pending"
    assert alert["details"]["trigger"] == "radius"
    assert alert["details"]["radius_count"] == 4 and alert["details"]["zone_count"] == 2
    assert alert["details"]["radius_m"] == 500
    assert len(alerts(ctx)) == 1


def test_radius_three_citizens_no_alert(client, ctx):
    (_, pa), (_, pb) = border_points(ctx)
    insert_sos(ctx, cid(1), *pa)
    insert_sos(ctx, cid(2), *pb)
    last = insert_sos(ctx, cid(3), pb[0], pb[1] + 0.0001)
    assert crisis.evaluate_sos_cluster(ctx, last) is None
    assert alerts(ctx) == []


def test_points_beyond_radius_in_different_districts_do_not_trigger(client, ctx):
    rows = []
    for n, district in enumerate(["sabail", "binagadi", "nizami", "khatai"], start=1):
        lat, lon = district_point(ctx, district)
        rows.append(insert_sos(ctx, cid(n), lat, lon))
    for a in rows:
        for b in rows:
            if a["id"] != b["id"]:
                assert haversine_m(a["lat"], a["lon"], b["lat"], b["lon"]) > 500
    assert all(crisis.evaluate_sos_cluster(ctx, r) is None for r in rows)
    assert alerts(ctx) == []


def test_radius_boundary_distance(client, ctx):
    """Radius həddi: ~480 m daxildir, ~520 m xaricdir (fərqli rayonlar)."""
    (da, pa), (db_, pb) = border_points(ctx, offset_m=240)      # pa <-> pb ~480 m
    assert da != db_ and 450 < haversine_m(*pa, *pb) <= 500
    for n in (1, 2, 3):
        insert_sos(ctx, cid(n), *pa)
    inside = insert_sos(ctx, cid(4), *pb)
    assert crisis.evaluate_sos_cluster(ctx, inside)["details"]["trigger"] == "radius"

    ctx.db.execute("DELETE FROM alerts")
    ctx.db.execute("DELETE FROM sos_signals")
    (da, pa), (db_, pb) = border_points(ctx, offset_m=260)      # pa <-> pb ~520 m
    assert da != db_ and haversine_m(*pa, *pb) > 500
    for n in (1, 2, 3):
        insert_sos(ctx, cid(n), *pa)
    outside = insert_sos(ctx, cid(4), *pb)
    assert crisis.evaluate_sos_cluster(ctx, outside) is None


def test_cluster_threshold_is_configurable(client, ctx, monkeypatch):
    monkeypatch.setattr(settings, "sos_cluster_count", 2)
    _sos_from(client, ctx, 1)
    assert alerts(ctx) == []
    _sos_from(client, ctx, 2)
    assert len(alerts(ctx)) == 1


def test_no_new_alert_during_cooldown_after_decision(client, ctx, auth_headers):
    for n in range(1, 5):
        _sos_from(client, ctx, n)
    alert_id = alerts(ctx)[0]["id"]
    assert client.post(f"{DISPATCH}/alerts/{alert_id}/dismiss", headers=auth_headers).status_code == 200
    _sos_from(client, ctx, 5)       # qərardan dərhal sonra eyni zona üçün təkrar həyəcan yaranmır
    assert len(alerts(ctx)) == 1 and alerts(ctx)[0]["status"] == "dismissed"


def test_radius_cluster_on_border_creates_single_alert(client, ctx):
    (_, pa), (_, pb) = border_points(ctx)
    for n, p in enumerate([pa, pa, pb, pb], start=1):
        assert send_sos(client, cid(n), "fire", *p).status_code == 201
    assert len(alerts(ctx, status="pending")) == 1
    send_sos(client, cid(5), "fire", *pa)       # eyni klasterə 5-ci siqnal (digər rayon tərəfdən)
    assert len(alerts(ctx, status="pending")) == 1


# ---------------------------------------------------------------------------
# Qayda B — məntəqə doluluğu > 95%
# ---------------------------------------------------------------------------
def test_exactly_95_pct_no_alert_above_creates_one(client, ctx, auth_headers, events):
    # sh07: 700 yer -> 665 = tam 95.0%
    r = client.patch(f"{DISPATCH}/shelters/sh07", json={"occupancy": 665}, headers=auth_headers)
    assert r.status_code == 200
    assert r.json()["occupancy_pct"] == 95.0 and r.json()["level"] == "full"
    assert alerts(ctx) == []

    r = client.patch(f"{DISPATCH}/shelters/sh07", json={"occupancy": 666}, headers=auth_headers)   # 95.14%
    assert r.json()["occupancy_pct"] > 95 and r.json()["level"] == "critical"
    rows = alerts(ctx)
    assert len(rows) == 1
    assert rows[0]["kind"] == "shelter_overflow" and rows[0]["status"] == "pending"
    assert rows[0]["shelter_id"] == "sh07" and rows[0]["district_id"] == "yasamal"
    assert len([e for e in events if e["event"] == "alert_new"]) == 1


def test_no_duplicate_overflow_alert_on_further_updates(client, ctx, auth_headers, events):
    client.patch(f"{DISPATCH}/shelters/sh07", json={"occupancy": 666}, headers=auth_headers)
    client.patch(f"{DISPATCH}/shelters/sh07", json={"occupancy": 690}, headers=auth_headers)
    client.patch(f"{DISPATCH}/shelters/sh07", json={"occupancy": 700}, headers=auth_headers)
    rows = alerts(ctx)
    assert len(rows) == 1
    assert len([e for e in events if e["event"] == "alert_new"]) == 1
    import json
    assert json.loads(rows[0]["details"])["occupancy_pct"] == 100.0     # təfərrüat yenilənir


def test_each_overflowing_shelter_gets_its_own_alert(client, ctx, auth_headers):
    client.patch(f"{DISPATCH}/shelters/sh07", json={"occupancy": 700}, headers=auth_headers)
    client.patch(f"{DISPATCH}/shelters/sh09", json={"occupancy": 650}, headers=auth_headers)
    assert sorted(a["shelter_id"] for a in alerts(ctx, kind="shelter_overflow")) == ["sh07", "sh09"]


def test_reserve_shelter_does_not_alert(client, ctx, auth_headers):
    r = client.patch(f"{DISPATCH}/shelters/sh18", json={"occupancy": 2500}, headers=auth_headers)
    assert r.json()["status"] == "reserve" and r.json()["occupancy_pct"] == 100.0
    assert alerts(ctx) == []


def test_citizen_checkin_crossing_95_pct_creates_alert(client, ctx):
    ctx.db.execute("UPDATE shelters SET occupancy=665 WHERE id='sh07'")      # tam 95%
    assert alerts(ctx) == []
    r = client.post(STATUS, json={"citizen_id": cid(1), "status": "safe", "shelter_id": "sh07"})
    assert r.json()["shelter"]["occupancy"] == 666
    rows = alerts(ctx)
    assert len(rows) == 1 and rows[0]["kind"] == "shelter_overflow" and rows[0]["shelter_id"] == "sh07"


def test_occupancy_is_clamped_to_capacity(client, ctx, auth_headers):
    r = client.patch(f"{DISPATCH}/shelters/sh07", json={"occupancy": 99999}, headers=auth_headers)
    assert r.json()["occupancy"] == 700 and r.json()["occupancy_pct"] == 100.0
    assert client.patch(f"{DISPATCH}/shelters/sh07", json={"occupancy": -1}, headers=auth_headers).status_code == 422


def test_slightly_above_95_pct_on_large_shelter_alerts(client, ctx, auth_headers):
    # sh01: 6000 yer, 5701 nəfər = 95.017% (> 95%) -> həyəcan olmalıdır
    client.patch(f"{DISPATCH}/shelters/sh01", json={"occupancy": 5701}, headers=auth_headers)
    assert 5701 * 100 / 6000 > 95
    assert len(alerts(ctx, kind="shelter_overflow")) == 1


# ---------------------------------------------------------------------------
# Təsdiq / rədd (bir kliklə qərar)
# ---------------------------------------------------------------------------
def test_confirm_dispatches_brigade_and_opens_reserve_shelter(client, ctx, auth_headers, events):
    sos_ids = [_sos_from(client, ctx, n)["id"] for n in range(1, 5)]
    alert = alerts(ctx)[0]
    reserves = ctx.db.query("SELECT * FROM shelters WHERE status='reserve'")
    expected_shelter = min(reserves, key=lambda s: haversine_m(alert["lat"], alert["lon"], s["lat"], s["lon"]))
    fire = ctx.db.query("SELECT * FROM brigades WHERE status='available' AND type='fire'")
    expected_brigade = min(fire, key=lambda b: haversine_m(alert["lat"], alert["lon"], b["lat"], b["lon"]))
    dispatched_before = ctx.db.scalar("SELECT COUNT(*) FROM brigades WHERE status='dispatched'")

    r = _confirm(client, auth_headers, alert["id"])
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "confirmed" and body["decided_by"] == "dispatcher" and body["decided_at"]

    # (1) ən yaxın uyğun briqada göndərilir
    assert body["actions"]["brigade"]["id"] == expected_brigade["id"]
    b = ctx.db.one("SELECT * FROM brigades WHERE id=?", (expected_brigade["id"],))
    assert b["status"] == "dispatched" and b["assigned_alert_id"] == alert["id"]
    assert b["target_lat"] == pytest.approx(alert["lat"]) and b["target_lon"] == pytest.approx(alert["lon"])
    assert ctx.db.scalar("SELECT COUNT(*) FROM brigades WHERE status='dispatched'") == dispatched_before + 1

    # (2) ən yaxın ehtiyat məntəqə açılır
    assert body["actions"]["new_shelter"]["id"] == expected_shelter["id"]
    assert shelter_row(ctx, expected_shelter["id"])["status"] == "open"
    assert ctx.db.scalar("SELECT COUNT(*) FROM shelters WHERE status='reserve'") == len(reserves) - 1

    # klasterdəki SOS-lar briqadaya təyin olunur
    rows = ctx.db.query("SELECT status, brigade_id FROM sos_signals")
    assert len(rows) == len(sos_ids)
    assert all(s["status"] == "assigned" and s["brigade_id"] == expected_brigade["id"] for s in rows)

    # audit jurnalı
    audit = ctx.db.query("SELECT * FROM audit_log WHERE action='alert_confirm'")
    assert len(audit) == 1 and audit[0]["actor"] == "dispatcher" and audit[0]["role"] == "dispatcher"
    import json
    assert json.loads(audit[0]["details"])["alert_id"] == alert["id"]

    # canlı hadisələr
    names = [e["event"] for e in events]
    assert "brigade_update" in names
    opened = [e for e in events if e["event"] == "shelter_update" and e["data"].get("opened")]
    assert len(opened) == 1 and opened[0]["channel"] == "public"


def test_confirm_twice_is_idempotent(client, ctx, auth_headers):
    for n in range(1, 5):
        _sos_from(client, ctx, n)
    alert_id = alerts(ctx)[0]["id"]
    first = _confirm(client, auth_headers, alert_id).json()
    snapshot = (ctx.db.scalar("SELECT COUNT(*) FROM brigades WHERE status='dispatched'"),
                ctx.db.scalar("SELECT COUNT(*) FROM shelters WHERE status='reserve'"))
    second = _confirm(client, auth_headers, alert_id)
    assert second.status_code == 200
    assert second.json()["status"] == "confirmed"
    assert second.json()["actions"] == first["actions"]
    assert second.json()["decided_at"] == first["decided_at"]
    assert (ctx.db.scalar("SELECT COUNT(*) FROM brigades WHERE status='dispatched'"),
            ctx.db.scalar("SELECT COUNT(*) FROM shelters WHERE status='reserve'")) == snapshot
    assert snapshot[0] == 1                                    # ikinci briqada göndərilmir
    assert ctx.db.scalar("SELECT COUNT(*) FROM audit_log WHERE action='alert_confirm'") == 1


def test_confirm_shelter_overflow_alert(client, ctx, auth_headers):
    client.patch(f"{DISPATCH}/shelters/sh07", json={"occupancy": 700}, headers=auth_headers)
    alert_id = alerts(ctx)[0]["id"]
    body = _confirm(client, auth_headers, alert_id).json()
    assert body["status"] == "confirmed"
    assert body["actions"]["brigade"]["type"] == "rescue"       # növ yoxdursa xilasetmə briqadası
    assert body["actions"]["new_shelter"]["id"] == "sh24"       # Yasamaldakı ehtiyat məntəqə
    assert shelter_row(ctx, "sh24")["status"] == "open"


def test_confirm_without_available_brigade_still_opens_shelter(client, ctx, auth_headers):
    for n in range(1, 5):
        _sos_from(client, ctx, n)
    ctx.db.execute("UPDATE brigades SET status='busy'")
    body = _confirm(client, auth_headers, alerts(ctx)[0]["id"]).json()
    assert body["status"] == "confirmed"
    assert body["actions"]["brigade"] is None
    assert body["actions"]["new_shelter"] is not None


def test_dismiss_alert(client, ctx, auth_headers):
    for n in range(1, 5):
        _sos_from(client, ctx, n)
    alert_id = alerts(ctx)[0]["id"]
    reserves = ctx.db.scalar("SELECT COUNT(*) FROM shelters WHERE status='reserve'")
    r = client.post(f"{DISPATCH}/alerts/{alert_id}/dismiss", headers=auth_headers)
    assert r.status_code == 200
    assert r.json()["status"] == "dismissed" and r.json()["decided_by"] == "dispatcher"
    assert r.json()["actions"] == {}
    assert ctx.db.scalar("SELECT COUNT(*) FROM brigades WHERE status='dispatched'") == 0
    assert ctx.db.scalar("SELECT COUNT(*) FROM shelters WHERE status='reserve'") == reserves
    assert ctx.db.scalar("SELECT COUNT(*) FROM audit_log WHERE action='alert_dismiss'") == 1
    # rədd edilmiş həyəcan sonradan təsdiqlənə bilməz
    assert _confirm(client, auth_headers, alert_id).json()["status"] == "dismissed"
    assert ctx.db.scalar("SELECT COUNT(*) FROM brigades WHERE status='dispatched'") == 0


def test_confirm_and_dismiss_unknown_alert_404(client, auth_headers):
    assert _confirm(client, auth_headers, 9999).status_code == 404
    assert client.post(f"{DISPATCH}/alerts/9999/dismiss", headers=auth_headers).status_code == 404
    assert client.post(f"{DISPATCH}/alerts/abc/confirm", headers=auth_headers).status_code == 422


def test_alerts_listing_pending_first(client, ctx, auth_headers):
    client.patch(f"{DISPATCH}/shelters/sh07", json={"occupancy": 700}, headers=auth_headers)
    first_id = alerts(ctx)[0]["id"]
    client.post(f"{DISPATCH}/alerts/{first_id}/dismiss", headers=auth_headers)
    for n in range(1, 5):
        _sos_from(client, ctx, n)
    listed = client.get(f"{DISPATCH}/alerts", headers=auth_headers).json()["alerts"]
    assert [a["status"] for a in listed] == ["pending", "dismissed"]
    assert listed[0]["kind"] == "sos_cluster" and listed[0]["district_name"] == "Xətai"
    assert isinstance(listed[0]["details"], dict)
