"""Təhlükəsizlik: autentifikasiya, RBAC, rate limit, başlıqlar, giriş validasiyası, parol heşi."""
from __future__ import annotations

import base64
import json

import pytest

from app.config import settings
from app.core import security
from app.core.security import (RateLimiter, create_token, decode_token, has_permission, hash_password,
                               role_permissions, verify_password)
from app.schemas import SosCreate

from .helpers import DISPATCH, EVAC, SOS, cid, send_sos

# (metod, yol, gövdə) — bütün qorunan dispetçer endpoint-ləri
PROTECTED = [
    ("GET", f"{DISPATCH}/overview", None),
    ("GET", f"{DISPATCH}/stream", None),
    ("GET", f"{DISPATCH}/sos", None),
    ("POST", f"{DISPATCH}/sos/1/assign", {}),
    ("POST", f"{DISPATCH}/sos/1/resolve", None),
    ("GET", f"{DISPATCH}/alerts", None),
    ("POST", f"{DISPATCH}/alerts/1/confirm", None),
    ("POST", f"{DISPATCH}/alerts/1/dismiss", None),
    ("PATCH", f"{DISPATCH}/shelters/sh01", {"occupancy": 1}),
    ("PATCH", f"{DISPATCH}/brigades/br01", {"status": "busy"}),
    ("POST", f"{DISPATCH}/roads/e01/close", {}),
    ("POST", f"{DISPATCH}/roads/e01/open", None),
    ("GET", f"{DISPATCH}/audit", None),
    ("POST", f"{DISPATCH}/demo/scenario", {"name": "reset"}),
    ("GET", "/api/v1/auth/me", None),
]


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# 401 / 403
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("method,path,body", PROTECTED)
def test_protected_endpoints_require_token(client, method, path, body):
    r = client.request(method, path, json=body)
    assert r.status_code == 401
    assert r.json() == {"detail": "authentication_required"}


@pytest.mark.parametrize("method,path,body", PROTECTED)
def test_protected_endpoints_forbid_unknown_role(client, method, path, body):
    token = create_token("x", "unknown_role")
    r = client.request(method, path, json=body, headers=_bearer(token))
    assert r.status_code == 403
    assert r.json() == {"detail": "permission_denied"}


def test_unauthorized_request_changes_nothing(client, ctx):
    before = ctx.db.one("SELECT occupancy FROM shelters WHERE id='sh01'")["occupancy"]
    client.patch(f"{DISPATCH}/shelters/sh01", json={"occupancy": 1})
    client.post(f"{DISPATCH}/roads/e01/close", json={}, headers=_bearer(create_token("x", "unknown_role")))
    assert ctx.db.one("SELECT occupancy FROM shelters WHERE id='sh01'")["occupancy"] == before
    assert ctx.routing.closed_edges == set()


def test_tampered_token_rejected(client, auth_headers):
    token = auth_headers["Authorization"][7:]
    header, payload, sig = token.split(".")
    # (a) imza dəyişdirilib
    bad_sig = sig[:-2] + ("AA" if not sig.endswith("AA") else "BB")
    assert client.get(f"{DISPATCH}/overview", headers=_bearer(f"{header}.{payload}.{bad_sig}")).status_code == 401
    # (b) payload dəyişdirilib (rol -> admin), imza köhnə qalıb
    data = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    data["role"] = "admin"
    forged = base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()
    assert client.get(f"{DISPATCH}/overview", headers=_bearer(f"{header}.{forged}.{sig}")).status_code == 401
    # (c) orijinal token işləyir
    assert client.get(f"{DISPATCH}/overview", headers=auth_headers).status_code == 200


def test_expired_token_rejected(client):
    token = create_token("dispatcher", "dispatcher", ttl=-10)
    assert decode_token(token) is None
    assert client.get(f"{DISPATCH}/overview", headers=_bearer(token)).status_code == 401


def test_token_signed_with_other_key_rejected(client, monkeypatch):
    monkeypatch.setattr(settings, "secret_key", "attacker-key")
    forged = create_token("dispatcher", "admin")
    monkeypatch.undo()
    assert client.get(f"{DISPATCH}/overview", headers=_bearer(forged)).status_code == 401


def test_alg_none_token_rejected(client):
    def b64(obj):
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()
    token = f"{b64({'alg': 'none', 'typ': 'JWT'})}.{b64({'sub': 'x', 'role': 'admin', 'exp': 9999999999})}."
    assert decode_token(token) is None
    assert client.get(f"{DISPATCH}/overview", headers=_bearer(token)).status_code == 401


@pytest.mark.parametrize("value", ["", "Bearer", "Bearer ", "Bearer not.a.token", "Basic ZGlzcGF0Y2hlcjp4", "garbage"])
def test_malformed_authorization_header_rejected(client, value):
    assert client.get(f"{DISPATCH}/overview", headers={"Authorization": value}).status_code == 401


