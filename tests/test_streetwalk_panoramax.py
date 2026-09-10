"""End-to-end tests for the PANORAMAX arm of the road-walk collector (#331).

Panoramax is the third census provider bound to `census_walk`: no per-sample
endpoint, so a road walk is one z15 tile census over the frozen bbox joined
locally onto the same on-street sample points the GSV walk queries one at a
time. These tests drive the real `collect.run_collect` flow with the OSM fetch
and the tile census both served from memory, and assert the things that make
this arm trustworthy in its own right rather than by analogy to Mapillary:

  * one row per sample location, with the #116 status vocabulary and the
    match-distance guard applied;
  * requests metered under `panoramax_streets`, never `panoramax`;
  * it runs with a COMPLETELY BARE environment -- the one walk with no
    credential at all, which is a contract rather than a convenience;
  * it is priced and paced by PANORAMAX's constants, not Mapillary's, whose
    module exports the same four identifier names with different numbers;
  * the census cache (#290): on a paired night the grid run's tiles are this
    walk's census for ZERO requests;
  * a failed tile publishing REQUEST_FAILED rather than ZERO_RESULTS, so an
    unswept sample is never recorded as measured emptiness;
  * cost independent of sample spacing (the whole point of a census).
"""

import gzip
import os
from datetime import date
from unittest import mock

import geopandas as gpd
import numpy as np
from shapely.geometry import LineString

from streetscape_metadata_tracker import db
from streetscape_metadata_tracker.checkpointing import census_cache_path_for
from streetscape_metadata_tracker.download_common import (
    grid_bbox,
    lonlat_to_tile_frac,
    tile_frac_to_lonlat,
)
from streetscape_metadata_tracker.download_panoramax import (
    TILE_ZOOM,
    _points_in_tiles,
    estimate_tile_count,
    records_to_census,
)
from streetscape_metadata_tracker.naming import (
    generate_streetwalk_filename,
    streetwalk_coverage_filename,
)
from streetscape_street_analyzer import collect
from streetscape_street_analyzer import collect_panoramax as cp

# Same geometry as the GSV, Mapillary and KartaView walk tests, so all four arms
# are scored over an identical street network and their numbers are comparable.
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


def _picture(picture_id, lat, lon, *, is_pano=True, ts="2025-11-02 00:24:37+00"):
    """One decoded Panoramax tile picture (`pictures_from_tile`'s shape).

    `ts` defaults to a real timestamp shape phase 1 saw on the wire. Passing the
    Unix epoch is how a test reaches NO_DATE, because that is this provider's
    known sentinel and the plausibility floor is what drops it.
    """
    return {
        "id": str(picture_id),
        "lat": lat,
        "lon": lon,
        "ts": ts,
        "image_type": "equirectangular" if is_pano else "flat",
        "is_pano": is_pano,
        "account_id": "0f1f2e3d-4c5b-6a79-8899-aabbccddeeff",
        "sequence_id": "seq-1",
    }


def _setup(
    tmp_path,
    monkeypatch,
    pictures,
    *,
    api_requests=5,
    failed_tiles=None,
    edges=None,
    grid_m=200,
):
    """Data dir + catalog with one city; edges and the tile census served locally.

    ``grid_m`` widens the FROZEN GRID without touching the street network: the
    edges are served from memory either way, so a wider grid moves exactly one
    number -- the tile-count estimate, which is priced from the grid's bbox.
    """
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
        grid_width_m=grid_m,
        grid_height_m=grid_m,
        step_m=20,
    )
    conn.close()
    walked = _edges() if edges is None else edges
    monkeypatch.setattr(collect, "fetch_street_edges", lambda *a, **k: walked)

    calls = {"n": 0}

    async def fake_fetch_images(city_name, bbox, **kwargs):
        # POSITIONAL ARGUMENTS ARE THE ASSERTION HERE: the real
        # download_panoramax.fetch_city_images_async takes NO access_token, so a
        # collector that grew one -- copied from its Mapillary sibling -- would
        # fail loudly on this signature rather than silently passing a token
        # nothing reads.
        calls["n"] += 1
        calls["bbox"] = bbox
        calls["checkpoint_path"] = kwargs.get("checkpoint_path")
        calls["checkpoint_channel"] = kwargs.get("checkpoint_channel")
        calls["checkpoint_variant"] = kwargs.get("checkpoint_variant")
        calls["max_requests_per_minute"] = kwargs.get("max_requests_per_minute")
        calls["jitter"] = kwargs.get("jitter")
        policy = kwargs.get("census_cache")
        calls["cache_path"] = policy.path if policy else None
        calls["reuse_census"] = policy.reuse if policy else None
        return {
            "census": records_to_census(pictures),
            # Per-process spend and the crawl's cumulative spend are different
            # numbers by design: the first feeds the additive daily ledger, the
            # second the street_walks row.
            "api_requests": api_requests,
            "api_requests_total": api_requests,
            "checkpoint_path": kwargs.get("checkpoint_path"),
            "tiles": 5,
            "raw_feature_count": len(pictures),
            # Tiles the fetch never got back. A 404 is an EMPTY tile here and
            # never lands in this list, so everything in it is genuinely
            # unmeasured ground.
            "failed_tiles": list(failed_tiles or []),
            "census_fetched_by": kwargs.get("checkpoint_channel"),
            "census_fetched_at": None,
            "census_reused": False,
        }

    monkeypatch.setattr(cp, "fetch_city_images_async", fake_fetch_images)
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
        "panoramax",
    ]
    for k, v in overrides.items():
        argv += [f"--{k}", str(v)] if v is not True else [f"--{k}"]
    return collect.build_parser().parse_args(argv)


