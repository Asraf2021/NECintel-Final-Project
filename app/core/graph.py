"""Yol qrafı və ən qısa yol (Dijkstra).

Bu modul həm simulyasiya olunmuş Xarici API, həm də lokal ehtiyat (fallback)
marşrutlaşdırıcı tərəfindən istifadə olunur — vahid alqoritm, iki icraçı.
"""
from __future__ import annotations

import heapq
import json
from dataclasses import dataclass, field
from typing import Iterable

from .geo import haversine_m

ROAD_FACTOR = 1.25      # düz xətt -> real yol uzunluğu əmsalı
ACCESS_FACTOR = 1.35    # başlanğıc nöqtədən qrafa qədər piyada çıxış əmsalı
SNAP_CANDIDATES = 3     # başlanğıc üçün yoxlanan ən yaxın qovşaq sayı


@dataclass
class Edge:
    id: str
    a: str
    b: str
    name: str
    length_m: float
    accessible: bool = True      # əlil arabası / tibbi nəqliyyat üçün keçilə bilən (pandus, maneəsiz)
    barrier: str | None = None   # stairs | underpass | steep | no_sidewalk


# Marşrut profilləri. Yeni profil = bir sətir (məs. "stretcher", "elderly").
PROFILES = {
    "foot": {"accessible_only": False},
    "wheelchair": {"accessible_only": True},
}


@dataclass
class RouteResult:
    found: bool
    distance_m: float = 0.0
    duration_min: float = 0.0
    points: list = field(default_factory=list)      # [[lat, lon], ...]
    edge_ids: list = field(default_factory=list)
    steps: list = field(default_factory=list)       # [{"road": ..., "distance_m": ...}]

    def to_dict(self) -> dict:
        return {
            "found": self.found,
            "distance_m": round(self.distance_m),
            "duration_min": round(self.duration_min, 1),
            "points": self.points,
            "edge_ids": self.edge_ids,
            "steps": self.steps,
        }


class RoadGraph:
    def __init__(self, nodes: list[dict], edges: list[dict]):
        self.nodes = {n["id"]: n for n in nodes}
        self.edges: dict[str, Edge] = {}
        self.adj: dict[str, list[tuple[str, str]]] = {n: [] for n in self.nodes}
        for e in edges:
            a, b = self.nodes[e["a"]], self.nodes[e["b"]]
            length = haversine_m(a["lat"], a["lon"], b["lat"], b["lon"]) * ROAD_FACTOR
            self.edges[e["id"]] = Edge(e["id"], e["a"], e["b"], e["name"], length,
                                       e.get("accessible", True), e.get("barrier"))
            self.adj[e["a"]].append((e["b"], e["id"]))
            self.adj[e["b"]].append((e["a"], e["id"]))

    @classmethod
    def from_file(cls, path: str) -> "RoadGraph":
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return cls(data["nodes"], data["edges"])

    def nearest_nodes(self, lat: float, lon: float, k: int = 1) -> list[tuple[str, float]]:
        ranked = sorted(
            ((nid, haversine_m(lat, lon, n["lat"], n["lon"])) for nid, n in self.nodes.items()),
            key=lambda x: x[1],
        )
        return ranked[:k]

    def shortest_path(
        self,
        origin: tuple[float, float],
        dest: tuple[float, float],
        closed: Iterable[str] = (),
        congestion: dict[str, float] | None = None,
        speed_kmh: float = 4.5,
        profile: str = "foot",
    ) -> RouteResult:
        """Başlanğıc koordinatdan təyinat koordinatına ən sürətli marşrut.

        closed     — bağlı yol (edge) id-ləri, marşrutdan çıxarılır.
        congestion — edge_id -> əmsal (1.0 = sərbəst, 2.0 = iki dəfə yavaş).
        """
        closed_set = set(closed)
        if PROFILES.get(profile, PROFILES["foot"])["accessible_only"]:
            # Əlçatan profil: pilləkən, yeraltı keçid, dik yoxuş və səkisiz yollar marşrutdan çıxarılır
            closed_set |= {eid for eid, e in self.edges.items() if not e.accessible}
        congestion = congestion or {}
        speed_m_min = speed_kmh * 1000 / 60

        dest_nodes = dict(self.nearest_nodes(dest[0], dest[1], 2))
        # Birbaşa piyada məsafə çox qısadırsa, qrafa ehtiyac yoxdur
        direct = haversine_m(origin[0], origin[1], dest[0], dest[1])
        if direct < 250:
            d = direct * ACCESS_FACTOR
            return RouteResult(True, d, d / speed_m_min, [list(origin), list(dest)], [], [])

        # Dijkstra: xərc = dəqiqə. Virtual başlanğıc ən yaxın K qovşağa bağlanır.
        dist: dict[str, float] = {}
        prev: dict[str, tuple[str | None, str | None]] = {}
        heap: list[tuple[float, str]] = []
        for nid, d in self.nearest_nodes(origin[0], origin[1], SNAP_CANDIDATES):
            cost = d * ACCESS_FACTOR / speed_m_min
            dist[nid] = cost
            prev[nid] = (None, None)
            heapq.heappush(heap, (cost, nid))

        best_total, best_node = float("inf"), None
        visited: set[str] = set()
        while heap:
            cost, u = heapq.heappop(heap)
            if u in visited:
                continue
            visited.add(u)
            if cost >= best_total:
                break
            if u in dest_nodes:
                total = cost + dest_nodes[u] * ACCESS_FACTOR / speed_m_min
                if total < best_total:
                    best_total, best_node = total, u
            for v, eid in self.adj[u]:
                if eid in closed_set or v in visited:
                    continue
                e = self.edges[eid]
                nc = cost + e.length_m * congestion.get(eid, 1.0) / speed_m_min
                if nc < dist.get(v, float("inf")):
                    dist[v] = nc
                    prev[v] = (u, eid)
                    heapq.heappush(heap, (nc, v))

        if best_node is None:
            return RouteResult(False)

        # Yolu bərpa et
        node_path, edge_path = [], []
        cur: str | None = best_node
        while cur is not None:
            node_path.append(cur)
            p, eid = prev[cur]
            if eid:
                edge_path.append(eid)
            cur = p
        node_path.reverse()
        edge_path.reverse()

        points = [list(origin)]
        points += [[self.nodes[n]["lat"], self.nodes[n]["lon"]] for n in node_path]
        points.append(list(dest))

        first = self.nodes[node_path[0]]
        distance = haversine_m(origin[0], origin[1], first["lat"], first["lon"]) * ACCESS_FACTOR
        steps = []
        for eid in edge_path:
            e = self.edges[eid]
            distance += e.length_m
            if steps and steps[-1]["road"] == e.name:
                steps[-1]["distance_m"] += round(e.length_m)
            else:
                steps.append({"road": e.name, "distance_m": round(e.length_m)})
        distance += dest_nodes[best_node] * ACCESS_FACTOR
        return RouteResult(True, distance, best_total, points, edge_path, steps)
