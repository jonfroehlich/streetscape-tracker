"""
KartaView sweep cost (issue #225): scripts/kartaview_sweep_cost.py.

Network-free. The script's job is to answer the one question that gates a
production KartaView channel -- how many requests does a census of a frozen grid
bbox cost -- and every way it can be wrong is a way of *under*-counting, which is
the direction that gets a per-IP-metered provider angry at us.

Four properties carry it:

(1) THE LATTICE COVERS THE BBOX. Cells are squares covered by their circumscribed
    circle. If the lattice under-covers, the sweep silently misses imagery and
    the cost looks better than it is.
(2) THE COST MODEL COUNTS PAGE 1. An empty circle still costs one request, and an
    unknown total is priced as one rather than zero.
(3) A REFUSAL IS RETRIED BEFORE IT IS BELIEVED. apiCode 690 was measured flaky;
    subdividing on the first refusal turns one cell into four that did not need
    it and inflates every number the script exists to produce.
(4) TRUNCATION IS VISIBLE AND UNBIASED. When --max-requests-per-city bites, the
    record says so and the roots that were probed are a seeded shuffle, not the
    northern strip of the map.
"""

import argparse
import importlib.util
import math
import os
import sys

import pytest
import requests

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(PROJECT_ROOT, "scripts")


def _load(name):
    path = os.path.join(SCRIPTS_DIR, f"{name}.py")
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


ks = _load("kartaview_sweep_cost")
kp = sys.modules["kartaview_probe"]

SEATTLE = {
    "city_id": "seattle--washington--united-states",
    "center_lat": 47.60757625,
    "center_lon": -122.34206449999999,
    "grid_width_m": 17689,
    "grid_height_m": 28145,
    "step_m": 20,
}


# ── (1) the lattice ────────────────────────────────────────────────────────


def test_a_cells_circle_circumscribes_it_so_the_lattice_leaves_no_gap():
    cell = ks.Cell(lat=47.6, lon=-122.3, size_m=1414.2)
    # r = s*sqrt(2)/2 is exactly the circumradius: the corner is on the circle.
    assert cell.radius_m == pytest.approx(1000, abs=1)
    assert cell.radius_m >= cell.size_m / 2  # never inscribed, which WOULD gap


def test_cells_cover_the_whole_bbox_including_its_far_corner():
    bbox = (-122.5, 47.4, -122.2, 47.8)
    cells = ks.cells_for_bbox(*bbox, 1414.2)
    min_lon, min_lat, max_lon, max_lat = bbox
    # Every cell centre is inside the bbox's own half-cell margin, and the
    # lattice extends past the far edge rather than stopping short of it.
    lats = [c.lat for c in cells]
    lons = [c.lon for c in cells]
    deg_lat = 1414.2 / ks.METERS_PER_DEG_LAT
    deg_lon = deg_lat / math.cos(math.radians((min_lat + max_lat) / 2))
    assert min(lats) < min_lat + deg_lat
    assert max(lats) + deg_lat / 2 >= max_lat
    assert min(lons) < min_lon + deg_lon
    assert max(lons) + deg_lon / 2 >= max_lon


def test_cell_count_tracks_area_not_shape():
    """A 4x-larger bbox is ~4x the cells, whatever its aspect ratio."""
    small = ks.cells_for_bbox(-122.5, 47.4, -122.4, 47.5, 1414.2)
    wide = ks.cells_for_bbox(-122.5, 47.4, -122.1, 47.5, 1414.2)
    tall = ks.cells_for_bbox(-122.5, 47.4, -122.4, 47.8, 1414.2)
    assert len(wide) == pytest.approx(4 * len(small), rel=0.35)
    assert len(tall) == pytest.approx(4 * len(small), rel=0.35)


