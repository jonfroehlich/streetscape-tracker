"""
The Panoramax grid-run wrapper: the join between the fetch and the shared tail.

`tests/test_panoramax.py` covers the decoder and the transport; `tests/test_census.py`
covers the provider-agnostic seam driven by a deliberately un-Mapillary schema.
Neither can see what this file is about — that the bindings the tail actually
reaches are PANORAMAX's. A wrapper that passed another provider's dtypes or
another provider's `image_columns` would still produce a valid CSV, register a
run, and publish it, with the wrong schema in the wrong column order under a
filename claiming otherwise.

Modelled on `tests/test_kartaview_grid_run.py`, which exists for the same reason
and names the same gap.
"""

import asyncio
import gzip
import re

import mapbox_vector_tile
import pandas as pd
import pytest

from streetscape_metadata_tracker import analysis
from streetscape_metadata_tracker import download_panoramax as dp
from streetscape_metadata_tracker.checkpointing import CensusCache, load_census_cache_marker
from streetscape_metadata_tracker.config import PANORAMAX_METADATA_DTYPES
from streetscape_metadata_tracker.download_common import (
    DownloadError,
    HostUnavailableError,
    SweepIncompleteError,
    tile_frac_to_lonlat,
)
from tests.test_panoramax import encode_tile, make_picture

# Centred on a z15 tile x-boundary so the bbox spans two tiles and a border
# picture lands in both — the arrangement cross-tile dedup needs.
SEATTLE = (47.6062, -122.3321)


@pytest.fixture
def straddling_city():
    from streetscape_metadata_tracker.download_common import (
        lonlat_to_tile_frac,
        tile_frac_to_lonlat,
    )

    lat = SEATTLE[0]
    fx, fy = lonlat_to_tile_frac(SEATTLE[1], lat, dp.TILE_ZOOM)
    boundary_lon, _ = tile_frac_to_lonlat(int(fx), fy, dp.TILE_ZOOM)
    return lat, boundary_lon


def _stub_fetch_tile(monkeypatch, fetch):
    """
    Install ``fetch`` as the tile fetcher, honouring the limiter and the request
    counter on the stub's behalf.

    Stubbing ``_fetch_tile`` also stubs out its retry decorator, which is what
    keeps these instant — but #198 moved pacing and counting INSIDE that
    function deliberately, so a stub ignoring them would leave every city-level
    test seeing zero requests. Tests about STATUS CODES do not use this: they go
    through :func:`_run`, which keeps the real `_fetch_tile` (issue #407).
    """

    async def paced(session, url, timeout, rate_limiter=None, on_request=None, on_empty=None):
        if rate_limiter is not None:
            await rate_limiter.acquire()
        if on_request is not None:
            on_request()
        return await fetch(session, url, timeout)

    monkeypatch.setattr(dp, "_fetch_tile", paced)


def _tile_xy_from_url(url):
    _, _, tail = url.rpartition("/map/")
    zoom, x, y = tail.replace(".mvt", "").split("/")
    assert int(zoom) == dp.TILE_ZOOM, f"a tile was requested at z{zoom}, not z{dp.TILE_ZOOM}"
    return int(x), int(y)


class _TileResponse:
    def __init__(self, status, body=b""):
        self.status = status
        # What the host sends: a vector tile under its own type, and NO body
        # (and so nothing worth typing) under a 204 or a 404.
        self.headers = {"Content-Type": "application/vnd.mapbox-vector-tile"} if body else {}
        self._body = body

    async def read(self):
        return self._body

    def raise_for_status(self):
        if self.status >= 400:
            raise RuntimeError(f"HTTP {self.status} reached raise_for_status")


class _TileSession:
    """
    An in-memory ``aiohttp.ClientSession`` answering each tile URL with a status.

    THE REAL `_fetch_tile` RUNS AGAINST THIS (issue #407). Every status-code
    test used to stub `_fetch_tile` and call ``on_empty`` by hand, which is how
    the suite stayed green while the host's real empty-tile answer -- 204 --
    went through no branch that counted it: a stub decides the classification,
    so it cannot test it.
    """

    def __init__(self, answer):
        self._answer = answer

    def get(self, url, **kwargs):
        response = self._answer(url)

        class _Ctx:
            async def __aenter__(self):
                return response

            async def __aexit__(self, *exc):
                return False

        return _Ctx()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _run(
    monkeypatch,
    tmp_path,
    tiles_by_xy,
    center_lat,
    center_lon,
    *,
    missing=(),
    no_content=(),
    **kwargs,
):
    """
    One grid run through the REAL tile fetcher, over an in-memory session.

    ``missing`` tiles answer 404 and ``no_content`` tiles answer 204 (the host's
    empty tile); every other tile answers 200 with its payload from
    ``tiles_by_xy``, or with a tile carrying no picture layer.
    """
    served = []

    def answer(url):
        xy = _tile_xy_from_url(url)
        served.append(xy)
        # No credential rides in a Panoramax URL, which is the point.
        assert "access_token" not in url and "key=" not in url
        if xy in missing:
            return _TileResponse(404)
        if xy in no_content:
            return _TileResponse(204)
        return _TileResponse(200, tiles_by_xy.get(xy, mapbox_vector_tile.encode([])))

    session = _TileSession(answer)
    monkeypatch.setattr(dp.aiohttp, "ClientSession", lambda **kw: session)
    out_path = str(tmp_path / "test_panoramax_2026-09-06.csv.gz")
    result = asyncio.run(
        dp.download_panoramax_metadata_async(
            "Test City",
            center_lat,
            center_lon,
            kwargs.pop("width", 100),
            kwargs.pop("height", 100),
            kwargs.pop("step", 20),
            out_path,
            **kwargs,
        )
    )
    return result, served, out_path