def _walk_csv(data_dir, network_type="drive", spacing=15, grid_m=200):
    stem = generate_streetwalk_filename(
        CITY_ID,
        grid_m,
        grid_m,
        20,
        spacing,
        date.fromisoformat(RUN_DATE),
        provider="panoramax",
        network_type=network_type,
    )
    return os.path.join(data_dir, stem + ".csv.gz")


def _rows(path):
    with gzip.open(path, "rt") as f:
        return f.read().splitlines()


def _column(path, name):
    body = _rows(path)
    idx = body[0].split(",").index(name)
    return [r.split(",")[idx] for r in body[1:]]


# ── The arm exists at all ────────────────────────────────────────────────────


def test_panoramax_walk_writes_artifacts_and_meters_its_own_channel(tmp_path, monkeypatch):
    """
    The whole arm, end to end: the tile census is joined locally, both artifacts
    land under the PANORAMAX filename, and the spend is metered under
    panoramax_streets rather than the grid channel.

    The ledger assertion is the one that would go wrong silently. The two
    channels share a provider, a host and (there being none) a credential, so
    nothing about the request itself distinguishes them -- only the channel the
    caller meters it under. Charging a walk to `panoramax` would let a night's
    grid budget be spent by walks and read as if the grid run had done it.
    """
    pictures = [_picture("px1", 44.0500, -121.30), _picture("px2", 44.0510, -121.30)]
    data_dir, calls = _setup(tmp_path, monkeypatch, pictures, api_requests=5)

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
    assert db.get_api_usage(conn, date.fromisoformat(RUN_DATE), provider="panoramax_streets") == 5
    assert db.get_api_usage(conn, date.fromisoformat(RUN_DATE), provider="panoramax") == 0
    # The catalog row carries the provider token too, or streets.html renders
    # this walk as some other provider's.
    assert (
        conn.execute("SELECT COUNT(*) FROM street_walks WHERE provider = 'panoramax'").fetchone()[0]
        == 1
    )
    conn.close()


def test_the_walk_runs_with_a_completely_bare_environment(tmp_path, monkeypatch):
    """
    No credential, and that is a CONTRACT rather than a convenience (#316/#331).

    `panoramax_streets` declares an empty tuple in CHANNEL_ENV_VARS, which is
    what puts it in CREDENTIAL_FREE_CHANNELS and makes load_config return
    access_token=None instead of raising. Drop that row and load_config falls
    through to its final `raise`, so the one walk that needs no key becomes the
    one walk that cannot start -- and the failure would name a missing
    credential, which is the least true explanation available.

    `clear=True` is the whole test: it asserts the run survives an environment
    holding nothing at all, not merely one that happens to lack some variable.
    """
    data_dir, calls = _setup(tmp_path, monkeypatch, [_picture("px1", 44.05, -121.30)])
    # find_dotenv/load_dotenv would repopulate the environment from a developer's
    # own .env, which is exactly what this test must not let happen.
    monkeypatch.setattr(collect, "load_dotenv", lambda *a, **k: False)
    monkeypatch.setattr(collect, "find_dotenv", lambda *a, **k: "")

    with mock.patch.dict(os.environ, {}, clear=True):
        assert collect.run_collect(_args(data_dir)) == 0
    assert calls["n"] == 1


