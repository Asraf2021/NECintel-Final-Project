"""myGov ilə qeydiyyat və əlillik statusunun təsdiqi: tam axın, arayış yükləmə, təhlükəsizlik, dispetçer yoxlaması."""
from __future__ import annotations

import base64
import hashlib
import html
import os
import re
import sqlite3
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi import HTTPException
from starlette.requests import Request

import app.config
from app.config import settings
from app.services import identity
from mock_mygov.main import CLIENT_ID, CLIENT_SECRET
from tests.helpers import DISPATCH, EVAC, SOS, STATUS, cid, send_sos

ME = "/api/v1/me"
CERT = "/api/v1/me/disability/certificate"
VERIF = f"{DISPATCH}/verifications"
RAW_PIN = "7ABC1D2"
CALLBACK = "http://testserver/api/v1/auth/mygov/callback"

PDF = b"%PDF-1.4\n% test arayis\n" + b"0" * 200
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 200
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 200


@pytest.fixture(autouse=True)
def uploads(tmp_path, monkeypatch):
    """Yükləmələr data/uploads-a deyil, testin müvəqqəti qovluğuna yazılır."""
    d = tmp_path / "uploads"
    monkeypatch.setattr(app.config.settings, "upload_dir", str(d))
    return d


# ---------------------------------------------------------------------------
# Köməkçilər
# ---------------------------------------------------------------------------
def _form(page_text: str) -> dict:
    return {k: html.unescape(v) for k, v in
            re.findall(r'name="(redirect_uri|state|code_challenge)" value="([^"]*)"', page_text)}


def mygov_start(client, device_id=None) -> dict:
    """Axının ilk yarısı: start → authorize səhifəsi → gizli sahələr (redirect_uri, state, code_challenge)."""
    params = {"citizen_id": device_id} if device_id else {}
    r = client.get("/api/v1/auth/mygov/start", params=params, follow_redirects=False)
    assert r.status_code == 302, r.text
    page = client.get(r.headers["location"])
    assert page.status_code == 200, page.text
    return _form(page.text)


def mygov_callback_url(client, form: dict, persona="p1") -> str:
    r = client.post("/mock-mygov/authorize", data={**form, "persona": persona}, follow_redirects=False)
    assert r.status_code == 303, r.text
    return r.headers["location"]


def mygov_login(client, persona="p1", device_id=None) -> str:
    """Tam giriş axını; vətəndaş JWT-ni qaytarır."""
    form = mygov_start(client, device_id)
    r = client.get(mygov_callback_url(client, form, persona), follow_redirects=False)
    assert r.status_code == 303, r.text
    loc = r.headers["location"]
    assert "#mygov=" in loc, loc
    return loc.split("#mygov=")[1]


def bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def me(client, token: str) -> dict:
    r = client.get(ME, headers=bearer(token))
    assert r.status_code == 200, r.text
    return r.json()


def account(ctx, citizen_id: str) -> dict:
    return ctx.db.one("SELECT * FROM citizen_accounts WHERE citizen_id=?", (citizen_id,))


def upload(client, token: str | None, content: bytes, mime: str):
    headers = {"Content-Type": mime}
    if token:
        headers |= bearer(token)
    return client.post(CERT, content=content, headers=headers)


