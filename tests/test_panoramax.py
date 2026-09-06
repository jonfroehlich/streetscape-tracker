"""
The Panoramax tile decoder, its date rules and its transport (issue #316).

Offline throughout: tiles are encoded in memory with the same coordinate math
the decoder inverts, and the transport tests drive `_fetch_tile` against a
minimal `aiohttp.ClientSession` stand-in rather than mocking HTTP. Nothing here
touches api.panoramax.xyz.

Three groups, each pinning a way this provider differs from the tile census it
is modelled on, because those are the differences a reader coming from
`download_mapillary` will assume away:

  * THE ZOOM IS 15, not 14. Panoramax's per-picture layer does not exist below
    z15, so a bbox costs ~4x the tiles and every derived cost figure moves with
    it. A default that silently reverted to 14 would collect NOTHING and publish
    it as a city with no imagery.
  * `type` IS THE SOURCE OF `is_pano`, kept verbatim beside it. The census
    schema declares `is_pano` NON-nullable, which is only honest because the
    field has no absent state -- so what is pinned is that an absent type
    produces flat rather than a null.
  * 403/429 IS A STOP AND 404 IS AN ANSWER. There is no credential here, so a
    403 cannot be a rejected token; and an empty area answers 200 with no layer,
    so a 404 means "nothing at this tile" -- which is why a whole lattice of
    them has to be refused rather than published.
"""

import asyncio
from datetime import UTC, datetime, timedelta

import aiohttp
import mapbox_vector_tile
import pandas as pd
import pytest
import yarl
from multidict import CIMultiDict, CIMultiDictProxy

from streetscape_metadata_tracker import config
from streetscape_metadata_tracker import download_panoramax as dp
from streetscape_metadata_tracker.census import QUERY_COLUMNS, census_is_pano
from streetscape_metadata_tracker.download_common import (
    HOST_BUSY_EXIT_CODES,
    HOST_EXIT_CODES,
    HOST_LABELS,
    HOST_PANORAMAX,
    DownloadError,
    HostBlockedError,
)

SEATTLE = (47.6062, -122.3321)


def encode_tile(features, tile_x, tile_y, zoom=dp.TILE_ZOOM, extent=4096, layer=None):
    """
    Raw MVT bytes for the `pictures` layer, inverting the decode path's math.

    Properties are passed through as given, so a test can omit one entirely --
    which is what a real tile does for an absent field, and is a different thing
    from sending it as null.
    """
    encoded = []
    for f in features:
        fx, fy = _lonlat_to_tile_frac(f["lon"], f["lat"], zoom)
        px = (fx - tile_x) * extent
        py = (1 - (fy - tile_y)) * extent  # y-up, matching decode()'s default
        encoded.append(
            {
                "geometry": {"type": "Point", "coordinates": [px, py]},
                "properties": {k: v for k, v in f.items() if k not in ("lon", "lat")},
            }
        )
    return mapbox_vector_tile.encode([{"name": layer or dp.PICTURE_LAYER, "features": encoded}])


def _lonlat_to_tile_frac(lon, lat, zoom):
    from streetscape_metadata_tracker.download_common import lonlat_to_tile_frac

    return lonlat_to_tile_frac(lon, lat, zoom)


def make_picture(
    picture_id, lon, lat, *, image_type="equirectangular", ts="2025-11-02 00:24:37+00"
):
    """One tile feature. `image_type=None` OMITS the property, as a real tile does."""
    picture = {"id": picture_id, "lon": lon, "lat": lat, "ts": ts}
    if image_type is not None:
        picture["type"] = image_type
    return picture


# ── 1. The zoom, which is the assumption a reader brings from Mapillary ─────


def test_the_default_zoom_is_15_because_the_picture_layer_starts_there():
    """
    Not a tunable, and getting it wrong is SILENT: below z15 the `pictures`
    layer is absent entirely, so every tile would decode to nothing and the run
    would publish a city holding no imagery rather than failing.
    """
    assert dp.TILE_ZOOM == 15


def test_tiles_for_bbox_defaults_to_this_providers_zoom_not_mapillarys():
    """
    Both providers expose a `tiles_for_bbox` with a default zoom, over ONE
    shared implementation that has none. This pins that the two defaults are
    different, which is the whole reason the shared one takes zoom as a required
    argument.
    """
    from streetscape_metadata_tracker import download_mapillary as dm
    from streetscape_metadata_tracker.download_common import tiles_for_bbox as shared

    bbox = dp.grid_bbox(*SEATTLE, 400, 400, 20)
    assert dp.tiles_for_bbox(*bbox) == shared(*bbox, 15)
    assert dm.tiles_for_bbox(*bbox) == shared(*bbox, 14)
    # And the z15 lattice really is finer, or the two defaults would be
    # interchangeable and this test would pin nothing.
    assert len(dp.tiles_for_bbox(*bbox)) > len(dm.tiles_for_bbox(*bbox))


