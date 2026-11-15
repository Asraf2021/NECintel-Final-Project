"""Challenge Drop 3 — SOS daşqını (flood / bot hücumu) yük testi.

Ssenari: eyni anda N (susmaya görə 600) saxta SOS — hər biri fərqli "vətəndaş ID"-si ilə, üçdə biri eyni
nöqtələrdən; paralel olaraq real istifadəçi trafiki (təxliyə planı) və dispetçer paneli (icmal) sorğuları.

Ölçülür: bütün sorğuların orta / p95 gecikməsi (hədəf: orta < 300 ms), status kodları (201 / 429),
daşqından sonra dispetçer panelində aktiv SOS sayı (karantin sayəsində məhdud qalmalıdır).

İşə salma:
  # 1) Tək IP-dən hücum (IP limiti tutur):
  python scripts/sos_flood_test.py --url http://127.0.0.1:8000
  # 2) Botnet (çox IP) simulyasiyası — IP limitini server tərəfdə qaldırın ki, daşqın detektoru yoxlansın:
  #    UCE_RATE_SOS_PER_MIN=100000 UCE_RATE_GENERAL_PER_MIN=10000000 python -m uvicorn app.main:app --port 8000
  python scripts/sos_flood_test.py --url http://127.0.0.1:8000 --sos 600
"""
from __future__ import annotations

import argparse
import asyncio
import random
import statistics
import time
import uuid

import httpx


def pct(values, p):
    if not values:
        return 0.0
    values = sorted(values)
    return values[min(len(values) - 1, int(len(values) * p / 100))]


async def main() -> int:
    ap = argparse.ArgumentParser(description="SOS daşqını yük testi (Drop 3)")
    ap.add_argument("--url", default="http://127.0.0.1:8000")
    ap.add_argument("--sos", type=int, default=600, help="saxta SOS sayı (eyni anda)")
    ap.add_argument("--users", type=int, default=60, help="paralel real istifadəçi sorğusu")
    ap.add_argument("--concurrency", type=int, default=200)
    ap.add_argument("--target-ms", type=float, default=300.0)
    args = ap.parse_args()

    lat_by: dict[str, list[float]] = {"sos": [], "evacuation": [], "overview": []}
    codes: dict[str, dict[int, int]] = {k: {} for k in lat_by}
    sem = asyncio.Semaphore(args.concurrency)
    limits = httpx.Limits(max_connections=args.concurrency, max_keepalive_connections=args.concurrency)

    pools = [httpx.AsyncClient(base_url=args.url, timeout=30, limits=httpx.Limits(max_connections=20, max_keepalive_connections=20))
             for _ in range(max(1, args.concurrency // 20))]
    async with httpx.AsyncClient(base_url=args.url, timeout=30, limits=limits) as c:
        tok = (await c.post("/api/v1/auth/login", json={"username": "dispatcher", "password": "dispatcher123"})).json()["token"]
        H = {"Authorization": f"Bearer {tok}"}
        before = (await c.get("/api/v1/dispatch/overview", headers=H)).json()["kpi"]
        hotspots = [(random.uniform(40.37, 40.44), random.uniform(49.82, 49.95)) for _ in range(3)]

        async def timed(kind, coro):
            async with sem:
                t = time.perf_counter()
                try:
                    r = await coro
                    code = r.status_code
                except Exception:
                    code = 0
                lat_by[kind].append((time.perf_counter() - t) * 1000)
                codes[kind][code] = codes[kind].get(code, 0) + 1

        def fake_sos(i):
            lat, lon = random.choice(hotspots) if i % 3 == 0 else (random.uniform(40.36, 40.45), random.uniform(49.80, 49.96))
            return random.choice(pools).post("/api/v1/sos", json={"citizen_id": f"bot-{uuid.uuid4().hex[:16]}",
                                               "type": random.choice(["fire", "collapse", "flood", "medical"]),
                                               "lat": lat, "lon": lon, "note": ""})

        tasks = [timed("sos", fake_sos(i)) for i in range(args.sos)]
        tasks += [timed("evacuation", random.choice(pools).get("/api/v1/evacuation", params={
            "lat": random.uniform(40.37, 40.44), "lon": random.uniform(49.82, 49.95)})) for _ in range(args.users)]
        tasks += [timed("overview", random.choice(pools).get("/api/v1/dispatch/overview", headers=H)) for _ in range(20)]
        random.shuffle(tasks)
        started = time.perf_counter()
        await asyncio.gather(*tasks)
        elapsed = time.perf_counter() - started
        await asyncio.sleep(1.2)  # icmalın 1 s-lik mikro-keşi yenilənsin
        after = (await c.get("/api/v1/dispatch/overview", headers=H)).json()
        health = (await c.get("/health")).json()
    for p_ in pools:
        await p_.aclose()

    allv = [v for vs in lat_by.values() for v in vs]
    avg = statistics.mean(allv)
    print("=" * 78)
    print("SOS DAŞQINI TESTİ (Drop 3) —", args.url)
    print("=" * 78)
    print(f"Saxta SOS: {args.sos} eyni anda · real istifadəçi: {args.users} · dispetçer icmalı: 20 · müddət {elapsed:.2f} s")
    print(f"{'sorğu':<12}{'say':>6}{'orta ms':>10}{'p50':>9}{'p95':>9}{'maks':>9}   status kodları")
    for k, vs in lat_by.items():
        print(f"{k:<12}{len(vs):>6}{statistics.mean(vs):>10.1f}{pct(vs, 50):>9.1f}{pct(vs, 95):>9.1f}{max(vs):>9.1f}   {codes[k]}")
    print(f"{'ÜMUMİ':<12}{len(allv):>6}{avg:>10.1f}{pct(allv, 50):>9.1f}{pct(allv, 95):>9.1f}{max(allv):>9.1f}")
    k = after["kpi"]
    print("-" * 78)
    print(f"Dispetçer paneli: aktiv SOS {before['sos_active']} -> {k['sos_active']} · karantin {k.get('sos_quarantine', 0)}"
          f" · birləşdirilmiş dublikat {k.get('sos_merged', 0)}")
    f = health.get("flood") or {}
    print(f"Daşqın detektoru: aktiv={f.get('active')} · pik {f.get('peak_rate')} SOS/{f.get('window_s')} s"
          f" · karantin cəmi {f.get('quarantined_total')} · birləşdirilmiş {f.get('merged_total')}")
    rejected = codes["sos"].get(429, 0)
    print(f"Rate limit (429): {rejected} · 5xx: {sum(v for kk, vv in codes.items() for c2, v in vv.items() if c2 >= 500 or c2 == 0)}")
    ok = avg < args.target_ms
    print("=" * 78)
    print(f"NƏTİCƏ: {'UĞURLU' if ok else 'UĞURSUZ'} — orta gecikmə {avg:.1f} ms (hədəf < {args.target_ms:.0f} ms)")
    print("=" * 78)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
