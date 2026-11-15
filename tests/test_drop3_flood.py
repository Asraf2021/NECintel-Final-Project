"""Challenge Drop 3: SOS daşqını (botnet hücumu) — rate limit, dublikat filtri, karantin, JWT/RBAC, gecikmə."""
from __future__ import annotations

import random
import time

import pytest

import app.config
from app.core.security import create_token
from app.services import analytics
from app.services.flood import FloodGuard
from tests.helpers import DISPATCH, SOS, alerts, cid, send_sos
from tests.test_mygov import bearer, mygov_login

THRESHOLD = 10
BASE_LAT, BASE_LON = 40.40, 49.85


# ---------------------------------------------------------------------------
# Köməkçilər
# ---------------------------------------------------------------------------
@pytest.fixture
def botnet(ctx, monkeypatch):
    """Botnet simulyasiyası: çoxlu IP olduğunu təqlid etmək üçün IP/vətəndaş limitləri çox yüksək, daşqın həddi kiçik."""
    s = app.config.settings
    monkeypatch.setattr(s, "rate_limit_sos_per_min", 100_000)
    monkeypatch.setattr(s, "rate_limit_general_per_min", 10**7)
    monkeypatch.setattr(s, "sos_flood_threshold", THRESHOLD)
    ctx.flood.reset()
    yield ctx.flood
    ctx.flood.reset()