def test_the_cost_estimate_counts_the_z15_lattice_over_the_frozen_grid():
    """
    The scheduler's cost hook, and the number this channel's per-city timeout
    would be derived from. Exact rather than estimated -- it counts the lattice
    the fetch will walk -- so it is asserted against that lattice rather than
    against a remembered figure.
    """
    lat, lon = SEATTLE
    assert dp.estimate_tile_count(lat, lon, 1000, 1000, 20) == len(
        dp.tiles_for_bbox(*dp.grid_bbox(lat, lon, 1000, 1000, 20))
    )


# ── 2. Decoding: `type` verbatim, `is_pano` derived, nothing absent ─────────


def test_the_raw_type_is_kept_beside_the_boolean_it_produces():
    """
    A run file records what the provider said. `type` is two-state across the
    federation's own totals today, but keeping it means a third value Panoramax
    has never yet served would show up in the data as itself rather than being
    counted as flat by the decoder and never seen again.
    """
    x, y = _tile_of(*SEATTLE)
    raw = encode_tile(
        [
            make_picture("a", -122.3321, 47.6062, image_type="equirectangular"),
            make_picture("b", -122.3322, 47.6063, image_type="flat"),
        ],
        x,
        y,
    )
    decoded = {p["id"]: p for p in dp.pictures_from_tile(raw, x, y)}
    assert decoded["a"]["image_type"] == dp.TYPE_360
    assert decoded["a"]["is_pano"] is True
    assert decoded["b"]["image_type"] == "flat"
    assert decoded["b"]["is_pano"] is False


def test_an_absent_type_decodes_as_FLAT_and_never_as_a_null():
    """
    The census schema declares `is_pano` NON-nullable "bool", which is only
    honest if the decoder can never produce a null -- and `census_is_pano`
    documents at length what the two nullable spellings cost (an `object` column
    of Python bools marks the ENTIRE city FLAT_ONLY without raising).

    Phase 1 measured 0 absent types over 1,345,143 pictures, so this is the
    belt to that: if Panoramax ever omits the field, the row is flat imagery
    with a recorded null `image_type`, not a null boolean.
    """
    x, y = _tile_of(*SEATTLE)
    raw = encode_tile([make_picture("a", -122.3321, 47.6062, image_type=None)], x, y)
    decoded = dp.pictures_from_tile(raw, x, y)
    assert len(decoded) == 1
    assert decoded[0]["is_pano"] is False
    assert decoded[0]["image_type"] is None

    census = dp.records_to_census(decoded)
    assert census["is_pano"].dtype == "bool"  # non-nullable, so `~` is safe
    assert not census_is_pano(census).any()


def test_the_sequence_comes_from_first_sequence_and_the_contributor_from_account_id():
    """The two tile property names differ from the column names they become, and
    a silent rename here would publish an all-null column rather than fail."""
    x, y = _tile_of(*SEATTLE)
    raw = encode_tile(
        [
            {
                "id": "a",
                "lon": -122.3321,
                "lat": 47.6062,
                "ts": "2025-11-02 00:24:37+00",
                "type": "equirectangular",
                "first_sequence": "seq-1",
                "account_id": "acct-1",
            }
        ],
        x,
        y,
    )
    decoded = dp.pictures_from_tile(raw, x, y)[0]
    assert decoded["sequence_id"] == "seq-1"
    assert decoded["account_id"] == "acct-1"


def test_an_empty_tile_and_empty_bytes_are_answers_not_errors():
    """
    Most z15 tiles over a real bbox hold nothing, so this is the common case
    rather than an edge one -- and a raise here would fail a city over its own
    empty water.
    """
    x, y = _tile_of(*SEATTLE)
    assert dp.pictures_from_tile(b"", x, y) == []
    assert dp.pictures_from_tile(mapbox_vector_tile.encode([]), x, y) == []
    # A tile carrying only some OTHER layer is the same answer.
    assert dp.pictures_from_tile(encode_tile([], x, y, layer="sequences"), x, y) == []


