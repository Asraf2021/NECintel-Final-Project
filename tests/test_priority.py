"""Challenge Drop 1: Prioritet SOS (priority_citizen) və əlçatan (wheelchair) marşrutlaşdırma testləri."""
from __future__ import annotations

import math
import re
import sqlite3
from pathlib import Path

import pytest

from app.config import settings
from app.core.db import Database
from app.core.graph import PROFILES
from app.core.roles import (CITIZEN_ROLES, DEFAULT_CITIZEN_ROLE, PRIORITY_CRITICAL, PRIORITY_STANDARD,
                            citizen_role, role_policy)
from tests.helpers import DISPATCH, EVAC, SOS, cid, district_point, send_sos, shelter_row


@pytest.fixture(autouse=True)
def _self_declared_priority_allowed(monkeypatch):
    """Bu fayl prioritet MEXANİKASINI yoxlayır (Drop 1). Təsdiq siyasəti test_mygov.py-də ayrıca yoxlanılır."""
    from app.config import settings
    monkeypatch.setattr(settings, "priority_requires_verification", False)


ACCESSIBLE_BRIGADES = {"br03", "br04", "br05", "br08"}
I18N_PATH = Path(__file__).resolve().parent.parent / "static" / "js" / "i18n.js"


# ---------------------------------------------------------------------------
# Köməkçilər
# ---------------------------------------------------------------------------
def set_role(client, citizen_id: str, role: str = "priority_citizen"):
    r = client.put(f"/api/v1/citizens/{citizen_id}/profile", json={"role": role})
    assert r.status_code == 200, r.text
    return r.json()


def inaccessible_edges(ctx) -> set[str]:
    return {eid for eid, e in ctx.graph.edges.items() if not e.accessible}


def inaccessible_shelters(ctx) -> set[str]:
    return {s["id"] for s in ctx.db.query("SELECT id, accessible FROM shelters") if not s["accessible"]}


def evac(client, **params) -> dict:
    r = client.get(EVAC, params=params)
    assert r.status_code == 200, r.text
    return r.json()


def assert_accessible_plan(ctx, plan: dict):
    """Plan əlçatandır: profil wheelchair, tövsiyə olunan məntəqə əlçatan, marşrutda maneəli seqment yoxdur."""
    assert plan["profile"] == "wheelchair"
    assert plan["role"] == "priority_citizen"
    assert plan["sos_priority"] == 1
    rec = plan["recommended"]
    assert rec is not None
    assert rec["accessible"] is True or rec["accessible"] == 1
    assert rec["shelter_id"] not in inaccessible_shelters(ctx)
    assert not set(rec["route"]["edge_ids"]) & inaccessible_edges(ctx)
    # siyahıda göstərilən bütün məntəqələr də əlçatan olmalıdır
    bad = inaccessible_shelters(ctx)
    for s in plan["shelters"]:
        assert s["id"] not in bad
        if s.get("route"):
            assert not set(s["route"]["edge_ids"]) & inaccessible_edges(ctx)


# ---------------------------------------------------------------------------
# Rollar reyestri (vahid testlər)
# ---------------------------------------------------------------------------
def test_role_registry_contains_both_roles():
    assert set(CITIZEN_ROLES) == {"citizen", "priority_citizen"}
    assert DEFAULT_CITIZEN_ROLE == "citizen"
    assert PRIORITY_CRITICAL == 1 and PRIORITY_STANDARD == 2


def test_role_policy_priority_citizen():
    p = role_policy("priority_citizen")
    assert p["sos_priority"] == 1
    assert p["route_profile"] == "wheelchair"
    assert p["accessible_shelters_only"] is True
    assert p["brigade_requirement"] == "accessible_transport"
    assert p["route_profile"] in PROFILES


def test_role_policy_defaults_for_unknown_or_missing_role():
    std = role_policy("citizen")
    assert std["sos_priority"] == 2 and std["route_profile"] == "foot"
    assert std["accessible_shelters_only"] is False and std["brigade_requirement"] is None
    assert role_policy(None) == std
    assert role_policy("") == std
    assert role_policy("hacker") == std


