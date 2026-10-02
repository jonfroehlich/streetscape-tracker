"""
scripts/panoramax_world_screen_{collect,analyze}.py -- the #406 candidate-city screen.

No network: the collector's pass runs against an injected `get` and pacer, and
the analysis against synthetic hexagons placed at LITERAL distances, so a
changed radius or threshold fails here rather than reading its own constant
back. The raw outputs are gitignored, so the last group checks the COMMITTED
record against itself and against the literal parameters the writeup quotes.
"""

import csv
import importlib.util
import io
import json
import math
import os
import sys

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOCS_DIR = os.path.join(PROJECT_ROOT, "docs", "experiments")
METRICS = os.path.join(DOCS_DIR, "panoramax-world-screen_metrics.json")
CLUSTERS = os.path.join(DOCS_DIR, "panoramax-world-screen_clusters.csv")
PLACES = os.path.join(DOCS_DIR, "panoramax-world-screen_places.csv")
EVIDENCE_DIR = os.path.join(PROJECT_ROOT, "experiments", "candidate-360-cities-2026-10-01")

# Degrees of latitude per km on the analysis's own sphere (diameter 12,742 km),
# written out here so the tests do not read the module's constant back.
DEG_PER_KM = 360 / (12742 * math.pi)


def _load(name):
    path = os.path.join(PROJECT_ROOT, "scripts", f"{name}.py")
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


collect = _load("panoramax_world_screen_collect")
analyze = _load("panoramax_world_screen_analyze")


def north(km, lat=45.0):
    return lat + km * DEG_PER_KM


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
    assert collect.MAX_REQUESTS == 250


def test_a_shared_tile_belongs_to_the_first_region_that_claims_it():
    regions = {"a": (0.0, 0.0, 10.0, 10.0), "b": (5.0, 5.0, 15.0, 15.0)}
    plan = collect.plan_tiles(regions)
    a_only = collect.plan_tiles({"a": regions["a"]})
    assert all(plan[t] == "a" for t in a_only)
    assert any(r == "b" for r in plan.values())


def test_the_committed_metrics_agree_with_the_plan():
    metrics = json.load(open(METRICS))
    assert metrics["requests"]["tiles"]["by_region"] == collect.region_counts(collect.plan_tiles())


def test_the_dry_run_sends_nothing(monkeypatch, capsys, tmp_path):
    def boom(*_a, **_k):
        raise AssertionError("dry run must not collect")

    monkeypatch.setattr(collect, "collect", boom)
    assert collect.main(["--raw-dir", str(tmp_path / "fresh")]) == 0
    assert "plan: 235 tiles" in capsys.readouterr().out


# ── The collector never touches an existing record ────────────────────────────


def test_the_default_output_is_a_fresh_dated_dir_under_the_repo():
    path = collect.default_raw_dir("2027-01-02")
    assert path == os.path.join(PROJECT_ROOT, "experiments", "panoramax-world-screen-2027-01-02")
    assert os.path.abspath(path) != os.path.abspath(EVIDENCE_DIR)


def test_a_non_empty_output_dir_is_refused_before_anything_is_sent(monkeypatch, tmp_path):
    (tmp_path / "panoramax").mkdir()
    (tmp_path / "panoramax" / "requests.log").write_text("{}\n")
    monkeypatch.setattr(collect, "refuse_on_collection_host", lambda: None)

    def boom(*_a, **_k):
        raise AssertionError("must refuse before collecting")

    monkeypatch.setattr(collect, "collect", boom)
    for argv in ([], ["--execute"]):
        with pytest.raises(SystemExit) as exc:
            collect.main(argv + ["--raw-dir", str(tmp_path)])
        assert "already holds files" in str(exc.value)
    assert (tmp_path / "panoramax" / "requests.log").read_text() == "{}\n"


def test_collect_itself_refuses_a_non_empty_dir(tmp_path):
    (tmp_path / "panoramax").mkdir()
    (tmp_path / "panoramax" / "hexes.csv").write_text("evidence")
    with pytest.raises(SystemExit):
        collect.collect(str(tmp_path), 30, 0.6)
    assert (tmp_path / "panoramax" / "hexes.csv").read_text() == "evidence"


def test_the_2026_10_01_evidence_dir_would_be_refused():
    # Read-only check: the path the old default pointed at is not writable by a pass.
    if not os.path.isdir(os.path.join(EVIDENCE_DIR, "panoramax")):
        pytest.skip("evidence dir is gitignored and absent in this checkout")
    with pytest.raises(SystemExit):
        collect.refuse_nonempty_dir(EVIDENCE_DIR)


