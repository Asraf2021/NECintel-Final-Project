"""Giriş (login) və cari istifadəçi."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from ..config import settings
from ..context import AppContext, get_ctx
from ..core.security import create_token, hash_password, rate_limit, require, role_permissions, verify_password
from ..schemas import LoginRequest

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])

# Mövcud olmayan istifadəçi üçün də eyni vaxt sərf olunsun (timing hücumuna qarşı)
_DUMMY_HASH = hash_password("dummy-password")


@router.post("/login", dependencies=[rate_limit("login", "rate_limit_login_per_min")])
def login(body: LoginRequest, ctx: AppContext = Depends(get_ctx)):
    user = ctx.db.one("SELECT * FROM users WHERE username=?", (body.username,))
    ok = verify_password(body.password, user["password_hash"] if user else _DUMMY_HASH)
    if not user or not ok:
        ctx.db.audit(body.username[:64], "-", "login_failed")
        raise HTTPException(401, "invalid_credentials")
    ctx.db.audit(user["username"], user["role"], "login")
    return {
        "token": create_token(user["username"], user["role"]),
        "expires_in": settings.token_ttl_seconds,
        "user": {"username": user["username"], "role": user["role"], "display_name": user["display_name"],
                 "permissions": sorted(role_permissions(user["role"]))},
    }


@router.get("/me")
def me(user: dict = Depends(require("ops:read")), ctx: AppContext = Depends(get_ctx)):
    row = ctx.db.one("SELECT username, role, display_name FROM users WHERE username=?", (user["sub"],))
    if not row:
        raise HTTPException(401, "authentication_required")
    return {**row, "permissions": sorted(role_permissions(row["role"]))}
