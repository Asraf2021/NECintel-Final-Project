"""SOS siqnalları və vətəndaşın "Təhlükəsizəm" statusu."""
from __future__ import annotations

import time

from ..context import AppContext
from ..core.geo import haversine_m
from ..core.roles import CITIZEN_ROLES, DEFAULT_CITIZEN_ROLE, citizen_role, role_policy
from . import crisis, shelters as shelter_svc

DUPLICATE_WINDOW_S = 30  # eyni vətəndaşın təkrar kliki yeni siqnal yaratmır (idempotentlik)


def _sos_out(ctx: AppContext, s: dict) -> dict:
    return {**s, "district_name": ctx.district_name(s["district_id"])}


def create_sos(ctx: AppContext, citizen_id: str, sos_type: str, lat: float, lon: float, note: str = "") -> dict:
    now = time.time()
    dup = ctx.db.one(
        "SELECT * FROM sos_signals WHERE citizen_id=? AND type=? AND created_at>=? AND status IN ('active','assigned')"
        " ORDER BY id DESC LIMIT 1", (citizen_id, sos_type, now - DUPLICATE_WINDOW_S))
    if dup:
        return {"sos": _sos_out(ctx, dup), "duplicate": True, "alert": None}

    district_id = ctx.nearest_district(lat, lon)
    # Prioritet server tərəfində vətəndaşın rolundan təyin olunur (müştəri prioriteti özü göndərə bilməz)
    role = citizen_role(ctx.db, citizen_id)
    priority = role_policy(role)["sos_priority"]
    with ctx.db.transaction():
        cur = ctx.db.execute(
            "INSERT INTO sos_signals (citizen_id,type,lat,lon,district_id,note,status,priority,citizen_role,created_at,updated_at)"
            " VALUES (?,?,?,?,?,?,'active',?,?,?,?)",
            (citizen_id, sos_type, lat, lon, district_id, note, priority, role, now, now))
        sos_id = cur.lastrowid
        ctx.db.execute(
            "INSERT INTO citizen_status (citizen_id,status,lat,lon,updated_at) VALUES (?,?,?,?,?)"
            " ON CONFLICT(citizen_id) DO UPDATE SET status='sos', lat=excluded.lat,"
            " lon=excluded.lon, updated_at=excluded.updated_at", (citizen_id, "sos", lat, lon, now))
    sos = _sos_out(ctx, ctx.db.one("SELECT * FROM sos_signals WHERE id=?", (sos_id,)))
    ctx.bus.publish("sos_new", {"sos": sos}, channel="ops")
    if priority == 1:
        ctx.bus.publish("sos_priority", {"sos": sos}, channel="ops")
    alert = crisis.evaluate_sos_cluster(ctx, sos)
    return {"sos": sos, "duplicate": False, "alert": alert}


def list_sos(ctx: AppContext, status: str | None = None, limit: int = 200) -> list[dict]:
    # Sıralama: əvvəl aktiv Kritik Prioritet (Priority #1), sonra ən yenilər
    order = " ORDER BY (status IN ('active','assigned') AND priority=1) DESC, created_at DESC LIMIT ?"
    if status:
        rows = ctx.db.query("SELECT * FROM sos_signals WHERE status=?" + order, (status, limit))
    else:
        rows = ctx.db.query("SELECT * FROM sos_signals" + order, (limit,))
    return [_sos_out(ctx, s) for s in rows]