# ── The pass: cap, stops and latency ─────────────────────────────────────────


class _Resp:
    def __init__(self, status, content=b"", headers=None):
        self.status_code = status
        self.content = content
        self.headers = headers or {}


class _Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def _plan(n):
    return {(x, 0): "r" for x in range(n)}


def _run(plan, responder, **kw):
    clock = _Clock()
    calls = []

    def acquire():
        clock.t += 10.0  # the pacer's sleep: must never reach `s`

    def get(url):
        calls.append(url)
        clock.t += 0.5
        return responder(len(calls))

    log = io.StringIO()
    merged, summary = collect.run_pass(plan, get, acquire, log, clock=clock, **kw)
    lines = [json.loads(line) for line in log.getvalue().splitlines()]
    return merged, summary, lines, calls


def test_latency_excludes_the_pacer_sleep():
    _, summary, lines, _ = _run(_plan(3), lambda n: _Resp(204))
    assert summary["complete"] and summary["requests"] == 3
    assert [r["s"] for r in lines] == [0.5, 0.5, 0.5]
    assert all(r["attempts"] == 1 for r in lines)


def test_a_refusal_stops_after_one_attempt():
    _, summary, lines, calls = _run(_plan(5), lambda n: _Resp(429 if n == 2 else 204))
    assert not summary["complete"]
    assert len(calls) == 2
    assert "stop" in lines[-1] and "429" in lines[-1]["stop"]


def test_a_tile_that_exhausts_its_retries_stops_cleanly():
    _, summary, lines, calls = _run(_plan(5), lambda n: _Resp(503), max_tries=3)
    assert len(calls) == 3
    assert lines[0]["attempts"] == 3 and lines[0]["status"] == 503
    assert "stop" in lines[-1] and not summary["complete"]


def test_a_transport_error_is_retried_then_stops_without_raising():
    def responder(n):
        raise ConnectionError("reset")

    _, summary, lines, calls = _run(_plan(2), responder, max_tries=2)
    assert len(calls) == 2
    assert lines[0]["status"] == "EXC" and "reset" in lines[0]["error"]
    assert "stop" in lines[-1]


def test_the_request_cap_counts_retries():
    # Tile 0 succeeds on its third try; the cap of 4 leaves one attempt for tile 1.
    script = {1: 503, 2: 503, 3: 204, 4: 503}
    _, summary, lines, calls = _run(_plan(3), lambda n: _Resp(script.get(n, 204)), max_requests=4)
    assert len(calls) == 4
    assert summary["requests"] == 4 and not summary["complete"]
    assert "cap 4" in lines[-1]["stop"]


def test_a_plan_over_the_cap_is_refused_unsent():
    _, summary, lines, calls = _run(_plan(5), lambda n: _Resp(204), max_requests=4)
    assert calls == [] and not summary["complete"]
    assert "over the 4-request cap" in lines[-1]["stop"]


# ── The analyzer refuses a record that is not one complete pass ──────────────


def _log_for(plan, status=204):
    return [
        {
            "i": i,
            "x": x,
            "y": y,
            "status": status,
            "bytes": 0,
            "s": 0.1,
            "ts": "2026-10-01T00:00:00Z",
        }
        for i, (x, y) in enumerate(sorted(plan))
    ]


def test_a_complete_pass_validates():
    plan = _plan(4)
    analyze.validate_tile_log(_log_for(plan), plan)
    analyze.validate_tile_log(_log_for(plan, 404), plan)


@pytest.mark.parametrize(
    "mutate, why",
    [
        (lambda log: log[:-1], "never logged"),
        (lambda log: log + log, "more than once"),
        (lambda log: log + [{"stop": "cap", "requests": 4}], "stop record"),
        (lambda log: [dict(log[0], status=500)] + log[1:], "neither 200 nor empty"),
        (lambda log: log + [dict(log[0], i=4, x=99)], "outside the plan"),
    ],
)
def test_an_incomplete_or_doubled_pass_is_refused(mutate, why):
    plan = _plan(4)
    with pytest.raises(analyze.IncompleteRecord, match=why):
        analyze.validate_tile_log(mutate(_log_for(plan)), plan)


# ── The derivation, at literal distances and thresholds ───────────────────────


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


def test_the_place_radius_is_10_km_from_both_sides():
    index = analyze.HexIndex([_hex(north(9.5), 5.0, 3000), _hex(north(10.5), 5.0, 9000)])
    [row] = analyze.place_bounds([_place("P", 45.0, 5.0, 1)], index)
    assert row["ub_360"] == 3000
    assert row["n_hex"] == 1


