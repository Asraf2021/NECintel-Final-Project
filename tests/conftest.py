"""Ümumi fixture-lər: hər test üçün təzə tətbiq (yaddaşda SQLite, fon dövrü yoxdur)."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import app.core.security as security
from app.config import settings
from app.main import create_app
from app.services import analytics


@pytest.fixture(scope="session", autouse=True)
def _fast_password_hashing():
    """Testlərdə PBKDF2 iterasiya sayını azaldır (yalnız sürət üçün; alqoritm eynidir)."""
    old = security.PBKDF2_ITERATIONS
    security.PBKDF2_ITERATIONS = 2_000
    yield
    security.PBKDF2_ITERATIONS = old


@pytest.fixture
def client():
    """Təzə tətbiq + işləyən lifespan (app.state.ctx burada yaranır)."""
    security.limiter.reset()
    analytics.invalidate()
    app = create_app(db_path=":memory:", start_background=False)
    with TestClient(app) as c:
        yield c
    security.limiter.reset()
    analytics.invalidate()


@pytest.fixture
def ctx(client):
    """Tətbiq konteksti (db, bus, graph, routing)."""
    return client.app.state.ctx


@pytest.fixture
def auth_headers(client):
    """Dispetçer kimi daxil olur və Bearer başlığını qaytarır."""
    r = client.post("/api/v1/auth/login",
                    json={"username": "dispatcher", "password": settings.dispatcher_password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


@pytest.fixture
def admin_headers(client):
    """Admin kimi daxil olur."""
    r = client.post("/api/v1/auth/login", json={"username": "admin", "password": settings.admin_password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


@pytest.fixture
def events(ctx, monkeypatch):
    """ctx.bus.publish çağırışlarını qeyd edir (orijinal davranış saxlanılır)."""
    recorded: list[dict] = []
    original = ctx.bus.publish

    def spy(event, data=None, channel="public", citizen_id=None):
        recorded.append({"event": event, "data": data or {}, "channel": channel, "citizen_id": citizen_id})
        return original(event, data, channel=channel, citizen_id=citizen_id)

    monkeypatch.setattr(ctx.bus, "publish", spy)
    return recorded


@pytest.fixture
def sim(ctx):
    """Simulyasiya olunmuş xarici API-nin daxili vəziyyəti (closures, outage, ...)."""
    return ctx.routing.client._transport.app.state.sim
