"""End-to-end tests for the KARTAVIEW arm of the road-walk collector (#258).

KartaView, like Mapillary, has no per-point metadata endpoint: a road walk reads
the radius-sweep census once and joins it onto the same on-street sample points
the GSV walk uses. These tests drive the real `collect.run_collect` flow with the
OSM fetch and the sweep both served from memory, and assert the things that make
this arm trustworthy in its own right rather than by analogy to Mapillary:

  * one row per sample location, with the #116 status vocabulary and the
    match-distance guard applied;
  * requests metered under `kartaview_streets`, never `kartaview`;
  * NO_DATE covering the street while ageing nothing -- KartaView's
    `shot_date >= date_added -> NULL` rule makes this a LARGE population by
    construction, which is why #257 was a hard prerequisite;
  * the census cache (#290): on a paired night the grid run's sweep is this
    walk's census for ZERO requests, which is what makes the arm affordable;
  * a failed cell publishing REQUEST_FAILED rather than ZERO_RESULTS, so an
    unswept sample is never recorded as measured emptiness;
  * cost independent of sample spacing (the whole point of a census).
"""

import gzip
import os
from datetime import date

import geopandas as gpd
import pytest
from shapely.geometry import LineString

from streetscape_metadata_tracker import db
from streetscape_metadata_tracker.checkpointing import census_cache_path_for
from streetscape_metadata_tracker.download_common import SWEEP_INCOMPLETE_EXIT_CODE, grid_bbox
from streetscape_metadata_tracker.download_kartaview import (
    Cell,
    SweepIncompleteError,
    records_to_census,
)
from streetscape_metadata_tracker.naming import (
    generate_streetwalk_filename,
    streetwalk_coverage_filename,
)
from streetscape_street_analyzer import collect
from streetscape_street_analyzer import collect_kartaview as ck

# Same geometry as the GSV and Mapillary walk tests, so the three arms are
# scored over an identical street network and their numbers are comparable.
LONG_EDGE = LineString([(-121.30, 44.05), (-121.30, 44.052)])
SHORT_EDGE = LineString([(-121.30, 44.052), (-121.30, 44.0525)])
CITY_QUERY = "Bend, Oregon, United States"
CITY_ID = "bend--oregon--united-states"
RUN_DATE = "2026-07-08"


def _edges():
    return gpd.GeoDataFrame(
        {"edge_id": ["1_2", "2_3"], "highway": ["residential", "service"], "length": [222.0, 55.0]},
        geometry=[LONG_EDGE, SHORT_EDGE],
        crs="EPSG:4326",
    )


def _image(image_id, lat, lon, *, is_pano=True, shot_date="2024-05-01", date_added="2024-06-01"):
    """One decoded KartaView photo (decode_photo_items' shape).

    `shot_date` before `date_added` is the DATED case. Passing a shot_date at or
    after date_added is how a test reaches NO_DATE, because that is KartaView
    serving an ingest timestamp as a capture date.
    """
    return {
        "id": str(image_id),
        "lat": lat,
        "lon": lon,
        "shot_date": shot_date,
        "date_added": date_added,
        "is_pano": is_pano,
        "username": "OpenStreetView",
        "sequence_id": "8312969",
        "sequence_index": 1,
        "field_of_view": 360.0 if is_pano else 90.0,
        "compass_angle": 12.5,
        "org_code": "CMNT",
        "way_id": "627024267",
    }


def _setup(
    tmp_path, monkeypatch, images, *, api_requests=9, failed_cells=None, token=None, raises=None
):
    """Data dir + catalog with one city; edges and the sweep served locally."""
    data_dir = str(tmp_path)
    conn = db.connect(db.get_default_db_path(data_dir))
    db.register_city(
        conn,
        city_name="Bend",
        state_name="Oregon",
        state_code="OR",
        country_name="United States",
        country_code="US",
        center_lat=44.05,
        center_lon=-121.30,
        grid_width_m=200,
        grid_height_m=200,
        step_m=20,
    )
    conn.close()
    monkeypatch.setattr(collect, "fetch_street_edges", lambda *a, **k: _edges())

    calls = {"n": 0}

    async def fake_sweep(city_name, bbox, access_token, **kwargs):
        calls["n"] += 1
        calls["access_token"] = access_token
        calls["checkpoint_path"] = kwargs.get("checkpoint_path")
        calls["checkpoint_channel"] = kwargs.get("checkpoint_channel")
        calls["checkpoint_variant"] = kwargs.get("checkpoint_variant")
        calls["max_requests_per_minute"] = kwargs.get("max_requests_per_minute")
        calls["max_requests"] = kwargs.get("max_requests")
        if raises is not None:
            raise raises
        policy = kwargs.get("census_cache")
        calls["cache_path"] = policy.path if policy else None
        calls["reuse_census"] = policy.reuse if policy else None
        return {
            "census": records_to_census(images),
            "api_requests": api_requests,
            "api_requests_total": api_requests,
            "checkpoint_path": kwargs.get("checkpoint_path"),
            "cells_visited": 4,
            "failed_cells": failed_cells or [],
            "census_fetched_by": kwargs.get("checkpoint_channel"),
            "census_fetched_at": None,
            "census_reused": False,
        }

    monkeypatch.setattr(ck, "fetch_city_images_async", fake_sweep)
    monkeypatch.setenv("KARTAVIEW_ACCESS_TOKEN", token or "KV-TESTTOKEN")
    return data_dir, calls