def test_subdivide_makes_four_half_size_cells_that_cover_the_parent():
    parent = ks.Cell(lat=47.6, lon=-122.3, size_m=1414.2)
    kids = ks.subdivide(parent)
    assert len(kids) == 4
    assert all(k.size_m == parent.size_m / 2 for k in kids)
    assert all(k.depth == parent.depth + 1 for k in kids)
    # Total child area equals the parent's: no gap, no double-tiling.
    assert sum(k.size_m**2 for k in kids) == pytest.approx(parent.size_m**2)
    # Offsets are +/- a quarter-side, i.e. the four quadrant centres.
    d_lat = (parent.size_m / 4) / ks.METERS_PER_DEG_LAT
    assert sorted(round(k.lat - parent.lat, 9) for k in kids) == sorted(
        [round(-d_lat, 9)] * 2 + [round(d_lat, 9)] * 2
    )


def test_a_zero_or_negative_cell_size_is_refused_not_silently_looped():
    for bad in (0, -1.0):
        with pytest.raises(ValueError, match="must be positive"):
            ks.cells_for_bbox(-122.5, 47.4, -122.4, 47.5, bad)


def test_the_redundancy_factor_is_the_circle_over_the_square():
    """
    A photo is seen ~pi/2 times, which is why the collector's cross-cell dedup
    is load-bearing rather than defensive -- and why photos_seen_sum_over_cells
    is NOT a city's photo count.
    """
    assert ks.redundancy_factor() == pytest.approx(math.pi / 2)
    assert ks.redundancy_factor() > 1.0


# ── (2) the cost model ─────────────────────────────────────────────────────


def test_an_empty_circle_still_costs_one_request():
    assert ks.pages_for_total(0, 2000) == 1


def test_an_unknown_total_is_priced_as_one_page_not_zero():
    """Pricing an unknown at zero under-budgets exactly the cities that broke."""
    assert ks.pages_for_total(None, 2000) == 1


def test_pages_match_the_measured_seattle_circle():
    """
    Measured 2026-08-19: Seattle r=400 ipp=200 held 1004 photos over exactly 6
    pages (200x5 + 4), page 7 empty. The model must say 6, not 5 and not 7.
    """
    assert ks.pages_for_total(1004, 200) == 6
    assert ks.pages_for_total(1000, 200) == 5
    assert ks.pages_for_total(1001, 200) == 6


def test_sweep_requests_adds_only_pages_two_and_up():
    # 10 cells visited, three of them leaves needing 1, 2 and 6 pages.
    assert ks.sweep_requests(10, [0, 2500, 1004], 2000) == 10 + 0 + 1 + 0
    assert ks.sweep_requests(10, [0, 2500, 1004], 200) == 10 + 0 + 12 + 5


def test_sweep_requests_never_undercounts_the_cells_already_spent():
    """Internal nodes paid a page-1 before their refusal was known."""
    assert ks.sweep_requests(7, [], 2000) == 7


# ── (3) the walk: retries, subdivision, floors ─────────────────────────────


class _FakeSession:
    """Serves scripted answers to `_post_nearby` without touching the network."""

    def __init__(self, answer):
        self.answer = answer
        self.calls = []

    def post(self, url, data=None, params=None, timeout=None):
        self.calls.append(dict(data))
        answer = self.answer(data)
        if isinstance(answer, BaseException):
            raise answer
        return _FakeResponse(answer)


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload
        self.status_code = 200
        self.headers = {"Content-Type": "application/json"}

    def json(self):
        return self._payload


def _page(total, n=None):
    n = total if n is None else n
    return {
        "status": {"apiCode": 600},
        "currentPageItems": [{"id": str(i)} for i in range(min(n, 2000))],
        "totalFilteredItems": str(total),
    }


_REFUSAL = {"status": {"apiCode": 690, "apiMessage": "server error"}}


class _NoSleep:
    def acquire(self):
        pass


def _seattle_bbox():
    return ks.grid_bbox(
        SEATTLE["center_lat"],
        SEATTLE["center_lon"],
        SEATTLE["grid_width_m"],
        SEATTLE["grid_height_m"],
        SEATTLE["step_m"],
    )


def _plan(answer, **kw):
    session = _FakeSession(answer)
    opts = dict(
        ipp=2000,
        start_radius_m=1000,
        max_requests=10_000,
        access_token=None,
        seed=225,
        probes_per_rung=2,
    )
    opts.update(kw)
    return ks.plan_city(session, _NoSleep(), SEATTLE, **opts), session


