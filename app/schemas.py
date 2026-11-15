"""Giriş məlumatlarının yoxlanması (Pydantic). Bütün xarici giriş burada validasiya olunur."""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator

SOS_TYPES = ("fire", "collapse", "flood", "medical")
SHELTER_TYPES = ("shelter", "park", "stadium")
BRIGADE_STATUSES = ("available", "dispatched", "on_scene", "busy", "offline")

CitizenId = Field(min_length=8, max_length=64, pattern=r"^[A-Za-z0-9_\-]+$")
Lat = Field(ge=-90, le=90)
Lon = Field(ge=-180, le=180)


class SosCreate(BaseModel):
    citizen_id: str = CitizenId
    type: Literal["fire", "collapse", "flood", "medical"]
    lat: float = Lat
    lon: float = Lon
    note: str = Field(default="", max_length=280)

    @field_validator("note")
    @classmethod
    def clean_note(cls, v: str) -> str:
        # idarəetmə simvollarını təmizlə; HTML frontend-də escape olunur
        return "".join(ch for ch in v if ch.isprintable()).strip()


class StatusUpdate(BaseModel):
    citizen_id: str = CitizenId
    status: Literal["safe", "evacuating"]
    shelter_id: Optional[str] = Field(default=None, max_length=16)
    lat: Optional[float] = Field(default=None, ge=-90, le=90)
    lon: Optional[float] = Field(default=None, ge=-180, le=180)


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)


class ShelterUpdate(BaseModel):
    occupancy: Optional[int] = Field(default=None, ge=0, le=1_000_000)
    status: Optional[Literal["open", "reserve", "closed"]] = None


class BrigadeUpdate(BaseModel):
    status: Literal["available", "dispatched", "on_scene", "busy", "offline"]


class SosAssign(BaseModel):
    brigade_id: Optional[str] = Field(default=None, max_length=16)


class RoadCloseRequest(BaseModel):
    reason: str = Field(default="", max_length=120)


class ScenarioRequest(BaseModel):
    name: Literal["sos_cluster", "shelter_fill", "road_close", "road_open_all", "api_outage", "api_restore", "crowd", "reset"]
    district_id: Optional[str] = Field(default=None, max_length=32)
    shelter_id: Optional[str] = Field(default=None, max_length=16)
    edge_id: Optional[str] = Field(default=None, max_length=16)