def test_a_picture_with_no_id_is_dropped_rather_than_given_the_TILE_LOCAL_one():
    """
    `dedupe_census` factorizes on `id`, and an MVT feature id is numbered PER
    TILE -- so falling back to it (which `download_mapillary`'s decoder does)
    would mint id "0" in every tile of the city and silently collapse those
    distinct pictures into one.

    This test caught exactly that: the fallback was copied across with the rest
    of the decoder's shape, and mapbox_vector_tile supplies a feature id even
    when the properties carry none, so nothing else would have shown it.
    """
    x, y = _tile_of(*SEATTLE)
    raw = encode_tile(
        [
            {"lon": -122.3321, "lat": 47.6062, "ts": "2025-01-01 00:00:00+00"},
            make_picture("a", -122.3322, 47.6063),
        ],
        x,
        y,
    )
    assert [p["id"] for p in dp.pictures_from_tile(raw, x, y)] == ["a"]


# ── 3. Capture dates: the floor, the sentinel, and the format pin ───────────


@pytest.mark.parametrize(
    "ts",
    [
        "2025-11-02 00:24:37+00",  # the shape the tiles actually serve
        "2015-06-01T12:00:00Z",  # ISO with T and Z
        "2026-08-27 21:27:43.123+00",  # fractional seconds
        "1970-01-01 00:00:00+00",  # the measured epoch sentinel
        "",
        None,
        "not a date",
    ],
)
def test_the_scalar_and_vectorized_date_rules_agree(ts):
    """
    The scalar form is the readable statement of the rules and the vectorized
    one is what a collection actually calls, so they are pinned element-wise --
    the same contract `captured_at_to_iso_date(s)` carries for Mapillary. They
    diverge exactly where a whole-column parse behaves differently from a
    per-value one, which is the bug this pair exists to catch.
    """
    assert dp.ts_to_iso_dates([ts]).tolist() == [dp.ts_to_iso_date(ts)]


def test_the_epoch_sentinel_is_dropped_by_the_floor():
    """
    Panoramax's first known sentinel, measured: two Paris pictures stamped
    1970-01-01. It is not a null and no null check catches it -- it is a
    perfectly well-formed timestamp that cannot be a capture date.
    """
    assert dp.ts_to_iso_date("1970-01-01 00:00:00+00") == ""
    assert dp.ts_to_iso_date("2003-12-31 00:00:00+00") == ""
    assert dp.ts_to_iso_date("2004-01-01 00:00:00+00") == "2004-01-01"


def test_a_capture_date_in_the_future_is_dropped():
    """Nothing can be captured after the query that saw it."""
    tomorrow = (datetime.now(UTC) + timedelta(days=1)).isoformat()
    assert dp.ts_to_iso_date(tomorrow) == ""


def test_MIXED_PRECISION_IN_ONE_COLUMN_SURVIVES_the_parse():
    """
    THE REASON `format="ISO8601"` IS PINNED IN THE PARSER, and the one property
    here that fails silently rather than loudly.

    Left to infer, pandas locks onto ONE format from the first non-null value
    and coerces every value at another precision to NaT -- so a whole-second
    timestamp beside a fractional-second one nulls whichever came second, and
    `errors="coerce"` is what makes that survivable instead of noisy. Same
    defect as #226 from one direction and KartaView's mixed-precision pages
    from another.
    """
    mixed = ["2025-11-02 00:24:37+00", "2026-08-27 21:27:43.123+00", "2015-06-01T12:00:00Z"]
    assert dp.ts_to_iso_dates(mixed).tolist() == ["2025-11-02", "2026-08-27", "2015-06-01"]
    # And the failure it prevents, demonstrated against the same input, so this
    # test cannot pass by pinning a distinction that does not exist.
    inferred = pd.to_datetime(pd.Series(mixed), utc=True, errors="coerce")
    assert inferred.isna().any()


def test_the_floor_is_read_from_analysis_rather_than_spelled_twice():
    """A decode-time floor that drifted from the readers' would drop rows the
    catalog then re-admits, or the reverse. Both existing census providers read
    it from the same table."""
    from streetscape_metadata_tracker.analysis import EARLIEST_PLAUSIBLE_CAPTURE

    assert dp._EARLIEST_CAPTURE is EARLIEST_PLAUSIBLE_CAPTURE["panoramax"]


# ── 4. The census bindings' contract with the shared seam ───────────────────