# ── Priced and paced by ITS constants, not its sibling's ─────────────────────


def test_the_estimate_prices_z15_tiles_and_issues_no_requests(tmp_path, monkeypatch, capsys):
    """
    --estimate must reach download_panoramax's estimator, not download_mapillary's.

    Both modules export `estimate_tile_count`, `DEFAULT_TILE_REQUESTS_PER_MINUTE`
    and `DEFAULT_TILE_JITTER` -- the same four spellings, different numbers -- so
    an unaliased import in collect.py would silently rebind whichever came
    second and price this channel with the other's z14 lattice. That is the #268
    failure (one provider's cost model wearing another's name) reached through
    the import list, and it is invisible: a z14 count is a plausible number.

    Asserted against the z15 count computed here from the city's own frozen
    geometry, so the test cannot pass by agreeing with a wrong constant.
    """
    data_dir, calls = _setup(tmp_path, monkeypatch, [], grid_m=10_000)

    assert collect.run_collect(_args(data_dir, estimate=True)) == 0
    assert calls["n"] == 0, "an --estimate must issue no requests at all"

    expected = estimate_tile_count(44.05, -121.30, 10_000, 10_000, 20)
    out = capsys.readouterr().out
    assert f"~{expected} Panoramax tile requests" in out, out
    # The z15 lattice really is ~4x the z14 one, so a Mapillary-priced estimate
    # would be a different number rather than an accidental match.
    from streetscape_metadata_tracker.download_mapillary import (
        estimate_tile_count as mapillary_estimate,
    )

    assert expected > mapillary_estimate(44.05, -121.30, 10_000, 10_000, 20)


def test_the_pacing_flags_reach_the_fetch_rather_than_only_being_parsed(tmp_path, monkeypatch):
    """
    A flag parsed and dropped paces nothing.

    Panoramax documents no rate limit and returns no X-RateLimit-*/Retry-After
    header, so the client-side figure is the ONLY thing bounding what this walk
    does to volunteer-run infrastructure -- there is no server-side backstop to
    catch a call site that hardcoded a default.

    Asserted against values that are not the defaults, so a dispatch arm passing
    `DEFAULT_TILE_REQUESTS_PER_MINUTE` literally (or Mapillary's flag by
    copy-paste) still fails.
    """
    data_dir, calls = _setup(tmp_path, monkeypatch, [_picture("px1", 44.05, -121.30)])
    args = _args(
        data_dir,
        **{"panoramax-max-requests-per-minute": 7, "panoramax-jitter": 0.25},
    )
    assert collect.run_collect(args) == 0
    assert calls["max_requests_per_minute"] == 7
    assert calls["jitter"] == 0.25

    # ...and the defaults are PANORAMAX's, half Mapillary's rate, rather than
    # whatever the sibling module exports under the same name.
    from streetscape_metadata_tracker.download_mapillary import (
        DEFAULT_TILE_REQUESTS_PER_MINUTE as mapillary_rate,
    )
    from streetscape_metadata_tracker.download_panoramax import (
        DEFAULT_TILE_REQUESTS_PER_MINUTE as panoramax_rate,
    )

    data_dir2, calls2 = _setup(tmp_path / "b", monkeypatch, [_picture("px1", 44.05, -121.30)])
    assert collect.run_collect(_args(data_dir2)) == 0
    assert calls2["max_requests_per_minute"] == panoramax_rate
    assert panoramax_rate < mapillary_rate


# ── The date rule is the grid run's, not a second one ───────────────────────


