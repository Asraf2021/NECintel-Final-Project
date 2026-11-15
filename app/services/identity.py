"""myGov ilə qeydiyyat / giriş və əlillik statusunun təsdiqi.

Axın (OAuth2 Authorization Code + PKCE):
  1. /api/v1/auth/mygov/start  — state + code_verifier yaradılır, brauzer provayderin authorize səhifəsinə yönəlir;
  2. provayder geri qaytarır: /api/v1/auth/mygov/callback?code&state;
  3. kod token-ə dəyişdirilir (PKCE yoxlaması ilə), userinfo alınır;
  4. vətəndaş hesabı yaradılır / yenilənir (cihazın anonim citizen_id-si ilə bağlanır), vətəndaş tokeni verilir.

Provayder adapteri: mode="mock" — mock_mygov (eyni prosesdə); mode="oidc" — real provayder URL-ləri env-dən.

Məxfilik: FİN və provayder identifikatoru yalnız heş kimi saxlanılır; telefon maskalanır;
arayış faylı açıq qovluqda deyil, yalnız icazəli dispetçer yükləyə bilər.

Əlillik statusları:
  none           — qeyd yoxdur;
  declared       — vətəndaş özü bildirib (sübut yoxdur);
  mygov_record   — myGov qeydində əlillik var, vətəndaş hələ təsdiqləməyib (popup gözləyir);
  pending_review — fayl yüklənib -> yalnız dispetçer yoxlaya bilər (fayl heç vaxt avtomatik təsdiq etmir);
  verified       — təsdiq mənbəyi myGov-dur: profil qeydi və ya arayış nömrəsinin myGov reyestrində
                   yoxlanması (mövcuddur, bu şəxsə aiddir, qüvvədədir); və ya dispetçerin əl ilə təsdiqi;
  rejected       — dispetçer rədd edib.
Prioritet SOS təsdiqi gözləmir (həyat təhlükəsizliyi); dispetçer statusu SOS kartında görür.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from urllib.parse import urlencode

import httpx

from ..config import settings
from ..context import AppContext
from ..core.security import create_token

STATE_TTL_S = 600
VERIFIED_STATES = ("verified",)
ALLOWED_UPLOADS = {  # MIME -> (uzantı, sehrli baytlar)
    "application/pdf": (".pdf", (b"%PDF",)),
    "image/jpeg": (".jpg", (b"\xff\xd8\xff",)),
    "image/png": (".png", (b"\x89PNG\r\n\x1a\n",)),
}


def _hash(value: str) -> str:
    return hmac.new(settings.identity_pepper.encode(), value.encode(), hashlib.sha256).hexdigest()


def _s256(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()


def _mask_phone(phone: str | None) -> str:
    digits = (phone or "").strip()
    return digits[:7] + " *** ** " + digits[-2:] if len(digits) >= 9 else ""


class MyGovProvider:
    def __init__(self, client: httpx.AsyncClient, authorize_url: str, token_path: str, userinfo_path: str,
                 client_id: str, client_secret: str):
        self.client = client
        self.authorize_base = authorize_url
        self.token_path = token_path
        self.userinfo_path = userinfo_path
        self.client_id = client_id
        self.client_secret = client_secret
        self.certificates_path = "/certificates"
        self._states: dict[str, dict] = {}

    async def lookup_certificate(self, number: str) -> dict:
        """myGov-dan arayış nömrəsini yoxlamaq (serverdən-serverə, client açarları ilə)."""
        from urllib.parse import quote
        resp = await self.client.get(f"{self.certificates_path}/{quote(number, safe='')}", timeout=5.0,
                                     headers={"X-Client-Id": self.client_id, "X-Client-Secret": self.client_secret})
        if resp.status_code != 200:
            raise RuntimeError("mygov_unavailable")
        return resp.json()

    # ----- addım 1 -----
    def begin(self, citizen_id: str | None, redirect_uri: str) -> str:
        now = time.time()
        for k in [k for k, v in self._states.items() if v["exp"] < now]:
            self._states.pop(k, None)
        if len(self._states) > 10_000:
            raise RuntimeError("too_many_pending_logins")
        state = secrets.token_urlsafe(24)
        verifier = secrets.token_urlsafe(48)
        self._states[state] = {"verifier": verifier, "citizen_id": citizen_id, "redirect_uri": redirect_uri,
                               "exp": now + STATE_TTL_S}
        query = urlencode({"client_id": self.client_id, "redirect_uri": redirect_uri, "state": state,
                           "code_challenge": _s256(verifier), "code_challenge_method": "S256",
                           "scope": "openid profile disability", "response_type": "code"})
        return f"{self.authorize_base}?{query}"

    # ----- addım 3 -----
    async def complete(self, code: str, state: str) -> tuple[dict, str | None]:
        entry = self._states.pop(state, None)  # state birdəfəlikdir (CSRF / replay-ə qarşı)
        if not entry or entry["exp"] < time.time():
            raise PermissionError("invalid_state")
        resp = await self.client.post(self.token_path, data={
            "grant_type": "authorization_code", "code": code, "redirect_uri": entry["redirect_uri"],
            "code_verifier": entry["verifier"], "client_id": self.client_id, "client_secret": self.client_secret,
        }, timeout=5.0)
        if resp.status_code != 200:
            raise PermissionError("token_exchange_failed")
        access = resp.json()["access_token"]
        info = await self.client.get(self.userinfo_path, headers={"Authorization": f"Bearer {access}"}, timeout=5.0)
        if info.status_code != 200:
            raise PermissionError("userinfo_failed")
        return info.json(), entry["citizen_id"]


def build_provider(mock_app=None) -> MyGovProvider | None:
    mode = settings.mygov_mode
    if mode == "mock" and mock_app is not None:
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=mock_app), base_url="http://mock-mygov")
        from mock_mygov.main import CLIENT_ID, CLIENT_SECRET
        return MyGovProvider(client, "/mock-mygov/authorize", "/token", "/userinfo", CLIENT_ID, CLIENT_SECRET)
    if mode == "oidc" and settings.mygov_authorize_url:
        client = httpx.AsyncClient()
        return MyGovProvider(client, settings.mygov_authorize_url, settings.mygov_token_url,
                             settings.mygov_userinfo_url, settings.mygov_client_id, settings.mygov_client_secret)
    return None


# ---------------------------------------------------------------------------
# Hesablar
# ---------------------------------------------------------------------------
def account_out(row: dict | None) -> dict | None:
    if not row:
        return None
    return {
        "citizen_id": row["citizen_id"], "provider": row["provider"],
        "given_name": row["given_name"], "family_name": row["family_name"],
        "birthdate": row["birthdate"], "phone_masked": row["phone_masked"],
        "mygov_disability": bool(row["mygov_disability"]), "mygov_disability_group": row["mygov_disability_group"],
        "disability_enabled": bool(row["disability_enabled"]), "disability_status": row["disability_status"],
        "certificate_uploaded": bool(row["certificate_file"]), "certificate_uploaded_at": row["certificate_uploaded_at"],
        "verified_by": row["verified_by"],
        "prompt_pending": bool(row["mygov_disability"]) and not row["prompt_done"],
        "certificate_service_url": settings.mygov_certificate_url,
    }


def ensure_owner(ctx: AppContext, request, citizen_id: str | None) -> None:
    """Qeydiyyatdan keçmiş (myGov) vətəndaşın adından yalnız onun tokeni ilə əməliyyat aparmaq olar.

    Anonim (qeydiyyatsız) rejim dəyişmir — fövqəladə halda heç kim SOS-dan əvvəl qeydiyyata məcbur edilmir.
    """
    if not citizen_id or not ctx.db.one("SELECT 1 FROM citizen_accounts WHERE citizen_id=?", (citizen_id,)):
        return
    from fastapi import HTTPException
    from ..core.security import current_user
    user = current_user(request)
    if not user or user.get("role") != "citizen" or user.get("sub") != citizen_id:
        raise HTTPException(401, "citizen_auth_required")


def get_account(ctx: AppContext, citizen_id: str) -> dict | None:
    return ctx.db.one("SELECT * FROM citizen_accounts WHERE citizen_id=?", (citizen_id,))


def upsert_from_mygov(ctx: AppContext, info: dict, device_citizen_id: str | None) -> dict:
    """myGov məlumatından hesab. Eyni şəxs başqa cihazdan girsə, mövcud hesab (və citizen_id) qaytarılır."""
    sub_hash = _hash("sub:" + str(info["sub"]))
    dis = info.get("disability") or {}
    registered = bool(dis.get("registered"))
    now = time.time()
    with ctx.db.transaction():
        existing = ctx.db.one("SELECT * FROM citizen_accounts WHERE sub_hash=?", (sub_hash,))
        if existing:
            citizen_id = existing["citizen_id"]
            status = existing["disability_status"]
            if registered and status in ("none", "declared"):
                status = "mygov_record"
            ctx.db.execute(
                "UPDATE citizen_accounts SET given_name=?, family_name=?, birthdate=?, phone_masked=?, mygov_disability=?,"
                " mygov_disability_group=?, disability_status=?, updated_at=? WHERE citizen_id=?",
                (info.get("given_name", ""), info.get("family_name", ""), info.get("birthdate", ""),
                 _mask_phone(info.get("phone")), 1 if registered else 0, dis.get("group"), status, now, citizen_id))
        else:
            citizen_id = device_citizen_id
            if not citizen_id or ctx.db.one("SELECT 1 FROM citizen_accounts WHERE citizen_id=?", (citizen_id,)):
                citizen_id = "u-" + secrets.token_hex(12)
            prev_role = ctx.db.one("SELECT role FROM citizen_profiles WHERE citizen_id=?", (citizen_id,))
            declared = bool(prev_role and prev_role["role"] == "priority_citizen")
            status = "mygov_record" if registered else ("declared" if declared else "none")
            ctx.db.execute(
                "INSERT INTO citizen_accounts (citizen_id, sub_hash, pin_hash, provider, given_name, family_name, birthdate,"
                " phone_masked, mygov_disability, mygov_disability_group, disability_enabled, disability_status, prompt_done,"
                " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,0,?,?)",
                (citizen_id, sub_hash, _hash("pin:" + str(info.get("pin", ""))), "mygov", info.get("given_name", ""),
                 info.get("family_name", ""), info.get("birthdate", ""), _mask_phone(info.get("phone")),
                 1 if registered else 0, dis.get("group"), 1 if declared else 0, status, now, now))
        ctx.db.audit(citizen_id, "citizen", "mygov_login", {"mygov_disability": registered, "new": not existing})
    return get_account(ctx, citizen_id)


def citizen_token(citizen_id: str) -> str:
    return create_token(citizen_id, "citizen", ttl=settings.citizen_token_ttl_seconds)


def set_disability(ctx: AppContext, citizen_id: str, enabled: bool) -> dict:
    """Əlillik xüsusiyyətini istənilən vaxt yandırıb-söndürmək. Rol (priority_citizen) bununla sinxron saxlanılır."""
    from . import sos as sos_svc
    acc = get_account(ctx, citizen_id)
    now = time.time()
    if acc:
        status = acc["disability_status"]
        if enabled and acc["mygov_disability"] and status != "rejected":
            status = "verified"  # mənbə: myGov profil qeydi (OAuth ilə birbaşa myGov-dan alınıb)
        elif enabled and status == "none":
            status = "declared"
        verified_now = status == "verified" and acc["disability_status"] != "verified"
        ctx.db.execute("UPDATE citizen_accounts SET disability_enabled=?, disability_status=?, prompt_done=1, updated_at=?,"
                       " verified_at=COALESCE(?, verified_at), verified_by=COALESCE(?, verified_by) WHERE citizen_id=?",
                       (1 if enabled else 0, status, now, now if verified_now else None,
                        "mygov_record" if verified_now else None, citizen_id))
        ctx.db.audit(citizen_id, "citizen", "disability_toggle", {"enabled": enabled})
    sync_role(ctx, citizen_id)
    return account_out(get_account(ctx, citizen_id))


def priority_allowed(acc: dict | None) -> bool:
    """Prioritet rol hüququ: əlillik myGov ilə təsdiqlənib və aktivdir."""
    if not settings.priority_requires_verification:
        return bool(acc and acc["disability_enabled"])
    return bool(acc and acc["disability_enabled"] and acc["disability_status"] == "verified")


def sync_role(ctx: AppContext, citizen_id: str) -> str:
    """Rolu hesab vəziyyətindən hesablayır (müştərinin istəyindən yox): təsdiqlənib -> priority_citizen, əks halda citizen."""
    from . import sos as sos_svc
    role = "priority_citizen" if priority_allowed(get_account(ctx, citizen_id)) else "citizen"
    sos_svc.set_profile(ctx, citizen_id, role)
    return role


def skip_prompt(ctx: AppContext, citizen_id: str) -> dict:
    ctx.db.execute("UPDATE citizen_accounts SET prompt_done=1, updated_at=? WHERE citizen_id=?", (time.time(), citizen_id))
    ctx.db.audit(citizen_id, "citizen", "disability_prompt_skip")
    return account_out(get_account(ctx, citizen_id))


def save_certificate(ctx: AppContext, citizen_id: str, content: bytes, mime: str) -> dict:
    """Arayış faylını yoxlayıb saxlayır. myGov qeydi varsa — avtomatik 'verified', yoxdursa — dispetçer yoxlaması."""
    if mime not in ALLOWED_UPLOADS:
        raise ValueError("unsupported_file_type")
    ext, magics = ALLOWED_UPLOADS[mime]
    if not any(content.startswith(m) for m in magics):
        raise ValueError("file_content_mismatch")  # uzantı/MIME saxtakarlığına qarşı
    if len(content) > settings.max_upload_bytes:
        raise ValueError("file_too_large")
    acc = get_account(ctx, citizen_id)
    if not acc:
        raise PermissionError("account_required")
    os.makedirs(settings.upload_dir, exist_ok=True)
    name = secrets.token_hex(16) + ext
    with open(os.path.join(settings.upload_dir, name), "wb") as f:
        f.write(content)
    if acc["certificate_file"]:
        try:
            os.remove(os.path.join(settings.upload_dir, acc["certificate_file"]))
        except OSError:
            pass
    # Faylın məzmununu sistem yoxlaya bilməz -> fayl heç vaxt avtomatik təsdiq etmir, yalnız dispetçer yoxlayır.
    # Artıq myGov ilə təsdiqlənibsə, status dəyişmir (fayl sadəcə əlavə olunur).
    status = "verified" if acc["disability_status"] == "verified" else "pending_review"
    now = time.time()
    ctx.db.execute(
        "UPDATE citizen_accounts SET certificate_file=?, certificate_mime=?, certificate_uploaded_at=?, disability_status=?,"
        " disability_enabled=1, prompt_done=1, updated_at=? WHERE citizen_id=?",
        (name, mime, now, status, now, citizen_id))
    ctx.db.audit(citizen_id, "citizen", "certificate_upload", {"status": status, "bytes": len(content)})
    sync_role(ctx, citizen_id)   # fayl özü prioritet vermir — dispetçer təsdiqindən sonra
    ctx.bus.publish("verification_update", {"citizen_id": citizen_id, "status": status}, channel="ops")
    return account_out(get_account(ctx, citizen_id))


async def verify_certificate_number(ctx: AppContext, provider: MyGovProvider, citizen_id: str, number: str) -> dict:
    """Arayış nömrəsini myGov reyestrində yoxlayır: mövcuddur, BU şəxsə aiddir, qüvvədədir -> verified.

    Uğursuz cəhd statusu dəyişmir; səbəb qaytarılır (not_found / not_owner / expired / revoked).
    """
    from . import sos as sos_svc
    acc = get_account(ctx, citizen_id)
    if not acc:
        raise PermissionError("account_required")
    result = await provider.lookup_certificate(number)
    reason = None
    if not result.get("found"):
        reason = "not_found"
    elif _hash("sub:" + str(result.get("holder_sub"))) != acc["sub_hash"]:
        reason = "not_owner"  # başqasının arayışı
    elif result.get("status") != "active":
        reason = result.get("status") or "invalid"
    ctx.db.audit(citizen_id, "citizen", "certificate_verify", {"ok": reason is None, "reason": reason})
    if reason:
        raise ValueError(reason)
    now = time.time()
    ctx.db.execute(
        "UPDATE citizen_accounts SET disability_status='verified', disability_enabled=1, prompt_done=1,"
        " mygov_disability_group=COALESCE(mygov_disability_group, ?), verified_at=?, verified_by='mygov_certificate',"
        " updated_at=? WHERE citizen_id=?", (result.get("group"), now, now, citizen_id))
    sync_role(ctx, citizen_id)
    ctx.bus.publish("verification_update", {"citizen_id": citizen_id, "status": "verified"}, channel="ops")
    return {**account_out(get_account(ctx, citizen_id)), "certificate": {
        "number": result.get("certificate_no"), "group": result.get("group"), "valid_until": result.get("valid_until")}}


def review(ctx: AppContext, citizen_id: str, approve: bool, actor: dict) -> dict | None:
    acc = get_account(ctx, citizen_id)
    if not acc:
        return None
    status = "verified" if approve else "rejected"
    now = time.time()
    ctx.db.execute("UPDATE citizen_accounts SET disability_status=?, verified_at=?, verified_by=?, updated_at=? WHERE citizen_id=?",
                   (status, now, actor["sub"], now, citizen_id))
    ctx.db.audit(actor["sub"], actor["role"], "verification_" + ("approve" if approve else "reject"), {"citizen_id": citizen_id})
    sync_role(ctx, citizen_id)
    ctx.bus.publish("verification_update", {"citizen_id": citizen_id, "status": status}, channel="ops")
    ctx.bus.publish("account_update", {"status": status}, citizen_id=citizen_id)
    return account_out(get_account(ctx, citizen_id))


def pending_reviews(ctx: AppContext) -> list[dict]:
    rows = ctx.db.query("SELECT * FROM citizen_accounts WHERE disability_status IN ('pending_review','declared')"
                        " AND disability_enabled=1 ORDER BY updated_at DESC LIMIT 100")
    return [{**account_out(r), "full_name": f'{r["given_name"]} {r["family_name"]}'.strip()} for r in rows]
