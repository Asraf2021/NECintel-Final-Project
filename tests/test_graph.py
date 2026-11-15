"""Yol qrafı və Dijkstra — vahid (unit) testlər."""
from __future__ import annotations

import pytest

from app.config import settings
from app.core.geo import haversine_m
from app.core.graph import RoadGraph

# Sintetik qraf: A -> D iki yolla (qısa: ab+bd, uzun: ac+cd). A1/A2/D1 — "snap" üçün köməkçi qovşaqlar.
NODES = [
    {"id": "A", "name": "A", "lat": 40.4000, "lon": 49.800},
    {"id": "A1", "name": "A1", "lat": 40.4005, "lon": 49.800},
    {"id": "A2", "name": "A2", "lat": 40.3995, "lon": 49.800},
    {"id": "B", "name": "B", "lat": 40.4000, "lon": 49.812},
    {"id": "C", "name": "C", "lat": 40.3920, "lon": 49.806},
    {"id": "D", "name": "D", "lat": 40.4000, "lon": 49.824},
    {"id": "D1", "name": "D1", "lat": 40.4005, "lon": 49.824},
    # Ada: qrafın qalan hissəsi ilə əlaqəsi yoxdur
    {"id": "X", "name": "X", "lat": 40.5000, "lon": 50.100},
    {"id": "Y", "name": "Y", "lat": 40.5005, "lon": 50.100},
]
EDGES = [
    {"id": "a1", "a": "A", "b": "A1", "name": "Stub"},
    {"id": "a2", "a": "A", "b": "A2", "name": "Stub"},
    {"id": "d1", "a": "D", "b": "D1", "name": "Stub"},
    {"id": "ab", "a": "A", "b": "B", "name": "Şimal yolu"},
    {"id": "bd", "a": "B", "b": "D", "name": "Şimal yolu"},
    {"id": "ac", "a": "A", "b": "C", "name": "Cənub yolu"},
    {"id": "cd", "a": "C", "b": "D", "name": "Cənub dairəvi"},
    {"id": "xy", "a": "X", "b": "Y", "name": "Ada yolu"},
]
ORIGIN = (40.4000, 49.800)
DEST = (40.4000, 49.824)
ISLAND = (40.5000, 50.100)


@pytest.fixture
def graph():
    return RoadGraph(NODES, EDGES)


@pytest.fixture(scope="module")
def city_graph():
    return RoadGraph.from_file(settings.city_file)


def test_haversine_sanity():
    # 1 dərəcə enlik ~ 111.2 km
    assert haversine_m(40.0, 49.0, 41.0, 49.0) == pytest.approx(111_195, rel=0.001)
    assert haversine_m(40.4, 49.8, 40.4, 49.8) == 0.0
    # simmetriya
    assert haversine_m(40.36, 49.83, 40.43, 49.92) == pytest.approx(haversine_m(40.43, 49.92, 40.36, 49.83))
    # Bakıda 0.01 dərəcə uzunluq ~ 850 m
    assert 800 < haversine_m(40.4, 49.80, 40.4, 49.81) < 900


def test_route_found_takes_shortest_path(graph):
    r = graph.shortest_path(ORIGIN, DEST)
    assert r.found is True
    assert r.edge_ids == ["ab", "bd"]
    assert r.points[0] == list(ORIGIN) and r.points[-1] == list(DEST)
    # müddət = məsafə / sürət (4.5 km/s = 75 m/dəq)
    assert r.duration_min == pytest.approx(r.distance_m / 75, rel=1e-6)
    expected = graph.edges["ab"].length_m + graph.edges["bd"].length_m
    assert r.distance_m == pytest.approx(expected, rel=1e-6)


def test_steps_merge_same_road_name(graph):
    r = graph.shortest_path(ORIGIN, DEST)
    # ab və bd eyni küçədir -> bir addım
    assert [s["road"] for s in r.steps] == ["Şimal yolu"]
    assert r.steps[0]["distance_m"] == pytest.approx(r.distance_m, abs=2)


def test_closed_edge_is_avoided(graph):
    base = graph.shortest_path(ORIGIN, DEST)
    r = graph.shortest_path(ORIGIN, DEST, closed={"ab"})
    assert r.found is True
    assert "ab" not in r.edge_ids
    assert r.edge_ids == ["ac", "cd"]
    assert r.edge_ids != base.edge_ids
    assert r.duration_min > base.duration_min