def test_route_profiles_registry():
    assert set(PROFILES) == {"foot", "wheelchair"}
    assert PROFILES["wheelchair"]["accessible_only"] is True
    assert PROFILES["foot"]["accessible_only"] is False


def test_citizen_role_lookup(client, ctx):
    assert citizen_role(ctx.db, None) == "citizen"
    assert citizen_role(ctx.db, cid(1)) == "citizen"  # profil yoxdur
    set_role(client, cid(1))
    assert citizen_role(ctx.db, cid(1)) == "priority_citizen"
    # bazada naməlum rol -> standart
    ctx.db.execute("INSERT INTO citizen_profiles (citizen_id, role, updated_at) VALUES (?,?,0)", (cid(2), "bogus"))
    assert citizen_role(ctx.db, cid(2)) == "citizen"


# ---------------------------------------------------------------------------
# Profil endpoint-i
# ---------------------------------------------------------------------------
def test_profile_set_priority_role(client):
    body = set_role(client, cid(1))
    assert body == {"citizen_id": cid(1), "role": "priority_citizen", "sos_priority": 1}
    r = client.get(f"/api/v1/citizens/{cid(1)}")
    assert r.status_code == 200 and r.json()["role"] == "priority_citizen"


def test_citizen_default_role_is_citizen(client):
    r = client.get(f"/api/v1/citizens/{cid(9)}")
    assert r.status_code == 200 and r.json()["role"] == "citizen"


def test_profile_invalid_role_rejected(client, ctx):
    for bad in ("admin", "dispatcher", "PRIORITY_CITIZEN", "", None, 1):
        r = client.put(f"/api/v1/citizens/{cid(1)}/profile", json={"role": bad})
        assert r.status_code == 422, (bad, r.text)
    r = client.put(f"/api/v1/citizens/{cid(1)}/profile", json={})
    assert r.status_code == 422
    assert ctx.db.one("SELECT * FROM citizen_profiles WHERE citizen_id=?", (cid(1),)) is None


def test_profile_invalid_citizen_id_rejected(client):
    for bad in ("short", "bad id with spaces!", "x" * 65, "abc$%^&*()"):
        r = client.put(f"/api/v1/citizens/{bad}/profile", json={"role": "priority_citizen"})
        assert r.status_code in (404, 422), (bad, r.status_code)
    r = client.put("/api/v1/citizens/short/profile", json={"role": "priority_citizen"})
    assert r.status_code == 422


def test_profile_switch_back_to_citizen(client):
    set_role(client, cid(1))
    body = set_role(client, cid(1), "citizen")
    assert body["role"] == "citizen" and body["sos_priority"] == 2
    assert client.get(f"/api/v1/citizens/{cid(1)}").json()["role"] == "citizen"
    r = send_sos(client, cid(1))
    assert r.status_code == 201 and r.json()["sos"]["priority"] == 2


def test_profile_change_deletes_stored_plan(client, ctx):
    me = cid(1)
    evac(client, district_id="sabail", citizen_id=me)
    assert ctx.db.one("SELECT * FROM evacuation_plans WHERE citizen_id=?", (me,))["profile"] == "foot"
    set_role(client, me)
    assert ctx.db.one("SELECT * FROM evacuation_plans WHERE citizen_id=?", (me,)) is None
    evac(client, district_id="sabail", citizen_id=me)
    assert ctx.db.one("SELECT * FROM evacuation_plans WHERE citizen_id=?", (me,))["profile"] == "wheelchair"


def test_meta_lists_roles_and_profiles(client):
    body = client.get("/api/v1/meta").json()
    assert set(body["citizen_roles"]) == {"citizen", "priority_citizen"}
    assert body["route_profiles"] == ["foot", "wheelchair"]


def test_roads_include_accessibility(client, ctx):
    edges = client.get("/api/v1/roads").json()["edges"]
    assert len(edges) == len(ctx.graph.edges)
    bad = [e for e in edges if e["accessible"] is False]
    assert len(bad) == 12
    assert all(e["barrier"] in {"stairs", "underpass", "steep", "no_sidewalk"} for e in bad)
    assert all(e["barrier"] is None for e in edges if e["accessible"])