def test_a_refusal_that_clears_on_retry_does_not_subdivide():
    """
    apiCode 690 is flaky (measured: Singapore r=1000 refused at ipp=10 and
    ipp=100, answered at ipp=2000). Believing the first refusal turns one cell
    into four and inflates the whole estimate.
    """
    state = {"refused": set()}

    def answer(data):
        key = (data["lat"], data["lng"], data["radius"])
        if key not in state["refused"]:
            state["refused"].add(key)
            return _REFUSAL
        return _page(10)

    out, _ = _plan(answer)
    assert out["retries_attempted"] == out["cells_visited"]
    assert out["retries_cleared"] == out["cells_visited"]
    assert out["subdivisions"] == 0
    assert out["refusals"] == 0
    assert out["cells_visited"] == out["root_cells"]


# ── calibration: the working radius is a property of the LOCATION ──────────


def test_calibration_picks_the_largest_radius_the_server_answers():
    """
    Measured 2026-08-19: Horace ND -- which holds NO imagery -- refused r=1000
    on 10 of 10 attempts across two page sizes and answered r=250 on 4 of 4.
    So the walk must tile at a radius the server will actually serve, and
    discovering that per CELL costs a cascade per cell.
    """
    out, _ = _plan(lambda data: _REFUSAL if data["radius"] > 600 else _page(5))
    assert out["calibrated_radius_m"] == 500
    # Tiling happened at 500, not at the 1000 we started the ladder from.
    assert out["root_cells"] == len(ks.cells_for_bbox(*_seattle_bbox(), 500 * math.sqrt(2)))
    # And having calibrated, no cell had to be subdivided at all.
    assert out["subdivisions"] == 0
    assert out["refusals"] == 0


def test_calibration_accepts_a_rung_only_if_every_probe_answers():
    """One lucky point would set a radius the rest of the city re-discovers."""
    centre_lat = ks.calibration_points(_seattle_bbox(), 2)[0][0]

    def answer(data):
        # r=1000 answers at the centre but ALWAYS refuses at the inset corner,
        # so no number of retries can rescue the rung.
        if data["radius"] == 1000 and data["lat"] != centre_lat:
            return _REFUSAL
        return _page(5)

    out, _ = _plan(answer, probes_per_rung=2)
    assert out["calibrated_radius_m"] == 500
    assert out["calibration"][0] == {"radius_m": 1000, "probes": 2, "answered": 1}


def test_calibration_costs_are_counted_into_the_citys_spend():
    out, _ = _plan(lambda data: _page(5), probes_per_rung=2)
    assert out["calibration_requests"] == 2  # r=1000 answered both probes
    assert out["requests_spent_planning"] >= out["calibration_requests"]


def test_a_city_where_no_radius_answers_is_null_not_zero():
    """
    Refusing everywhere must not read as the CHEAPEST city in the study. Zero
    would sort to the front of the cost distribution; null says what happened.
    This is the same "refused is not empty" distinction the feasibility probe
    makes with max_working_radius_m: null.
    """
    out, _ = _plan(lambda data: _REFUSAL)
    assert out["reachable"] is False
    assert out["calibrated_radius_m"] is None
    assert out["sweep_requests_estimate"] is None
    assert out["photos_in_bbox_estimate"] is None
    assert "NOT evidence of an empty city" in out["note"]
    # It cost the full ladder x probes, and that is recorded.
    assert out["requests_spent_planning"] == out["calibration_requests"] > 0


def test_an_unreachable_city_is_excluded_from_the_distribution_not_counted_as_zero():
    cities = [
        {
            "sweep_requests_estimate": 40,
            "calibrated_radius_m": 1000,
            "plan_complete": True,
            "cells_visited": 10,
            "refusals": 0,
            "retries_cleared": 0,
            "retries_attempted": 0,
            "floor_failures": 0,
            "broken_cells": 0,
            "requests_spent_planning": 10,
        },
        {
            "sweep_requests_estimate": None,
            "calibrated_radius_m": None,
            "plan_complete": False,
            "cells_visited": 0,
            "refusals": 0,
            "retries_cleared": 0,
            "retries_attempted": 0,
            "floor_failures": 0,
            "broken_cells": 0,
            "requests_spent_planning": 12,
        },
    ]
    s = ks.summarize(cities)
    assert s["n"] == 1
    assert s["unreachable"] == 1
    assert s["sweep_requests_estimate"]["min"] == 40