def test_closed_edge_accepts_any_iterable(graph):
    assert "bd" not in graph.shortest_path(ORIGIN, DEST, closed=["bd"]).edge_ids
    assert "bd" not in graph.shortest_path(ORIGIN, DEST, closed=("bd",)).edge_ids


def test_congestion_changes_duration(graph):
    base = graph.shortest_path(ORIGIN, DEST)
    slow = graph.shortest_path(ORIGIN, DEST, congestion={"ab": 1.2})
    assert slow.edge_ids == base.edge_ids          # marşrut eyni qalır
    assert slow.duration_min > base.duration_min   # amma müddət artır
    assert slow.distance_m == pytest.approx(base.distance_m)
    extra = graph.edges["ab"].length_m * 0.2 / 75
    assert slow.duration_min == pytest.approx(base.duration_min + extra, rel=1e-6)


def test_heavy_congestion_switches_route(graph):
    base = graph.shortest_path(ORIGIN, DEST)
    r = graph.shortest_path(ORIGIN, DEST, congestion={"ab": 3.0})
    assert r.edge_ids == ["ac", "cd"]
    assert r.duration_min > base.duration_min


def test_speed_scales_duration(graph):
    walk = graph.shortest_path(ORIGIN, DEST, speed_kmh=4.5)
    fast = graph.shortest_path(ORIGIN, DEST, speed_kmh=9.0)
    assert fast.duration_min == pytest.approx(walk.duration_min / 2, rel=1e-6)


def test_disconnected_destination_not_found(graph):
    r = graph.shortest_path(ORIGIN, ISLAND)
    assert r.found is False
    assert r.to_dict()["edge_ids"] == [] and r.to_dict()["points"] == []


def test_all_paths_closed_not_found(graph):
    assert graph.shortest_path(ORIGIN, DEST, closed={"ab", "ac"}).found is False
    assert graph.shortest_path(ORIGIN, DEST, closed={"bd", "cd"}).found is False


def test_very_short_trip_is_direct_walk(graph):
    near = (ORIGIN[0] + 0.001, ORIGIN[1])  # ~111 m
    r = graph.shortest_path(ORIGIN, near)
    assert r.found is True and r.edge_ids == []
    assert len(r.points) == 2
    assert 100 < r.distance_m < 250


def test_nearest_nodes_sorted(graph):
    ranked = graph.nearest_nodes(*ORIGIN, k=3)
    assert ranked[0] == ("A", 0.0)
    assert [d for _, d in ranked] == sorted(d for _, d in ranked)
    assert {n for n, _ in ranked} == {"A", "A1", "A2"}


def test_to_dict_rounds_values(graph):
    d = graph.shortest_path(ORIGIN, DEST).to_dict()
    assert set(d) == {"found", "distance_m", "duration_min", "points", "edge_ids", "steps"}
    assert isinstance(d["distance_m"], int)
    assert d["duration_min"] == round(d["duration_min"], 1)


# ----- Real şəhər qrafı (city.json) -----
def test_city_graph_is_consistent(city_graph):
    assert len(city_graph.nodes) >= 30 and len(city_graph.edges) >= 50
    for e in city_graph.edges.values():
        assert e.a in city_graph.nodes and e.b in city_graph.nodes
        assert e.length_m > 0


def test_city_graph_all_districts_mutually_reachable(city_graph):
    import json
    with open(settings.city_file, encoding="utf-8") as f:
        districts = json.load(f)["districts"]
    for a in districts:
        for b in districts:
            if a["id"] == b["id"]:
                continue
            r = city_graph.shortest_path((a["lat"], a["lon"]), (b["lat"], b["lon"]))
            assert r.found, f"{a['id']} -> {b['id']}"
            assert r.duration_min > 0


def test_city_graph_closing_each_route_edge_reroutes(city_graph):
    origin, dest = (40.364, 49.835), (40.425, 49.930)  # Səbail -> Sabunçu
    base = city_graph.shortest_path(origin, dest)
    assert base.found and len(base.edge_ids) >= 3
    for eid in base.edge_ids:
        r = city_graph.shortest_path(origin, dest, closed={eid})
        assert eid not in r.edge_ids
        if r.found:
            assert r.duration_min >= base.duration_min - 1e-9