def test_a_dated_picture_reads_OK_and_carries_its_capture_date(tmp_path, monkeypatch):
    """
    The positive half of the date rule, and the half a NO_DATE-only test misses.

    Found by mutation on the KartaView arm: a `capture_dates_for` binding that
    returns '' for every row -- reading the wrong column, or losing the rule --
    leaves the undated tests green, and the walk then publishes full coverage
    that ages nothing, reading as a data property rather than as a bug.
    """
    data_dir, _ = _setup(
        tmp_path, monkeypatch, [_picture("px1", 44.0500, -121.30, ts="2025-11-02 00:24:37+00")]
    )
    assert collect.run_collect(_args(data_dir)) == 0

    csv_path = _walk_csv(data_dir)
    statuses, dates = _column(csv_path, "status"), _column(csv_path, "capture_date")
    ok = [d for s, d in zip(statuses, dates, strict=True) if s == "OK"]
    assert ok, "a picture with a plausible timestamp must read OK, not NO_DATE"
    assert all(d == "2025-11-02" for d in ok), (
        "the tile's own `ts` must survive to the row, parsed as a DATE"
    )

    conn = db.connect(db.get_default_db_path(data_dir))
    median_age = conn.execute(
        "SELECT median_covered_age_years FROM street_walks WHERE provider = 'panoramax'"
    ).fetchone()[0]
    conn.close()
    assert median_age is not None and median_age > 0


def test_the_1970_sentinel_covers_the_street_but_ages_nothing(tmp_path, monkeypatch):
    """
    Panoramax serves a genuine epoch sentinel (phase 1 found two Paris pictures
    dated 1970-01-01), which the plausibility floor drops.

    A dropped date must become NO_DATE, never a dropped SAMPLE: an undated pano
    still covers, it simply ages nothing (#257). Both halves matter, and only
    the pair distinguishes "counted correctly" from "counted as a dated pano" --
    or from "not counted at all", which would understate the provider most
    honest about its timestamps.

    It also pins the REUSE that makes this arm small: the rule lives in the grid
    run's `_panoramax_capture_dates`, so a second implementation here would have
    one city's grid and street artifacts disagree about the same picture.
    """
    data_dir, _ = _setup(
        tmp_path, monkeypatch, [_picture("px1", 44.0500, -121.30, ts="1970-01-01T00:00:00Z")]
    )
    assert collect.run_collect(_args(data_dir)) == 0

    csv_path = _walk_csv(data_dir)
    statuses, dates = _column(csv_path, "status"), _column(csv_path, "capture_date")
    assert "NO_DATE" in statuses, "an undated pano must still be recorded as present"
    assert all(d == "" for s, d in zip(statuses, dates, strict=True) if s == "NO_DATE"), (
        "a NO_DATE row must carry no capture date"
    )

    conn = db.connect(db.get_default_db_path(data_dir))
    covered, median_age = conn.execute(
        "SELECT coverage_pct_by_length, median_covered_age_years FROM street_walks "
        "WHERE provider = 'panoramax'"
    ).fetchone()
    conn.close()
    assert covered > 0, "an undated pano covers"
    assert median_age is None, "...and ages nothing"


def test_flat_imagery_alone_is_FLAT_ONLY_with_no_capture_date(tmp_path, monkeypatch):
    """
    `type` is TWO-STATE here: equirectangular, or flat. A picture that is not a
    360° pano covers the any-imagery number and not the 360° one, and its
    timestamp is never a capture date -- FLAT_ONLY rows carry none, so flat
    imagery can never enter a dated statistic.
    """
    data_dir, _ = _setup(tmp_path, monkeypatch, [_picture("px1", 44.0500, -121.30, is_pano=False)])
    assert collect.run_collect(_args(data_dir)) == 0

    csv_path = _walk_csv(data_dir)
    statuses, dates = _column(csv_path, "status"), _column(csv_path, "capture_date")
    assert "FLAT_ONLY" in statuses
    assert all(d == "" for s, d in zip(statuses, dates, strict=True) if s == "FLAT_ONLY")

    conn = db.connect(db.get_default_db_path(data_dir))
    pano_pct, any_pct = conn.execute(
        "SELECT coverage_pct_by_length, coverage_pct_by_length_any FROM street_walks "
        "WHERE provider = 'panoramax'"
    ).fetchone()
    conn.close()
    assert pano_pct == 0, "flat imagery is not 360 coverage"
    assert any_pct > 0, "...but it is any-imagery coverage"


# ── The census cache is what makes this affordable (#290) ────────────────────