# ---------------------------------------------------------------------------
# SOS prioriteti
# ---------------------------------------------------------------------------
def test_priority_citizen_sos_gets_priority_1(client):
    set_role(client, cid(1))
    r = send_sos(client, cid(1), "medical")
    assert r.status_code == 201
    sos = r.json()["sos"]
    assert sos["priority"] == 1 and sos["citizen_role"] == "priority_citizen"


def test_normal_citizen_sos_gets_priority_2(client):
    sos = send_sos(client, cid(2)).json()["sos"]
    assert sos["priority"] == 2 and sos["citizen_role"] == "citizen"


def test_sos_body_cannot_override_priority(client, ctx):
    r = client.post(SOS, json={"citizen_id": cid(3), "type": "fire", "lat": 40.383, "lon": 49.9,
                               "priority": 1, "citizen_role": "priority_citizen"})
    assert r.status_code in (201, 422)
    if r.status_code == 201:
        assert r.json()["sos"]["priority"] == 2
        assert r.json()["sos"]["citizen_role"] == "citizen"
    rows = ctx.db.query("SELECT priority FROM sos_signals WHERE citizen_id=?", (cid(3),))
    assert all(row["priority"] == 2 for row in rows)


def test_profile_change_reprioritises_active_sos(client, ctx):
    me = cid(4)
    sos = send_sos(client, me).json()["sos"]
    assert sos["priority"] == 2
    set_role(client, me)
    row = ctx.db.one("SELECT * FROM sos_signals WHERE id=?", (sos["id"],))
    assert row["priority"] == 1 and row["citizen_role"] == "priority_citizen"
    set_role(client, me, "citizen")
    row = ctx.db.one("SELECT * FROM sos_signals WHERE id=?", (sos["id"],))
    assert row["priority"] == 2 and row["citizen_role"] == "citizen"


def test_profile_change_does_not_touch_resolved_sos(client, ctx, auth_headers):
    me = cid(5)
    sos = send_sos(client, me).json()["sos"]
    assert client.post(f"{DISPATCH}/sos/{sos['id']}/resolve", headers=auth_headers).status_code == 200
    set_role(client, me)
    assert ctx.db.one("SELECT priority FROM sos_signals WHERE id=?", (sos["id"],))["priority"] == 2


def test_priority_sos_listed_first(client, auth_headers):
    set_role(client, cid(10))
    prio = send_sos(client, cid(10), "medical").json()["sos"]
    # sonra daha yeni adi SOS-lar
    for i in range(11, 14):
        send_sos(client, cid(i), "fire", lat=40.40 + i * 0.001, lon=49.85)
    r = client.get(f"{DISPATCH}/sos", headers=auth_headers)
    assert r.status_code == 200
    items = r.json()["sos"] if isinstance(r.json(), dict) else r.json()
    assert items[0]["id"] == prio["id"] and items[0]["priority"] == 1
    assert all(s["priority"] == 2 for s in items[1:])

    ov = client.get(f"{DISPATCH}/overview", headers=auth_headers).json()
    assert ov["sos"][0]["id"] == prio["id"]
    assert ov["kpi"]["sos_priority"] == 1
    assert ov["kpi"]["sos_active"] == 4


def test_kpi_sos_priority_counts_only_active(client, auth_headers):
    ov = client.get(f"{DISPATCH}/overview", headers=auth_headers).json()
    assert ov["kpi"]["sos_priority"] == 0
    set_role(client, cid(1))
    set_role(client, cid(2))
    s1 = send_sos(client, cid(1), "medical").json()["sos"]
    send_sos(client, cid(2), "flood", lat=40.41, lon=49.86)
    ov = client.get(f"{DISPATCH}/overview", headers=auth_headers).json()
    assert ov["kpi"]["sos_priority"] == 2
    client.post(f"{DISPATCH}/sos/{s1['id']}/resolve", headers=auth_headers)
    ov = client.get(f"{DISPATCH}/overview", headers=auth_headers).json()
    assert ov["kpi"]["sos_priority"] == 1