def _args(data_dir, **overrides):
    argv = [
        CITY_QUERY,
        "--data-dir",
        data_dir,
        "--run-date",
        RUN_DATE,
        "--spacing",
        "15",
        "--provider",
        "kartaview",
    ]
    for k, v in overrides.items():
        argv += [f"--{k}", str(v)] if v is not True else [f"--{k}"]
    return collect.build_parser().parse_args(argv)


def _paused_sweep(*, spent):
    """A sweep that stopped with roots unvisited, as the request cap leaves it.

    `api_requests` is attached by the collector's `spent` helper rather than
    passed to __init__, so the fixture builds it the same way the real sweep
    does -- otherwise the ledger assertion would pin a shape the collector
    never produces.
    """
    error = SweepIncompleteError(
        "stopped at the request cap",
        checkpoint_path="/checkpoints/kv",
        roots_done=3,
        root_count=8,
    )
    error.api_requests = spent
    error.api_requests_total = spent
    return error


def _walk_csv(data_dir, network_type="drive"):
    stem = generate_streetwalk_filename(
        CITY_ID,
        200,
        200,
        20,
        15,
        date.fromisoformat(RUN_DATE),
        provider="kartaview",
        network_type=network_type,
    )
    return os.path.join(data_dir, stem + ".csv.gz")


def _rows(path):
    with gzip.open(path, "rt") as f:
        return f.read().splitlines()


# ── The arm exists at all ────────────────────────────────────────────────────


def test_kartaview_walk_writes_artifacts_and_meters_its_own_channel(tmp_path, monkeypatch):
    """
    The whole arm, end to end: the sweep is joined locally, both artifacts land
    under the KARTAVIEW filename, and the spend is metered under
    kartaview_streets rather than the grid channel it shares a token with.

    The ledger assertion is the one that would go wrong silently: the walk and
    the grid run use the same credential by design (see config.load_config), so
    nothing about the request itself distinguishes them -- only the channel the
    caller meters it under.
    """
    images = [_image("kv1", 44.0500, -121.30), _image("kv2", 44.0510, -121.30)]
    data_dir, calls = _setup(tmp_path, monkeypatch, images, api_requests=9)

    assert collect.run_collect(_args(data_dir)) == 0
    assert calls["n"] == 1

    csv_path = _walk_csv(data_dir)
    assert os.path.exists(csv_path)
    assert os.path.exists(
        os.path.join(data_dir, streetwalk_coverage_filename(os.path.basename(csv_path)))
    )
    # One row per sample location, plus the header.
    assert len(_rows(csv_path)) > 1

    conn = db.connect(db.get_default_db_path(data_dir))
    assert db.get_api_usage(conn, date.fromisoformat(RUN_DATE), provider="kartaview_streets") == 9
    assert db.get_api_usage(conn, date.fromisoformat(RUN_DATE), provider="kartaview") == 0
    conn.close()


