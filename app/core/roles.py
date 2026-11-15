"""Vətəndaş rolları reyestri (Challenge Drop 1).

Hər rol məlumat kimi təsvir olunur: SOS prioriteti, marşrut profili, uyğun toplanış məntəqəsi
şərti və tələb olunan briqada xüsusiyyəti. Yeni rol əlavə etmək = bu lüğətə bir giriş;
servis kodu dəyişmir (Open/Closed prinsipi).

Prioritet şkalası: 1 = Kritik Prioritet (Priority #1), 2 = Standart.
"""
from __future__ import annotations

DEFAULT_CITIZEN_ROLE = "citizen"

CITIZEN_ROLES: dict[str, dict] = {
    "citizen": {
        "sos_priority": 2,
        "route_profile": "foot",
        "accessible_shelters_only": False,
        "brigade_requirement": None,
    },
    # "Hərəkət Məhdudiyyətli Şəxs / Xüsusi Qayğıya Ehtiyacı Olan Vətəndaş" (Prioritet SOS)
    "priority_citizen": {
        "sos_priority": 1,
        "route_profile": "wheelchair",          # yalnız panduslu və maneəsiz yol seqmentləri
        "accessible_shelters_only": True,       # yalnız əlçatan toplanış məntəqələri
        "brigade_requirement": "accessible_transport",  # əlil arabası / tibbi nəqliyyat
    },
}

PRIORITY_CRITICAL = 1
PRIORITY_STANDARD = 2


def role_policy(role: str | None) -> dict:
    return CITIZEN_ROLES.get(role or DEFAULT_CITIZEN_ROLE, CITIZEN_ROLES[DEFAULT_CITIZEN_ROLE])


def citizen_role(db, citizen_id: str | None) -> str:
    """Vətəndaşın saxlanılmış rolu (profil yoxdursa — standart)."""
    if not citizen_id:
        return DEFAULT_CITIZEN_ROLE
    row = db.one("SELECT role FROM citizen_profiles WHERE citizen_id=?", (citizen_id,))
    return row["role"] if row and row["role"] in CITIZEN_ROLES else DEFAULT_CITIZEN_ROLE