def _written(path):
    with gzip.open(path, "rt", encoding="utf-8") as f:
        return pd.read_csv(f, dtype=str, keep_default_na=False)


# ── The bindings the tail actually reaches ─────────────────────────────────


def test_the_csv_carries_PANORAMAXS_schema_in_its_own_column_order(
    monkeypatch, tmp_path, straddling_city
):
    """
    The headline: a wrapper handed another provider's dtypes writes a perfectly
    valid CSV with the wrong columns, under a filename that says panoramax. The
    column ORDER is asserted too, because `pd.DataFrame(..., columns=...)`
    reorders silently and a run file is compared byte-for-byte to its
    predecessor by `diff.py`.
    """
    lat, lon = straddling_city
    tiles = dp.tiles_for_bbox(*dp.grid_bbox(lat, lon, 100, 100, 20))
    served_tile = encode_tile([make_picture("p1", lon, lat)], *tiles[0])

    _, _, path = _run(monkeypatch, tmp_path, {tiles[0]: served_tile}, lat, lon)
    assert list(_written(path).columns) == list(PANORAMAX_METADATA_DTYPES)


def test_a_360_picture_a_flat_only_point_and_an_empty_point_are_three_statuses(
    monkeypatch, tmp_path, straddling_city
):
    """
    #116's stratification, reached through this provider's bindings: a grid
    point with a pano is OK, one covered ONLY by flat imagery is FLAT_ONLY with
    a NULL capture date (so flat timestamps never enter a dated statistic), and
    one with neither is ZERO_RESULTS.

    Panoramax needs this more than Mapillary does, not less: the 360 share is a
    per-city property running from 99.5% in Des Moines to 0% in Tulsa and Boise,
    so a collector reporting one coverage number misdescribes half the cities
    worth collecting.
    """
    lat, lon = straddling_city
    tiles = dp.tiles_for_bbox(*dp.grid_bbox(lat, lon, 100, 100, 20))
    raw = encode_tile(
        [
            make_picture("pano", lon, lat, image_type="equirectangular"),
            # ~20 m north: the next grid row, covered by flat imagery only.
            make_picture("flat", lon, lat + 0.00018, image_type="flat"),
        ],
        *tiles[0],
    )
    result, _, path = _run(monkeypatch, tmp_path, {tiles[0]: raw}, lat, lon)
    written = _written(path)

    statuses = set(written["status"])
    assert {"OK", "FLAT_ONLY", "ZERO_RESULTS"} <= statuses

    flat_rows = written[written["status"] == analysis.FLAT_ONLY]
    assert len(flat_rows) >= 1
    assert (flat_rows["capture_date"] == "").all(), (
        "a FLAT_ONLY row carries the picture as a presence marker but must never "
        "carry its date, or flat timestamps enter every dated statistic"
    )
    assert result["num_flat_images"] >= 1


def test_the_epoch_sentinel_lands_as_NO_DATE_rather_than_removing_the_picture(
    monkeypatch, tmp_path, straddling_city
):
    """
    Panoramax's measured sentinel is a 1970 timestamp, and what it must produce
    is a COVERED point with no date -- `NO_DATE` is in `PRESENT_STATUSES`, so the
    imagery still counts toward coverage while ageing nothing.

    Dropping the row instead would understate coverage; keeping the date would
    put 1970 into every capture-date statistic the city has.
    """
    lat, lon = straddling_city
    tiles = dp.tiles_for_bbox(*dp.grid_bbox(lat, lon, 100, 100, 20))
    raw = encode_tile([make_picture("old", lon, lat, ts="1970-01-01 00:00:00+00")], *tiles[0])

    _, _, path = _run(monkeypatch, tmp_path, {tiles[0]: raw}, lat, lon)
    written = _written(path)
    row = written[written["pano_id"] == "old"]
    assert len(row) == 1
    assert row.iloc[0]["status"] == "NO_DATE"
    assert row.iloc[0]["capture_date"] == ""
    assert "NO_DATE" in analysis.PRESENT_STATUSES


