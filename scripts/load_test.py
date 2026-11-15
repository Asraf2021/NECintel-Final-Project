#!/usr/bin/env python3
"""Yük testi — Urban Crisis & Crowd Evacuation Engine.

Çoxlu virtual vətəndaşı simulyasiya edir və real istifadə qarışığı ilə API-ni yükləyir:
  * GET  /api/v1/evacuation  — təxliyə planı (təsadüfi rayon və ya Bakı ətrafında təsadüfi nöqtə);
  * GET  /api/v1/shelters    — toplanış məntəqələri və doluluq;
  * POST /api/v1/sos         — SOS siqnalı (~5%);
  * POST /api/v1/status      — "Təhlükəsizəm" / "Təxliyə olunuram" statusu (~7%).

Yalnız httpx tələb olunur. İstifadə:

    python3 scripts/load_test.py --url http://127.0.0.1:8000 --users 200 --duration 15

Qeyd: bütün virtual istifadəçilər eyni IP-dən gəldiyi üçün serverin IP üzrə limiti tez dolur.
Tam ötürücülüyü ölçmək üçün serveri yüksək limitlərlə işə salın:

    UCE_RATE_GENERAL_PER_MIN=1000000 UCE_RATE_SOS_PER_MIN=1000000 python3 -m uvicorn app.main:app --port 8000

429 cavabları xəta sayılmır — ayrıca "limitləndi (gözlənilən qoruma)" kimi göstərilir.
"""
from __future__ import annotations

import argparse
import asyncio
import math
import random
import sys
import time
import uuid
from collections import Counter, defaultdict

import httpx

DISTRICTS = ["sabail", "nasimi", "yasamal", "narimanov", "khatai", "binagadi", "nizami", "sabunchu"]
SOS_TYPES = ["fire", "collapse", "flood", "medical"]
LAT_RANGE = (40.36, 40.45)   # Bakı ətrafı
LON_RANGE = (49.80, 49.96)

# Sorğu qarışığı (çəkilər cəmi = 100)
MIX = [("evacuation", 60), ("shelters", 28), ("sos", 5), ("status", 7)]


class Stats:
    """Bütün virtual istifadəçilərin paylaşdığı sayğaclar."""

    def __init__(self) -> None:
        self.latencies: dict[str, list[float]] = defaultdict(list)   # endpoint -> ms
        self.codes: Counter = Counter()                              # status kodu -> say
        self.by_endpoint: dict[str, Counter] = defaultdict(Counter)  # endpoint -> {ok, limited, error}
        self.transport_errors: Counter = Counter()                   # istisna növü -> say
        self.started = 0.0
        self.finished = 0.0
        self.health_after: dict | None = None             # testdən sonra /health cavabı

    def record(self, endpoint: str, status: int | None, ms: float, exc: str | None = None) -> None:
        self.latencies[endpoint].append(ms)
        if exc is not None:
            self.transport_errors[exc] += 1
            self.by_endpoint[endpoint]["error"] += 1
            return
        self.codes[status] += 1
        if status == 429:
            self.by_endpoint[endpoint]["limited"] += 1
        elif 200 <= status < 400:
            self.by_endpoint[endpoint]["ok"] += 1
        else:
            self.by_endpoint[endpoint]["error"] += 1


def percentile(sorted_values: list[float], p: float) -> float:
    """Ən yaxın dərəcə (nearest-rank) üsulu ilə persentil."""
    if not sorted_values:
        return 0.0
    k = max(0, min(len(sorted_values) - 1, math.ceil(p / 100 * len(sorted_values)) - 1))
    return sorted_values[k]


def random_point(rng: random.Random) -> tuple[float, float]:
    return round(rng.uniform(*LAT_RANGE), 5), round(rng.uniform(*LON_RANGE), 5)