def set_status(ctx: AppContext, citizen_id: str, status: str, shelter_id: str | None,
               lat: float | None, lon: float | None) -> dict:
    """"Təhlükəsizəm" statusu. Məntəqə göstərilibsə, doluluq 1 nəfər artır (bir vətəndaş — bir dəfə)."""
    now = time.time()
    checked_in = None
    with ctx.db.transaction():
        prev = ctx.db.one("SELECT * FROM citizen_status WHERE citizen_id=?", (citizen_id,))
        # Vətəndaşın hazırda sayıldığı məntəqə (statusundan asılı olmayaraq) — ikiqat sayılmanın qarşısı alınır
        prev_shelter = prev["shelter_id"] if prev else None
        new_shelter = shelter_id if status == "safe" else None
        if new_shelter and not ctx.db.one("SELECT id FROM shelters WHERE id=? AND status='open'", (new_shelter,)):
            new_shelter = None
        ctx.db.execute(
            "INSERT INTO citizen_status (citizen_id,status,shelter_id,lat,lon,updated_at) VALUES (?,?,?,?,?,?)"
            " ON CONFLICT(citizen_id) DO UPDATE SET status=excluded.status, shelter_id=excluded.shelter_id,"
            " lat=COALESCE(excluded.lat, citizen_status.lat), lon=COALESCE(excluded.lon, citizen_status.lon),"
            " updated_at=excluded.updated_at", (citizen_id, status, new_shelter, lat, lon, now))
        resolved = 0
        if status == "safe":
            resolved = ctx.db.execute(
                "UPDATE sos_signals SET status='safe', updated_at=? WHERE citizen_id=? AND status IN ('active','assigned')",
                (now, citizen_id)).rowcount
            ctx.db.execute("DELETE FROM evacuation_plans WHERE citizen_id=?", (citizen_id,))
    # Doluluq dəyişikliyi (əvvəlki məntəqədən çıxış, yenisinə giriş)
    if prev_shelter and prev_shelter != new_shelter:
        shelter_svc.update_shelter(ctx, prev_shelter, delta=-1)
    if new_shelter and new_shelter != prev_shelter:
        checked_in = shelter_svc.update_shelter(ctx, new_shelter, delta=1)
        if checked_in:
            crisis.evaluate_shelter(ctx, checked_in)
    ctx.bus.publish("citizen_status", {"citizen_id": citizen_id, "status": status, "shelter_id": new_shelter,
                                       "resolved_sos": resolved}, channel="ops")
    return {"citizen_id": citizen_id, "status": status, "shelter": checked_in, "resolved_sos": resolved}


def citizen_state(ctx: AppContext, citizen_id: str) -> dict:
    st = ctx.db.one("SELECT * FROM citizen_status WHERE citizen_id=?", (citizen_id,))
    active = ctx.db.query(
        "SELECT * FROM sos_signals WHERE citizen_id=? AND status IN ('active','assigned') ORDER BY id DESC", (citizen_id,))
    for s in active:
        if s["brigade_id"]:
            b = ctx.db.one("SELECT name, name_en FROM brigades WHERE id=?", (s["brigade_id"],))
            s["brigade_name"] = b["name"] if b else None
            s["brigade_name_en"] = b["name_en"] if b else None
    profile = ctx.db.one("SELECT role, needs FROM citizen_profiles WHERE citizen_id=?", (citizen_id,))
    return {"citizen_id": citizen_id, "status": st["status"] if st else "unknown",
            "shelter_id": st["shelter_id"] if st else None, "active_sos": active,
            "role": profile["role"] if profile else DEFAULT_CITIZEN_ROLE}


def set_profile(ctx: AppContext, citizen_id: str, role: str) -> dict:
    """Vətəndaşın rolunu saxlayır. Aktiv SOS-ların prioriteti yeni rola uyğun yenilənir."""
    if role not in CITIZEN_ROLES:
        raise ValueError("unknown_role")
    now = time.time()
    priority = role_policy(role)["sos_priority"]
    with ctx.db.transaction():
        ctx.db.execute(
            "INSERT INTO citizen_profiles (citizen_id, role, updated_at) VALUES (?,?,?)"
            " ON CONFLICT(citizen_id) DO UPDATE SET role=excluded.role, updated_at=excluded.updated_at",
            (citizen_id, role, now))
        changed = ctx.db.execute(
            "UPDATE sos_signals SET priority=?, citizen_role=?, updated_at=? WHERE citizen_id=?"
            " AND status IN ('active','assigned') AND (priority!=? OR citizen_role!=?)",
            (priority, role, now, citizen_id, priority, role)).rowcount
        # Rol dəyişdi -> köhnə marşrut etibarsızdır (məs. pilləkənli yol); növbəti sorğuda yenidən qurulur
        ctx.db.execute("DELETE FROM evacuation_plans WHERE citizen_id=?", (citizen_id,))
    if changed:
        ctx.bus.publish("sos_update", {"citizen_id": citizen_id, "priority": priority}, channel="ops")
    return {"citizen_id": citizen_id, "role": role, "sos_priority": priority}