def test_an_undownloaded_tile_marks_its_points_REQUEST_FAILED_not_ZERO_RESULTS(
    monkeypatch, tmp_path, straddling_city
):
    """
    #168's rule, and the `unmeasured_mask` binding is the only place a provider
    can get it wrong. ZERO_RESULTS is a claim that the provider was asked and
    said no; a tile that never downloaded supports no such claim, and publishing
    one into an immutable snapshot is unrecoverable.

    The mask must also be evaluated at THIS provider's zoom: at z14 it would
    select a four-times-larger square and mark measured points unknown.
    """
    lat, lon = straddling_city
    tiles = dp.tiles_for_bbox(*dp.grid_bbox(lat, lon, 100, 100, 20))
    assert len(tiles) >= 2, "this fixture is meant to straddle a tile boundary"

    failing = tiles[0]

    async def fake_fetch(session, url, timeout):
        if _tile_xy_from_url(url) == failing:
            raise RuntimeError("tile did not download")
        return mapbox_vector_tile.encode([])

    _stub_fetch_tile(monkeypatch, fake_fetch)
    out_path = str(tmp_path / "test_panoramax_2026-09-06.csv.gz")
    # One failed tile of two is 50%, far over the 2% tolerance, so this run must
    # refuse rather than publish a half-measured city.
    with pytest.raises(DownloadError, match="refusing to finalize"):
        asyncio.run(
            dp.download_panoramax_metadata_async("Test City", lat, lon, 100, 100, 20, out_path)
        )

    # And the mask itself, at the right zoom: a point inside the failed tile is
    # selected, one outside it is not.
    import numpy as np

    from streetscape_metadata_tracker.download_common import tile_frac_to_lonlat

    inside_lon, inside_lat = tile_frac_to_lonlat(failing[0] + 0.5, failing[1] + 0.5, dp.TILE_ZOOM)
    other = tiles[-1]
    outside_lon, outside_lat = tile_frac_to_lonlat(other[0] + 0.5, other[1] + 0.5, dp.TILE_ZOOM)
    mask = dp._points_in_tiles(
        np.array([inside_lat, outside_lat]), np.array([inside_lon, outside_lon]), [failing]
    )
    assert list(mask) == [True, False]


# The moved-endpoint GUARD's own wording. Not "has moved or been renamed": each
# 404 also raises TileNotServedError saying that, so the generic tile-failure
# refusal carries the phrase too, and a match on it stayed green with the guard
# deleted (found by mutation, #407).
_MOVED_ENDPOINT = r"Every one of the \d+ Panoramax tiles requested for .* answered HTTP 404"


def test_a_lattice_that_answers_404_EVERYWHERE_is_refused_rather_than_published(
    monkeypatch, tmp_path, straddling_city
):
    """
    THE MOVED-ENDPOINT GUARD, REACHED THROUGH THE REAL FETCHER (issue #407).
    This host answers an empty tile with 204 and never 404s a tile route, so a
    whole lattice of 404s is not an empty city -- it is what a MOVED OR RENAMED
    endpoint looks like, and it is refused by that name rather than as a pile
    of failed tiles, so the operator is pointed at the URL and not at the night.
    """
    lat, lon = straddling_city
    tiles = dp.tiles_for_bbox(*dp.grid_bbox(lat, lon, 100, 100, 20))
    with pytest.raises(DownloadError, match=_MOVED_ENDPOINT):
        _run(monkeypatch, tmp_path, {}, lat, lon, missing=set(tiles))


def test_a_404_LATTICE_is_still_refused_on_the_NIGHT_AFTER_it_checkpointed(
    monkeypatch, tmp_path, straddling_city
):
    """
    THE GUARD ABOVE HAS TO SURVIVE THE CHECKPOINT, and the evidence it reads is
    per-invocation: it asks whether everything REQUESTED answered 404. So a 404
    must not be committed. If it were, the run that correctly refuses would
    leave every tile recorded fetched-and-empty, and the NEXT invocation would
    find `todo` empty, never evaluate the guard, and re-finalize the city from
    disk as a genuine ZERO_RESULTS snapshot for zero requests -- publishing
    "every pano in the city removed" through the very path the guard exists to
    close, and promoting that empty census into the shared #290 cache.

    The sibling test above passes without a checkpoint, which is exactly the
    blind spot: the CLI always passes one (`crawl_store_for` returns a store for
    every CENSUS_PROVIDERS member), so the unchecked path is the only one that
    ever runs in production.
    """
    lat, lon = straddling_city
    tiles = dp.tiles_for_bbox(*dp.grid_bbox(lat, lon, 100, 100, 20))
    assert len(tiles) >= 2, "the guard is bounded by len(todo) >= 2"
    checkpoint_path = str(tmp_path / "crawl")

    for night in (1, 2):
        with pytest.raises(DownloadError, match=_MOVED_ENDPOINT):
            _run(
                monkeypatch,
                tmp_path,
                {},
                lat,
                lon,
                missing=set(tiles),
                checkpoint_path=checkpoint_path,
                checkpoint_channel="panoramax",
            )
        done = dp._open_tile_checkpoint(
            checkpoint_path,
            bbox=dp.grid_bbox(lat, lon, 100, 100, 20),
            tiles=tiles,
            channel="panoramax",
            variant=None,
        )
        assert done.done == {}, (
            f"night {night} committed a 404 tile; the next invocation would find "
            f"nothing to do and publish the city as empty"
        )