def test_the_image_columns_binding_supplies_exactly_the_providers_own_columns():
    """
    `census._check_image_columns` enforces this, and it is asserted here too
    because the three ways to get it wrong are each silent in the artifact: an
    omitted column publishes all-null, a misspelled one vanishes, and one
    colliding with the shared core overwrites `pano_lat`.
    """
    core = set(QUERY_COLUMNS) | {"pano_lat", "pano_lon", "pano_id", "capture_date"}
    expected = set(config.PANORAMAX_METADATA_DTYPES) - core

    picked = dp.records_to_census(
        [
            {
                "id": "a",
                "lon": -122.3,
                "lat": 47.6,
                "ts": "2025-11-02 00:24:37+00",
                "image_type": "equirectangular",
                "is_pano": True,
                "account_id": "acct-1",
                "sequence_id": "seq-1",
            }
        ]
    )
    assert set(dp._panoramax_image_columns(picked)) == expected


def test_the_copyright_string_carries_the_contributor_and_degrades_to_the_platform():
    """
    Parity with Mapillary's `creator_id` and KartaView's `username`. It is NOT
    the licence: Panoramax publishes per-picture licences only through
    `/api/search`, which cannot be the census, so the honest thing a tile can
    say is who took the picture.
    """
    picked = dp.records_to_census(
        [
            {
                "id": "a",
                "lon": -122.3,
                "lat": 47.6,
                "ts": "2025-11-02 00:24:37+00",
                "image_type": "flat",
                "is_pano": False,
                "account_id": "acct-1",
                "sequence_id": None,
            },
            {
                "id": "b",
                "lon": -122.3,
                "lat": 47.6,
                "ts": "2025-11-02 00:24:37+00",
                "image_type": "flat",
                "is_pano": False,
                "account_id": None,
                "sequence_id": None,
            },
        ]
    )
    copyright_info = list(dp._panoramax_image_columns(picked)["copyright_info"])
    assert copyright_info[0] == "© Panoramax contributor acct-1"
    # A missing account must not render "<NA>" into a published column.
    assert copyright_info[1] == "© Panoramax"


def test_the_capture_date_binding_indexes_positions_rather_than_taking_a_frame():
    """
    The seam hands a provider POSITIONS precisely so it can index the one or two
    date columns it needs; a `.take()` here would materialize every column of a
    multi-million-row census a second time (issue #157).
    """
    import numpy as np

    census = dp.records_to_census(
        [
            {
                "id": str(i),
                "lon": -122.3,
                "lat": 47.6,
                "ts": ts,
                "image_type": "equirectangular",
                "is_pano": True,
                "account_id": None,
                "sequence_id": None,
            }
            for i, ts in enumerate(["2025-11-02 00:24:37+00", "1970-01-01 00:00:00+00"])
        ]
    )
    dates = dp._panoramax_capture_dates(census, np.array([1, 0]))
    assert list(dates) == ["", "2025-11-02"]


# ── 5. Transport: what stops, what retries, what is merely empty ───────────


_TILE_URL = "https://api.panoramax.xyz/api/map/15/5241/11447.mvt"


class _FakeTileResponse:
    def __init__(self, status, headers=None, body=b""):
        self.status = status
        self.headers = headers or {}
        self._body = body

    async def read(self):
        return self._body

    def raise_for_status(self):
        """As aiohttp does it — without this the fake cannot reach the 4xx/5xx
        path and a test aimed at a block fails with an AttributeError instead of
        its own assertion."""
        if self.status >= 400:
            request_info = aiohttp.RequestInfo(
                url=yarl.URL(_TILE_URL),
                method="GET",
                headers=CIMultiDictProxy(CIMultiDict()),
                real_url=yarl.URL(_TILE_URL),
            )
            raise aiohttp.ClientResponseError(
                request_info=request_info, history=(), status=self.status, message="error"
            )


class _FakeTileSession:
    """Minimal aiohttp.ClientSession stand-in: .get() as an async CM."""

    def __init__(self, response):
        self._response = response
        self.get_kwargs = []
        self.calls = 0

    def get(self, url, **kwargs):
        self.calls += 1
        self.get_kwargs.append(kwargs)
        response = self._response

        class _Ctx:
            async def __aenter__(self):
                return response

            async def __aexit__(self, *exc):
                return False

        return _Ctx()


def _fetch(session, **kwargs):
    return asyncio.run(dp._fetch_tile(session, _TILE_URL, aiohttp.ClientTimeout(total=5), **kwargs))


@pytest.mark.parametrize("status", [403, 429])
def test_a_refusal_stops_at_the_FIRST_request_and_is_never_a_credential_problem(status):
    """
    There is no credential on this host, so a 403 cannot mean "rejected token"
    the way it does on Mapillary and KartaView -- both of these mean the IP is
    refused, and retrying into a refusal is reported to extend it. Typed as a
    host condition so the scheduler's night-level breaker sees it.
    """
    session = _FakeTileSession(_FakeTileResponse(status))
    with pytest.raises(HostBlockedError) as excinfo:
        _fetch(session)
    assert excinfo.value.host == HOST_PANORAMAX
    assert session.calls == 1, "a refusal must not be retried"