# ── a non-backpressure failure must never subdivide ────────────────────────


def test_a_transport_failure_does_not_subdivide():
    """
    Subdividing after a timeout asks a server that just failed to serve one
    request for four. That is the shape of the Mapillary block (#198), not a
    fix for it -- so only BackpressureError may shrink the query.
    """
    out, session = _plan(lambda data: requests.ConnectionError("reset"))
    assert out["reachable"] is False  # calibration could not land either
    assert out["subdivisions"] == 0
    # Bounded: the ladder x probes x (retries + 1), and not one request more.
    assert len(session.calls) == len(ks.RADIUS_LADDER_M) * 2 * (ks.DEFAULT_BACKPRESSURE_RETRIES + 1)


def test_an_unparseable_body_is_not_even_retried():
    """
    The server gave a definite answer we cannot use. Re-asking cannot change
    it, and a rejected credential re-asked at every cell looks like an attack.
    """
    out, session = _plan(lambda data: {"status": {"apiCode": 600}, "no_items_key": True})
    assert out["subdivisions"] == 0
    assert out["reachable"] is False
    assert len(session.calls) == len(ks.RADIUS_LADDER_M) * 2  # one try per probe


def test_a_cell_too_deep_to_page_is_subdivided_rather_than_paged_forever():
    """Above MAX_PAGES_PER_CELL, four shallower circles beat one deep page walk."""
    deep = ks.MAX_PAGES_PER_CELL * 2000 + 1

    def answer(data):
        return _page(deep if data["radius"] > 600 else 5, n=2000)

    out, _ = _plan(answer)
    assert out["subdivisions"] == out["root_cells"]
    assert out["refusals"] == 0  # subdivided on SIZE, not on backpressure
    assert out["leaf_cells"] == 4 * out["root_cells"]


def test_the_floor_is_enforced_on_the_children_not_on_the_parent():
    """
    The natural spelling -- "is this cell above the floor?" -- halves the floor
    it means to enforce: a 125 m cell passes it and yields four 63 m children,
    a radius no feasibility rung ever tested. Asked of the children instead.
    """
    above = ks.Cell(lat=47.6, lon=-122.3, size_m=ks.RADIUS_FLOOR_M * 2 * math.sqrt(2))
    assert above.radius_m == pytest.approx(2 * ks.RADIUS_FLOOR_M, abs=1)
    assert ks.can_subdivide(above) is True
    assert ks.subdivide(above)[0].radius_m >= ks.RADIUS_FLOOR_M

    # A cell that is itself above the floor but whose children would not be.
    marginal = ks.Cell(lat=47.6, lon=-122.3, size_m=1.25 * ks.RADIUS_FLOOR_M * math.sqrt(2))
    assert marginal.radius_m > ks.RADIUS_FLOOR_M
    assert ks.can_subdivide(marginal) is False


def test_the_radius_floor_stops_subdivision_before_it_runs_away():
    radii = set()

    def answer(data):
        radii.add(data["radius"])
        return _REFUSAL

    _plan(answer)
    assert min(radii) >= ks.RADIUS_FLOOR_M
    # And it really did descend, rather than giving up at depth 0.
    assert max(radii) > min(radii)


# ── (4) truncation is visible and unbiased ─────────────────────────────────


def test_a_full_plan_reports_itself_complete_and_is_not_scaled():
    out, _ = _plan(lambda data: _page(100))
    assert out["plan_complete"] is True
    assert out["roots_probed"] == out["root_cells"]
    assert out["sweep_requests_estimate"] == out["sweep_requests_over_probed_roots"]