def test_one_404_among_answered_tiles_is_an_UNREAD_tile_not_a_moved_endpoint(
    monkeypatch, tmp_path, straddling_city
):
    """
    The bound on the guard above, and the other half of what a 404 now means.

    A single 404 is not a moved endpoint, so the city is not refused by that
    name -- it matters most on a RESUME, where a run finishing its last tile
    asks for exactly one. But it is not an empty tile either (issue #407): the
    ground under it was never read, so its points publish REQUEST_FAILED under
    #168's tolerance, never ZERO_RESULTS. Before #407 they published as a
    MEASURED absence, which no later reader can tell from a real one.

    The tolerance is raised for this test because the fixture is a handful of
    tiles; one of four is far over the 2% a real city's lattice allows.
    """
    lat, lon = straddling_city
    tiles = dp.tiles_for_bbox(*dp.grid_bbox(lat, lon, 100, 100, 20))
    monkeypatch.setattr(dp, "MAX_FAILED_TILE_FRACTION", 0.99)
    result, _, path = _run(monkeypatch, tmp_path, {}, lat, lon, missing={tiles[0]})
    statuses = set(_written(path)["status"])
    assert "REQUEST_FAILED" in statuses, (
        f"a 404 tile's points must be unmeasured, not empty; got {statuses}"
    )
    assert "ZERO_RESULTS" in statuses, "the tiles that answered are still measured"
    assert result["api_requests"] == len(tiles)


def test_one_404_at_the_default_tolerance_is_refused_as_failed_tiles_not_as_a_move(
    monkeypatch, tmp_path, straddling_city
):
    """The same lone 404 against the real 2% tolerance: refused, and by the
    generic tile-failure message -- the moved-endpoint wording is reserved for a
    lattice where EVERY tile 404s."""
    lat, lon = straddling_city
    tiles = dp.tiles_for_bbox(*dp.grid_bbox(lat, lon, 100, 100, 20))
    with pytest.raises(DownloadError, match="tiles failed") as excinfo:
        _run(monkeypatch, tmp_path, {}, lat, lon, missing={tiles[0]})
    assert "has moved or been renamed — refusing" not in str(excinfo.value)


def test_a_lattice_of_204s_is_an_EMPTY_CITY_and_publishes_rather_than_refusing(
    monkeypatch, tmp_path, straddling_city
):
    """
    THE HOST'S REAL EMPTY-TILE ANSWER (issue #407), over every tile.

    204 is what the meta-catalog sends for a tile holding no pictures, and 730
    of 1,144 catalog cities genuinely hold none, so a lattice of them is a true
    answer and must collect and publish as ZERO_RESULTS. The guard above keys on
    the 404 for exactly this reason; keyed on emptiness it would fail two
    thirds of the catalog.
    """
    lat, lon = straddling_city
    tiles = dp.tiles_for_bbox(*dp.grid_bbox(lat, lon, 100, 100, 20))
    assert len(tiles) >= 2, "the guard's bound is two tiles; this must clear it"
    result, served, path = _run(monkeypatch, tmp_path, {}, lat, lon, no_content=set(tiles))
    assert set(_written(path)["status"]) == {"ZERO_RESULTS"}
    assert result["api_requests"] == len(tiles) == len(served)


def test_an_empty_city_that_answers_200_is_published_as_empty(
    monkeypatch, tmp_path, straddling_city
):
    """
    The other empty answer: a 200 whose body carries no picture layer, which is
    what phase 1 saw at z14 (where the layer is not served at all). Equally a
    measured absence, and it must publish as ZERO_RESULTS exactly as the 204
    lattice above does.
    """
    lat, lon = straddling_city
    _, _, path = _run(monkeypatch, tmp_path, {}, lat, lon)
    written = _written(path)
    assert set(written["status"]) == {"ZERO_RESULTS"}