def test_token_roundtrip_and_query_param(client):
    token = create_token("dispatcher", "dispatcher")
    data = decode_token(token)
    assert data["sub"] == "dispatcher" and data["role"] == "dispatcher" and data["exp"] > data["iat"]
    # SSE üçün token sorğu parametri ilə də qəbul olunur
    assert client.get(f"{DISPATCH}/overview", params={"token": token}).status_code == 200


def test_rbac_matrix():
    assert has_permission("admin", "anything:at_all") is True
    assert has_permission("dispatcher", "alert:decide") is True
    assert has_permission("dispatcher", "user:manage") is False
    assert has_permission("unknown_role", "ops:read") is False
    assert has_permission("", "ops:read") is False
    assert role_permissions("ghost") == set()


def test_admin_can_use_dispatcher_endpoints(client, admin_headers):
    r = client.patch(f"{DISPATCH}/brigades/br01", json={"status": "busy"}, headers=admin_headers)
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------
def test_login_success(client):
    r = client.post("/api/v1/auth/login", json={"username": "dispatcher", "password": "dispatcher123"})
    assert r.status_code == 200
    body = r.json()
    assert body["user"]["role"] == "dispatcher" and body["expires_in"] == settings.token_ttl_seconds
    assert decode_token(body["token"])["sub"] == "dispatcher"
    assert "password" not in json.dumps(body)


@pytest.mark.parametrize("username,password", [
    ("dispatcher", "wrong-password"),
    ("dispatcher", "Dispatcher123"),
    ("nobody", "dispatcher123"),
    ("admin' OR '1'='1", "x"),
    ("dispatcher'--", "dispatcher123"),
])
def test_login_wrong_credentials_401(client, ctx, username, password):
    r = client.post("/api/v1/auth/login", json={"username": username, "password": password})
    assert r.status_code == 401
    assert r.json() == {"detail": "invalid_credentials"}      # istifadəçinin mövcudluğu sızdırılmır
    assert ctx.db.scalar("SELECT COUNT(*) FROM audit_log WHERE action='login_failed'") == 1
    assert ctx.db.scalar("SELECT COUNT(*) FROM users") == 2


@pytest.mark.parametrize("payload", [{}, {"username": "dispatcher"}, {"username": "", "password": "x"},
                                     {"username": "u" * 65, "password": "x"}, {"username": "u", "password": "p" * 129}])
def test_login_invalid_payload_422(client, payload):
    assert client.post("/api/v1/auth/login", json=payload).status_code == 422


def test_login_rate_limit(client, monkeypatch):
    monkeypatch.setattr(settings, "rate_limit_login_per_min", 3)
    for _ in range(3):
        assert client.post("/api/v1/auth/login", json={"username": "dispatcher", "password": "bad"}).status_code == 401
    r = client.post("/api/v1/auth/login", json={"username": "dispatcher", "password": "bad"})
    assert r.status_code == 429
    assert r.headers["Retry-After"] == "60"
    # limit dolduqdan sonra düzgün parol da qəbul edilmir (brute-force qorunması)
    ok = client.post("/api/v1/auth/login", json={"username": "dispatcher", "password": "dispatcher123"})
    assert ok.status_code == 429


def test_login_default_rate_limit_value(client):
    limit = settings.rate_limit_login_per_min
    codes = [client.post("/api/v1/auth/login", json={"username": "a", "password": "b"}).status_code
             for _ in range(limit + 1)]
    assert codes[:limit] == [401] * limit and codes[limit] == 429


# ---------------------------------------------------------------------------
# Rate limit
# ---------------------------------------------------------------------------
def test_sos_rate_limit_per_citizen(client, ctx, monkeypatch):
    monkeypatch.setattr(settings, "rate_limit_sos_per_min", 2)
    assert send_sos(client, cid(1), "fire").status_code == 201
    assert send_sos(client, cid(1), "flood").status_code == 201
    r = send_sos(client, cid(1), "medical")
    assert r.status_code == 429 and r.headers["Retry-After"] == "60"
    assert send_sos(client, cid(2), "fire").status_code == 201      # başqa vətəndaş bloklanmır
    assert ctx.db.scalar("SELECT COUNT(*) FROM sos_signals") == 3


def test_sos_rate_limit_per_ip(client, ctx, monkeypatch):
    monkeypatch.setattr(settings, "rate_limit_sos_per_min", 1)      # IP limiti = 5
    codes = [send_sos(client, cid(n), "fire").status_code for n in range(1, 8)]
    assert codes == [201] * 5 + [429] * 2
    assert ctx.db.scalar("SELECT COUNT(*) FROM sos_signals") == 5