def test_zone_with_priority_sos_is_critical(client, ctx, auth_headers):
    # adi tək SOS -> "medium", prioritet tək SOS -> "critical"
    lat, lon = district_point(ctx, "yasamal")
    send_sos(client, cid(1), "fire", lat=lat, lon=lon)
    lat2, lon2 = district_point(ctx, "binagadi")
    set_role(client, cid(2))
    send_sos(client, cid(2), "medical", lat=lat2, lon=lon2)

    zones = {z["district_id"]: z for z in client.get(f"{DISPATCH}/overview", headers=auth_headers).json()["zones"]}
    assert zones["binagadi"]["risk"] == "critical"
    assert zones["binagadi"]["sos_priority"] == 1
    assert zones["yasamal"]["risk"] != "critical"
    assert zones["yasamal"]["sos_priority"] == 0


def test_sos_priority_event_published(client, events):
    set_role(client, cid(1))
    sos = send_sos(client, cid(1), "medical").json()["sos"]
    prio = [e for e in events if e["event"] == "sos_priority"]
    assert len(prio) == 1
    assert prio[0]["channel"] == "ops" and prio[0]["data"]["sos"]["id"] == sos["id"]
    assert prio[0]["data"]["sos"]["priority"] == 1

    events.clear()
    send_sos(client, cid(2), "fire")
    assert not [e for e in events if e["event"] == "sos_priority"]
    assert [e for e in events if e["event"] == "sos_new"]


# ---------------------------------------------------------------------------
# Briqada təyinatı
# ---------------------------------------------------------------------------
def _assign(client, auth_headers, sos_id: int):
    r = client.post(f"{DISPATCH}/sos/{sos_id}/assign", json={}, headers=auth_headers)
    assert r.status_code == 200, r.text
    return r.json()


def test_priority_sos_assigned_accessible_medical_brigade(client, ctx, auth_headers):
    from app.core.geo import haversine_m
    set_role(client, cid(1))
    lat, lon = district_point(ctx, "khatai")
    sos = send_sos(client, cid(1), "fire", lat=lat, lon=lon).json()["sos"]
    out = _assign(client, auth_headers, sos["id"])
    b = ctx.db.one("SELECT * FROM brigades WHERE id=?", (out["brigade_id"],))
    assert b["accessible_transport"] == 1 and b["id"] in ACCESSIBLE_BRIGADES
    assert b["type"] == "medical"  # yanğın SOS olsa da, prioritet vətəndaş üçün tibbi nəqliyyat üstündür
    cands = ctx.db.query("SELECT * FROM brigades WHERE accessible_transport=1 AND type='medical'")
    nearest = min(cands, key=lambda c: haversine_m(lat, lon, c["lat"], c["lon"]))
    assert b["id"] == nearest["id"]
    assert out["status"] == "assigned"


def test_priority_sos_uses_accessible_rescue_when_medical_busy(client, ctx, auth_headers):
    for bid in ("br03", "br04", "br08"):
        assert client.patch(f"{DISPATCH}/brigades/{bid}", json={"status": "busy"}, headers=auth_headers).status_code == 200
    set_role(client, cid(1))
    sos = send_sos(client, cid(1), "medical").json()["sos"]
    out = _assign(client, auth_headers, sos["id"])
    assert out["brigade_id"] == "br05"  # yeganə boş əlçatan nəqliyyatlı briqada


def test_priority_sos_falls_back_when_accessible_brigades_busy(client, ctx, auth_headers):
    for bid in ACCESSIBLE_BRIGADES:
        r = client.patch(f"{DISPATCH}/brigades/{bid}", json={"status": "busy"}, headers=auth_headers)
        assert r.status_code == 200
    set_role(client, cid(1))
    sos = send_sos(client, cid(1), "medical").json()["sos"]
    out = _assign(client, auth_headers, sos["id"])
    assert out["brigade_id"] not in ACCESSIBLE_BRIGADES
    assert ctx.db.one("SELECT status FROM brigades WHERE id=?", (out["brigade_id"],))["status"] == "dispatched"


