"""Mərkəzi konfiqurasiya. Bütün dəyərlər mühit dəyişənləri ilə dəyişdirilə bilər (12-factor)."""
import os
import secrets
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _env(name: str, default: str) -> str:
    return os.getenv(name, default)


class Settings:
    # --- Ümumi ---
    app_name = "Urban Crisis & Crowd Evacuation Engine"
    version = "1.0.0"
    db_path = _env("UCE_DB_PATH", str(BASE_DIR / "data" / "uce.db"))
    city_file = _env("UCE_CITY_FILE", str(BASE_DIR / "app" / "data" / "city.json"))
    demo_mode = _env("UCE_DEMO_MODE", "1") == "1"

    # --- Təhlükəsizlik ---
    secret_key = _env("UCE_SECRET_KEY", "") or secrets.token_hex(32)
    token_ttl_seconds = int(_env("UCE_TOKEN_TTL", "28800"))
    dispatcher_password = _env("UCE_DISPATCHER_PASSWORD", "dispatcher123")
    admin_password = _env("UCE_ADMIN_PASSWORD", "admin123")
    cors_origins = [o for o in _env("UCE_CORS_ORIGINS", "").split(",") if o]
    rate_limit_sos_per_min = int(_env("UCE_RATE_SOS_PER_MIN", "6"))
    rate_limit_login_per_min = int(_env("UCE_RATE_LOGIN_PER_MIN", "10"))
    rate_limit_general_per_min = int(_env("UCE_RATE_GENERAL_PER_MIN", "600"))

    # --- myGov ilə qeydiyyat (mock | oidc | off) ---
    mygov_mode = _env("UCE_MYGOV_MODE", "mock")
    # Prioritet (Kritik Prioritet SOS) yalnız myGov ilə əlilliyi TƏSDİQLƏNMİŞ vətəndaşa verilir (komanda qərarı)
    priority_requires_verification = _env("UCE_PRIORITY_REQUIRES_VERIFICATION", "1") == "1"
    # FİN / provayder ID-sinin heşi üçün sabit açar (SECRET_KEY dəyişsə də şəxs öz hesabına uyğun gəlsin)
    identity_pepper = _env("UCE_IDENTITY_PEPPER", "uce-identity-pepper-change-in-production")
    mygov_authorize_url = _env("UCE_MYGOV_AUTHORIZE_URL", "")
    mygov_token_url = _env("UCE_MYGOV_TOKEN_URL", "")
    mygov_userinfo_url = _env("UCE_MYGOV_USERINFO_URL", "")
    mygov_client_id = _env("UCE_MYGOV_CLIENT_ID", "")
    mygov_client_secret = _env("UCE_MYGOV_CLIENT_SECRET", "")
    # Əlillik arayışı xidmətinin ünvanı (dəqiq xidmət linki məlum olduqda env ilə dəyişdirilir)
    mygov_certificate_url = _env("UCE_MYGOV_CERTIFICATE_URL", "https://mygov.az")
    citizen_token_ttl_seconds = int(_env("UCE_CITIZEN_TOKEN_TTL", "604800"))
    upload_dir = _env("UCE_UPLOAD_DIR", str(BASE_DIR / "data" / "uploads"))
    max_upload_bytes = int(_env("UCE_MAX_UPLOAD_BYTES", str(5 * 1024 * 1024)))

    # --- Biznes qaydaları (tapşırıqdakı hədlər) ---
    shelter_reroute_pct = float(_env("UCE_SHELTER_REROUTE_PCT", "90"))   # >= 90% -> alternativ marşrut
    shelter_critical_pct = float(_env("UCE_SHELTER_CRITICAL_PCT", "95"))  # > 95% -> Qırmızı Həyəcan
    sos_cluster_count = int(_env("UCE_SOS_CLUSTER_COUNT", "4"))          # ən azı 4 fərqli SOS
    sos_cluster_window_s = int(_env("UCE_SOS_CLUSTER_WINDOW_S", "300"))  # 5 dəqiqə
    sos_cluster_radius_m = float(_env("UCE_SOS_CLUSTER_RADIUS_M", "500"))  # 500 m radius
    alert_cooldown_s = int(_env("UCE_ALERT_COOLDOWN_S", "300"))
    nearest_shelters_limit = int(_env("UCE_NEAREST_LIMIT", "5"))
    walking_speed_kmh = float(_env("UCE_WALK_SPEED_KMH", "4.5"))
    wheelchair_speed_kmh = float(_env("UCE_WHEELCHAIR_SPEED_KMH", "3.0"))

    # --- Xarici Xəritə və Marşrutlaşdırma API-si ---
    # Boşdursa: simulyasiya olunmuş API eyni prosesdə (in-process ASGI) çağırılır.
    # Docker-də: http://external-api:8001 (ayrı konteyner).
    external_api_url = _env("UCE_EXTERNAL_API_URL", "")
    external_timeout_s = float(_env("UCE_EXTERNAL_TIMEOUT_S", "2.5"))  # Drop 2: 5 s həddi aşılmadan ehtiyata keç
    external_max_wait_s = float(_env("UCE_EXTERNAL_MAX_WAIT_S", "5"))   # retry daxil ümumi gözləmə tavanı
    external_retries = int(_env("UCE_EXTERNAL_RETRIES", "1"))
    breaker_fail_threshold = int(_env("UCE_BREAKER_FAILS", "3"))
    breaker_reset_s = float(_env("UCE_BREAKER_RESET_S", "15"))
    traffic_sync_interval_s = float(_env("UCE_TRAFFIC_SYNC_S", "4"))
    route_cache_ttl_s = float(_env("UCE_ROUTE_CACHE_TTL_S", "20"))
    replan_concurrency = int(_env("UCE_REPLAN_CONCURRENCY", "1"))   # fon yenidən marşrutlaşdırmanın paralelliyi (GIL: 1 ən yaxşı gecikmə)


settings = Settings()