async def virtual_user(idx: int, client: httpx.AsyncClient, sem: asyncio.Semaphore, stats: Stats,
                       deadline: float, think_s: float, seed: int) -> None:
    """Bir vətəndaşın davranışı: plan alır, məntəqələrə baxır, nadir hallarda SOS / status göndərir."""
    rng = random.Random(seed * 100_003 + idx)
    citizen_id = f"load-{uuid.UUID(int=rng.getrandbits(128)).hex[:16]}"
    lat, lon = random_point(rng)
    district = rng.choice(DISTRICTS)
    by_district = rng.random() < 0.4          # istifadəçilərin ~40%-i rayon seçir, qalanı GPS istifadə edir
    last_shelter: str | None = None
    names = [n for n, _ in MIX]
    weights = [w for _, w in MIX]

    await asyncio.sleep(rng.uniform(0, min(1.0, think_s * 5)))   # başlanğıcı səpələyir (thundering herd olmasın)
    while time.perf_counter() < deadline:
        action = rng.choices(names, weights)[0]
        if rng.random() < 0.15:               # vətəndaş hərəkət edir
            lat = min(LAT_RANGE[1], max(LAT_RANGE[0], lat + rng.uniform(-0.002, 0.002)))
            lon = min(LON_RANGE[1], max(LON_RANGE[0], lon + rng.uniform(-0.002, 0.002)))

        if action == "evacuation":
            params: dict = {"citizen_id": citizen_id}
            if by_district:
                params["district_id"] = district
            else:
                params.update(lat=round(lat, 5), lon=round(lon, 5))
            request = client.build_request("GET", "/api/v1/evacuation", params=params)
        elif action == "shelters":
            params = {"lat": round(lat, 5), "lon": round(lon, 5)} if rng.random() < 0.5 else {}
            request = client.build_request("GET", "/api/v1/shelters", params=params)
        elif action == "sos":
            request = client.build_request("POST", "/api/v1/sos", json={
                "citizen_id": citizen_id, "type": rng.choice(SOS_TYPES),
                "lat": round(lat, 5), "lon": round(lon, 5), "note": "Yük testi"})
        else:
            safe = last_shelter is not None and rng.random() < 0.5
            body = {"citizen_id": citizen_id, "status": "safe" if safe else "evacuating",
                    "lat": round(lat, 5), "lon": round(lon, 5)}
            if safe:
                body["shelter_id"] = last_shelter
            request = client.build_request("POST", "/api/v1/status", json=body)

        async with sem:
            t0 = time.perf_counter()
            try:
                resp = await client.send(request)
                ms = (time.perf_counter() - t0) * 1000
                stats.record(action, resp.status_code, ms)
                if action == "evacuation" and resp.status_code == 200:
                    rec = resp.json().get("recommended")
                    last_shelter = rec["shelter_id"] if rec else last_shelter
            except Exception as exc:  # şəbəkə xətası / timeout
                stats.record(action, None, (time.perf_counter() - t0) * 1000, type(exc).__name__)

        if think_s > 0:
            await asyncio.sleep(rng.uniform(0.5, 1.5) * think_s)


async def run(args: argparse.Namespace) -> Stats:
    stats = Stats()
    concurrency = args.concurrency or args.users
    limits = httpx.Limits(max_connections=concurrency, max_keepalive_connections=concurrency)
    async with httpx.AsyncClient(base_url=args.url, timeout=args.timeout, limits=limits) as client:
        try:
            health = await client.get("/health")
            health.raise_for_status()
        except Exception as exc:
            print(f"XƏTA: server əlçatmazdır ({args.url}): {type(exc).__name__}: {exc}", file=sys.stderr)
            raise SystemExit(2)
        sem = asyncio.Semaphore(concurrency)
        stats.started = time.perf_counter()
        deadline = stats.started + args.duration
        await asyncio.gather(*(virtual_user(i, client, sem, stats, deadline, args.think, args.seed)
                               for i in range(args.users)))
        stats.finished = time.perf_counter()
        try:
            stats.health_after = (await client.get("/health")).json()
        except Exception:
            stats.health_after = None
    return stats