def test_normal_fire_sos_still_prefers_fire_brigade(client, ctx, auth_headers):
    lat, lon = district_point(ctx, "sabail")  # br05 (əlçatan, xilasedici) buradadır
    sos = send_sos(client, cid(2), "fire", lat=lat, lon=lon).json()["sos"]
    out = _assign(client, auth_headers, sos["id"])
    b = ctx.db.one("SELECT * FROM brigades WHERE id=?", (out["brigade_id"],))
    assert b["type"] == "fire"


# ---------------------------------------------------------------------------
# Qraf: wheelchair profili
# ---------------------------------------------------------------------------
def _graph_points(ctx):
    pts = [(n["lat"], n["lon"]) for n in ctx.graph.nodes.values()]
    pts += [(s["lat"], s["lon"]) for s in ctx.city["shelters"]]
    return pts


def test_wheelchair_profile_never_uses_inaccessible_edge(ctx):
    bad = inaccessible_edges(ctx)
    assert len(bad) == 12
    pts = _graph_points(ctx)
    found = 0
    for o in pts:
        for d in pts[::3]:
            r = ctx.graph.shortest_path(o, d, profile="wheelchair")
            if r.found:
                found += 1
                assert not set(r.edge_ids) & bad
    assert found > 0


def _barrier_pair(ctx):
    """Piyada marşrutu maneəli seqmentdən keçən, wheelchair marşrutu isə mövcud olan cüt."""
    bad = inaccessible_edges(ctx)
    pts = _graph_points(ctx)
    for o in pts:
        for d in pts:
            foot = ctx.graph.shortest_path(o, d, profile="foot")
            if not foot.found or not set(foot.edge_ids) & bad:
                continue
            wc = ctx.graph.shortest_path(o, d, profile="wheelchair")
            if wc.found:
                return o, d, foot, wc
    raise AssertionError("maneəli seqmentdən keçən cüt tapılmadı")


def test_wheelchair_route_avoids_barrier_used_by_foot_route(ctx):
    o, d, foot, wc = _barrier_pair(ctx)
    bad = inaccessible_edges(ctx)
    assert set(foot.edge_ids) & bad
    assert not set(wc.edge_ids) & bad
    assert wc.edge_ids != foot.edge_ids
    assert wc.distance_m >= foot.distance_m - 1e-6  # məhdudiyyət marşrutu qısalda bilməz


def test_unknown_profile_behaves_like_foot(ctx):
    o, d, foot, _ = _barrier_pair(ctx)
    other = ctx.graph.shortest_path(o, d, profile="spaceship")
    assert other.edge_ids == foot.edge_ids


def test_wheelchair_duration_uses_slower_speed(ctx):
    assert settings.wheelchair_speed_kmh == 3.0 < settings.walking_speed_kmh
    o, d, _, _ = _barrier_pair(ctx)
    wc_slow = ctx.graph.shortest_path(o, d, speed_kmh=settings.wheelchair_speed_kmh, profile="wheelchair")
    wc_fast = ctx.graph.shortest_path(o, d, speed_kmh=settings.walking_speed_kmh, profile="wheelchair")
    assert wc_slow.edge_ids == wc_fast.edge_ids
    ratio = wc_slow.duration_min / wc_fast.duration_min
    assert ratio == pytest.approx(settings.walking_speed_kmh / settings.wheelchair_speed_kmh, rel=1e-6)


def test_priority_plan_duration_matches_wheelchair_speed(client):
    plan = evac(client, district_id="khatai", role="priority_citizen")
    route = plan["recommended"]["route"]
    expected = route["distance_m"] / (settings.wheelchair_speed_kmh * 1000 / 60)
    assert route["duration_min"] == pytest.approx(expected, abs=0.15)
    normal = evac(client, district_id="khatai")
    nr = normal["recommended"]["route"]
    assert nr["duration_min"] == pytest.approx(nr["distance_m"] / (settings.walking_speed_kmh * 1000 / 60), abs=0.15)


# ---------------------------------------------------------------------------
# Təxliyə planı
# ---------------------------------------------------------------------------
def test_priority_plan_accessible_for_every_district(client, ctx):
    for d in ctx.districts:
        plan = evac(client, district_id=d["id"], role="priority_citizen")
        assert_accessible_plan(ctx, plan)