def test_a_redirect_is_seen_rather_than_followed():
    """The whole point of allow_redirects=False: if aiohttp follows a redirect
    to a login or error page, that page's own HTTP 200 is what the status checks
    see and a block reads as corrupt protobuf (issue #199's shape)."""
    session = _FakeTileSession(_FakeTileResponse(200, {"Content-Type": "application/x-protobuf"}))
    _fetch(session)
    assert session.get_kwargs[0]["allow_redirects"] is False

    session = _FakeTileSession(_FakeTileResponse(302, {"Location": "https://example.test/login"}))
    with pytest.raises(HostBlockedError) as excinfo:
        _fetch(session)
    assert excinfo.value.host == HOST_PANORAMAX


def test_an_error_page_served_with_a_200_is_a_block_not_a_corrupt_tile():
    session = _FakeTileSession(_FakeTileResponse(200, {"Content-Type": "text/html; charset=utf-8"}))
    with pytest.raises(HostBlockedError):
        _fetch(session)


def test_a_404_is_an_EMPTY_TILE_that_is_counted_rather_than_raised():
    """
    The opposite reading from Mapillary's, and it is measured rather than
    assumed: phase 1 saw 0 empty tiles across 3,321 requests INCLUDING 20 cities
    holding no imagery at all, because an empty area answers 200 with no layer.
    Counted so the caller can refuse a whole lattice of them.
    """
    empties = []
    session = _FakeTileSession(_FakeTileResponse(404))
    assert _fetch(session, on_empty=lambda: empties.append(1)) == b""
    assert len(empties) == 1
    assert session.calls == 1, "a 404 is an answer, so it must not be retried"


def test_a_5xx_is_retried_and_every_attempt_is_paced_and_counted():
    """
    Pacing and counting sit INSIDE the retried body (issue #198): a stub that
    took one token in the caller would let a retrying tile present five times
    the configured rate during exactly the storm the host is least able to
    absorb, while under-reporting the same factor to the ledger.
    """
    counted = []

    class _Limiter:
        def __init__(self):
            self.acquired = 0

        async def acquire(self):
            self.acquired += 1

    limiter = _Limiter()
    session = _FakeTileSession(_FakeTileResponse(503))
    with pytest.raises(aiohttp.ClientResponseError):
        _fetch(session, rate_limiter=limiter, on_request=lambda: counted.append(1))
    assert session.calls == dp._TILE_MAX_TRIES
    assert len(counted) == session.calls == limiter.acquired


def test_a_healthy_tile_returns_its_body():
    body = mapbox_vector_tile.encode([])
    session = _FakeTileSession(
        _FakeTileResponse(200, {"Content-Type": "application/x-protobuf"}, body)
    )
    assert _fetch(session) == body


# ── 6. The host, and the two exit codes it owns ────────────────────────────


def test_panoramax_is_a_locked_host_with_its_own_unallocated_exit_codes():
    """
    A fourth per-IP host. The codes continue past 83 rather than filling the
    77/78 gap, which stays open because those are EX_NOPERM and EX_CONFIG and a
    plausible-sounding wrong answer is worse than an unallocated number.
    """
    assert HOST_LABELS[HOST_PANORAMAX] == "the Panoramax meta-catalog (api.panoramax.xyz)"
    assert HOST_EXIT_CODES[HOST_PANORAMAX] == 84
    assert HOST_BUSY_EXIT_CODES[HOST_PANORAMAX] == 85
    # Distinct from every other host's, in both families.
    assert len(set(HOST_EXIT_CODES.values())) == len(HOST_EXIT_CODES)
    assert len(set(HOST_BUSY_EXIT_CODES.values())) == len(HOST_BUSY_EXIT_CODES)
    assert not set(HOST_EXIT_CODES.values()) & set(HOST_BUSY_EXIT_CODES.values())


def test_the_downloader_raises_DownloadError_kinds_the_cli_already_maps():
    """`HostBlockedError` is a `DownloadError`, which is what lets cli.py's
    existing exception handling record the spend and exit with a host code
    without a Panoramax-shaped arm of its own."""
    assert issubclass(HostBlockedError, DownloadError)


def _tile_of(lat, lon, zoom=dp.TILE_ZOOM):
    fx, fy = _lonlat_to_tile_frac(lon, lat, zoom)
    return int(fx), int(fy)