def report(stats: Stats, args: argparse.Namespace) -> int:
    elapsed = max(stats.finished - stats.started, 1e-9)
    total = sum(len(v) for v in stats.latencies.values())
    limited = stats.codes.get(429, 0)
    ok = sum(n for code, n in stats.codes.items() if 200 <= code < 400)
    http_errors = sum(n for code, n in stats.codes.items() if code >= 400 and code != 429)
    transport = sum(stats.transport_errors.values())
    errors = http_errors + transport

    line = "=" * 78
    print(line)
    print("YÜK TESTİ HESABATI — Urban Crisis & Crowd Evacuation Engine")
    print(line)
    print(f"Hədəf:                {args.url}")
    print(f"Virtual istifadəçi:   {args.users}   (paralellik: {args.concurrency or args.users}, "
          f"düşünmə vaxtı: ~{args.think:g} s)")
    print(f"Müddət:               {elapsed:.1f} s")
    print(f"Ümumi sorğu:          {total}")
    print(f"Ötürücülük:           {total / elapsed:.1f} sorğu/s (RPS)")
    print(f"Uğurlu (2xx/3xx):     {ok}  ({ok * 100 / total if total else 0:.2f}%)")
    print(f"Limitləndi (429):     {limited}  — gözlənilən qoruma, xəta sayılmır")
    print(f"Xətalar:              {errors}  ({errors * 100 / total if total else 0:.2f}%)"
          f"   [HTTP 4xx/5xx: {http_errors}, şəbəkə/timeout: {transport}]")
    print()
    print("Status kodları üzrə:")
    for code in sorted(stats.codes):
        label = {429: "limitləndi (gözlənilən qoruma)"}.get(code, "uğurlu" if code < 400 else "XƏTA")
        print(f"  {code}: {stats.codes[code]:>8}   {label}")
    for name, n in sorted(stats.transport_errors.items()):
        print(f"  {name}: {n:>8}   XƏTA (şəbəkə)")
    print()
    print("Gecikmə (ms):")
    header = f"  {'endpoint':<24}{'say':>8}{'uğurlu':>8}{'429':>6}{'xəta':>6}{'p50':>9}{'p95':>9}{'p99':>9}{'maks':>9}"
    print(header)
    print("  " + "-" * (len(header) - 2))
    labels = {"evacuation": "GET /api/v1/evacuation", "shelters": "GET /api/v1/shelters",
              "sos": "POST /api/v1/sos", "status": "POST /api/v1/status"}
    everything: list[float] = []
    for key, _ in MIX:
        values = sorted(stats.latencies.get(key, []))
        everything += values
        c = stats.by_endpoint[key]
        print(f"  {labels[key]:<24}{len(values):>8}{c['ok']:>8}{c['limited']:>6}{c['error']:>6}"
              f"{percentile(values, 50):>9.1f}{percentile(values, 95):>9.1f}{percentile(values, 99):>9.1f}"
              f"{(values[-1] if values else 0):>9.1f}")
    everything.sort()
    print("  " + "-" * (len(header) - 2))
    print(f"  {'ÜMUMİ':<24}{len(everything):>8}{ok:>8}{limited:>6}{errors:>6}"
          f"{percentile(everything, 50):>9.1f}{percentile(everything, 95):>9.1f}{percentile(everything, 99):>9.1f}"
          f"{(everything[-1] if everything else 0):>9.1f}")
    health = stats.health_after
    if health:
        r = health.get("routing", {})
        print()
        print(f"Testdən sonra server:  status={health.get('status')}, marşrut provayderi={r.get('provider')}, "
              f"breaker={r.get('breaker')}, xarici çağırış={r.get('external_calls')}, "
              f"fallback={r.get('fallback_calls')}, keş={r.get('cache_hits')}")
    print(line)
    verdict = "UĞURLU" if total and errors * 100 / total <= args.max_error_pct else "UĞURSUZ"
    print(f"NƏTİCƏ: {verdict}  (xəta payı {errors * 100 / total if total else 100:.2f}%, hədd {args.max_error_pct:g}%)")
    print(line)
    return 0 if verdict == "UĞURLU" else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Urban Crisis & Crowd Evacuation Engine üçün asinxron yük testi")
    parser.add_argument("--url", default="http://127.0.0.1:8000", help="serverin ünvanı")
    parser.add_argument("--users", type=int, default=200, help="virtual istifadəçilərin sayı")
    parser.add_argument("--duration", type=float, default=15.0, help="testin müddəti (saniyə)")
    parser.add_argument("--concurrency", type=int, default=0,
                        help="eyni anda açıq sorğuların maksimum sayı (standart: --users)")
    parser.add_argument("--think", type=float, default=0.2,
                        help="sorğular arası orta düşünmə vaxtı, saniyə (0 = maksimum təzyiq)")
    parser.add_argument("--timeout", type=float, default=10.0, help="sorğu timeout-u (saniyə)")
    parser.add_argument("--seed", type=int, default=42, help="təsadüfilik toxumu (təkrarlana bilən yük)")
    parser.add_argument("--max-error-pct", type=float, default=1.0, help="icazə verilən maksimum xəta payı (%%)")
    args = parser.parse_args()
    if args.users < 1 or args.duration <= 0:
        parser.error("--users >= 1 və --duration > 0 olmalıdır")
    stats = asyncio.run(run(args))
    return report(stats, args)


if __name__ == "__main__":
    sys.exit(main())