def test_priority_plan_accessible_from_many_points(client, ctx):
    for i in range(6):
        for j in range(5):
            plan = evac(client, lat=40.36 + i * 0.012, lon=49.80 + j * 0.035, role="priority_citizen")
            assert_accessible_plan(ctx, plan)


def test_stored_priority_role_used_without_role_param(client, ctx):
    for n, d in enumerate(ctx.districts):
        me = cid(100 + n)
        set_role(client, me)
        plan = evac(client, district_id=d["id"], citizen_id=me)
        assert_accessible_plan(ctx, plan)


def test_role_param_ignored_when_citizen_id_given(client, ctx):
    # saxlanılmış rol "citizen" -> role parametri vətəndaşın profilini dəyişmir
    plan = evac(client, district_id="sabail", citizen_id=cid(1), role="priority_citizen")
    assert plan["role"] == "citizen" and plan["profile"] == "foot"


def test_evacuation_rejects_unknown_role_param(client):
    r = client.get(EVAC, params={"district_id": "sabail", "role": "admin"})
    assert r.status_code == 422


def test_normal_plan_profile_is_foot(client):
    plan = evac(client, district_id="sabail")
    assert plan["role"] == "citizen" and plan["profile"] == "foot" and plan["sos_priority"] == 2


def test_inaccessible_metro_shelter_never_offered_to_priority_citizen(client, ctx):
    assert shelter_row(ctx, "sh08")["accessible"] == 0
    sh = shelter_row(ctx, "sh08")
    lat, lon = sh["lat"], sh["lon"]
    # normal vətəndaş üçün ən yaxın məntəqə sh08 (İçərişəhər) ola bilər
    normal = evac(client, lat=lat, lon=lon)
    assert "sh08" in {s["id"] for s in normal["shelters"]}
    prio = evac(client, lat=lat, lon=lon, role="priority_citizen")
    assert "sh08" not in {s["id"] for s in prio["shelters"]}
    assert prio["recommended"]["shelter_id"] != "sh08"
    assert_accessible_plan(ctx, prio)
    sab = evac(client, district_id="sabail", role="priority_citizen")
    assert "sh08" not in {s["id"] for s in sab["shelters"]}


def test_accessible_metro_shelters_can_be_offered(ctx):
    acc = {s["id"] for s in ctx.db.query("SELECT id, accessible FROM shelters WHERE type='shelter'") if s["accessible"]}
    assert acc == {"sh09", "sh15"}


def test_priority_citizen_accessible_route_during_outage(client, ctx, sim, auth_headers):
    r = client.post(f"{DISPATCH}/demo/scenario", json={"name": "api_outage"}, headers=auth_headers)
    assert r.status_code == 200, r.text
    assert sim["outage"] == "error"
    for d in ctx.districts:
        plan = evac(client, district_id=d["id"], role="priority_citizen")
        assert plan["routing"]["source"] == "fallback"
        assert_accessible_plan(ctx, plan)


def test_priority_citizen_accessible_route_with_sim_outage(client, ctx, sim):
    me = cid(7)
    set_role(client, me)
    sim["outage"] = "error"
    plan = evac(client, district_id="sabail", citizen_id=me)
    assert plan["routing"]["source"] == "fallback"
    assert_accessible_plan(ctx, plan)


def test_route_cache_separates_profiles(client):
    a = evac(client, district_id="nizami", role="priority_citizen")
    b = evac(client, district_id="nizami")
    c = evac(client, district_id="nizami", role="priority_citizen")
    assert a["profile"] == c["profile"] == "wheelchair" and b["profile"] == "foot"
    assert a["recommended"]["route"] == c["recommended"]["route"]
    assert b["recommended"]["route"]["duration_min"] != a["recommended"]["route"]["duration_min"]


