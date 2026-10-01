"""
scripts/panoramax_world_screen_{collect,analyze}.py -- the #406 candidate-city screen.

No network: the collector is exercised only through its offline tile plan, and
the analysis through synthetic hexagons. The raw outputs are gitignored, so the
last group of tests checks the COMMITTED record against itself: the metrics file
and the summary CSV must describe the same clusters, and every count the
writeup quotes has to be recomputable from them.
"""

import csv
import importlib.util
import json
import os
import sys

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOCS_DIR = os.path.join(PROJECT_ROOT, "docs", "experiments")
METRICS = os.path.join(DOCS_DIR, "panoramax-world-screen_metrics.json")
CLUSTERS = os.path.join(DOCS_DIR, "panoramax-world-screen_clusters.csv")


def _load(name):
    path = os.path.join(PROJECT_ROOT, "scripts", f"{name}.py")
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


collect = _load("panoramax_world_screen_collect")
analyze = _load("panoramax_world_screen_analyze")


# ── The sampling invariant: the tile plan the 2026-10-01 log records ─────────


def test_the_tile_plan_is_the_235_tiles_the_run_requested():
    plan = collect.plan_tiles()
    assert len(plan) == 235
    assert collect.region_counts(plan) == {
        "conus_scanada": 84,
        "europe": 64,
        "turkey": 9,
        "taiwan": 2,
        "japan": 12,
        "se_australia": 16,
        "s_america_cone": 36,
        "mexico": 12,
    }
    assert len(plan) <= collect.MAX_REQUESTS


def test_a_shared_tile_belongs_to_the_first_region_that_claims_it():
    regions = {"a": (0.0, 0.0, 10.0, 10.0), "b": (5.0, 5.0, 15.0, 15.0)}
    plan = collect.plan_tiles(regions)
    a_only = collect.plan_tiles({"a": regions["a"]})
    assert all(plan[t] == "a" for t in a_only)
    assert any(r == "b" for r in plan.values())


def test_the_committed_metrics_agree_with_the_plan():
    metrics = json.load(open(METRICS))
    assert metrics["requests"]["tiles"]["by_region"] == collect.region_counts(collect.plan_tiles())


def test_the_dry_run_sends_nothing(monkeypatch, capsys):
    def boom(*_a, **_k):
        raise AssertionError("dry run must not collect")

    monkeypatch.setattr(collect, "collect", boom)
    assert collect.main([]) == 0
    assert "plan: 235 tiles" in capsys.readouterr().out


# ── The derivation, on synthetic hexagons ─────────────────────────────────────


def _hex(lat, lon, n360, nall=None, date="2026-01-01"):
    nall = n360 if nall is None else nall
    return {
        "lat": lat,
        "lon": lon,
        "nb_360_pictures": n360,
        "nb_pictures": nall,
        "nb_flat_pictures": nall - n360,
        "date": date,
    }


def _place(name, lat, lon, pop, cc="FR"):
    return {"name": name, "lat": lat, "lon": lon, "pop": pop, "cc": cc, "admin1": "X"}


def test_a_place_sums_only_hexes_within_10_km():
    # 0.08 deg of latitude is ~8.9 km, 0.1 is ~11.1 km.
    index = analyze.HexIndex([_hex(45.08, 5.0, 3000), _hex(45.10, 5.0, 9000)])
    [row] = analyze.place_bounds([_place("P", 45.0, 5.0, 1)], index)
    assert row["ub_360"] == 3000
    assert row["n_hex"] == 1


def test_a_place_under_the_floor_is_dropped():
    index = analyze.HexIndex([_hex(45.0, 5.0, analyze.MIN_PLACE_360 - 1)])
    assert analyze.place_bounds([_place("P", 45.0, 5.0, 1)], index) == []


def test_a_cluster_reports_the_anchor_bound_under_the_most_populous_name():
    # Anchor A (bigger bound, small town) and B 5 km away (smaller bound, big city).
    index = analyze.HexIndex([_hex(45.0, 5.0, 50000), _hex(45.045, 5.0, 10000)])
    ranked = analyze.place_bounds(
        [_place("A", 45.0, 5.0, 100), _place("B", 45.045, 5.0, 900000)], index
    )
    [cluster] = analyze.cluster_places(ranked)
    row = analyze.summarize_cluster(cluster, catalog=[])
    assert row["name"] == "B"
    assert row["ub_360_10km"] == ranked[0]["ub_360"]
    assert row["n_members"] == 2
    assert "pop" not in row