def test_the_walk_shares_the_grid_channels_token_by_default(tmp_path, monkeypatch):
    """
    KARTAVIEW_ACCESS_TOKEN alone is enough to run the walk -- unlike gsv_streets
    and mapillary_streets, which refuse without their own credential.

    Those two isolate a QUOTA that two processes could burn in parallel. Here
    one machine-wide host lock serializes every KartaView request in the repo,
    so there is no parallel spend to isolate, and requiring a second token would
    only make the channel un-runnable. The override is still honoured, which the
    next assertion pins.
    """
    data_dir, calls = _setup(tmp_path, monkeypatch, [_image("kv1", 44.05, -121.30)])
    monkeypatch.delenv("KARTAVIEW_STREETS_ACCESS_TOKEN", raising=False)

    assert collect.run_collect(_args(data_dir)) == 0
    assert calls["access_token"] == "KV-TESTTOKEN"

    # An explicit streets token wins where one is set.
    monkeypatch.setenv("KARTAVIEW_STREETS_ACCESS_TOKEN", "KV-STREETS")
    assert collect.run_collect(_args(data_dir, force=True)) == 0
    assert calls["access_token"] == "KV-STREETS"


# ── NO_DATE is a large population here, not an edge case (#257) ──────────────


def test_undated_kartaview_panos_cover_the_street_but_age_nothing(tmp_path, monkeypatch):
    """
    The arm the Mapillary tests structurally under-exercise.

    KartaView rejects a shot_date at or after its date_added -- that is an
    ingest timestamp being served as a capture date -- so NO_DATE is a large
    population BY CONSTRUCTION. Were the walk to count only OK, the provider
    most honest about its dates would read as the one with the least coverage.

    Covered, and contributing no age: both halves matter, and only the pair
    distinguishes "counted correctly" from "counted as a dated pano".
    """
    # shot_date == date_added -> rejected (the rule is >=, not >).
    undated = [_image("kv1", 44.0500, -121.30, shot_date="2024-06-01", date_added="2024-06-01")]
    data_dir, _ = _setup(tmp_path, monkeypatch, undated)

    assert collect.run_collect(_args(data_dir)) == 0
    body = _rows(_walk_csv(data_dir))
    header, rows = body[0], body[1:]
    assert "NO_DATE" in "\n".join(rows), "an undated pano must still be recorded as present"

    status_i = header.split(",").index("status")
    date_i = header.split(",").index("capture_date")
    no_date_rows = [r.split(",") for r in rows if r.split(",")[status_i] == "NO_DATE"]
    assert no_date_rows, "fixture should produce at least one NO_DATE row"
    for r in no_date_rows:
        assert r[date_i] == "", "a NO_DATE row must carry no capture date"

    conn = db.connect(db.get_default_db_path(data_dir))
    covered, median_age = conn.execute(
        "SELECT coverage_pct_by_length, median_covered_age_years FROM street_walks "
        "WHERE provider = 'kartaview'"
    ).fetchone()
    conn.close()
    # It covers...
    assert covered > 0
    # ...and ages nothing: no dated pano exists, so there is no median age.
    assert median_age is None


def test_a_dated_kartaview_pano_reads_OK_and_carries_its_shot_date(tmp_path, monkeypatch):
    """
    The other half of the date rule, and the half a NO_DATE-only test misses.

    Found by mutation: replacing the whole capture_dates_for binding with one
    that returns '' for every row -- i.e. reading the wrong column, or losing
    KartaView's rule entirely -- left every other test in this file green,
    because they only ever assert that an UNDATED pano is handled. A provider
    whose dates all silently vanish would publish a full-coverage walk that ages
    nothing, and read as a data property rather than a bug.

    So this pins the positive case: shot_date strictly before date_added
    survives as the capture date, the row is OK, and it reaches the age stat.
    """
    dated = [_image("kv1", 44.0500, -121.30, shot_date="2024-05-01", date_added="2024-06-01")]
    data_dir, _ = _setup(tmp_path, monkeypatch, dated)

    assert collect.run_collect(_args(data_dir)) == 0
    body = _rows(_walk_csv(data_dir))
    header = body[0].split(",")
    status_i, date_i = header.index("status"), header.index("capture_date")
    ok_rows = [r.split(",") for r in body[1:] if r.split(",")[status_i] == "OK"]
    assert ok_rows, "a pano dated before its ingest timestamp must read OK, not NO_DATE"
    assert all(r[date_i] == "2024-05-01" for r in ok_rows), (
        "the SHOT date must survive to the row, not the ingest date or a blank"
    )

    conn = db.connect(db.get_default_db_path(data_dir))
    median_age = conn.execute(
        "SELECT median_covered_age_years FROM street_walks WHERE provider = 'kartaview'"
    ).fetchone()[0]
    conn.close()
    # A dated pano ages: the complement of the NO_DATE test's `is None`.
    assert median_age is not None and median_age > 0


# ── The census cache is what makes this affordable (#290) ────────────────────