# ---------------------------------------------------------------------------
# Avtomatik yenidən marşrutlaşdırma
# ---------------------------------------------------------------------------
def test_priority_citizen_rerouted_to_accessible_shelter(client, ctx, auth_headers, events):
    me = cid(20)
    set_role(client, me)
    first = evac(client, district_id="khatai", citizen_id=me)
    assert_accessible_plan(ctx, first)
    target = first["recommended"]["shelter_id"]
    cap = shelter_row(ctx, target)["capacity"]

    r = client.patch(f"{DISPATCH}/shelters/{target}", json={"occupancy": math.ceil(cap * 0.90)}, headers=auth_headers)
    assert r.status_code == 200 and r.json()["accepting"] is False

    reroutes = [e for e in events if e["event"] == "reroute"]
    assert [e["citizen_id"] for e in reroutes] == [me]
    pushed = reroutes[0]["data"]["plan"]
    assert pushed["rerouted"] is True and pushed["reroute_reason"] == "shelter_full"
    assert pushed["recommended"]["shelter_id"] != target
    assert_accessible_plan(ctx, pushed)
    assert ctx.db.one("SELECT profile FROM evacuation_plans WHERE citizen_id=?", (me,))["profile"] == "wheelchair"


def test_priority_citizen_rerouted_after_road_closure_stays_accessible(client, ctx, auth_headers, events):
    me = cid(21)
    set_role(client, me)
    # marşrutunda ən azı bir seqment olan başlanğıcı dinamik tap
    plan = None
    for i in range(10):
        for j in range(9):
            p = evac(client, lat=40.36 + i * 0.01, lon=49.80 + j * 0.02, citizen_id=me)
            if p["recommended"] and p["recommended"]["route"]["edge_ids"]:
                plan = p
                break
        if plan:
            break
    assert plan is not None
    edge = plan["recommended"]["route"]["edge_ids"][0]
    events.clear()
    r = client.post(f"{DISPATCH}/roads/{edge}/close", json={"reason": "test"}, headers=auth_headers)
    assert r.status_code == 200, r.text
    reroutes = [e for e in events if e["event"] == "reroute" and e["citizen_id"] == me]
    assert reroutes
    pushed = reroutes[-1]["data"]["plan"]
    assert pushed["reroute_reason"] == "road_closed"
    assert_accessible_plan(ctx, pushed)
    assert edge not in pushed["recommended"]["route"]["edge_ids"]


# ---------------------------------------------------------------------------
# Verilənlər bazası miqrasiyası
# ---------------------------------------------------------------------------
OLD_SCHEMA = """
CREATE TABLE shelters (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, type TEXT NOT NULL, district_id TEXT NOT NULL,
    lat REAL NOT NULL, lon REAL NOT NULL, capacity INTEGER NOT NULL, occupancy INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'open', updated_at REAL NOT NULL
);
CREATE TABLE sos_signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT, citizen_id TEXT NOT NULL, type TEXT NOT NULL,
    lat REAL NOT NULL, lon REAL NOT NULL, district_id TEXT NOT NULL, note TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active', brigade_id TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL
);
CREATE TABLE evacuation_plans (
    citizen_id TEXT PRIMARY KEY, origin_lat REAL NOT NULL, origin_lon REAL NOT NULL,
    shelter_id TEXT NOT NULL, edge_ids TEXT NOT NULL, updated_at REAL NOT NULL
);
CREATE TABLE brigades (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, type TEXT NOT NULL, district_id TEXT NOT NULL,
    lat REAL NOT NULL, lon REAL NOT NULL, status TEXT NOT NULL DEFAULT 'available',
    target_lat REAL, target_lon REAL, assigned_alert_id INTEGER, assigned_sos_id INTEGER, updated_at REAL NOT NULL
);
"""


def _cols(path, table):
    conn = sqlite3.connect(path)
    try:
        return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    finally:
        conn.close()


