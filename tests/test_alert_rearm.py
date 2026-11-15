"""Komanda rəyi: (1) məntəqə 95%-i HƏR DƏFƏ aşanda həyəcan gəlməlidir; (2) "Hamısını bağla"."""

SHELTER = "/api/v1/dispatch/shelters"
ALERTS = "/api/v1/dispatch/alerts"


def set_occ(client, headers, sid, pct, cap):
    r = client.patch(f"{SHELTER}/{sid}", headers=headers, json={"occupancy": round(cap * pct / 100)})
    assert r.status_code == 200, r.text
    return r.json()


def overflow_alerts(ctx, sid):
    return ctx.db.query("SELECT * FROM alerts WHERE kind='shelter_overflow' AND shelter_id=? ORDER BY id", (sid,))


def cap_of(ctx, sid):
    return ctx.db.one("SELECT capacity FROM shelters WHERE id=?", (sid,))["capacity"]


def test_new_alert_on_each_crossing_after_dismiss(client, ctx, auth_headers):
    sid = "sh05"; cap = cap_of(ctx, sid)
    set_occ(client, auth_headers, sid, 97, cap)
    first = overflow_alerts(ctx, sid)
    assert len(first) == 1 and first[0]["status"] == "pending"
    client.post(f"{ALERTS}/{first[0]['id']}/dismiss", headers=auth_headers)
    set_occ(client, auth_headers, sid, 80, cap)          # 95%-dən aşağı
    set_occ(client, auth_headers, sid, 98, cap)          # yenidən aşır (5 dəqiqəlik cooldown daxilində)
    alerts = overflow_alerts(ctx, sid)
    assert len(alerts) == 2 and alerts[-1]["status"] == "pending"


def test_new_alert_on_each_crossing_after_confirm(client, ctx, auth_headers):
    sid = "sh07"; cap = cap_of(ctx, sid)
    set_occ(client, auth_headers, sid, 96, cap)
    a = overflow_alerts(ctx, sid)[0]
    client.post(f"{ALERTS}/{a['id']}/confirm", headers=auth_headers)
    set_occ(client, auth_headers, sid, 50, cap)
    set_occ(client, auth_headers, sid, 99, cap)
    assert [x["status"] for x in overflow_alerts(ctx, sid)] == ["confirmed", "pending"]


def test_pending_alert_auto_resolves_when_below_and_rearms(client, ctx, auth_headers):
    sid = "sh09"; cap = cap_of(ctx, sid)
    set_occ(client, auth_headers, sid, 97, cap)
    set_occ(client, auth_headers, sid, 70, cap)          # qərarsız aşağı düşdü -> avtomatik bağlanır
    alerts = overflow_alerts(ctx, sid)
    assert len(alerts) == 1 and alerts[0]["status"] == "auto_resolved" and alerts[0]["decided_by"] == "system"
    set_occ(client, auth_headers, sid, 97, cap)          # yeni keçid -> yeni həyəcan
    assert [x["status"] for x in overflow_alerts(ctx, sid)] == ["auto_resolved", "pending"]


def test_no_spam_while_staying_above_threshold(client, ctx, auth_headers):
    sid = "sh10"; cap = cap_of(ctx, sid)
    set_occ(client, auth_headers, sid, 96, cap)
    a = overflow_alerts(ctx, sid)[0]
    client.post(f"{ALERTS}/{a['id']}/dismiss", headers=auth_headers)
    set_occ(client, auth_headers, sid, 97, cap)          # aşağı düşmədən dəyişir
    set_occ(client, auth_headers, sid, 99, cap)
    assert len(overflow_alerts(ctx, sid)) == 1           # təkrar həyəcan yoxdur


def test_exactly_95_counts_as_below(client, ctx, auth_headers):
    sid = "sh12"; cap = cap_of(ctx, sid)                 # 750 -> 95% = 712.5
    set_occ(client, auth_headers, sid, 96, cap)
    a = overflow_alerts(ctx, sid)[0]
    client.post(f"{ALERTS}/{a['id']}/dismiss", headers=auth_headers)
    client.patch(f"{SHELTER}/{sid}", headers=auth_headers, json={"occupancy": int(cap * 0.95)})
    set_occ(client, auth_headers, sid, 97, cap)
    assert len(overflow_alerts(ctx, sid)) == 2


def test_citizen_leaving_shelter_auto_resolves(client, ctx, auth_headers):
    """Doluluq vətəndaş çıxanda azalsa da (update_shelter delta=-1) yoxlanılır."""
    sid = "sh11"; cap = cap_of(ctx, sid)
    target = int(cap * 0.95) + 1                         # 95%-dən 1 nəfər yuxarı
    client.patch(f"{SHELTER}/{sid}", headers=auth_headers, json={"occupancy": target - 1})
    client.post("/api/v1/status", json={"citizen_id": "leaver-0001", "status": "safe", "shelter_id": sid})
    assert overflow_alerts(ctx, sid)[-1]["status"] == "pending"
    client.post("/api/v1/status", json={"citizen_id": "leaver-0001", "status": "evacuating"})
    assert overflow_alerts(ctx, sid)[-1]["status"] == "auto_resolved"


def test_dismiss_all(client, ctx, auth_headers, events):
    for sid in ("sh05", "sh07", "sh09"):
        set_occ(client, auth_headers, sid, 97, cap_of(ctx, sid))
    r = client.post(f"{ALERTS}/dismiss-all", headers=auth_headers)
    assert r.status_code == 200 and r.json()["dismissed"] == 3
    assert ctx.db.scalar("SELECT COUNT(*) FROM alerts WHERE status='pending'") == 0
    row = ctx.db.one("SELECT * FROM audit_log WHERE action='alert_dismiss_all'")
    assert row is not None
    assert client.post(f"{ALERTS}/dismiss-all", headers=auth_headers).json()["dismissed"] == 0   # idempotent


def test_dismiss_all_by_kind(client, ctx, auth_headers):
    set_occ(client, auth_headers, "sh05", 97, cap_of(ctx, "sh05"))
    for i in range(4):
        client.post("/api/v1/sos", json={"citizen_id": f"kind-test-{i:04d}", "type": "fire", "lat": 40.39 + i * 0.0004, "lon": 49.84})
    r = client.post(f"{ALERTS}/dismiss-all", headers=auth_headers, params={"kind": "shelter_overflow"}).json()
    assert r["dismissed"] == 1
    assert ctx.db.scalar("SELECT COUNT(*) FROM alerts WHERE status='pending' AND kind='sos_cluster'") == 1
    assert client.post(f"{ALERTS}/dismiss-all", headers=auth_headers, params={"kind": "bogus"}).status_code == 422


def test_dismiss_all_requires_permission(client):
    from app.core.security import create_token
    assert client.post(f"{ALERTS}/dismiss-all").status_code == 401
    bad = {"Authorization": "Bearer " + create_token("x", "citizen")}
    assert client.post(f"{ALERTS}/dismiss-all", headers=bad).status_code == 403