def test_the_walk_reads_the_grid_runs_cache_entry(tmp_path, monkeypatch):
    """
    The cache path the walk asks for must be the one the GRID run writes:
    keyed on (provider, city, bbox) with no channel, no variant and no date.

    This is the whole cost argument for the arm. A channel-keyed path would
    reuse nothing and every walk would re-sweep the city -- silently, since a
    re-sweep produces the same census, just at full price.
    """
    data_dir, calls = _setup(tmp_path, monkeypatch, [_image("kv1", 44.05, -121.30)])
    assert collect.run_collect(_args(data_dir)) == 0

    conn = db.connect(db.get_default_db_path(data_dir))
    city = db.resolve_city(conn, CITY_QUERY)
    conn.close()
    bbox = grid_bbox(
        city.center_lat, city.center_lon, city.grid_width_m, city.grid_height_m, city.step_m
    )

    assert calls["cache_path"] == census_cache_path_for("kartaview", CITY_ID, bbox)
    # The CHECKPOINT, by contrast, is the walk's own: it carries the channel and
    # the network type, so the walk can never resume the grid run's sweep into
    # the wrong ledger.
    assert calls["checkpoint_channel"] == "kartaview_streets"
    assert calls["checkpoint_variant"] == "drive"
    assert "kartaview_streets" in calls["checkpoint_path"]


def test_the_request_cap_reaches_the_sweep_rather_than_only_the_budget_gate(tmp_path, monkeypatch):
    """
    --daily-budget only GATES: it is priced from estimate_sweep_requests, which
    the estimator's own docstring calls a geometric FLOOR (measured overhead
    1.80x; Yogyakarta ran 3.0x). So a gate that passes at 1,800 does not stop
    the sweep spending 5,400 against a host that meters by IP and sends no
    Retry-After. The cap is the enforceable half, and it has to reach the
    sweep -- a flag parsed and dropped bounds nothing.

    Asserted against a value that is not the default, so a call site that
    hardcoded the default (or dropped the argument) still fails.
    """
    data_dir, calls = _setup(tmp_path, monkeypatch, [_image("kv1", 44.05, -121.30)])
    assert collect.run_collect(_args(data_dir, **{"kartaview-max-requests": 500})) == 0
    assert calls["max_requests"] == 500

    # ...and unset stays unset, rather than a cap nobody asked for silently
    # truncating a city's sweep into a permanent hole.
    data_dir2, calls2 = _setup(tmp_path / "b", monkeypatch, [_image("kv1", 44.05, -121.30)])
    assert collect.run_collect(_args(data_dir2)) == 0
    assert calls2["max_requests"] is None


def test_the_walks_request_cap_refuses_nonpositive_values_like_the_grid_flag(tmp_path):
    """
    The grid CLI refuses `--kartaview-max-requests 0` at parse time, because 0
    spends the whole calibration ladder, checkpoints roots_done=0 and exits 83
    printing "re-run the same command to resume" -- a loop the message
    encourages. This copy of the flag carried plain `type=int`, so the guard was
    real on one path and absent on the other: the shape a copied argument always
    takes (#273). Both now share download_common.positive_int.
    """
    for bad in ("0", "-5"):
        with pytest.raises(SystemExit) as excinfo:
            _args(str(tmp_path), **{"kartaview-max-requests": bad})
        assert excinfo.value.code == 2


def test_a_sweep_that_stops_at_its_cap_exits_83_rather_than_failing(tmp_path, monkeypatch):
    """
    Hitting the cap is PROGRESS: the work is checkpointed and the next run
    resumes from it. Exit 83 is what says so.

    Folding it into 1 would be worse than cosmetic. The scheduler amnesties 83
    but counts a 1 as a consecutive_failure, and nothing but a success resets
    that -- so a city too large to sweep in one night, which is exactly the
    city the checkpoint exists for, would quarantine itself after five nights
    of making steady progress.
    """
    data_dir, _ = _setup(
        tmp_path,
        monkeypatch,
        [_image("kv1", 44.05, -121.30)],
        raises=_paused_sweep(spent=500),
    )
    assert collect.run_collect(_args(data_dir, **{"kartaview-max-requests": 500})) == (
        SWEEP_INCOMPLETE_EXIT_CODE
    )
    assert not os.path.exists(_walk_csv(data_dir)), "a paused sweep publishes nothing"

    # The spend still reaches the ledger, or tomorrow's gate overspends by
    # whatever a paused night cost.
    conn = db.connect(db.get_default_db_path(data_dir))
    spent = db.get_api_usage(conn, date.fromisoformat(RUN_DATE), provider="kartaview_streets")
    conn.close()
    assert spent == 500