def test_the_place_floor_is_2000():
    index = analyze.HexIndex([_hex(45.0, 5.0, 2000), _hex(46.0, 5.0, 1999)])
    rows = analyze.place_bounds([_place("A", 45.0, 5.0, 1), _place("B", 46.0, 5.0, 1)], index)
    assert [r["name"] for r in rows] == ["A"]


def test_places_are_ranked_by_descending_bound():
    index = analyze.HexIndex([_hex(45.0, 5.0, 3000), _hex(46.0, 5.0, 9000), _hex(47.0, 5.0, 5000)])
    places = [_place(n, lat, 5.0, 1) for n, lat in (("a", 45.0), ("b", 46.0), ("c", 47.0))]
    assert [r["name"] for r in analyze.place_bounds(places, index)] == ["b", "c", "a"]


def _ranked(name, lat, lon, ub, *, pop=1, cc="FR", date="2026-01-01", max_hex=None):
    return {
        **_place(name, lat, lon, pop, cc),
        "ub_360": ub,
        "ub_all": ub,
        "n_hex": 1,
        "newest_hex_date": date,
        "max_hex_360": ub if max_hex is None else max_hex,
    }


def test_the_cluster_radius_is_20_km_from_both_sides():
    joins = [_ranked("A", 45.0, 5.0, 9000), _ranked("B", north(19.5), 5.0, 5000)]
    splits = [_ranked("A", 45.0, 5.0, 9000), _ranked("B", north(20.5), 5.0, 5000)]
    assert len(analyze.cluster_places(joins)) == 1
    assert len(analyze.cluster_places(splits)) == 2


def test_the_cluster_radius_is_measured_from_the_anchor_not_a_member():
    # B is 15 km from anchor A; C is 15 km beyond B, 30 km from A.
    chain = [
        _ranked("A", 45.0, 5.0, 9000),
        _ranked("B", north(15), 5.0, 6000),
        _ranked("C", north(30), 5.0, 5000),
    ]
    clusters = analyze.cluster_places(chain)
    assert [[m["name"] for m in c["members"]] for c in clusters] == [["A", "B"], ["C"]]


def test_a_cluster_reports_the_anchor_bound_under_the_most_populous_name():
    anchor = _ranked("A", 45.0, 5.0, 50000, pop=100, date="2025-01-01", max_hex=40000)
    big = _ranked("B", north(12), 5.0, 10000, pop=900000, date="2026-06-01", max_hex=7000)
    row = analyze.summarize_cluster({"anchor": anchor, "members": [anchor, big]}, catalog=[])
    assert (row["name"], row["lat"]) == ("B", big["lat"])
    assert row["ub_360_10km"] == 50000
    assert row["max_hex_360"] == 40000
    assert row["newest_hex_date"] == "2026-06-01"  # from the member, not the anchor
    assert row["anchor_name"] == "A"
    assert row["n_members"] == 2
    assert "pop" not in row


def _one(r, catalog=()):
    return analyze.summarize_cluster({"anchor": r, "members": [r]}, list(catalog))


@pytest.mark.parametrize(
    "cc, ub, expected",
    [
        ("FR", 5000, "yes"),
        ("FR", 4999, "no"),
        ("US", 2000, "yes"),
        ("US", 1999, "no"),
        ("CA", 2000, "yes"),
        ("CA", 1999, "no"),
        ("MX", 2000, "no"),
    ],
)
def test_candidate_thresholds_are_5000_and_2000_in_the_us_and_canada(cc, ub, expected):
    assert _one(_ranked("P", 10.0, 10.0, ub, cc=cc))["candidate"] == expected


def test_the_catalog_radius_is_25_km_from_both_sides():
    catalog = [{"city_id": "c", "lat": 45.0, "lon": 5.0, "enabled": "1"}]
    assert analyze.nearest_catalog(north(24.9), 5.0, catalog) is not None
    assert analyze.nearest_catalog(north(25.1), 5.0, catalog) is None


def test_a_catalog_match_is_found_far_north_and_across_the_antimeridian():
    far_north = [{"city_id": "n", "lat": 70.0, "lon": 20.0, "enabled": "1"}]
    # 0.65 degrees of longitude at 70N is ~24.7 km: a match the old 0.6-degree cut missed.
    d, _ = analyze.nearest_catalog(70.0, 20.65, far_north)
    assert 24 < d < 25
    dateline = [{"city_id": "f", "lat": -17.0, "lon": 179.95, "enabled": "1"}]
    assert analyze.nearest_catalog(-17.0, -179.95, dateline) is not None