def test_migration_adds_new_columns_to_old_db(tmp_path):
    path = str(tmp_path / "old.db")
    conn = sqlite3.connect(path)
    conn.executescript(OLD_SCHEMA)
    conn.execute("INSERT INTO sos_signals (citizen_id,type,lat,lon,district_id,created_at,updated_at)"
                 " VALUES ('citizen-0001','fire',40.38,49.9,'sabail',1,1)")
    conn.execute("INSERT INTO shelters (id,name,type,district_id,lat,lon,capacity,updated_at)"
                 " VALUES ('shX','Köhnə','park','sabail',40.38,49.9,100,1)")
    conn.commit()
    conn.close()
    assert "priority" not in _cols(path, "sos_signals")

    db = Database(path)
    try:
        assert {"priority", "citizen_role"} <= set(db.query("SELECT * FROM sos_signals")[0])
        old = db.one("SELECT * FROM sos_signals WHERE citizen_id='citizen-0001'")
        assert old["priority"] == 2 and old["citizen_role"] == "citizen"
        sh = db.one("SELECT * FROM shelters WHERE id='shX'")
        assert sh["accessible"] == 1 and sh["name_en"] == ""
        assert db.one("SELECT name FROM sqlite_master WHERE type='index' AND name='idx_sos_priority'")
    finally:
        db.close()

    assert {"priority", "citizen_role"} <= _cols(path, "sos_signals")
    assert "profile" in _cols(path, "evacuation_plans")
    assert {"accessible", "name_en"} <= _cols(path, "shelters")
    assert {"accessible_transport", "name_en"} <= _cols(path, "brigades")
    assert "role" in _cols(path, "citizen_profiles")

    # təkrar açılış idempotentdir
    Database(path).close()


# ---------------------------------------------------------------------------
# i18n tamlığı (fayl test zamanı oxunur — açarlar sərt kodlanmır)
# ---------------------------------------------------------------------------
ENTRY_RE = re.compile(r'^\s*"((?:[^"\\]|\\.)+)"\s*:\s*("(?:[^"\\]|\\.)*"|\'(?:[^\'\\]|\\.)*\'|`[^`]*`)\s*,?\s*$')
PLACEHOLDER_RE = re.compile(r"\{(\w+)\}")


def _parse_dicts() -> dict[str, dict[str, str]]:
    text = I18N_PATH.read_text(encoding="utf-8")
    m = re.search(r"const\s+DICT\s*=\s*\{(.*?)\n\s*\};", text, re.S)
    assert m, "DICT tapılmadı"
    body = m.group(1)
    out: dict[str, dict[str, str]] = {}
    for lang in ("az", "ru", "en"):
        bm = re.search(rf"\n\s*{lang}\s*:\s*\{{(.*?)\n\s*\}},?", body, re.S)
        assert bm, f"{lang} lüğəti tapılmadı"
        entries: dict[str, str] = {}
        for line in bm.group(1).splitlines():
            s = line.strip()
            if not s or s.startswith("//") or s.startswith("/*") or s.startswith("*"):
                continue
            em = ENTRY_RE.match(line)
            assert em, f"{lang}: tanınmayan sətir: {line!r}"
            key, val = em.group(1), em.group(2)[1:-1]
            assert key not in entries, f"{lang}: təkrar açar {key}"
            entries[key] = val
        out[lang] = entries
    return out


def test_i18n_dictionaries_have_same_keys():
    d = _parse_dicts()
    az, ru, en = set(d["az"]), set(d["ru"]), set(d["en"])
    assert len(az) > 50
    assert az - en == set(), f"en-də çatışmır: {sorted(az - en)}"
    assert az - ru == set(), f"ru-da çatışmır: {sorted(az - ru)}"
    assert en - az == set(), f"az-da çatışmır: {sorted(en - az)}"
    assert ru - az == set(), f"az-da çatışmır: {sorted(ru - az)}"


def test_i18n_no_empty_values():
    for lang, entries in _parse_dicts().items():
        empty = [k for k, v in entries.items() if not v.strip()]
        assert not empty, f"{lang}: boş dəyərlər {empty}"


def test_i18n_placeholders_match():
    d = _parse_dicts()
    bad = []
    for key, az_val in d["az"].items():
        ph = set(PLACEHOLDER_RE.findall(az_val))
        for lang in ("ru", "en"):
            if key in d[lang] and set(PLACEHOLDER_RE.findall(d[lang][key])) != ph:
                bad.append((lang, key))
    assert not bad, f"yer tutucular uyğun gəlmir: {bad}"