def test_204_tiles_beside_real_imagery_change_nothing_about_the_imagery(
    monkeypatch, tmp_path, straddling_city
):
    """
    The common shape over a real bbox: most z15 tiles are empty and a few carry
    pictures. The 204s must read as empty ground, and the picture-bearing tile
    must publish exactly as it would have alone.
    """
    lat, lon = straddling_city
    tiles = dp.tiles_for_bbox(*dp.grid_bbox(lat, lon, 100, 100, 20))
    served_tile = encode_tile([make_picture("only", lon, lat)], *tiles[0])
    _, _, path = _run(
        monkeypatch, tmp_path, {tiles[0]: served_tile}, lat, lon, no_content=set(tiles[1:])
    )
    written = _written(path)
    assert "OK" in set(written["status"]), "the served picture must still be found"
    assert "REQUEST_FAILED" not in set(written["status"]), "a 204 is an answer, not a hole"


def test_a_204_is_COMMITTED_and_a_404_is_NOT(monkeypatch, tmp_path, straddling_city):
    """
    WHY 204 AND 404 MUST BE TOLD APART AT THE CHECKPOINT.

    A 204 is a settled observation, so it is committed as a zero-row tile; at
    z15 most tiles over a real bbox are empty, and holding them back would leave
    every city's checkpoint incomplete and every census-cache entry refused. A
    404 is an unread tile, so it stays uncommitted and is asked again next time
    -- which is also what keeps the moved-endpoint guard from being spent by
    the run that fires it (the night-after test above).
    """
    lat, lon = straddling_city
    tiles = dp.tiles_for_bbox(*dp.grid_bbox(lat, lon, 100, 100, 20))
    bbox = dp.grid_bbox(lat, lon, 100, 100, 20)
    checkpoint_path = str(tmp_path / "crawl")
    # One 404 tile among 204s; the tolerance is raised so the run finishes.
    # No census cache is passed, so nothing is promoted and the checkpoint is
    # left behind to be read. (With a cache it WOULD promote, failed tile and
    # all -- see the promotion test below.)
    monkeypatch.setattr(dp, "MAX_FAILED_TILE_FRACTION", 0.99)
    _run(
        monkeypatch,
        tmp_path,
        {},
        lat,
        lon,
        missing={tiles[0]},
        no_content=set(tiles[1:]),
        checkpoint_path=checkpoint_path,
        checkpoint_channel="panoramax",
    )
    done = dp._open_tile_checkpoint(
        checkpoint_path, bbox=bbox, tiles=tiles, channel="panoramax", variant=None
    ).done
    assert done == dict.fromkeys(tiles[1:], 0), (
        f"every 204 tile must be committed as zero rows and the 404 tile must not; got {done}"
    )


def test_the_walks_census_reports_a_404_tile_as_FAILED_and_a_204_tile_as_not(
    monkeypatch, straddling_city
):
    """
    The road walk (#331) reads `fetch_city_images_async`'s ``failed_tiles`` to
    mark samples REQUEST_FAILED rather than ZERO_RESULTS, so this is the walk's half of
    the rule, pinned at the seam it consumes: the 404 tile is in the list, the
    204 tile is not.
    """
    lat, lon = straddling_city
    bbox = dp.grid_bbox(lat, lon, 100, 100, 20)
    tiles = dp.tiles_for_bbox(*bbox)
    monkeypatch.setattr(dp, "MAX_FAILED_TILE_FRACTION", 0.99)

    def answer(url):
        xy = _tile_xy_from_url(url)
        return _TileResponse(404) if xy == tiles[0] else _TileResponse(204)

    monkeypatch.setattr(dp.aiohttp, "ClientSession", lambda **kw: _TileSession(answer))
    fetched = asyncio.run(dp.fetch_city_images_async("Test City", bbox))
    assert fetched["failed_tiles"] == [tiles[0]]
    assert len(fetched["census"]) == 0


# ── The two counters, and where each one goes ──────────────────────────────


def test_the_two_request_counters_are_reported_separately(monkeypatch, tmp_path, straddling_city):
    """
    `api_requests` is THIS call's spend, for the additive (date, provider)
    ledger; `api_requests_total` is the whole crawl's, for the catalog row. On a
    fresh uninterrupted run they agree, which is exactly why a wrapper that
    reported one for both would look correct here — so the tile COUNT is
    asserted too, tying both numbers to the lattice actually walked.
    """
    lat, lon = straddling_city
    tiles = dp.tiles_for_bbox(*dp.grid_bbox(lat, lon, 100, 100, 20))
    result, served, _ = _run(monkeypatch, tmp_path, {}, lat, lon)
    assert result["api_requests"] == len(tiles) == len(served)
    assert result["api_requests_total"] == result["api_requests"]