def test_a_truncated_plan_says_so_and_scales_its_estimates():
    out, _ = _plan(lambda data: _page(100), max_requests=25)
    assert out["plan_complete"] is False
    assert 0 < out["roots_probed"] < out["root_cells"]
    assert out["requests_spent_planning"] <= 25 + 1
    # Scaled up by exactly root_cells / roots_probed.
    scale = out["root_cells"] / out["roots_probed"]
    assert out["sweep_requests_estimate"] == pytest.approx(
        out["sweep_requests_over_probed_roots"] * scale, rel=0.01
    )


def test_a_truncated_plan_samples_the_whole_bbox_not_its_northern_strip():
    """
    Raster order would make a capped run describe the top of the map. The roots
    are shuffled, so the sampled latitudes must span most of the bbox.
    """
    _, session = _plan(lambda data: _page(1), max_requests=40)
    lats = [c["lat"] for c in session.calls]
    all_cells = ks.cells_for_bbox(
        *ks.grid_bbox(
            SEATTLE["center_lat"],
            SEATTLE["center_lon"],
            SEATTLE["grid_width_m"],
            SEATTLE["grid_height_m"],
            SEATTLE["step_m"],
        ),
        1000 * math.sqrt(2),
    )
    full_span = max(c.lat for c in all_cells) - min(c.lat for c in all_cells)
    assert (max(lats) - min(lats)) > 0.5 * full_span


def test_the_shuffle_is_seeded_so_a_rerun_probes_the_same_roots():
    _, a = _plan(lambda data: _page(1), max_requests=30)
    _, b = _plan(lambda data: _page(1), max_requests=30)
    assert [c["lat"] for c in a.calls] == [c["lat"] for c in b.calls]
    _, c = _plan(lambda data: _page(1), max_requests=30, seed=226)
    assert [x["lat"] for x in c.calls] != [x["lat"] for x in a.calls]


# ── the record's own contract ──────────────────────────────────────────────


def test_generated_by_names_the_invocation_not_a_constant():
    canonical = ks.docs_generated_by(
        argparse.Namespace(
            city=None,
            sample="default",
            ipp=ks.IPP_MAX,
            start_radius_m=ks.DEFAULT_START_RADIUS_M,
            max_requests_per_city=ks.DEFAULT_MAX_REQUESTS_PER_CITY,
            docs_dir="docs/experiments",
        )
    )
    assert canonical == (
        "scripts/kartaview_sweep_cost.py --sample default --docs-dir docs/experiments"
    )
    scratch = ks.docs_generated_by(
        argparse.Namespace(
            city=["bend--oregon--united-states"],
            sample="default",
            ipp=200,
            start_radius_m=500,
            max_requests_per_city=10,
            docs_dir="/tmp/scratch",
        )
    )
    for fragment in (
        "--city bend--oregon--united-states",
        "--ipp 200",
        "--start-radius-m 500",
        "--max-requests-per-city 10",
        "/tmp/scratch",
    ):
        assert fragment in scratch


def test_summary_quotes_the_distribution_not_a_headline():
    """CLAUDE.md: a writeup must quote the shape, so the record must carry it."""
    cities = [
        {
            "sweep_requests_estimate": v,
            "calibrated_radius_m": 1000,
            "plan_complete": v < 500,
            "cells_visited": 10,
            "refusals": 1,
            "retries_cleared": 0,
            "retries_attempted": 0,
            "floor_failures": 0,
            "broken_cells": 0,
            "requests_spent_planning": 10,
        }
        for v in (10, 100, 250, 900, 4000)
    ]
    s = ks.summarize(cities)
    assert s["n"] == 5
    assert s["sweep_requests_estimate"]["min"] == 10
    assert s["sweep_requests_estimate"]["max"] == 4000
    assert s["sweep_requests_estimate"]["p50"] == 250
    assert s["plans_truncated"] == 2
    assert s["refusal_rate_over_cells_visited"] == pytest.approx(0.1)


def test_summary_of_an_empty_study_set_is_not_a_crash():
    assert ks.summarize([]) == {"n": 0, "unreachable": 0}


def test_the_script_refuses_to_run_on_a_collection_host():
    """Identity, so the refusal cannot drift a copy (same guard as the probe)."""
    assert ks.refuse_on_collection_host is kp.refuse_on_collection_host