def test_the_walk_reads_the_grid_runs_cache_entry(tmp_path, monkeypatch):
    """
    The cache path the walk asks for must be the one the GRID run writes: keyed
    on (provider, city, bbox) with no channel, no variant and no date.

    This is the whole cost argument for the arm. A channel-keyed path would
    reuse nothing and every walk would re-tile the city -- silently, since a
    re-fetch produces the same census, just at full price against a host with no
    documented rate limit.
    """
    data_dir, calls = _setup(tmp_path, monkeypatch, [_picture("px1", 44.05, -121.30)])
    assert collect.run_collect(_args(data_dir)) == 0

    conn = db.connect(db.get_default_db_path(data_dir))
    city = db.resolve_city(conn, CITY_QUERY)
    conn.close()
    bbox = grid_bbox(
        city.center_lat, city.center_lon, city.grid_width_m, city.grid_height_m, city.step_m
    )
    assert calls["bbox"] == bbox, "the walk must tile the FROZEN grid bbox, not its own"
    assert calls["cache_path"] == census_cache_path_for("panoramax", CITY_ID, bbox)
    # The CHECKPOINT, by contrast, is the walk's own: it carries the channel and
    # the network type, so the walk can never resume the grid run's crawl into
    # the wrong ledger, nor one --network-type inherit the other's holes.
    assert calls["checkpoint_channel"] == "panoramax_streets"
    assert calls["checkpoint_variant"] == "drive"
    assert "panoramax_streets" in calls["checkpoint_path"]


def test_the_walks_variant_reaches_the_fetch(tmp_path, monkeypatch):
    """
    --network-type has to arrive at the fetch, not be assumed None.

    'drive' and 'all_public' are different series over the SAME bbox in the SAME
    channel, and the variant is what reconcile_cache_hit uses to tell a walk's
    own prior work from the other variant's.
    """
    data_dir, calls = _setup(tmp_path, monkeypatch, [_picture("px1", 44.05, -121.30)])
    assert collect.run_collect(_args(data_dir, **{"network-type": "all_public"})) == 0
    assert calls["checkpoint_variant"] == "all_public"


def test_refetch_census_opts_out_of_the_reuse(tmp_path, monkeypatch):
    """`--refetch-census` is the opt-out, and it must reach the fetch as a
    POLICY rather than being handled by the caller: the cache lifecycle lives in
    checkpointing.py, and a collector that decided reuse for itself would be the
    per-provider copy #290 exists to prevent."""
    data_dir, calls = _setup(tmp_path, monkeypatch, [_picture("px1", 44.05, -121.30)])
    assert collect.run_collect(_args(data_dir, **{"refetch-census": True})) == 0
    assert calls["reuse_census"] is False

    data_dir2, calls2 = _setup(tmp_path / "b", monkeypatch, [_picture("px1", 44.05, -121.30)])
    assert collect.run_collect(_args(data_dir2)) == 0
    assert calls2["reuse_census"] is True


# ── An unswept sample is not an empty one ───────────────────────────────────

# The city's own edges sit wholly inside ONE z15 tile, so failing that tile
# would mask every sample or none and could not tell the two apart. These tests
# walk an edge laid ACROSS a real z15 seam instead -- the northern boundary of
# the tile the city sits in -- and fail only the tile on one side, so a single
# run carries both kinds at once. The seam is derived from the shipped tiling
# rather than hardcoded, so a zoom change moves the geometry with it instead of
# quietly making these tests vacuous.
CITY_TILE = tuple(int(v) for v in lonlat_to_tile_frac(-121.30, 44.05, TILE_ZOOM))
NORTH_TILE = (CITY_TILE[0], CITY_TILE[1] - 1)
# y grows southward, so a tile's own y index names its NORTHERN edge.
SEAM_LAT = tile_frac_to_lonlat(CITY_TILE[0] + 0.5, CITY_TILE[1], TILE_ZOOM)[1]


def _seam_edges():
    """One ~220 m north-south edge straddling the seam: half in each tile."""
    return gpd.GeoDataFrame(
        {"edge_id": ["s1"], "highway": ["residential"], "length": [220.0]},
        geometry=[LineString([(-121.30, SEAM_LAT - 0.001), (-121.30, SEAM_LAT + 0.001)])],
        crs="EPSG:4326",
    )