def test_a_failure_after_the_fetch_still_carries_the_spend(monkeypatch, tmp_path, straddling_city):
    """
    The census is paid for before the CSV is written, so a crash in the tail
    must not lose the ledger entry — cli.py records `e.api_requests` from the
    exception. Without the wrapper's try/except that spend would vanish and the
    night would under-report what it cost.
    """
    lat, lon = straddling_city

    async def fake_fetch(session, url, timeout):
        return mapbox_vector_tile.encode([])

    _stub_fetch_tile(monkeypatch, fake_fetch)

    def explode(*args, **kwargs):
        raise OSError("no space left on device")

    monkeypatch.setattr(dp.census_core, "write_census_grid_run", explode)
    with pytest.raises(OSError) as excinfo:
        asyncio.run(
            dp.download_panoramax_metadata_async(
                "Test City", lat, lon, 100, 100, 20, str(tmp_path / "x_2026-09-06.csv.gz")
            )
        )
    assert excinfo.value.api_requests > 0
    assert excinfo.value.api_requests_total == excinfo.value.api_requests


# ── #407 review: the guard is never stricter than the tolerance ─────────────
#
# A 404 is never committed, so a re-run only re-asks for the tiles that 404ed.
# The moved-endpoint guard used to read THIS invocation's `todo`, and refused
# exactly those re-runs. Each route that produces one is tested: a plain resume
# from a retained checkpoint, and the crawl's own cache entry handed back by
# `reconcile_cache_hit` so its failed tiles are re-probed.


def _night(monkeypatch, tmp_path, lat, lon, *, missing, tolerance, **kwargs):
    monkeypatch.setattr(dp, "MAX_FAILED_TILE_FRACTION", tolerance)
    return _run(monkeypatch, tmp_path, {}, lat, lon, missing=missing, **kwargs)


def test_a_RESUME_whose_remaining_tiles_all_404_is_priced_by_the_tolerance_not_refused(
    monkeypatch, tmp_path, straddling_city
):
    """
    Night 1 publishes with two 404 tiles inside the tolerance and keeps its
    checkpoint (no cache, so nothing is promoted); night 2 asks for those two
    alone, both 404 again. The tolerance accepted this city once and nothing has
    changed, so it must accept it again -- keyed on `todo` the guard refused it
    as a moved endpoint, a counted failure every night until the seven-day wall.
    (The tolerance is raised because the fixture is four tiles: two of four
    stands in for the measured case, two of 324.)
    """
    lat, lon = straddling_city
    tiles = dp.tiles_for_bbox(*dp.grid_bbox(lat, lon, 100, 100, 20))
    assert len(tiles) >= 3, "the resume must leave committed tiles beside the two 404s"
    bad = set(tiles[:2])
    checkpoint = dict(checkpoint_path=str(tmp_path / "crawl"), checkpoint_channel="panoramax")
    _night(monkeypatch, tmp_path, lat, lon, missing=bad, tolerance=0.99, **checkpoint)

    result, served, path = _night(
        monkeypatch, tmp_path, lat, lon, missing=bad, tolerance=0.99, **checkpoint
    )
    assert sorted(served) == sorted(bad), "night 2 must re-ask for the 404 tiles only"
    assert "REQUEST_FAILED" in set(_written(path)["status"])
    assert result["api_requests"] == 2


def test_a_resume_of_404s_OVER_the_tolerance_is_refused_as_failed_tiles_naming_the_endpoint(
    monkeypatch, tmp_path, straddling_city
):
    """The same resume against the real 2% tolerance: refused by the tolerance,
    which says the endpoint has probably moved because every failure it counted
    was a 404 -- and NOT by the whole-lattice guard, whose wording is reserved
    for a lattice in which every tile 404ed."""
    lat, lon = straddling_city
    tiles = dp.tiles_for_bbox(*dp.grid_bbox(lat, lon, 100, 100, 20))
    bad = set(tiles[:2])
    checkpoint = dict(checkpoint_path=str(tmp_path / "crawl"), checkpoint_channel="panoramax")
    _night(monkeypatch, tmp_path, lat, lon, missing=bad, tolerance=0.99, **checkpoint)

    with pytest.raises(DownloadError, match="tiles failed") as excinfo:
        _night(monkeypatch, tmp_path, lat, lon, missing=bad, tolerance=0.02, **checkpoint)
    message = str(excinfo.value)
    assert "Every failed tile answered HTTP 404" in message
    assert not re.search(_MOVED_ENDPOINT, message)


def test_a_CACHE_HANDBACK_of_the_crawls_own_404s_is_priced_by_the_tolerance_not_refused(
    monkeypatch, tmp_path, straddling_city
):
    """
    The other route to a two-tile `todo`. Night 1 promotes into the census cache
    with its 404 tiles recorded as failed (item 3 below pins that); night 2 is
    the same channel, so `reconcile_cache_hit` hands the entry back to the
    checkpoint path and the crawl re-probes only its failed tiles. Both 404
    again, and the city must publish exactly as it did on night 1.
    """
    lat, lon = straddling_city
    tiles = dp.tiles_for_bbox(*dp.grid_bbox(lat, lon, 100, 100, 20))
    bad = set(tiles[:2])
    store = dict(
        checkpoint_path=str(tmp_path / "crawl"),
        checkpoint_channel="panoramax",
        census_cache=CensusCache(str(tmp_path / "cache")),
    )
    _night(monkeypatch, tmp_path, lat, lon, missing=bad, tolerance=0.99, **store)

    result, served, _ = _night(
        monkeypatch, tmp_path, lat, lon, missing=bad, tolerance=0.99, **store
    )
    assert sorted(served) == sorted(bad), "the handback must re-probe the failed tiles only"
    assert result["api_requests"] == 2


