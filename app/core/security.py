"""Təhlükəsizlik: parol heşi, imzalı token (JWT HS256), RBAC və sorğu limiti (rate limit)."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from collections import defaultdict, deque

from fastapi import Depends, HTTPException, Request

from ..config import settings

# ---------------------------------------------------------------------------
# RBAC: rol -> icazələr. Yeni rol əlavə etmək = bu lüğətə bir sətir (Challenge Drop-a hazır).
# ---------------------------------------------------------------------------
ROLES: dict[str, set[str]] = {
    "dispatcher": {
        "ops:read", "sos:manage", "alert:decide", "shelter:manage", "brigade:manage", "road:manage", "demo:run",
    },
    "admin": {"*"},
}


def role_permissions(role: str) -> set[str]:
    return ROLES.get(role, set())


def has_permission(role: str, permission: str) -> bool:
    perms = role_permissions(role)
    return "*" in perms or permission in perms


# ----- Parol -----
PBKDF2_ITERATIONS = 120_000


def hash_password(password: str, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), PBKDF2_ITERATIONS).hex()
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${salt}${digest}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, iterations, salt, digest = stored.split("$")
        check = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), int(iterations)).hex()
        return hmac.compare_digest(check, digest)
    except (ValueError, TypeError):
        return False


# ----- Token (JWT HS256, standart kitabxana ilə) -----
def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def create_token(username: str, role: str, ttl: int | None = None) -> str:
    header = _b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    now = int(time.time())
    payload = _b64(json.dumps({"sub": username, "role": role, "iat": now,
                               "exp": now + (ttl or settings.token_ttl_seconds)}).encode())
    sig = hmac.new(settings.secret_key.encode(), f"{header}.{payload}".encode(), hashlib.sha256).digest()
    return f"{header}.{payload}.{_b64(sig)}"


def decode_token(token: str) -> dict | None:
    try:
        header, payload, sig = token.split(".")
        expected = hmac.new(settings.secret_key.encode(), f"{header}.{payload}".encode(), hashlib.sha256).digest()
        if not hmac.compare_digest(expected, _unb64(sig)):
            return None
        if json.loads(_unb64(header)).get("alg") != "HS256":
            return None
        data = json.loads(_unb64(payload))
        if data.get("exp", 0) < time.time():
            return None
        return data
    except Exception:
        return None


def current_user(request: Request) -> dict | None:
    auth = request.headers.get("authorization", "")
    token = auth[7:] if auth.lower().startswith("bearer ") else request.query_params.get("token", "")
    return decode_token(token) if token else None


def require(permission: str):
    """FastAPI asılılığı: tələb olunan icazəni yoxlayır (401 / 403)."""

    def dependency(request: Request) -> dict:
        user = current_user(request)
        if not user:
            raise HTTPException(401, "authentication_required")
        if not has_permission(user.get("role", ""), permission):
            raise HTTPException(403, "permission_denied")
        return user

    return dependency


# ----- Rate limit (sürüşən pəncərə, yaddaşda) -----
class RateLimiter:
    def __init__(self) -> None:
        self._hits: dict[str, deque] = defaultdict(deque)
        self._last_cleanup = time.time()

    def allow(self, key: str, limit: int, window_s: float = 60.0) -> bool:
        now = time.time()
        q = self._hits[key]
        while q and q[0] <= now - window_s:
            q.popleft()
        if len(q) >= limit:
            return False
        q.append(now)
        if now - self._last_cleanup > 120:
            self._cleanup(now, window_s)
        return True

    def _cleanup(self, now: float, window_s: float) -> None:
        self._last_cleanup = now
        for k in [k for k, q in self._hits.items() if not q or q[-1] <= now - window_s]:
            del self._hits[k]

    def reset(self) -> None:
        self._hits.clear()


limiter = RateLimiter()


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def rate_limit(bucket: str, limit_attr: str):
    """IP üzrə dəqiqəlik limit. Limit `settings`-dən oxunur ki, işləyərkən dəyişdirilə bilsin."""

    def dependency(request: Request) -> None:
        limit = getattr(settings, limit_attr)
        if not limiter.allow(f"{bucket}:{client_ip(request)}", limit):
            raise HTTPException(429, "rate_limit_exceeded", headers={"Retry-After": "60"})

    return Depends(dependency)