def test_a_failed_tile_publishes_request_failed_not_zero_results(tmp_path, monkeypatch):
    """
    A tile the fetch never got back leaves its samples UNKNOWN.

    Street coverage is a share of samples, so recording an unswept sample as
    ZERO_RESULTS publishes an absence we never observed -- into an immutable
    dated snapshot that understates the city permanently. The grid run has
    always done this; the walk must too, or the two disagree about the same
    unswept ground.

    Panoramax sharpens the distinction: a 404 is an EMPTY TILE here, not a
    failure, so a tile genuinely holding no imagery never reaches failed_tiles
    at all. Everything this mask covers is ground the fetch really did not see.

    The split must follow the TILE boundary rather than some other accident, so
    the unknown rows are asserted to be exactly the samples north of the seam.
    """
    data_dir, _ = _setup(tmp_path, monkeypatch, [], failed_tiles=[NORTH_TILE], edges=_seam_edges())
    assert collect.run_collect(_args(data_dir)) == 0

    csv_path = _walk_csv(data_dir)
    statuses = _column(csv_path, "status")
    lats = np.array([float(v) for v in _column(csv_path, "query_lat")])
    assert "REQUEST_FAILED" in statuses, (
        f"samples under an unswept tile must be REQUEST_FAILED, got {set(statuses)}"
    )
    assert "ZERO_RESULTS" in statuses, "the swept half still reads as measured emptiness"

    unknown = np.array([s == "REQUEST_FAILED" for s in statuses])
    in_failed = _points_in_tiles(lats, np.full(lats.shape, -121.30), [NORTH_TILE])
    assert in_failed.any() and not in_failed.all(), (
        "the fixture geometry moved; this test needs samples on BOTH sides of the seam"
    )
    assert (unknown == in_failed).all(), (
        "the unknown rows must be exactly the samples inside the failed tile"
    )


def test_a_clean_fetch_still_publishes_zero_results(tmp_path, monkeypatch):
    """
    The other half of the pair: with no failed tiles, an empty bbox is a
    MEASURED absence and must read ZERO_RESULTS.

    Without this, marking everything REQUEST_FAILED would pass the test above
    while destroying the ordinary case -- the coverage denominator depends on
    telling observed emptiness from unobserved ground.
    """
    data_dir, _ = _setup(tmp_path, monkeypatch, [], failed_tiles=[], edges=_seam_edges())
    assert collect.run_collect(_args(data_dir)) == 0
    assert set(_column(_walk_csv(data_dir), "status")) == {"ZERO_RESULTS"}


def test_a_matched_sample_inside_a_failed_tile_stays_matched(tmp_path, monkeypatch):
    """
    The mask speaks only for samples that matched NOTHING.

    A sample that found imagery within --match-dist was measured by
    construction, whatever tile it sits in, so relabelling it would discard a
    real observation. Asserted with `_points_in_tiles` so the premise cannot go
    vacuous if the fixture geometry drifts.
    """
    # A pano just north of the seam, i.e. inside the tile declared failed.
    inside = _picture("px1", SEAM_LAT + 0.0002, -121.30)
    assert _points_in_tiles(
        np.array([inside["lat"]]), np.array([inside["lon"]]), [NORTH_TILE]
    ).all(), "the fixture pano must sit inside the failed tile for this test to mean anything"

    data_dir, _ = _setup(
        tmp_path, monkeypatch, [inside], failed_tiles=[NORTH_TILE], edges=_seam_edges()
    )
    assert collect.run_collect(_args(data_dir)) == 0
    statuses = _column(_walk_csv(data_dir), "status")
    assert "OK" in statuses, "a sample that matched imagery keeps its match"


# ── Cost is bbox area, not sample count ─────────────────────────────────────


def test_cost_is_independent_of_spacing(tmp_path, monkeypatch):
    """
    The defining property of a census arm, and the reason its --estimate text
    says "independent of spacing": halving the spacing triples the sample points
    and changes the request count not at all.
    """
    data_dir, calls = _setup(
        tmp_path, monkeypatch, [_picture("px1", 44.0500, -121.30)], api_requests=5
    )

    assert collect.run_collect(_args(data_dir, spacing=15)) == 0
    coarse_rows = len(_rows(_walk_csv(data_dir, spacing=15)))
    assert collect.run_collect(_args(data_dir, spacing=5)) == 0
    fine_rows = len(_rows(_walk_csv(data_dir, spacing=5)))

    assert fine_rows > coarse_rows, "the finer walk must actually score more samples"
    conn = db.connect(db.get_default_db_path(data_dir))
    # Two collections, each 5 requests -- the census is paid per RUN, not per
    # sample.
    assert db.get_api_usage(conn, date.fromisoformat(RUN_DATE), provider="panoramax_streets") == 10
    conn.close()
    assert calls["n"] == 2