def _one_tile_city():
    """A centre in the MIDDLE of a z15 tile, and a grid small enough to stay in it."""
    x, y = dp.tiles_for_bbox(*dp.grid_bbox(SEATTLE[0], SEATTLE[1], 20, 20, 20))[0]
    lon, lat = tile_frac_to_lonlat(x + 0.5, y + 0.5, dp.TILE_ZOOM)
    assert dp.tiles_for_bbox(*dp.grid_bbox(lat, lon, 40, 40, 20)) == [(x, y)]
    return lat, lon, (x, y)


def test_a_ONE_TILE_city_that_404s_is_refused_as_a_moved_endpoint(monkeypatch, tmp_path):
    """No lower bound on the whole-lattice guard: one tile that 404s is a city
    wholly unmeasured, and the refusal should say why rather than "1/1 tiles
    failed"."""
    lat, lon, tile = _one_tile_city()
    with pytest.raises(DownloadError, match=_MOVED_ENDPOINT):
        _run(monkeypatch, tmp_path, {}, lat, lon, missing={tile}, width=40, height=40)


# ── #407 review: a lone 404 promotes, and its walk reuse stays unmeasured ───


def test_a_lone_404_PROMOTES_with_its_tile_recorded_failed_and_the_walk_reuse_keeps_it_unmeasured(
    monkeypatch, tmp_path, straddling_city
):
    """
    A census with a tolerated 404 is complete in the #318 sense -- every tile
    fetched or recorded failed -- so it promotes into the #290 cache, and the
    marker carries the failed tile. That is what keeps the hole honest for the
    road walk reusing it for 0 requests: its samples under that tile publish
    REQUEST_FAILED, the rest ZERO_RESULTS, exactly as the grid run's points did.
    """
    from types import SimpleNamespace

    from streetscape_street_analyzer.collect_panoramax import (
        collect_panoramax_street_samples_async,
    )

    lat, lon = straddling_city
    tiles = dp.tiles_for_bbox(*dp.grid_bbox(lat, lon, 100, 100, 20))
    bad = tiles[0]
    cache = CensusCache(str(tmp_path / "cache"))
    grid, _, _ = _night(
        monkeypatch,
        tmp_path,
        lat,
        lon,
        missing={bad},
        tolerance=0.99,
        checkpoint_path=str(tmp_path / "crawl-grid"),
        checkpoint_channel="panoramax",
        census_cache=cache,
    )
    assert grid["checkpoint_path"] is None, "the census must have been promoted (the dir MOVED)"
    marker = load_census_cache_marker(cache.path, run_date=None)
    assert marker is not None
    assert marker["failed"] == [[bad[0], bad[1]]]

    # One sample at the centre of every tile, so each tile's verdict is visible.
    points = []
    for i, (x, y) in enumerate(tiles):
        p_lon, p_lat = tile_frac_to_lonlat(x + 0.5, y + 0.5, dp.TILE_ZOOM)
        points.append((p_lat, p_lon, i, 0))
    city = SimpleNamespace(
        city_id="test-city",
        display_name="Test City",
        center_lat=lat,
        center_lon=lon,
        grid_width_m=100,
        grid_height_m=100,
        step_m=20,
    )

    def no_request(url):
        raise AssertionError(f"a reused census must issue no request, asked for {url}")

    monkeypatch.setattr(dp.aiohttp, "ClientSession", lambda **kw: _TileSession(no_request))
    out = str(tmp_path / "walk.csv.gz")
    walk = asyncio.run(
        collect_panoramax_street_samples_async(
            points,
            city,
            out,
            match_dist_m=20,
            checkpoint_path=str(tmp_path / "crawl-walk"),
            checkpoint_channel="panoramax_streets",
            census_cache=cache,
        )
    )
    assert walk["census_reused"] is True
    assert walk["api_requests"] == 0
    statuses = list(_written(out)["status"])
    assert statuses[0] == "REQUEST_FAILED", "the walk sample under the 404 tile is unmeasured"
    assert set(statuses[1:]) == {"ZERO_RESULTS"}


# ── #407 review: the history-aware collapse guard ───────────────────────────