def s256(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()


def sos_row(ctx, citizen_id: str) -> dict:
    return ctx.db.one("SELECT * FROM sos_signals WHERE citizen_id=? ORDER BY id DESC LIMIT 1", (citizen_id,))


# ---------------------------------------------------------------------------
# Tam axın (p1 — II qrup əlillik)
# ---------------------------------------------------------------------------
def test_happy_path_device_binding_and_me_fields(client, ctx):
    device = cid(1)
    # Girişdən əvvəl anonim SOS — qeydiyyat tələb olunmur
    assert send_sos(client, device).status_code == 201
    token = mygov_login(client, "p1", device_id=device)
    data = me(client, token)
    # Hesab cihazın anonim ID-sinə bağlanır
    assert data["citizen_id"] == device
    assert data["provider"] == "mygov"
    assert data["given_name"] == "Aysel" and data["family_name"] == "Quliyeva"
    assert data["mygov_disability"] is True and data["mygov_disability_group"] == "II"
    assert data["disability_status"] == "mygov_record"
    assert data["disability_enabled"] is False
    assert data["prompt_pending"] is True
    assert data["certificate_uploaded"] is False
    # Telefon maskalanıb
    assert data["phone_masked"] == "+994 50 *** ** 01"
    assert "000 00" not in data["phone_masked"]
    # FİN /me cavabında yoxdur
    r = client.get(ME, headers=bearer(token))
    assert RAW_PIN not in r.text
    # FİN və provayder identifikatoru bazada yalnız heş kimi saxlanılır
    row = account(ctx, device)
    assert row["pin_hash"] != RAW_PIN and len(row["pin_hash"]) == 64
    assert row["sub_hash"] != "mg-0001"
    for col, value in row.items():
        assert RAW_PIN not in str(value), col
        assert "mg-0001" not in str(value), col
    # Girişdən əvvəlki SOS eyni ID ilə qalıb
    assert sos_row(ctx, device)["citizen_id"] == device


def test_prompt_pending_false_for_p2(client):
    data = me(client, mygov_login(client, "p2", device_id=cid(2)))
    assert data["prompt_pending"] is False
    assert data["mygov_disability"] is False
    assert data["disability_status"] == "none"


def test_skip_clears_prompt(client):
    token = mygov_login(client, "p1")
    assert me(client, token)["prompt_pending"] is True
    r = client.post(f"{ME}/disability/skip", headers=bearer(token))
    assert r.status_code == 200, r.text
    assert r.json()["prompt_pending"] is False
    assert me(client, token)["prompt_pending"] is False


def test_disability_toggle_changes_role_and_sos_priority(client, ctx):
    device = cid(3)
    token = mygov_login(client, "p1", device_id=device)
    r = client.put(f"{ME}/disability", json={"enabled": True}, headers=bearer(token))
    assert r.status_code == 200, r.text
    assert r.json()["disability_enabled"] is True and r.json()["prompt_pending"] is False
    assert ctx.db.one("SELECT role FROM citizen_profiles WHERE citizen_id=?", (device,))["role"] == "priority_citizen"
    r = client.post(SOS, json={"citizen_id": device, "type": "fire", "lat": 40.383, "lon": 49.9}, headers=bearer(token))
    assert r.status_code == 201, r.text
    assert r.json()["sos"]["priority"] == 1
    # "İstənilən vaxt dəyişmək olar": söndürüləndə aktiv SOS yenidən adi prioritetə qayıdır
    r = client.put(f"{ME}/disability", json={"enabled": False}, headers=bearer(token))
    assert r.status_code == 200, r.text
    assert r.json()["disability_enabled"] is False
    assert ctx.db.one("SELECT role FROM citizen_profiles WHERE citizen_id=?", (device,))["role"] == "citizen"
    row = sos_row(ctx, device)
    assert row["status"] in ("active", "assigned")
    assert row["priority"] == 2


def test_same_person_other_device_gets_same_citizen_id(client, ctx):
    first = me(client, mygov_login(client, "p1", device_id=cid(4)))["citizen_id"]
    second = me(client, mygov_login(client, "p1", device_id=cid(5)))["citizen_id"]
    assert first == second == cid(4)
    assert ctx.db.one("SELECT COUNT(*) AS n FROM citizen_accounts")["n"] == 1
    assert account(ctx, cid(5)) is None


def test_login_without_device_id_creates_generated_id(client):
    data = me(client, mygov_login(client, "p3"))
    assert data["citizen_id"].startswith("u-")
    assert data["mygov_disability_group"] == "I"


# ---------------------------------------------------------------------------
# Arayış yükləmə
# ---------------------------------------------------------------------------
def test_upload_never_auto_verifies_even_with_mygov_record(client, ctx, uploads):
    """Fayl heç vaxt avtomatik təsdiq etmir: məzmunu sistem yoxlaya bilmir -> yalnız dispetçer yoxlaması."""
    token = mygov_login(client, "p1", device_id=cid(10))
    r = upload(client, token, PDF, "application/pdf")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["disability_status"] == "pending_review"
    assert body["certificate_uploaded"] is True and body["disability_enabled"] is True
    row = account(ctx, cid(10))
    assert row["verified_by"] is None and row["certificate_mime"] == "application/pdf"
    assert (uploads / row["certificate_file"]).read_bytes() == PDF
    assert ctx.db.one("SELECT role FROM citizen_profiles WHERE citizen_id=?", (cid(10),))["role"] == "priority_citizen"


def test_confirm_with_mygov_record_verifies_without_any_file(client, ctx):
    """myGov profil qeydi olan şəxs "Təsdiq et" basanda fayl tələb olunmadan təsdiqlənir (mənbə: myGov)."""
    token = mygov_login(client, "p1", device_id=cid(12))
    assert client.get("/api/v1/me", headers=bearer(token)).json()["disability_status"] == "mygov_record"
    body = client.put("/api/v1/me/disability", headers=bearer(token), json={"enabled": True}).json()
    assert body["disability_status"] == "verified" and body["verified_by"] == "mygov_record"
    assert body["certificate_uploaded"] is False


def test_confirm_without_mygov_record_is_only_declared(client):
    token = mygov_login(client, "p2", device_id=cid(13))
    body = client.put("/api/v1/me/disability", headers=bearer(token), json={"enabled": True}).json()
    assert body["disability_status"] == "declared"


def test_upload_after_mygov_verification_keeps_verified(client):
    token = mygov_login(client, "p1", device_id=cid(14))
    client.put("/api/v1/me/disability", headers=bearer(token), json={"enabled": True})
    assert upload(client, token, PDF, "application/pdf").json()["disability_status"] == "verified"


VERIFY = "/api/v1/me/disability/verify"


def test_certificate_number_verified_by_mygov_registry(client, ctx):
    """p4: profil qeydi yoxdur, amma arayış reyestrdə var və ona aiddir -> verified."""
    token = mygov_login(client, "p4", device_id=cid(15))
    assert client.get("/api/v1/me", headers=bearer(token)).json()["prompt_pending"] is False
    r = client.post(VERIFY, headers=bearer(token), json={"certificate_no": "ƏL-2026-000777"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["disability_status"] == "verified" and body["verified_by"] == "mygov_certificate"
    assert body["certificate"]["group"] == "III"
    assert ctx.db.one("SELECT role FROM citizen_profiles WHERE citizen_id=?", (cid(15),))["role"] == "priority_citizen"


def test_certificate_number_accepts_latin_el_prefix_and_spaces(client):
    token = mygov_login(client, "p4", device_id=cid(16))
    r = client.post(VERIFY, headers=bearer(token), json={"certificate_no": " el-2026-000777 "})
    assert r.status_code == 200 and r.json()["disability_status"] == "verified"


def test_someone_elses_certificate_is_rejected(client):
    """p2 başqasının (p1-in) real arayış nömrəsini daxil edir -> rədd, status dəyişmir."""
    token = mygov_login(client, "p2", device_id=cid(17))
    r = client.post(VERIFY, headers=bearer(token), json={"certificate_no": "ƏL-2024-000123"})
    assert r.status_code == 422 and r.json()["detail"] == "certificate_not_owner"
    assert client.get("/api/v1/me", headers=bearer(token)).json()["disability_status"] == "none"


def test_unknown_certificate_number_is_rejected(client):
    token = mygov_login(client, "p2", device_id=cid(18))
    r = client.post(VERIFY, headers=bearer(token), json={"certificate_no": "ƏL-0000-000000"})
    assert r.status_code == 422 and r.json()["detail"] == "certificate_not_found"


def test_expired_certificate_is_rejected(client):
    token = mygov_login(client, "p2", device_id=cid(19))
    r = client.post(VERIFY, headers=bearer(token), json={"certificate_no": "ƏL-2020-000999"})
    assert r.status_code == 422 and r.json()["detail"] == "certificate_expired"


def test_certificate_verify_requires_citizen_token_and_valid_input(client, auth_headers):
    assert client.post(VERIFY, json={"certificate_no": "ƏL-2026-000777"}).status_code == 401
    assert client.post(VERIFY, headers=auth_headers, json={"certificate_no": "ƏL-2026-000777"}).status_code == 401
    token = mygov_login(client, "p4", device_id=cid(20))
    assert client.post(VERIFY, headers=bearer(token), json={"certificate_no": "x"}).status_code == 422
    assert client.post(VERIFY, headers=bearer(token), json={"certificate_no": "<script>alert(1)</script>"}).status_code == 422


def test_certificate_verify_is_rate_limited(client, monkeypatch):
    """Nömrələri sınamaqla (brute force) təsdiq almaq olmaz."""
    from app.config import settings
    monkeypatch.setattr(settings, "rate_limit_login_per_min", 3)
    token = mygov_login(client, "p2", device_id=cid(21))
    codes = [client.post(VERIFY, headers=bearer(token), json={"certificate_no": f"ƏL-1999-{i:06d}"}).status_code for i in range(5)]
    assert codes[:3] == [422, 422, 422] and 429 in codes[3:]


def test_certificate_verify_attempts_are_audited(client, ctx):
    token = mygov_login(client, "p2", device_id=cid(22))
    client.post(VERIFY, headers=bearer(token), json={"certificate_no": "ƏL-2024-000123"})
    row = ctx.db.one("SELECT details FROM audit_log WHERE action='certificate_verify' ORDER BY id DESC LIMIT 1")
    assert row and "not_owner" in row["details"]


def test_mock_registry_requires_client_credentials(client):
    assert client.get("/mock-mygov/certificates/ƏL-2024-000123").status_code == 401


def test_upload_png_without_record_goes_to_review_then_approve(client, ctx, auth_headers):
    token = mygov_login(client, "p2", device_id=cid(11))
    r = upload(client, token, PNG, "image/png")
    assert r.status_code == 200, r.text
    assert r.json()["disability_status"] == "pending_review"
    q = client.get(VERIF, headers=auth_headers)
    assert q.status_code == 200, q.text
    entry = next(v for v in q.json()["verifications"] if v["citizen_id"] == cid(11))
    assert entry["full_name"] == "Elvin Sadıqov"
    assert entry["disability_status"] == "pending_review"
    r = client.post(f"{VERIF}/{cid(11)}/approve", headers=auth_headers)
    assert r.status_code == 200, r.text
    assert r.json()["disability_status"] == "verified"
    assert account(ctx, cid(11))["verified_by"] == "dispatcher"
    assert me(client, token)["disability_status"] == "verified"


def test_upload_review_reject(client, ctx, auth_headers):
    token = mygov_login(client, "p2", device_id=cid(12))
    assert upload(client, token, PNG, "image/png").status_code == 200
    r = client.post(f"{VERIF}/{cid(12)}/reject", headers=auth_headers)
    assert r.status_code == 200, r.text
    assert r.json()["disability_status"] == "rejected"
    assert account(ctx, cid(12))["disability_status"] == "rejected"
    # Rədd edilmiş vətəndaş artıq növbədə görünmür
    q = client.get(VERIF, headers=auth_headers).json()["verifications"]
    assert all(v["citizen_id"] != cid(12) for v in q)


def test_review_unknown_decision_and_account(client, auth_headers):
    assert client.post(f"{VERIF}/{cid(99)}/approve", headers=auth_headers).status_code == 404
    assert client.post(f"{VERIF}/{cid(99)}/maybe", headers=auth_headers).status_code == 404


def test_upload_jpeg_accepted(client, ctx):
    token = mygov_login(client, "p1", device_id=cid(13))
    r = upload(client, token, JPEG, "image/jpeg")
    assert r.status_code == 200, r.text
    assert account(ctx, cid(13))["certificate_file"].endswith(".jpg")


def test_upload_wrong_magic_bytes_415(client, ctx, uploads):
    token = mygov_login(client, "p1", device_id=cid(14))
    # PNG kimi bildirilmiş, amma PDF məzmunu — saxtakarlıq
    r = upload(client, token, PDF, "image/png")
    assert r.status_code == 415, r.text
    assert r.json()["detail"] == "file_content_mismatch"
    assert account(ctx, cid(14))["certificate_file"] is None
    assert not uploads.exists() or not os.listdir(uploads)


def test_upload_unsupported_type_415(client):
    token = mygov_login(client, "p1")
    r = upload(client, token, b"salam dunya", "text/plain")
    assert r.status_code == 415, r.text
    assert r.json()["detail"] == "unsupported_file_type"


def test_upload_empty_body_422(client):
    token = mygov_login(client, "p1")
    r = upload(client, token, b"", "application/pdf")
    assert r.status_code == 422, r.text


def test_upload_over_configured_limit_413(client, monkeypatch):
    token = mygov_login(client, "p1")
    monkeypatch.setattr(app.config.settings, "max_upload_bytes", 100)
    r = upload(client, token, PDF, "application/pdf")
    assert r.status_code == 413, r.text
    assert r.json()["detail"] == "file_too_large"


def test_upload_over_middleware_limit_413(client):
    token = mygov_login(client, "p1")
    big = b"%PDF" + b"0" * (5 * 1024 * 1024)
    r = upload(client, token, big, "application/pdf")
    assert r.status_code == 413, r.text


def test_upload_path_larger_than_64kb_allowed(client):
    # Arayış yolu üçün hədd 5 MB-dır, 64 KB-dan böyük fayl qəbul olunur
    token = mygov_login(client, "p1")
    r = upload(client, token, b"%PDF" + b"0" * (200 * 1024), "application/pdf")
    assert r.status_code == 200, r.text


def test_normal_body_limit_still_applies_elsewhere(client):
    payload = {"citizen_id": cid(15), "type": "fire", "lat": 40.383, "lon": 49.9, "note": "x" * (70 * 1024)}
    r = client.post(SOS, json=payload)
    assert r.status_code == 413, r.text


def test_upload_without_token_401(client, auth_headers):
    assert upload(client, None, PDF, "application/pdf").status_code == 401
    # Dispetçer tokeni vətəndaş tokeni deyil
    r = client.post(CERT, content=PDF, headers={"Content-Type": "application/pdf", **auth_headers})
    assert r.status_code == 401


def test_reupload_replaces_old_file(client, ctx, uploads):
    token = mygov_login(client, "p1", device_id=cid(16))
    assert upload(client, token, PDF, "application/pdf").status_code == 200
    old = account(ctx, cid(16))["certificate_file"]
    assert (uploads / old).exists()
    assert upload(client, token, PNG, "image/png").status_code == 200
    row = account(ctx, cid(16))
    assert row["certificate_file"] != old and row["certificate_mime"] == "image/png"
    assert not (uploads / old).exists()
    assert os.listdir(uploads) == [row["certificate_file"]]


# ---------------------------------------------------------------------------
# Təhlükəsizlik: OAuth2 axını
# ---------------------------------------------------------------------------
def test_callback_with_unknown_state_is_rejected(client, ctx):
    form = mygov_start(client)
    loc = mygov_callback_url(client, {**form, "state": "saxta-state-123"})
    r = client.get(loc, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"].endswith("#mygov_error=invalid_state")
    assert ctx.db.one("SELECT COUNT(*) AS n FROM citizen_accounts")["n"] == 0


def test_callback_with_tampered_state_is_rejected(client):
    form = mygov_start(client)
    loc = mygov_callback_url(client, form)
    q = parse_qs(urlparse(loc).query)
    tampered = q["state"][0][:-1] + ("A" if q["state"][0][-1] != "A" else "B")
    r = client.get("/api/v1/auth/mygov/callback", params={"code": q["code"][0], "state": tampered},
                   follow_redirects=False)
    assert r.headers["location"].endswith("#mygov_error=invalid_state")


def test_state_is_single_use(client):
    form = mygov_start(client)
    loc = mygov_callback_url(client, form)
    first = client.get(loc, follow_redirects=False)
    assert "#mygov=" in first.headers["location"]
    # Eyni callback (eyni state + kod) təkrar göndərilir — replay
    second = client.get(loc, follow_redirects=False)
    assert second.headers["location"].endswith("#mygov_error=invalid_state")
    # Eyni state yeni kodla da işləmir
    loc2 = mygov_callback_url(client, form)
    third = client.get(loc2, follow_redirects=False)
    assert third.headers["location"].endswith("#mygov_error=invalid_state")


def test_callback_error_param(client):
    r = client.get("/api/v1/auth/mygov/callback", params={"error": "access_denied"}, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/citizen#mygov_error=access_denied"
    r = client.get("/api/v1/auth/mygov/callback", follow_redirects=False)
    assert r.headers["location"] == "/citizen#mygov_error=cancelled"


def _mock_code(client, verifier: str, persona: str = "p1") -> str:
    r = client.post("/mock-mygov/authorize", data={"persona": persona, "redirect_uri": CALLBACK, "state": "s-1",
                                                  "code_challenge": s256(verifier)}, follow_redirects=False)
    assert r.status_code == 303, r.text
    return parse_qs(urlparse(r.headers["location"]).query)["code"][0]


def _token_req(client, code: str, verifier: str, **over):
    data = {"grant_type": "authorization_code", "code": code, "redirect_uri": CALLBACK, "code_verifier": verifier,
            "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET, **over}
    return client.post("/mock-mygov/token", data=data)


def test_mock_token_wrong_pkce_verifier_400(client):
    verifier = "v" * 50
    code = _mock_code(client, verifier)
    r = _token_req(client, code, "yanlis-verifier-" + "x" * 40)
    assert r.status_code == 400
    assert r.json()["error_description"] == "pkce_mismatch"


def test_mock_token_code_reuse_400(client):
    verifier = "w" * 50
    code = _mock_code(client, verifier)
    ok = _token_req(client, code, verifier)
    assert ok.status_code == 200, ok.text
    access = ok.json()["access_token"]
    assert client.get("/mock-mygov/userinfo", headers=bearer(access)).json()["sub"] == "mg-0001"
    # Kod birdəfəlikdir
    again = _token_req(client, code, verifier)
    assert again.status_code == 400
    assert again.json()["error"] == "invalid_grant"


def test_mock_token_invalid_client_and_redirect(client):
    verifier = "z" * 50
    code = _mock_code(client, verifier)
    assert _token_req(client, code, verifier, client_secret="yanlis").status_code == 401
    code = _mock_code(client, verifier)
    assert _token_req(client, code, verifier, redirect_uri="http://evil.example/api/v1/auth/mygov/callback").status_code == 400


def test_mock_userinfo_requires_token(client):
    assert client.get("/mock-mygov/userinfo").status_code == 401
    assert client.get("/mock-mygov/userinfo", headers=bearer("saxta")).status_code == 401


def test_mock_authorize_invalid_redirect_uri_400(client):
    r = client.post("/mock-mygov/authorize", data={"persona": "p1", "redirect_uri": "https://evil.example/steal",
                                                  "state": "s", "code_challenge": s256("v" * 50)},
                    follow_redirects=False)
    assert r.status_code == 400
    r = client.post("/mock-mygov/authorize", data={"persona": "p1", "redirect_uri": "javascript:alert(1)//api/v1/auth/mygov/callback",
                                                  "state": "s", "code_challenge": s256("v" * 50)},
                    follow_redirects=False)
    assert r.status_code == 400
    r = client.get("/mock-mygov/authorize", params={"client_id": CLIENT_ID, "redirect_uri": "https://evil.example/x",
                                                   "state": "s", "code_challenge": s256("v" * 50)})
    assert r.status_code == 400


def test_mock_authorize_invalid_client_id_and_pkce_400(client):
    base = {"client_id": CLIENT_ID, "redirect_uri": CALLBACK, "state": "s", "code_challenge": s256("v" * 50)}
    assert client.get("/mock-mygov/authorize", params={**base, "client_id": "basqa-client"}).status_code == 400
    assert client.get("/mock-mygov/authorize", params={**base, "code_challenge_method": "plain"}).status_code == 400
    assert client.get("/mock-mygov/authorize", params={**base, "code_challenge": "qisa"}).status_code == 400
    r = client.post("/mock-mygov/authorize", data={"persona": "p9", "redirect_uri": CALLBACK, "state": "s",
                                                  "code_challenge": base["code_challenge"]}, follow_redirects=False)
    assert r.status_code == 400


def test_start_rejects_invalid_device_id(client):
    r = client.get("/api/v1/auth/mygov/start", params={"citizen_id": "qisa"}, follow_redirects=False)
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# Təhlükəsizlik: rollar və sahiblik (ensure_owner)
# ---------------------------------------------------------------------------
def test_citizen_token_cannot_access_dispatcher(client):
    token = mygov_login(client, "p1", device_id=cid(20))
    h = bearer(token)
    assert client.get(VERIF, headers=h).status_code == 403
    assert client.post(f"{VERIF}/{cid(20)}/approve", headers=h).status_code == 403
    assert client.get(f"{VERIF}/{cid(20)}/certificate", headers=h).status_code == 403
    assert client.get(f"{DISPATCH}/sos", headers=h).status_code == 403


def test_verification_endpoints_require_auth(client):
    assert client.get(VERIF).status_code == 401
    assert client.post(f"{VERIF}/{cid(20)}/approve").status_code == 401
    assert client.post(f"{VERIF}/{cid(20)}/reject").status_code == 401
    assert client.get(f"{VERIF}/{cid(20)}/certificate").status_code == 401


def test_dispatcher_token_not_accepted_as_citizen(client, auth_headers):
    assert client.get(ME, headers=auth_headers).status_code == 401
    assert client.post(f"{ME}/disability/skip", headers=auth_headers).status_code == 401
    assert client.put(f"{ME}/disability", json={"enabled": True}, headers=auth_headers).status_code == 401
    assert client.get(ME).status_code == 401
    assert client.get(ME, headers=bearer("saxta.token.deyer")).status_code == 401


def test_registered_citizen_id_requires_its_token(client, ctx):
    device = cid(21)
    token = mygov_login(client, "p1", device_id=device)
    other = mygov_login(client, "p2", device_id=cid(22))
    district = ctx.districts[0]["id"]
    sos_body = {"citizen_id": device, "type": "fire", "lat": 40.383, "lon": 49.9}
    status_body = {"citizen_id": device, "status": "evacuating"}
    calls = [
        ("POST", SOS, {"json": sos_body}),
        ("POST", STATUS, {"json": status_body}),
        ("GET", f"/api/v1/citizens/{device}", {}),
        ("PUT", f"/api/v1/citizens/{device}/profile", {"json": {"role": "priority_citizen"}}),
        ("GET", EVAC, {"params": {"district_id": district, "citizen_id": device}}),
    ]
    for method, url, kw in calls:
        # Tokensiz — 401
        assert client.request(method, url, **kw).status_code == 401, (method, url)
        # Başqa vətəndaşın tokeni ilə — 401
        assert client.request(method, url, headers=bearer(other), **kw).status_code == 401, (method, url)
    # Stream: tokensiz və başqasının tokeni ilə 401 (cavab yayım başlamazdan əvvəl qaytarılır)
    with client.stream("GET", "/api/v1/stream", params={"citizen_id": device}) as r:
        assert r.status_code == 401
    with client.stream("GET", "/api/v1/stream", params={"citizen_id": device, "token": other}) as r:
        assert r.status_code == 401
    # Öz tokeni ilə — işləyir
    expected = {SOS: 201}
    for method, url, kw in calls:
        r = client.request(method, url, headers=bearer(token), **kw)
        assert r.status_code == expected.get(url, 200), (method, url, r.text)
    # Profil PUT qeydiyyatlı vətəndaş üçün hesabdakı əlillik xüsusiyyətini dəyişir
    assert account(ctx, device)["disability_enabled"] == 1


def test_stream_owner_check_accepts_query_token(client, ctx):
    """SSE axını TestClient-i bloklaya bildiyi üçün ?token= yoxlaması ensure_owner üzərindən birbaşa test olunur."""
    device = cid(23)
    token = mygov_login(client, "p1", device_id=device)

    def req(qs: bytes, headers=()):
        return Request({"type": "http", "method": "GET", "path": "/api/v1/stream", "headers": list(headers),
                        "query_string": qs})

    identity.ensure_owner(ctx, req(f"citizen_id={device}&token={token}".encode()), device)
    identity.ensure_owner(ctx, req(b"", [(b"authorization", f"Bearer {token}".encode())]), device)
    with pytest.raises(HTTPException) as exc:
        identity.ensure_owner(ctx, req(f"citizen_id={device}".encode()), device)
    assert exc.value.status_code == 401
    # Anonim ID üçün yoxlama heç nə tələb etmir
    identity.ensure_owner(ctx, req(b""), cid(24))
    identity.ensure_owner(ctx, req(b""), None)


def test_anonymous_unregistered_citizen_unaffected(client, ctx):
    anon = cid(30)
    mygov_login(client, "p1", device_id=cid(31))  # başqa qeydiyyatlı vətəndaş mövcuddur
    district = ctx.districts[0]["id"]
    assert send_sos(client, anon).status_code == 201
    assert client.post(STATUS, json={"citizen_id": anon, "status": "evacuating"}).status_code == 200
    assert client.get(f"/api/v1/citizens/{anon}").status_code == 200
    r = client.put(f"/api/v1/citizens/{anon}/profile", json={"role": "priority_citizen"})
    assert r.status_code == 200, r.text
    assert r.json()["sos_priority"] == 1
    assert client.get(EVAC, params={"district_id": district, "citizen_id": anon}).status_code == 200
    assert account(ctx, anon) is None


# ---------------------------------------------------------------------------
# Dispetçer
# ---------------------------------------------------------------------------
def test_dispatch_sos_shows_verification(client, ctx, auth_headers):
    reg = cid(40)
    token = mygov_login(client, "p1", device_id=reg)
    assert client.put("/api/v1/me/disability", headers=bearer(token), json={"enabled": True}).status_code == 200
    r = client.post(SOS, json={"citizen_id": reg, "type": "medical", "lat": 40.383, "lon": 49.9}, headers=bearer(token))
    assert r.status_code == 201 and r.json()["sos"]["priority"] == 1
    assert send_sos(client, cid(41)).status_code == 201
    r = client.get(f"{DISPATCH}/sos", headers=auth_headers)
    assert r.status_code == 200, r.text
    body = r.json()
    items = body["sos"] if isinstance(body, dict) else body
    by_cid = {s["citizen_id"]: s for s in items}
    assert by_cid[reg]["verification"] == "verified"
    assert by_cid[reg]["priority"] == 1
    assert by_cid[cid(41)]["verification"] == "none"


def test_certificate_download_and_audit(client, ctx, auth_headers):
    token = mygov_login(client, "p2", device_id=cid(42))
    assert upload(client, token, PNG, "image/png").status_code == 200
    r = client.get(f"{VERIF}/{cid(42)}/certificate", headers=auth_headers)
    assert r.status_code == 200, r.text
    assert r.content == PNG
    assert r.headers["content-type"].startswith("image/png")
    assert r.headers["cache-control"] == "no-store"
    audit = ctx.db.query("SELECT * FROM audit_log WHERE action='certificate_view'")
    assert len(audit) == 1
    assert cid(42) in audit[0]["details"] and audit[0]["actor"] == "dispatcher"


def test_certificate_download_missing_404(client, auth_headers):
    mygov_login(client, "p1", device_id=cid(43))
    assert client.get(f"{VERIF}/{cid(43)}/certificate", headers=auth_headers).status_code == 404
    assert client.get(f"{VERIF}/{cid(44)}/certificate", headers=auth_headers).status_code == 404


def test_login_and_upload_are_audited(client, ctx):
    token = mygov_login(client, "p1", device_id=cid(45))
    upload(client, token, PDF, "application/pdf")
    actions = {r["action"] for r in ctx.db.query("SELECT action FROM audit_log WHERE actor=?", (cid(45),))}
    assert {"mygov_login", "certificate_upload"} <= actions


# ---------------------------------------------------------------------------
# Baza məhdudiyyəti və konfiqurasiya
# ---------------------------------------------------------------------------
def test_check_constraint_rejects_invalid_status(ctx):
    with pytest.raises(sqlite3.IntegrityError):
        ctx.db.execute(
            "INSERT INTO citizen_accounts (citizen_id, sub_hash, pin_hash, provider, disability_status, created_at, updated_at)"
            " VALUES ('citizen-9999','h1','h2','mygov','super_verified',0,0)")


def test_check_constraint_rejects_invalid_update(client, ctx):
    mygov_login(client, "p1", device_id=cid(50))
    with pytest.raises(sqlite3.IntegrityError):
        ctx.db.execute("UPDATE citizen_accounts SET disability_status='hacked' WHERE citizen_id=?", (cid(50),))


def test_build_provider_off_returns_none(monkeypatch):
    monkeypatch.setattr(app.config.settings, "mygov_mode", "off")
    assert identity.build_provider(object()) is None


def test_start_returns_503_when_mygov_disabled(client):
    client.app.state.mygov = None
    r = client.get("/api/v1/auth/mygov/start", follow_redirects=False)
    assert r.status_code == 503
    assert r.json()["detail"] == "mygov_disabled"