def test_tracked_is_keyed_on_the_name_point_and_the_anchor_is_reported_too():
    catalog = [{"city_id": "c", "lat": 45.0, "lon": 5.0, "enabled": "0"}]
    anchor = _ranked("A", north(18), 5.0, 9000, pop=1)  # 18 km from the catalog city
    named = _ranked("B", north(34), 5.0, 6000, pop=9)  # 34 km from it, 16 km from A
    row = analyze.summarize_cluster({"anchor": anchor, "members": [anchor, named]}, catalog)
    assert (row["in_catalog"], row["anchor_in_catalog"]) == ("no", "yes")
    assert row["anchor_catalog_city_id"] == "c"
    assert row["candidate"] == "yes"


def test_h3_resolution_is_read_from_the_index():
    assert analyze.h3_resolution("8728dc65dffffff") == 7
    assert analyze.h3_resolution("861f9b37fffffff") == 6


# ── The committed record is self-consistent ───────────────────────────────────


@pytest.fixture(scope="module")
def record():
    metrics = json.load(open(METRICS))
    with open(CLUSTERS, newline="") as f:
        rows = list(csv.DictReader(f))
    with open(PLACES, newline="") as f:
        places = list(csv.DictReader(f))
    return metrics, rows, places


def test_the_record_states_the_parameters_the_writeup_quotes(record):
    metrics, _, _ = record
    assert metrics["parameters"] == {
        "zoom": 6,
        "place_radius_km": 10.0,
        "min_place_360": 2000,
        "cluster_radius_km": 20.0,
        "catalog_radius_km": 25.0,
        "candidate_min_360": 5000,
        "candidate_min_360_us_ca": 2000,
    }
    assert metrics["parameters"] == {
        "zoom": 6,
        "place_radius_km": analyze.PLACE_RADIUS_KM,
        "min_place_360": analyze.MIN_PLACE_360,
        "cluster_radius_km": analyze.CLUSTER_RADIUS_KM,
        "catalog_radius_km": analyze.CATALOG_RADIUS_KM,
        "candidate_min_360": analyze.CANDIDATE_MIN_360,
        "candidate_min_360_us_ca": analyze.CANDIDATE_MIN_360_NA,
    }
    assert analyze.NORTH_AMERICA == ("US", "CA")


def test_the_hexagons_are_res_7(record):
    metrics, _, _ = record
    assert metrics["hexes"]["h3_resolution_counts"] == {"7": metrics["hexes"]["n_hexes"]}


def test_request_counts_add_up(record):
    metrics, _, _ = record
    req = metrics["requests"]
    assert req["total_requests"] == req["tiles"]["n"] + len(req["searches"]) == 239
    assert sum(req["tiles"]["status_counts"].values()) == req["tiles"]["n"] == 235
    assert req["refusals_403_429"] == 0


def test_the_csv_regenerates_the_cluster_counts(record):
    metrics, rows, _ = record
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
    split = [r for r in rows if r["in_catalog"] != r["anchor_in_catalog"]]
    assert [s["name"] for s in metrics["tracked_split"]] == [r["name"] for r in split]


def test_the_ranked_lists_in_the_metrics_are_the_csv_rows(record):
    metrics, rows, _ = record
    new = [r for r in rows if r["in_catalog"] == "no"]
    assert [(m["name"], m["ub_360_10km"]) for m in metrics["top_new"]] == [
        (r["name"], int(r["ub_360_10km"])) for r in new[: analyze.TOP_N]
    ]
    assert len(metrics["tracked"]) == metrics["clusters"]["n_tracked"]
    ranks = [int(r["rank"]) for r in rows]
    assert ranks == list(range(1, len(rows) + 1))
    bounds = [int(r["ub_360_10km"]) for r in rows]
    assert bounds == sorted(bounds, reverse=True)


def test_each_cluster_bound_is_its_anchor_place_bound(record):
    metrics, rows, places = record
    assert len(places) == metrics["places"]["n_with_bound_ge_min"]
    anchors = {int(p["cluster_rank"]): p for p in places if p["is_anchor"] == "yes"}
    assert sorted(anchors) == [int(r["rank"]) for r in rows]
    for r in rows:
        a = anchors[int(r["rank"])]
        assert (a["name"], a["ub_360_10km"]) == (r["anchor_name"], r["ub_360_10km"])


def test_no_population_is_published(record):
    metrics, rows, places = record
    assert "pop" not in rows[0] and "pop" not in places[0]
    assert all("pop" not in m for m in metrics["top_new"] + metrics["tracked"])