def test_an_EMPTY_census_is_REFUSED_when_the_caller_says_the_series_was_positive(
    monkeypatch, tmp_path, straddling_city
):
    """
    A lattice of 204s after a positive run: what a meta-catalog that lost its
    index produces, and what would publish as "every picture removed". Refused
    as a plain DownloadError -- not a per-IP refusal, not a resumable pause --
    and BEFORE promotion, so no walk can reuse the empty census for free.
    """
    lat, lon = straddling_city
    tiles = dp.tiles_for_bbox(*dp.grid_bbox(lat, lon, 100, 100, 20))
    cache = CensusCache(str(tmp_path / "cache"))
    with pytest.raises(dp.PanoramaxCollapseError, match="previous run of this series") as excinfo:
        _run(
            monkeypatch,
            tmp_path,
            {},
            lat,
            lon,
            no_content=set(tiles),
            checkpoint_path=str(tmp_path / "crawl"),
            checkpoint_channel="panoramax",
            census_cache=cache,
            refuse_empty_census=True,
        )
    error = excinfo.value
    assert isinstance(error, DownloadError)
    assert not isinstance(error, (HostUnavailableError, SweepIncompleteError))
    assert error.api_requests == len(tiles), "the refused crawl's spend must reach the ledger"
    assert load_census_cache_marker(cache.path, run_date=None) is None, (
        "a refused census must never be promoted into the shared cache"
    )


def test_a_refused_collapse_is_refused_AGAIN_when_re_finalized_from_its_checkpoint(
    monkeypatch, tmp_path, straddling_city
):
    """The 204 tiles are committed (they are answers), so the next invocation
    finds nothing to fetch and re-finalizes from disk. The guard reads the
    assembled census rather than this run's 204 count, so it still holds."""
    lat, lon = straddling_city
    tiles = dp.tiles_for_bbox(*dp.grid_bbox(lat, lon, 100, 100, 20))
    kwargs = dict(
        no_content=set(tiles),
        checkpoint_path=str(tmp_path / "crawl"),
        checkpoint_channel="panoramax",
        refuse_empty_census=True,
    )
    with pytest.raises(dp.PanoramaxCollapseError):
        _run(monkeypatch, tmp_path, {}, lat, lon, **kwargs)
    with pytest.raises(dp.PanoramaxCollapseError) as excinfo:
        _, served, _ = _run(monkeypatch, tmp_path, {}, lat, lon, **kwargs)
    assert excinfo.value.api_requests == 0, "the re-finalize issues no request"


def test_an_empty_CACHE_ENTRY_is_refused_on_reuse_for_a_positive_series(
    monkeypatch, tmp_path, straddling_city
):
    """An entry another consumer accepted (no positive history, or an override)
    must not publish unrefused through a consumer whose series WAS positive."""
    lat, lon = straddling_city
    bbox = dp.grid_bbox(lat, lon, 100, 100, 20)
    tiles = dp.tiles_for_bbox(*bbox)
    cache = CensusCache(str(tmp_path / "cache"))
    _run(
        monkeypatch,
        tmp_path,
        {},
        lat,
        lon,
        no_content=set(tiles),
        checkpoint_path=str(tmp_path / "crawl-grid"),
        checkpoint_channel="panoramax",
        census_cache=cache,
    )
    assert load_census_cache_marker(cache.path, run_date=None) is not None

    def no_request(url):
        raise AssertionError(f"a reused census must issue no request, asked for {url}")

    monkeypatch.setattr(dp.aiohttp, "ClientSession", lambda **kw: _TileSession(no_request))
    with pytest.raises(dp.PanoramaxCollapseError):
        asyncio.run(
            dp.fetch_city_images_async(
                "Test City",
                bbox,
                checkpoint_path=str(tmp_path / "crawl-walk"),
                checkpoint_channel="panoramax_streets",
                census_cache=cache,
                refuse_empty_census=True,
            )
        )


def test_an_empty_census_PUBLISHES_without_positive_history(monkeypatch, tmp_path, straddling_city):
    """The default -- no prior run, a prior zero, or an operator override all
    reach the downloader as refuse_empty_census=False -- publishes ZERO_RESULTS."""
    lat, lon = straddling_city
    tiles = dp.tiles_for_bbox(*dp.grid_bbox(lat, lon, 100, 100, 20))
    _, _, path = _run(
        monkeypatch, tmp_path, {}, lat, lon, no_content=set(tiles), refuse_empty_census=False
    )
    assert set(_written(path)["status"]) == {"ZERO_RESULTS"}


def test_a_census_WITH_imagery_is_never_refused_by_the_collapse_guard(
    monkeypatch, tmp_path, straddling_city
):
    lat, lon = straddling_city
    tiles = dp.tiles_for_bbox(*dp.grid_bbox(lat, lon, 100, 100, 20))
    served_tile = encode_tile([make_picture("only", lon, lat)], *tiles[0])
    _, _, path = _run(
        monkeypatch,
        tmp_path,
        {tiles[0]: served_tile},
        lat,
        lon,
        no_content=set(tiles[1:]),
        refuse_empty_census=True,
    )
    assert "OK" in set(_written(path)["status"])
