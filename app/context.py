"""Tətbiq konteksti: bütün servislərin paylaşdığı asılılıqlar (DI konteyneri)."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from fastapi import Request

from .core.db import Database
from .core.events import EventBus
from .core.geo import haversine_m
from .core.graph import RoadGraph
from .services.routing import RoutingService


@dataclass
class AppContext:
    db: Database
    bus: EventBus
    graph: RoadGraph
    routing: RoutingService
    city: dict
    replan: Any = field(default=None)       # ReplanScheduler (services/replan.py)
    inflow_task: Any = field(default=None)  # demo: canlı insan axını

    @property
    def districts(self) -> list[dict]:
        return self.city["districts"]

    def district_name(self, district_id: str | None) -> str:
        for d in self.districts:
            if d["id"] == district_id:
                return d["name"]
        return district_id or ""

    def district(self, district_id: str) -> dict | None:
        return next((d for d in self.districts if d["id"] == district_id), None)

    def nearest_district(self, lat: float, lon: float) -> str:
        return min(self.districts, key=lambda d: haversine_m(lat, lon, d["lat"], d["lon"]))["id"]


def load_city(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def get_ctx(request: Request) -> AppContext:
    return request.app.state.ctx