def test_sos_default_limit_blocks_spam(client):
    types = ["fire", "collapse", "flood", "medical"]
    codes = [send_sos(client, cid(1), types[i % 4]).status_code for i in range(settings.rate_limit_sos_per_min + 1)]
    assert codes[-1] == 429 and set(codes[:-1]) == {201}


def test_general_rate_limit(client, monkeypatch):
    monkeypatch.setattr(settings, "rate_limit_general_per_min", 3)
    assert [client.get("/api/v1/meta").status_code for _ in range(4)] == [200, 200, 200, 429]
    assert client.get("/health").status_code == 200       # sağlamlıq yoxlaması limitə düşmür


def test_rate_limiter_unit():
    rl = RateLimiter()
    assert all(rl.allow("k", 3) for _ in range(3))
    assert rl.allow("k", 3) is False
    assert rl.allow("other", 3) is True                   # açarlar müstəqildir
    rl.reset()
    assert rl.allow("k", 3) is True


def test_rate_limiter_sliding_window_expires(monkeypatch):
    rl = RateLimiter()
    now = [1000.0]
    monkeypatch.setattr(security.time, "time", lambda: now[0])
    assert rl.allow("k", 2, window_s=60) and rl.allow("k", 2, window_s=60)
    assert rl.allow("k", 2, window_s=60) is False
    now[0] += 59
    assert rl.allow("k", 2, window_s=60) is False
    now[0] += 2                                           # pəncərə keçdi
    assert rl.allow("k", 2, window_s=60) is True


def test_rate_limiter_cleanup_removes_stale_keys(monkeypatch):
    rl = RateLimiter()
    now = [1000.0]
    monkeypatch.setattr(security.time, "time", lambda: now[0])
    rl._last_cleanup = now[0]
    for i in range(50):
        rl.allow(f"ip-{i}", 5)
    now[0] += 200
    rl.allow("fresh", 5)
    assert set(rl._hits) == {"fresh"}                     # yaddaş sızması yoxdur


# ---------------------------------------------------------------------------
# Başlıqlar və gövdə ölçüsü
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("path,status", [("/health", 200), ("/api/v1/meta", 200), ("/api/v1/nope", 404),
                                         (f"{DISPATCH}/overview", 401), (EVAC, 422)])
def test_security_headers_present(client, path, status):
    r = client.get(path)
    assert r.status_code == status
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert r.headers["X-Frame-Options"] == "DENY"
    assert r.headers["Referrer-Policy"] == "no-referrer"
    csp = r.headers["Content-Security-Policy"]
    assert "default-src 'self'" in csp and "frame-ancestors 'none'" in csp
    assert "'unsafe-eval'" not in csp and "script-src 'self';" in csp
    if path.startswith("/api/"):
        assert r.headers["Cache-Control"] == "no-store"


def test_oversize_body_rejected_413(client, ctx):
    payload = {"citizen_id": cid(1), "type": "fire", "lat": 40.4, "lon": 49.9, "note": "A" * (65 * 1024)}
    r = client.post(SOS, json=payload)
    assert r.status_code == 413
    assert r.json() == {"detail": "payload_too_large"}
    assert ctx.db.scalar("SELECT COUNT(*) FROM sos_signals") == 0
    # limitdən kiçik, amma sahə üçün uzun gövdə -> 422 (413 deyil)
    payload["note"] = "A" * 1000
    assert client.post(SOS, json=payload).status_code == 422


@pytest.mark.xfail(strict=False, reason="BUG (kiçik): main.harden 413 cavabını təhlükəsizlik başlıqları "
                                        "əlavə olunmadan qaytarır (erkən return)")
def test_oversize_response_has_security_headers(client):
    r = client.post(SOS, content=b"x" * (65 * 1024), headers={"Content-Type": "application/json"})
    assert r.status_code == 413
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert "Content-Security-Policy" in r.headers


@pytest.mark.xfail(strict=False, reason="ZƏİFLİK: main.harden yalnız Content-Length başlığını yoxlayır; "
                                        "chunked (Content-Length-siz) böyük gövdə 413 almır və tam oxunur")
def test_oversize_chunked_body_rejected_413(client):
    body = json.dumps({"citizen_id": cid(1), "type": "fire", "lat": 40.4, "lon": 49.9,
                       "note": "A" * (200 * 1024)}).encode()

    def chunks():
        for i in range(0, len(body), 8192):
            yield body[i:i + 8192]

    r = client.post(SOS, content=chunks(), headers={"Content-Type": "application/json"})
    assert r.status_code == 413


# ---------------------------------------------------------------------------
# Giriş validasiyası / injection
# ---------------------------------------------------------------------------
INJECTIONS = [
    "'); DROP TABLE sos_signals;--",
    "\" OR \"1\"=\"1",
    "1; DELETE FROM shelters WHERE 1=1",
    "<script>alert(1)</script>",
    "Robert'); UPDATE users SET role='admin' WHERE ('1'='1",
]