def test_the_walks_variant_reaches_the_fetch(tmp_path, monkeypatch):
    """
    --network-type has to arrive at the sweep, not be assumed None.

    'drive' and 'all_public' are different series over the SAME bbox in the SAME
    channel, and the variant is what reconcile_cache_hit uses to tell a walk's
    own prior work from the other variant's. Hardcoding None there had the two
    walks reconcile against each other's checkpoints and inherit each other's
    holes; the grid run passes None because it genuinely has no variant.
    """
    data_dir, calls = _setup(tmp_path, monkeypatch, [_image("kv1", 44.05, -121.30)])
    assert collect.run_collect(_args(data_dir, **{"network-type": "all_public"})) == 0
    assert calls["checkpoint_variant"] == "all_public"


# ── An unswept sample is not an empty one ───────────────────────────────────


def test_a_failed_cell_publishes_request_failed_not_zero_results(tmp_path, monkeypatch):
    """
    A cell the sweep never got back leaves its samples UNKNOWN.

    Street coverage is a share of samples, so recording an unswept sample as
    ZERO_RESULTS publishes an absence we never observed -- into an immutable
    dated snapshot that understates the city permanently. The grid run has
    always done this; the walk must too, or the two disagree about the same
    unswept ground. (Mapillary's walk still has this gap: #259.)
    """
    # A SMALL cell over the north end of the long edge, so the run carries both
    # kinds at once. A whole-city failed cell would be 100% REQUEST_FAILED,
    # which detect_systemic_failure rejects outright (correctly -- that is a
    # broken credential, not a coverage measurement), and would prove nothing
    # about the discrimination this test is actually about. The real sweep
    # refuses to finalize past MAX_FAILED_AREA_FRACTION for the same reason.
    failed = [Cell(lat=44.0519, lon=-121.30, size_m=60.0)]
    images = [_image("kv1", 44.0500, -121.30)]
    data_dir, _ = _setup(tmp_path, monkeypatch, images, failed_cells=failed)

    assert collect.run_collect(_args(data_dir)) == 0
    body = _rows(_walk_csv(data_dir))
    status_i = body[0].split(",").index("status")
    statuses = [r.split(",")[status_i] for r in body[1:]]
    assert "REQUEST_FAILED" in statuses, (
        f"samples under an unswept cell must be REQUEST_FAILED, got {set(statuses)}"
    )
    # ...and the rest of the city, which WAS swept, still reads as measured.
    assert {"ZERO_RESULTS", "OK"} & set(statuses), "only the unswept samples may be REQUEST_FAILED"


def test_a_clean_sweep_still_publishes_zero_results(tmp_path, monkeypatch):
    """
    The other half of the pair: with no failed cells, an empty bbox is a
    MEASURED absence and must read ZERO_RESULTS.

    Without this, marking everything REQUEST_FAILED would pass the test above
    while destroying the ordinary case -- the coverage denominator depends on
    telling observed emptiness from unobserved ground.
    """
    data_dir, _ = _setup(tmp_path, monkeypatch, [], failed_cells=[])

    assert collect.run_collect(_args(data_dir)) == 0
    body = _rows(_walk_csv(data_dir))
    statuses = {r.split(",")[body[0].split(",").index("status")] for r in body[1:]}
    assert statuses == {"ZERO_RESULTS"}


# ── Cost is bbox area, not sample count ─────────────────────────────────────


def test_cost_is_independent_of_spacing(tmp_path, monkeypatch):
    """
    The defining property of a census arm, and the reason its --estimate text
    says "independent of spacing": halving the spacing doubles the sample points
    and changes the request count not at all.
    """
    images = [_image("kv1", 44.0500, -121.30)]
    data_dir, calls = _setup(tmp_path, monkeypatch, images, api_requests=9)

    assert collect.run_collect(_args(data_dir, spacing=15)) == 0
    coarse = calls["n"]
    assert collect.run_collect(_args(data_dir, spacing=5, force=True)) == 0

    conn = db.connect(db.get_default_db_path(data_dir))
    # Two collections, each 9 requests -- the sweep is paid per RUN, not per sample.
    assert db.get_api_usage(conn, date.fromisoformat(RUN_DATE), provider="kartaview_streets") == 18
    conn.close()
    assert calls["n"] == coarse + 1