def test_places_beyond_20_km_of_the_anchor_form_their_own_cluster():
    index = analyze.HexIndex([_hex(45.0, 5.0, 50000), _hex(45.3, 5.0, 10000)])
    ranked = analyze.place_bounds([_place("A", 45.0, 5.0, 1), _place("B", 45.3, 5.0, 1)], index)
    assert len(analyze.cluster_places(ranked)) == 2


def test_tracked_and_candidate_thresholds():
    catalog = [{"city_id": "c", "lat": 45.0, "lon": 5.2, "enabled": "0"}]  # ~15.7 km east
    tracked = analyze.summarize_cluster(
        {"anchor": _ranked(45.0, 5.0, 9000), "members": [_ranked(45.0, 5.0, 9000)]}, catalog
    )
    assert (tracked["in_catalog"], tracked["candidate"], tracked["catalog_enabled"]) == (
        "yes",
        "no",
        "0",
    )

    small_fr = _ranked(10.0, 10.0, 3000)
    small_us = _ranked(10.0, 10.0, 3000, cc="US")
    assert (
        analyze.summarize_cluster({"anchor": small_fr, "members": [small_fr]}, [])["candidate"]
        == "no"
    )
    assert (
        analyze.summarize_cluster({"anchor": small_us, "members": [small_us]}, [])["candidate"]
        == "yes"
    )


def _ranked(lat, lon, ub, cc="FR"):
    return {
        **_place("P", lat, lon, 1, cc),
        "ub_360": ub,
        "ub_all": ub,
        "n_hex": 1,
        "newest_hex_date": "2026",
        "max_hex_360": ub,
    }


def test_the_catalog_radius_is_25_km():
    catalog = [{"city_id": "c", "lat": 45.0, "lon": 5.0, "enabled": "1"}]
    assert analyze.nearest_catalog(45.22, 5.0, catalog) is not None  # ~24.5 km
    assert analyze.nearest_catalog(45.23, 5.0, catalog) is None  # ~25.6 km


# ── The committed record is self-consistent ───────────────────────────────────


@pytest.fixture(scope="module")
def record():
    metrics = json.load(open(METRICS))
    with open(CLUSTERS, newline="") as f:
        rows = list(csv.DictReader(f))
    return metrics, rows


def test_request_counts_add_up(record):
    metrics, _ = record
    req = metrics["requests"]
    assert req["total_requests"] == req["tiles"]["n"] + len(req["searches"]) == 239
    assert sum(req["tiles"]["status_counts"].values()) == req["tiles"]["n"]
    assert req["refusals_403_429"] == 0


def test_the_csv_regenerates_the_cluster_counts(record):
    metrics, rows = record
    c = metrics["clusters"]
    new = [r for r in rows if r["in_catalog"] == "no"]
    tracked = [r for r in rows if r["in_catalog"] == "yes"]
    candidates = [r for r in new if r["candidate"] == "yes"]
    assert (c["n"], c["n_new"], c["n_tracked"], c["n_new_candidates"]) == (
        len(rows),
        len(new),
        len(tracked),
        len(candidates),
    )
    assert c["n_new_candidates_us_ca"] == sum(r["cc"] in ("US", "CA") for r in candidates)
    assert c["n_tracked_disabled"] == sum(r["catalog_enabled"] == "0" for r in tracked)
    assert c["ub_360_new"]["n"] == len(new)
    assert c["ub_360_new"]["max"] == max(int(r["ub_360_10km"]) for r in new)


def test_the_ranked_lists_in_the_metrics_are_the_csv_rows(record):
    metrics, rows = record
    new = [r for r in rows if r["in_catalog"] == "no"]
    assert [(m["name"], m["ub_360_10km"]) for m in metrics["top_new"]] == [
        (r["name"], int(r["ub_360_10km"])) for r in new[: analyze.TOP_N]
    ]
    assert len(metrics["tracked"]) == metrics["clusters"]["n_tracked"]
    ranks = [int(r["rank"]) for r in rows]
    assert ranks == list(range(1, len(rows) + 1))
    bounds = [int(r["ub_360_10km"]) for r in rows]
    assert bounds == sorted(bounds, reverse=True)


def test_no_population_is_published(record):
    metrics, rows = record
    assert "pop" not in rows[0]
    assert all("pop" not in m for m in metrics["top_new"] + metrics["tracked"])