@pytest.mark.parametrize("note", INJECTIONS)
def test_sql_injection_in_note_stored_verbatim(client, ctx, note):
    r = send_sos(client, cid(1), "fire", note=note)
    assert r.status_code == 201
    assert r.json()["sos"]["note"] == note
    assert ctx.db.one("SELECT note FROM sos_signals")["note"] == note     # mətn kimi saxlanılır
    assert ctx.db.scalar("SELECT COUNT(*) FROM sos_signals") == 1
    assert ctx.db.scalar("SELECT COUNT(*) FROM shelters") == 25
    assert ctx.db.scalar("SELECT COUNT(*) FROM users WHERE role='admin'") == 1


@pytest.mark.parametrize("value", ["khatai' OR '1'='1", "x'; DROP TABLE shelters;--", "khatai UNION SELECT * FROM users"])
def test_sql_injection_in_district_is_rejected(client, ctx, value):
    assert client.get("/api/v1/shelters", params={"district_id": value}).status_code in (404, 422)
    assert client.get(EVAC, params={"district_id": value}).status_code == 422
    assert ctx.db.scalar("SELECT COUNT(*) FROM shelters") == 25
    assert len(client.get("/api/v1/shelters").json()["shelters"]) == 17


def test_sql_injection_in_ids_is_rejected(client, ctx, auth_headers):
    assert send_sos(client, "abc' OR 1=1 --", "fire").status_code == 422
    assert client.get("/api/v1/citizens/x'%20OR%20'1'='1").status_code == 422
    r = client.patch(f"{DISPATCH}/shelters/sh01'%20OR%20'1'='1", json={"occupancy": 0}, headers=auth_headers)
    assert r.status_code == 404
    r = client.post(STATUS_URL, json={"citizen_id": cid(1), "status": "safe", "shelter_id": "' OR 1=1 --"})
    assert r.status_code == 200 and r.json()["shelter"] is None
    assert ctx.db.scalar("SELECT COUNT(*) FROM shelters WHERE occupancy=0 AND status='open'") == 0


STATUS_URL = "/api/v1/status"


def test_note_control_characters_are_stripped(client, ctx):
    r = send_sos(client, cid(1), "fire", note="  Yan\x00ğın\x07 var\r\n\x1b[31m 3-cü mərtəbə\t ")
    assert r.status_code == 201
    note = r.json()["sos"]["note"]
    assert note == "Yanğın var[31m 3-cü mərtəbə"
    assert all(ch.isprintable() for ch in note)
    assert ctx.db.one("SELECT note FROM sos_signals")["note"] == note


def test_note_schema_unit():
    m = SosCreate(citizen_id="citizen-0001", type="fire", lat=40.4, lon=49.9, note="a\x00b\nc​ ")
    assert m.note == "abc"
    with pytest.raises(Exception):
        SosCreate(citizen_id="citizen-0001", type="fire", lat=40.4, lon=49.9, note="x" * 281)


def test_internal_errors_do_not_leak_details(client, ctx, monkeypatch):
    from fastapi.testclient import TestClient

    def boom(*a, **k):
        raise RuntimeError("secret internal detail: /etc/passwd")

    monkeypatch.setattr("app.services.shelters.list_shelters", boom)
    safe_client = TestClient(client.app, raise_server_exceptions=False)   # eyni tətbiq, lifespan təkrar işləmir
    r = safe_client.get("/api/v1/shelters")
    assert r.status_code == 500
    assert "secret" not in r.text and "passwd" not in r.text


# ---------------------------------------------------------------------------
# Parol heşi
# ---------------------------------------------------------------------------
def test_password_hash_and_verify():
    h = hash_password("S3cret-pass")
    assert h.startswith("pbkdf2_sha256$") and "S3cret-pass" not in h
    assert verify_password("S3cret-pass", h) is True
    assert verify_password("s3cret-pass", h) is False
    assert verify_password("", h) is False


def test_password_hash_uses_random_salt():
    a, b = hash_password("same"), hash_password("same")
    assert a != b
    assert verify_password("same", a) and verify_password("same", b)
    assert hash_password("same", salt="fixed") == hash_password("same", salt="fixed")


@pytest.mark.parametrize("stored", ["", "plain-text", "pbkdf2_sha256$abc$salt$digest", "a$b$c"])
def test_verify_password_malformed_hash_is_false(stored):
    assert verify_password("x", stored) is False


def test_passwords_are_not_stored_in_plain_text(client, ctx):
    for u in ctx.db.query("SELECT * FROM users"):
        assert u["password_hash"].startswith("pbkdf2_sha256$")
        assert "dispatcher123" not in u["password_hash"] and "admin123" not in u["password_hash"]