def grid_point(i: int) -> tuple[float, float]:
    """Bir-birindən ~300 m aralı nöqtələr (birləşmə radiusundan çox uzaq) — şəhər üzrə səpələnmiş."""
    return BASE_LAT + (i % 25) * 0.003, BASE_LON + (i // 25) * 0.003


def activate_flood(client, guard, prefix: int = 9000, sos_type: str = "medical") -> None:
    """Fərqli nöqtələrə anonim SOS göndərərək daşqın rejimini işə salır."""
    i = 0
    while not guard.active:
        lat, lon = grid_point(i)
        r = send_sos(client, cid(prefix + i), sos_type, lat, lon)
        assert r.status_code == 201, r.text
        i += 1
        assert i < THRESHOLD * 3, "daşqın rejimi aktivləşmədi"


def count(ctx, status: str) -> int:
    return ctx.db.scalar("SELECT COUNT(*) FROM sos_signals WHERE status=?", (status,)) or 0


def login(client, username: str, password: str) -> dict:
    r = client.post("/api/v1/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


@pytest.fixture
def operator_headers(client):
    return login(client, "operator", "operator123")


# ---------------------------------------------------------------------------
# Standart müdafiə (monkeypatch olmadan)
# ---------------------------------------------------------------------------
def test_single_ip_flood_hits_per_ip_limit(client, ctx):
    per_ip = app.config.settings.rate_limit_sos_per_min * 5
    codes = [send_sos(client, cid(i), "fire", *grid_point(i)).status_code for i in range(per_ip + 20)]
    assert set(codes) <= {201, 429}
    assert codes.count(201) <= per_ip
    assert codes.count(429) >= 20
    assert (ctx.db.scalar("SELECT COUNT(*) FROM sos_signals") or 0) <= per_ip


def test_per_citizen_limit(client):
    limit = app.config.settings.rate_limit_sos_per_min
    types = ["fire", "collapse", "flood", "medical"]
    codes = [send_sos(client, cid(1), types[i % 4], 40.40 + i * 0.001, 49.85).status_code for i in range(limit + 4)]
    assert codes.count(429) >= 4
    assert codes[:limit].count(201) == limit


def test_exact_duplicate_same_citizen_type(client, ctx):
    r1 = send_sos(client, cid(7), "fire")
    r2 = send_sos(client, cid(7), "fire")
    assert r1.status_code == r2.status_code == 201
    assert r1.json()["duplicate"] is False and r2.json()["duplicate"] is True
    assert r2.json()["sos"]["id"] == r1.json()["sos"]["id"]
    assert ctx.db.scalar("SELECT COUNT(*) FROM sos_signals WHERE citizen_id=?", (cid(7),)) == 1


# ---------------------------------------------------------------------------
# Botnet simulyasiyası
# ---------------------------------------------------------------------------
def test_botnet_600_sos_quarantined_and_panel_bounded(client, ctx, botnet, auth_headers):
    rnd = random.Random(42)
    codes, dups = [], 0
    for i in range(600):
        if i % 5 == 0:   # sıx "qaynar nöqtə"
            lat, lon = 40.41 + rnd.uniform(-0.0003, 0.0003), 49.86 + rnd.uniform(-0.0003, 0.0003)
        else:            # şəhər üzrə təsadüfi
            lat, lon = rnd.uniform(40.35, 40.47), rnd.uniform(49.75, 50.00)
        r = send_sos(client, f"bot-{i:06d}", rnd.choice(["fire", "collapse", "flood", "medical"]), lat, lon)
        codes.append(r.status_code)
        dups += r.json()["duplicate"] if r.status_code == 201 else 0
    assert set(codes) == {201}, "5xx/4xx olmamalıdır"
    assert botnet.active
    suspect, active = count(ctx, "suspect"), count(ctx, "active")
    assert suspect > 0 and dups > 0
    assert active <= THRESHOLD   # həddə qədər olanlar aktivdir, qalanı karantin/birləşmə
    analytics.invalidate()
    ov = client.get(f"{DISPATCH}/overview", headers=auth_headers)
    assert ov.status_code == 200
    body = ov.json()
    assert body["kpi"]["sos_active"] <= THRESHOLD
    assert body["kpi"]["sos_quarantine"] == suspect
    assert body["kpi"]["sos_merged"] >= dups
    assert all(s["status"] not in ("suspect", "spam") for s in body["sos"])
    assert body["flood"]["active"] is True
    fl = client.get(f"{DISPATCH}/flood", headers=auth_headers).json()
    assert fl["active"] and fl["quarantine"] == suspect


def test_near_duplicates_merged_in_flood_mode(client, ctx, botnet):
    activate_flood(client, botnet)
    n = 25
    resps = [send_sos(client, cid(100 + i), "fire", 40.30 + (i % 3) * 0.00005, 49.70).json() for i in range(n)]
    rows = ctx.db.query("SELECT * FROM sos_signals WHERE type='fire'")
    assert len(rows) == 1
    assert rows[0]["status"] == "suspect" and rows[0]["dup_count"] == n - 1
    assert all(r["duplicate"] is True for r in resps[1:])
    assert all(r["sos"]["id"] == rows[0]["id"] for r in resps)


def test_no_false_red_alert_from_bots(client, ctx, botnet):
    activate_flood(client, botnet)
    before = len(alerts(ctx, kind="sos_cluster"))
    for i in range(20):  # 100 m daxilində 20 fərqli ID
        lat = 40.30 + (i % 5) * 0.00018
        lon = 49.70 + (i // 5) * 0.00018
        assert send_sos(client, cid(500 + i), "collapse", lat, lon).status_code == 201
    assert len(alerts(ctx, kind="sos_cluster")) == before
    assert count(ctx, "suspect") >= 1


def test_verified_citizen_bypasses_quarantine(client, ctx, botnet, events, tmp_path, monkeypatch):
    monkeypatch.setattr(app.config.settings, "upload_dir", str(tmp_path / "uploads"))
    device = cid(4242)
    token = mygov_login(client, "p1", device_id=device)
    r = client.put("/api/v1/me/disability", json={"enabled": True}, headers=bearer(token))
    assert r.status_code == 200, r.text
    activate_flood(client, botnet)
    r = client.post(SOS, json={"citizen_id": device, "type": "fire", "lat": 40.30, "lon": 49.70},
                    headers=bearer(token))
    assert r.status_code == 201, r.text
    sos = r.json()["sos"]
    assert sos["status"] == "active" and sos["priority"] == 1
    published = {(e["event"], e["data"].get("sos", {}).get("id")) for e in events}
    assert ("sos_new", sos["id"]) in published and ("sos_priority", sos["id"]) in published


# ---------------------------------------------------------------------------
# Karantin qərarları
# ---------------------------------------------------------------------------
def test_quarantine_release(client, ctx, botnet, auth_headers):
    activate_flood(client, botnet)
    for i in range(5):
        send_sos(client, cid(700 + i), "fire", *grid_point(100 + i))
    n = count(ctx, "suspect")
    assert n >= 5
    active_before = count(ctx, "active")
    r = client.post(f"{DISPATCH}/quarantine/release", headers=auth_headers)
    assert r.status_code == 200, r.text
    assert count(ctx, "suspect") == 0 and count(ctx, "active") == active_before + n
    assert ctx.db.one("SELECT * FROM audit_log WHERE action='quarantine_release'")


def test_quarantine_discard(client, ctx, botnet, auth_headers):
    activate_flood(client, botnet)
    for i in range(5):
        send_sos(client, cid(800 + i), "fire", *grid_point(100 + i))
    n = count(ctx, "suspect")
    r = client.post(f"{DISPATCH}/quarantine/discard", headers=auth_headers)
    assert r.status_code == 200, r.text
    assert count(ctx, "suspect") == 0 and count(ctx, "spam") == n
    assert ctx.db.one("SELECT * FROM audit_log WHERE action='quarantine_discard'")


@pytest.mark.parametrize("path", ["release", "discard"])
def test_quarantine_requires_sos_manage(client, ctx, botnet, operator_headers, path):
    activate_flood(client, botnet)
    n = count(ctx, "suspect")
    assert client.post(f"{DISPATCH}/quarantine/{path}").status_code == 401
    assert client.post(f"{DISPATCH}/quarantine/{path}", headers=operator_headers).status_code == 403
    assert count(ctx, "suspect") == n


# ---------------------------------------------------------------------------
# JWT / RBAC
# ---------------------------------------------------------------------------
def test_operator_read_only(client, ctx, operator_headers):
    sos_id = send_sos(client, cid(1)).json()["sos"]["id"]
    assert client.get(f"{DISPATCH}/overview", headers=operator_headers).status_code == 200
    assert client.get(f"{DISPATCH}/flood", headers=operator_headers).status_code == 200
    assert client.post(f"{DISPATCH}/sos/{sos_id}/assign", json={}, headers=operator_headers).status_code == 403
    assert client.post(f"{DISPATCH}/alerts/1/confirm", headers=operator_headers).status_code == 403
    assert client.patch(f"{DISPATCH}/brigades/br03", json={"status": "offline"},
                        headers=operator_headers).status_code == 403
    assert ctx.db.one("SELECT status FROM sos_signals WHERE id=?", (sos_id,))["status"] == "active"


def test_dispatcher_can_assign(client, auth_headers):
    sos_id = send_sos(client, cid(1)).json()["sos"]["id"]
    r = client.post(f"{DISPATCH}/sos/{sos_id}/assign", json={}, headers=auth_headers)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "assigned"


def test_bad_tokens_on_assign(client, auth_headers):
    sos_id = send_sos(client, cid(1)).json()["sos"]["id"]
    url = f"{DISPATCH}/sos/{sos_id}/assign"
    good = auth_headers["Authorization"].split(" ", 1)[1]
    h, p, s = good.split(".")
    tampered = f"{h}.{p}.{s[:-2]}{'AA' if s[-2:] != 'AA' else 'BB'}"
    expired = create_token("dispatcher", "dispatcher", ttl=-10)
    assert client.post(url, json={}).status_code == 401
    assert client.post(url, json={}, headers=bearer(tampered)).status_code == 401
    assert client.post(url, json={}, headers=bearer(expired)).status_code == 401
    assert client.post(url, json={}, headers=bearer(create_token("x", "citizen"))).status_code == 403


# ---------------------------------------------------------------------------
# SSE seyrəltmə
# ---------------------------------------------------------------------------
def test_sse_throttled_during_flood(client, ctx, botnet, events):
    for i in range(300):
        send_sos(client, f"bot-{i:06d}", "fire", *grid_point(i))
    suspect = count(ctx, "suspect")
    sos_new = sum(1 for e in events if e["event"] == "sos_new")
    flood_status = sum(1 for e in events if e["event"] == "flood_status")
    assert suspect >= 250
    assert sos_new <= THRESHOLD
    assert 1 <= flood_status < suspect / 10


# ---------------------------------------------------------------------------
# FloodGuard histerezisi (vahid testlər, açıq vaxt ştampları)
# ---------------------------------------------------------------------------
@pytest.fixture
def guard(monkeypatch):
    s = app.config.settings
    monkeypatch.setattr(s, "sos_flood_threshold", 10)
    monkeypatch.setattr(s, "sos_flood_window_s", 10.0)
    monkeypatch.setattr(s, "sos_flood_cooldown_s", 30.0)
    return FloodGuard()


def test_guard_activates_at_threshold(guard):
    t0 = 1000.0
    for i in range(9):
        guard.record(t0 + i * 0.1)
    assert not guard.active
    guard.record(t0 + 1.0)
    assert guard.active and guard.since == t0 + 1.0 and guard.activations == 1


def test_guard_stays_active_while_rate_above_half(guard):
    t0 = 1000.0
    for i in range(10):
        guard.record(t0 + i * 0.1)
    # 10 s pəncərədə 5 hadisə (= hədd/2) — 120 s ərzində rejim sönməməlidir
    t = t0 + 1
    for _ in range(60):
        t += 2.0
        guard.record(t)
        assert guard.active


def test_guard_deactivates_only_after_cooldown(guard):
    t0 = 1000.0
    for i in range(10):
        guard.record(t0 + i * 0.1)
    assert guard.update(t0 + 15) is True        # sakitlik başladı
    assert guard.update(t0 + 15 + 29) is True   # soyuma müddəti hələ bitməyib
    assert guard.update(t0 + 15 + 30) is False
    assert guard.since is None
    # Sakitlik dövründə axın yenidən həddə çatsa — yenidən aktivləşir
    t1 = t0 + 100
    for i in range(10):
        guard.record(t1 + i * 0.1)
    assert guard.active and guard.activations == 2


def test_guard_calm_timer_resets_on_burst(guard):
    t0 = 1000.0
    for i in range(10):
        guard.record(t0 + i * 0.1)
    assert guard.update(t0 + 15)                # sakitlik başladı
    for i in range(10):                        # yeni partlayış — sakitlik sayğacı sıfırlanır
        guard.record(t0 + 40 + i * 0.1)
    assert guard.update(t0 + 55)                # yeni sakitlik başlanğıcı
    assert guard.update(t0 + 80)                # 25 s — hələ aktiv
    assert not guard.update(t0 + 86)


# ---------------------------------------------------------------------------
# Gecikmə (proses daxilində, TestClient)
# ---------------------------------------------------------------------------
def test_latency_under_mass_requests(client, ctx, botnet, auth_headers):
    rnd = random.Random(7)
    durations = []
    for i in range(600):
        lat, lon = rnd.uniform(40.35, 40.47), rnd.uniform(49.75, 50.00)
        t = time.perf_counter()
        r = send_sos(client, f"lat-{i:06d}", "fire", lat, lon)
        durations.append(time.perf_counter() - t)
        assert r.status_code == 201
    avg = sum(durations) / len(durations)
    assert avg < 0.3, f"orta gecikmə {avg * 1000:.1f} ms"
    analytics.invalidate()
    t = time.perf_counter()
    r = client.get(f"{DISPATCH}/overview", headers=auth_headers)
    assert r.status_code == 200
    assert time.perf_counter() - t < 0.3
