"""myGov ilə qeydiyyat, vətəndaş hesabı, əlillik statusu və arayışın yoxlanması."""
from __future__ import annotations

import os

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import BaseModel, Field

from ..config import settings
from ..context import AppContext, get_ctx
from ..core.security import current_user, rate_limit, require
from ..services import analytics, identity

router = APIRouter(tags=["account"])
CITIZEN_ID_RE = r"^[A-Za-z0-9_\-]{8,64}$"


class DisabilityToggle(BaseModel):
    enabled: bool


class CertificateNumber(BaseModel):
    certificate_no: str = Field(min_length=4, max_length=40, pattern=r"^[A-Za-zƏəӘә0-9\-/ ]+$")


def citizen_user(request: Request) -> dict:
    """Vətəndaş tokeni (myGov ilə giriş) tələb edir."""
    user = current_user(request)
    if not user or user.get("role") != "citizen":
        raise HTTPException(401, "citizen_auth_required")
    return user


def _provider(request: Request):
    p = request.app.state.mygov
    if p is None:
        raise HTTPException(503, "mygov_disabled")
    return p


# ----- OAuth2 axını -----
@router.get("/api/v1/auth/mygov/start", dependencies=[rate_limit("mygov", "rate_limit_login_per_min")])
def mygov_start(request: Request, citizen_id: str | None = Query(default=None, pattern=CITIZEN_ID_RE)):
    """Brauzeri myGov (və ya simulyatoru) giriş səhifəsinə yönləndirir. Cihazın anonim ID-si hesaba bağlanır."""
    redirect_uri = str(request.url_for("mygov_callback"))
    return RedirectResponse(_provider(request).begin(citizen_id, redirect_uri), status_code=302)


@router.get("/api/v1/auth/mygov/callback", name="mygov_callback")
async def mygov_callback(request: Request, code: str = Query(default="", max_length=512),
                         state: str = Query(default="", max_length=256), error: str = Query(default="", max_length=64),
                         ctx: AppContext = Depends(get_ctx)):
    if error or not code or not state:
        return RedirectResponse("/citizen#mygov_error=" + (error or "cancelled"), status_code=303)
    try:
        info, device_id = await _provider(request).complete(code, state)
    except PermissionError as exc:
        return RedirectResponse("/citizen#mygov_error=" + str(exc), status_code=303)
    except Exception:
        return RedirectResponse("/citizen#mygov_error=provider_unavailable", status_code=303)
    acc = identity.upsert_from_mygov(ctx, info, device_id)
    # Token URL fraqmentində ötürülür: fraqment serverə göndərilmir, loglara düşmür
    return RedirectResponse(f"/citizen#mygov={identity.citizen_token(acc['citizen_id'])}", status_code=303)


# ----- Vətəndaşın öz hesabı -----
@router.get("/api/v1/me")
def me(user: dict = Depends(citizen_user), ctx: AppContext = Depends(get_ctx)):
    acc = identity.get_account(ctx, user["sub"])
    if not acc:
        raise HTTPException(401, "citizen_auth_required")
    return identity.account_out(acc)


@router.post("/api/v1/me/disability/skip")
def disability_skip(user: dict = Depends(citizen_user), ctx: AppContext = Depends(get_ctx)):
    """Popup-da "Keç"."""
    return identity.skip_prompt(ctx, user["sub"])


@router.put("/api/v1/me/disability")
def disability_toggle(body: DisabilityToggle, user: dict = Depends(citizen_user), ctx: AppContext = Depends(get_ctx)):
    """Popup-da "Təsdiq et" (enabled=true) və ya profildə istənilən vaxt dəyişmək."""
    result = identity.set_disability(ctx, user["sub"], body.enabled)
    analytics.invalidate()
    return result


@router.post("/api/v1/me/disability/verify", dependencies=[rate_limit("certverify", "rate_limit_login_per_min")])
async def certificate_verify(body: CertificateNumber, request: Request, user: dict = Depends(citizen_user),
                             ctx: AppContext = Depends(get_ctx)):
    """Arayış nömrəsini myGov reyestrində yoxlamaq. Brute-force-a qarşı dəqiqədə limitlidir."""
    try:
        result = await identity.verify_certificate_number(ctx, _provider(request), user["sub"], body.certificate_no)
    except ValueError as exc:
        raise HTTPException(422, "certificate_" + str(exc))
    except PermissionError:
        raise HTTPException(401, "citizen_auth_required")
    except RuntimeError:
        raise HTTPException(503, "mygov_unavailable")
    analytics.invalidate()
    return result


@router.post("/api/v1/me/disability/certificate", dependencies=[rate_limit("upload", "rate_limit_login_per_min")])
async def certificate_upload(request: Request, user: dict = Depends(citizen_user), ctx: AppContext = Depends(get_ctx)):
    """Arayış faylı (PDF / JPG / PNG) — sorğu gövdəsi faylın özüdür, Content-Type faylın tipidir."""
    mime = request.headers.get("content-type", "").split(";")[0].strip().lower()
    content = await request.body()
    if not content:
        raise HTTPException(422, "empty_file")
    try:
        result = identity.save_certificate(ctx, user["sub"], content, mime)
    except ValueError as exc:
        raise HTTPException(415 if "type" in str(exc) or "mismatch" in str(exc) else 413, str(exc))
    except PermissionError:
        raise HTTPException(401, "citizen_auth_required")
    analytics.invalidate()
    return result


# ----- Dispetçer: yoxlama -----
@router.get("/api/v1/dispatch/verifications")
def verifications(user: dict = Depends(require("verification:manage")), ctx: AppContext = Depends(get_ctx)):
    return {"verifications": identity.pending_reviews(ctx)}


@router.post("/api/v1/dispatch/verifications/{citizen_id}/{decision}")
def verification_decide(citizen_id: str, decision: str, user: dict = Depends(require("verification:manage")),
                        ctx: AppContext = Depends(get_ctx)):
    if decision not in ("approve", "reject"):
        raise HTTPException(404, "not_found")
    result = identity.review(ctx, citizen_id, decision == "approve", user)
    if result is None:
        raise HTTPException(404, "account_not_found")
    analytics.invalidate()
    return result


@router.get("/api/v1/dispatch/verifications/{citizen_id}/certificate")
def verification_file(citizen_id: str, user: dict = Depends(require("verification:manage")),
                      ctx: AppContext = Depends(get_ctx)):
    acc = identity.get_account(ctx, citizen_id)
    if not acc or not acc["certificate_file"]:
        raise HTTPException(404, "certificate_not_found")
    path = os.path.join(settings.upload_dir, os.path.basename(acc["certificate_file"]))
    if not os.path.isfile(path):
        raise HTTPException(404, "certificate_not_found")
    ctx.db.audit(user["sub"], user["role"], "certificate_view", {"citizen_id": citizen_id})
    return FileResponse(path, media_type=acc["certificate_mime"], headers={"Cache-Control": "no-store"})