def assign_brigade(ctx: AppContext, sos_id: int, brigade_id: str | None, actor: dict) -> dict | None:
    """SOS-a briqada təyin edir. brigade_id verilməyibsə — ən yaxın uyğun boş briqada seçilir."""
    with ctx.db.transaction():
        s = ctx.db.one("SELECT * FROM sos_signals WHERE id=?", (sos_id,))
        if not s:
            return None
        if brigade_id:
            brigade = ctx.db.one("SELECT * FROM brigades WHERE id=? AND status='available'", (brigade_id,))
        else:
            policy = role_policy(s["citizen_role"])
            preferred = "medical" if policy["brigade_requirement"] == "accessible_transport" else crisis.PREFERRED_BRIGADE.get(s["type"])
            brigade = crisis.nearest_available_brigade(ctx, s["lat"], s["lon"], preferred, policy["brigade_requirement"])
        if not brigade:
            return {"error": "no_brigade_available"}
        now = time.time()
        ctx.db.execute(
            "UPDATE brigades SET status='dispatched', target_lat=?, target_lon=?, assigned_sos_id=?, updated_at=? WHERE id=?",
            (s["lat"], s["lon"], sos_id, now, brigade["id"]))
        ctx.db.execute("UPDATE sos_signals SET status='assigned', brigade_id=?, updated_at=? WHERE id=?",
                       (brigade["id"], now, sos_id))
        ctx.db.audit(actor["sub"], actor["role"], "sos_assign", {"sos_id": sos_id, "brigade_id": brigade["id"]})
    out = _sos_out(ctx, ctx.db.one("SELECT * FROM sos_signals WHERE id=?", (sos_id,)))
    out["brigade_name"] = brigade["name"]
    out["brigade_distance_m"] = round(haversine_m(s["lat"], s["lon"], brigade["lat"], brigade["lon"]))
    ctx.bus.publish("sos_update", {"sos": out}, channel="ops")
    ctx.bus.publish("sos_assigned", {"sos_id": sos_id, "brigade_name": brigade["name"]}, citizen_id=s["citizen_id"])
    return out


def resolve_sos(ctx: AppContext, sos_id: int, actor: dict) -> dict | None:
    with ctx.db.transaction():
        s = ctx.db.one("SELECT * FROM sos_signals WHERE id=?", (sos_id,))
        if not s:
            return None
        now = time.time()
        ctx.db.execute("UPDATE sos_signals SET status='resolved', updated_at=? WHERE id=?", (now, sos_id))
        if s["brigade_id"]:
            ctx.db.execute(
                "UPDATE brigades SET status='available', target_lat=NULL, target_lon=NULL, assigned_sos_id=NULL,"
                " updated_at=? WHERE id=? AND assigned_sos_id=?", (now, s["brigade_id"], sos_id))
        ctx.db.audit(actor["sub"], actor["role"], "sos_resolve", {"sos_id": sos_id})
    out = _sos_out(ctx, ctx.db.one("SELECT * FROM sos_signals WHERE id=?", (sos_id,)))
    ctx.bus.publish("sos_update", {"sos": out}, channel="ops")
    ctx.bus.publish("sos_resolved", {"sos_id": sos_id}, citizen_id=s["citizen_id"])
    return out
