"""
Resume tests for the Mapillary tile census checkpoint (issue #256).

The contract under test is narrower than "it resumes". A checkpoint that
resumed but assembled the census in a different order would be WORSE than no
checkpoint at all: a run file is an immutable dated snapshot, `diff.py` compares
one run to the previous of the same series, and a reordering shows up there as
imagery churn that did not happen. So the headline test here is not that a
resumed run finishes — it is that a resumed run writes the SAME BYTES as an
uninterrupted one, checked against the same golden fixture `test_mapillary.py`
pins the uninterrupted path with.

The rest fall into three groups: what survives an interruption (only successful
tiles), what the two request counters mean (this process vs the whole crawl,
which #239 got backwards once), and what an unusable checkpoint does (degrade to
a full fetch, never raise, never a wrong artifact).
"""

import asyncio
import gzip
import json
import os
import re
from datetime import UTC, datetime, timedelta

import aiohttp
import mapbox_vector_tile
import pandas as pd
import pyarrow.parquet as pq
import pytest
import yarl
from multidict import CIMultiDict, CIMultiDictProxy

from streetscape_metadata_tracker import checkpointing as dm_checkpointing
from streetscape_metadata_tracker import download_mapillary as dm
from streetscape_metadata_tracker.checkpointing import (
    CHECKPOINT_STATE_FILENAME,
    CensusCache,
    census_cache_path_for,
    checkpoint_path_for,
    load_census_cache_marker,
)
from streetscape_metadata_tracker.download_common import HOST_MAPILLARY_TILES, HostBlockedError
from tests.test_mapillary import (
    GOLDEN_PATH,
    GOLDEN_TIMESTAMP_PLACEHOLDER,
    _assert_csv_matches_golden,
    _golden_features,
    _stub_fetch_tile,
    encode_tile,
    make_image,
)

# The golden city: 200x200 m centred on a z14 tile boundary, so its bbox spans
# two tiles and a border image lands in both. That duplicate is the whole reason
# reassembly order matters — dedupe_census keeps the FIRST position, so a
# checkpoint that replayed tiles in fetch order rather than tile order would
# resolve it differently depending on which night fetched which copy.
SEATTLE = (47.6062, -122.3321)


@pytest.fixture
def straddling_city():
    lat = SEATTLE[0]
    fx, fy = dm.lonlat_to_tile_frac(SEATTLE[1], lat, 14)
    boundary_lon, _ = dm.tile_frac_to_lonlat(int(fx), fy, 14)
    return lat, boundary_lon


def _golden_tiles(lat, lon, width=200, height=200, step=20):
    """The golden fixture's tile payloads, keyed by (x, y)."""
    tiles = dm.tiles_for_bbox(*dm.grid_bbox(lat, lon, width, height, step))
    assert len(tiles) >= 2, "the straddle must span tiles for the dedup case to mean anything"
    everywhere, first_only, last_only = _golden_features(lat, lon)
    return tiles, {
        (x, y): encode_tile(
            everywhere
            + (first_only if (x, y) == tiles[0] else [])
            + (last_only if (x, y) == tiles[-1] else []),
            x,
            y,
        )
        for (x, y) in tiles
    }


def _serve(monkeypatch, tiles_by_xy, *, block_after=None, failing=(), served=None):
    """
    Install a tile fetcher over ``tiles_by_xy``.

    block_after: raise HostBlockedError once this many requests have been
        issued, i.e. the #205 whole-city condition an interruption looks like.
    failing: tiles that raise a per-tile error, which #168 tolerates.
    """
    served = [] if served is None else served

    async def fake_fetch(session, url, timeout):
        m = re.search(r"/2/14/(\d+)/(\d+)\?access_token=", url)
        assert m, f"unexpected tile URL: {url}"
        xy = (int(m.group(1)), int(m.group(2)))
        served.append(xy)
        if block_after is not None and len(served) > block_after:
            raise HostBlockedError("tile CDN redirected", host=HOST_MAPILLARY_TILES)
        if xy in failing:
            # A real RequestInfo: aiohttp's __str__ dereferences it, and the
            # error text goes through redact_credentials.
            raise aiohttp.ClientResponseError(
                request_info=aiohttp.RequestInfo(
                    url=yarl.URL(url),
                    method="GET",
                    headers=CIMultiDictProxy(CIMultiDict()),
                    real_url=yarl.URL(url),
                ),
                history=(),
                status=404,
                message="Not Found",
            )
        return tiles_by_xy.get(xy, mapbox_vector_tile.encode([]))

    _stub_fetch_tile(monkeypatch, fake_fetch)
    return served


def _download(tmp_path, lat, lon, checkpoint_path, *, name="run", width=200, height=200, step=20):
    """One full grid collection, serialized so an interruption lands predictably."""
    return asyncio.run(
        dm.download_mapillary_metadata_async(
            "Test City",
            lat,
            lon,
            width,
            height,
            step,
            "MLY|test|token",
            str(tmp_path / f"test_mapillary_{name}.csv.gz"),
            # One tile at a time: with the default 5, "block after 2 tiles"
            # would depend on scheduling rather than on the count.
            connection_limit=1,
            checkpoint_path=checkpoint_path,
            checkpoint_channel="mapillary",
        )
    )


def _state(checkpoint_path):
    with open(os.path.join(checkpoint_path, CHECKPOINT_STATE_FILENAME), encoding="utf-8") as f:
        return json.load(f)


def _write_state(checkpoint_path, **overrides):
    state = _state(checkpoint_path)
    state.update(overrides)
    with open(os.path.join(checkpoint_path, CHECKPOINT_STATE_FILENAME), "w", encoding="utf-8") as f:
        json.dump(state, f)


# ── The contract that matters: a resumed census is byte-identical ──────────


def test_a_resumed_census_writes_the_same_csv_as_an_uninterrupted_one(
    monkeypatch, tmp_path, straddling_city
):
    """
    Interrupt mid-census, resume, and land on the golden fixture.

    This is the reason parts are keyed by TILE rather than by fetch order. The
    fixture is the same one test_mapillary.py's uninterrupted run is pinned to,
    so passing it means the two paths agree byte for byte — including on the
    border duplicate, whose winner dedupe_census picks by POSITION.
    """
    lat, lon = straddling_city
    tiles, tiles_by_xy = _golden_tiles(lat, lon)
    checkpoint = str(tmp_path / "cp")

    _serve(monkeypatch, tiles_by_xy, block_after=1)
    with pytest.raises(HostBlockedError):
        _download(tmp_path, lat, lon, checkpoint, name="first")
    assert len(_state(checkpoint)["done_tiles"]) == 1, "the fetched tile should have survived"

    served = _serve(monkeypatch, tiles_by_xy)
    result = _download(tmp_path, lat, lon, checkpoint, name="second")
    assert len(served) == len(tiles) - 1, "the resume must not re-request the committed tile"

    with gzip.open(result["filename_with_path"], "rt", encoding="utf-8") as f:
        written = f.read()
    assert result["started_at"] in written
    written = written.replace(result["started_at"], GOLDEN_TIMESTAMP_PLACEHOLDER)
    _assert_csv_matches_golden(written, GOLDEN_PATH.read_text(encoding="utf-8"))


def test_the_border_duplicate_resolves_the_same_way_whichever_night_fetched_it(
    monkeypatch, tmp_path, straddling_city
):
    """
    The reassembly-order rule, isolated from the golden fixture.

    A duplicate spanning the two tiles is served with DIFFERENT payloads per
    tile, so the census records which copy won. dedupe_census keeps the first
    position, so tile order must decide it — not which night did the fetching.
    """
    lat, lon = straddling_city
    tiles = dm.tiles_for_bbox(*dm.grid_bbox(lat, lon, 200, 200, 20))
    # One image at the shared boundary longitude, carrying a per-tile marker.
    tiles_by_xy = {
        (x, y): encode_tile([make_image(7, lon, lat, creator_id=1000 + n)], x, y)
        for n, (x, y) in enumerate(tiles)
    }

    _serve(monkeypatch, tiles_by_xy)
    uninterrupted = asyncio.run(
        dm.fetch_city_images_async(
            "C", dm.grid_bbox(lat, lon, 200, 200, 20), "MLY|t", connection_limit=1
        )
    )["census"]

    checkpoint = str(tmp_path / "cp")
    _serve(monkeypatch, tiles_by_xy, block_after=1)
    bbox = dm.grid_bbox(lat, lon, 200, 200, 20)
    with pytest.raises(HostBlockedError):
        asyncio.run(
            dm.fetch_city_images_async(
                "C",
                bbox,
                "MLY|t",
                connection_limit=1,
                checkpoint_path=checkpoint,
                checkpoint_channel="mapillary",
            )
        )
    _serve(monkeypatch, tiles_by_xy)
    resumed = asyncio.run(
        dm.fetch_city_images_async(
            "C",
            bbox,
            "MLY|t",
            connection_limit=1,
            checkpoint_path=checkpoint,
            checkpoint_channel="mapillary",
        )
    )["census"]

    pd.testing.assert_frame_equal(uninterrupted, resumed)


# ── What survives an interruption ──────────────────────────────────────────


def test_only_successful_tiles_are_committed(monkeypatch, tmp_path, straddling_city):
    """
    A tile that failed is refetchable, so it must not be in the record — and
    #168's tolerance must keep measuring against the FULL tile set.
    """
    lat, lon = straddling_city
    tiles, tiles_by_xy = _golden_tiles(lat, lon)
    checkpoint = str(tmp_path / "cp")

    _serve(monkeypatch, tiles_by_xy, failing={tiles[-1]})
    bbox = dm.grid_bbox(lat, lon, 200, 200, 20)
    with pytest.raises(dm.DownloadError):
        # 1 of 2 tiles failing is far over MAX_FAILED_TILE_FRACTION.
        asyncio.run(
            dm.fetch_city_images_async(
                "C",
                bbox,
                "MLY|t",
                connection_limit=1,
                checkpoint_path=checkpoint,
                checkpoint_channel="mapillary",
            )
        )
    done = {(x, y) for x, y, _ in _state(checkpoint)["done_tiles"]}
    assert tiles[-1] not in done
    assert done == set(tiles) - {tiles[-1]}

    served = _serve(monkeypatch, tiles_by_xy)
    asyncio.run(
        dm.fetch_city_images_async(
            "C",
            bbox,
            "MLY|t",
            connection_limit=1,
            checkpoint_path=checkpoint,
            checkpoint_channel="mapillary",
        )
    )
    assert served == [tiles[-1]], "only the failed tile should be retried"


def test_an_empty_tile_is_committed_with_a_record_and_no_file(
    monkeypatch, tmp_path, straddling_city
):
    """Most tiles over a real bbox are empty; 870 files for Moscow to say nothing."""
    lat, lon = straddling_city
    bbox = dm.grid_bbox(lat, lon, 200, 200, 20)
    tiles = dm.tiles_for_bbox(*bbox)
    checkpoint = str(tmp_path / "cp")

    _serve(monkeypatch, {})  # every tile decodes to zero features
    asyncio.run(
        dm.fetch_city_images_async(
            "C",
            bbox,
            "MLY|t",
            connection_limit=1,
            checkpoint_path=checkpoint,
            checkpoint_channel="mapillary",
        )
    )
    state = _state(checkpoint)
    assert len(state["done_tiles"]) == len(tiles)
    assert all(rows == 0 for _, _, rows in state["done_tiles"])
    assert state["census_rows"] == 0
    assert [n for n in os.listdir(checkpoint) if n.endswith(".parquet")] == []


def test_the_parquet_parts_round_trip_the_census_extension_dtypes(
    monkeypatch, tmp_path, straddling_city
):
    """
    The reason parts are parquet and not CSV.

    Every string here is provider-supplied, and pandas' default na_values claims
    "NA"/"None"/"null" — so a CSV part would make a RESUMED run publish
    different rows than an uninterrupted one. Nulls in the nullable boolean and
    integer columns are the other half of the same trap.
    """
    lat, lon = straddling_city
    bbox = dm.grid_bbox(lat, lon, 200, 200, 20)
    tiles = dm.tiles_for_bbox(*bbox)
    checkpoint = str(tmp_path / "cp")
    tiles_by_xy = {
        tiles[0]: encode_tile(
            [
                make_image(1, lon, lat, on_foot=None, captured_at=None, sequence_id="NA"),
                make_image(2, lon, lat, on_foot=True, sequence_id="None"),
            ],
            *tiles[0],
        )
    }

    _serve(monkeypatch, tiles_by_xy, block_after=1)
    with pytest.raises(HostBlockedError):
        asyncio.run(
            dm.fetch_city_images_async(
                "C",
                bbox,
                "MLY|t",
                connection_limit=1,
                checkpoint_path=checkpoint,
                checkpoint_channel="mapillary",
            )
        )

    live = dm.records_to_census(dm.decode_image_features(tiles_by_xy[tiles[0]], *tiles[0]))
    part = dm._tile_part_path(checkpoint, *tiles[0])
    pd.testing.assert_frame_equal(pd.read_parquet(part), live)
    assert pd.read_parquet(part).dtypes.to_dict() == live.dtypes.to_dict()


# ── The two counters ───────────────────────────────────────────────────────


def test_the_ledger_gets_this_process_and_the_row_gets_the_whole_crawl(
    monkeypatch, tmp_path, straddling_city
):
    """
    #239's rule, restated for the census: api_requests is additive into a
    (date, provider) ledger, so a resumed night reporting the whole crawl there
    would bill last night's tiles against tonight's budget gate.
    """
    lat, lon = straddling_city
    tiles, tiles_by_xy = _golden_tiles(lat, lon)
    checkpoint = str(tmp_path / "cp")

    _serve(monkeypatch, tiles_by_xy, block_after=1)
    with pytest.raises(HostBlockedError) as blocked:
        _download(tmp_path, lat, lon, checkpoint, name="first")
    # Two, not one: the REFUSED request is counted as well. That is #198/#203's
    # "one token, one ledger increment, one HTTP request" invariant -- the
    # request was issued, and #205 declined to stop counting refusals because
    # fail-fast already bounds the over-count.
    assert blocked.value.api_requests == 2
    assert blocked.value.api_requests_total == 2
    assert len(_state(checkpoint)["done_tiles"]) == 1, "only the tile that answered"
    # The refused request landed in the record too, even though it committed no
    # tile -- otherwise the resumed row would price the city below what it cost.
    assert _state(checkpoint)["api_requests_total"] == 2

    _serve(monkeypatch, tiles_by_xy)
    result = _download(tmp_path, lat, lon, checkpoint, name="second")
    assert result["api_requests"] == len(tiles) - 1, "this process only"
    # So the crawl total legitimately EXCEEDS the tile count: it is what the
    # collection cost, refusals included, which is what belongs on the row.
    assert result["api_requests_total"] == 2 + (len(tiles) - 1)
    assert result["checkpoint_path"] == checkpoint


def test_a_complete_checkpoint_refinalizes_for_zero_requests(
    monkeypatch, tmp_path, straddling_city, caplog
):
    """
    Recovers a crash between the CSV write and cataloging. Loud, because the
    other way to arrive here is a checkpoint nobody discarded — and a
    zero-request collection must not read like a real one.
    """
    lat, lon = straddling_city
    tiles, tiles_by_xy = _golden_tiles(lat, lon)
    checkpoint = str(tmp_path / "cp")

    _serve(monkeypatch, tiles_by_xy)
    first = _download(tmp_path, lat, lon, checkpoint, name="first")
    assert first["api_requests"] == len(tiles)

    served = _serve(monkeypatch, tiles_by_xy)
    with caplog.at_level("WARNING"):
        second = _download(tmp_path, lat, lon, checkpoint, name="second")
    assert served == []
    assert second["api_requests"] == 0
    assert second["api_requests_total"] == len(tiles)
    assert "COMPLETE" in caplog.text

    with gzip.open(first["filename_with_path"], "rt") as f:
        first_csv = f.read().replace(first["started_at"], "T")
    with gzip.open(second["filename_with_path"], "rt") as f:
        second_csv = f.read().replace(second["started_at"], "T")
    assert first_csv == second_csv


# ── An unusable checkpoint degrades; it never raises and never lies ────────


@pytest.mark.parametrize(
    "overrides, expected",
    [
        ({"format_version": 99}, "format v99"),
        ({"bbox": [0.0, 0.0, 1.0, 1.0]}, "covers bbox"),
        ({"zoom": 13}, "fetched at z13"),
        ({"channel": "mapillary_streets"}, "different api_usage ledgers"),
        ({"tile_count": 99}, "covers 99 tiles"),
        ({"census_rows": 10_000}, "commit record says 10000"),
        (
            {"created_at": (datetime.now(UTC) - timedelta(days=8)).isoformat()},
            "spliced\ninto a snapshot dated today".replace("\n", " "),
        ),
        ({"variant": "all_public"}, "price this crawl with another one's requests"),
    ],
)
def test_an_unusable_checkpoint_is_discarded_and_the_city_is_refetched(
    monkeypatch, tmp_path, straddling_city, caplog, overrides, expected
):
    lat, lon = straddling_city
    tiles, tiles_by_xy = _golden_tiles(lat, lon)
    checkpoint = str(tmp_path / "cp")

    _serve(monkeypatch, tiles_by_xy, block_after=1)
    with pytest.raises(HostBlockedError):
        _download(tmp_path, lat, lon, checkpoint, name="first")
    _write_state(checkpoint, **overrides)

    served = _serve(monkeypatch, tiles_by_xy)
    with caplog.at_level("WARNING"):
        result = _download(tmp_path, lat, lon, checkpoint, name="second")
    assert expected in caplog.text.replace("\n", " ")
    assert len(served) == len(tiles), "a discarded checkpoint means fetch everything"
    assert result["api_requests_total"] == len(tiles), (
        "a discarded checkpoint's spend is not inherited"
    )


def test_a_corrupt_state_file_degrades_rather_than_raising(monkeypatch, tmp_path, straddling_city):
    lat, lon = straddling_city
    tiles, tiles_by_xy = _golden_tiles(lat, lon)
    checkpoint = str(tmp_path / "cp")
    os.makedirs(checkpoint)
    with open(os.path.join(checkpoint, CHECKPOINT_STATE_FILENAME), "w") as f:
        f.write("{not json at all")

    served = _serve(monkeypatch, tiles_by_xy)
    result = _download(tmp_path, lat, lon, checkpoint, name="run")
    assert len(served) == len(tiles)
    assert result["api_requests"] == len(tiles)


def test_a_truncated_part_is_caught_from_its_footer(monkeypatch, tmp_path, straddling_city):
    """
    Verified at LOAD, from the parquet footer, rather than at reassembly — after
    the fetch has already been paid for.
    """
    lat, lon = straddling_city
    tiles, tiles_by_xy = _golden_tiles(lat, lon)
    checkpoint = str(tmp_path / "cp")

    _serve(monkeypatch, tiles_by_xy, block_after=1)
    with pytest.raises(HostBlockedError):
        _download(tmp_path, lat, lon, checkpoint, name="first")

    part = dm._tile_part_path(checkpoint, *tiles[0])
    assert pq.ParquetFile(part).metadata.num_rows > 0
    frame = pd.read_parquet(part).iloc[:0]
    frame.to_parquet(part, index=False)  # same file, fewer rows than recorded

    served = _serve(monkeypatch, tiles_by_xy)
    _download(tmp_path, lat, lon, checkpoint, name="second")
    assert len(served) == len(tiles)


def test_a_part_for_an_uncommitted_tile_is_swept_at_load(monkeypatch, tmp_path, straddling_city):
    lat, lon = straddling_city
    tiles, tiles_by_xy = _golden_tiles(lat, lon)
    checkpoint = str(tmp_path / "cp")

    _serve(monkeypatch, tiles_by_xy, block_after=1)
    with pytest.raises(HostBlockedError):
        _download(tmp_path, lat, lon, checkpoint, name="first")

    torn = dm._tile_part_path(checkpoint, 999, 999)
    pd.read_parquet(dm._tile_part_path(checkpoint, *tiles[0])).to_parquet(torn, index=False)
    stray_tmp = os.path.join(checkpoint, "tile-1-2.parquet.tmp")
    open(stray_tmp, "w").close()

    _serve(monkeypatch, tiles_by_xy)
    _download(tmp_path, lat, lon, checkpoint, name="second")
    assert not os.path.exists(torn)
    assert not os.path.exists(stray_tmp)


def test_a_failing_commit_never_fails_the_city(monkeypatch, tmp_path, straddling_city, caplog):
    """
    The deliberate divergence from KartaView's fail-fast: ~15 minutes of census
    is not worth failing over its own safety net.
    """
    lat, lon = straddling_city
    tiles, tiles_by_xy = _golden_tiles(lat, lon)
    checkpoint = str(tmp_path / "cp")

    def exploding_to_parquet(self, *a, **k):
        raise OSError("no space left on device")

    monkeypatch.setattr(pd.DataFrame, "to_parquet", exploding_to_parquet)
    served = _serve(monkeypatch, tiles_by_xy)
    with caplog.at_level("WARNING"):
        result = _download(tmp_path, lat, lon, checkpoint, name="run")

    assert len(served) == len(tiles), "the city still collected"
    assert result["api_requests"] == len(tiles)
    assert "continuing" in caplog.text
    # Latched: one warning, not one per tile.
    assert caplog.text.count("Could not checkpoint tile") == 1


def test_an_unwritable_checkpoint_directory_fetches_unprotected(
    monkeypatch, tmp_path, straddling_city, caplog
):
    lat, lon = straddling_city
    tiles, tiles_by_xy = _golden_tiles(lat, lon)
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("")

    _serve(monkeypatch, tiles_by_xy)
    with caplog.at_level("WARNING"):
        result = _download(tmp_path, lat, lon, str(blocker / "cp"), name="run")
    assert result["api_requests"] == len(tiles)
    assert result["checkpoint_path"] is None
    assert "fetching unprotected" in caplog.text


def test_no_checkpoint_path_creates_nothing(monkeypatch, tmp_path, straddling_city):
    """The historical behaviour is the default, and it must stay inert."""
    lat, lon = straddling_city
    tiles, tiles_by_xy = _golden_tiles(lat, lon)
    _serve(monkeypatch, tiles_by_xy)
    result = _download(tmp_path, lat, lon, None, name="run")
    assert result["checkpoint_path"] is None
    assert result["api_requests"] == result["api_requests_total"] == len(tiles)
    assert not any(p.name == "cp" for p in tmp_path.iterdir())


def test_a_city_blocked_before_committing_leaves_no_empty_directory(
    monkeypatch, tmp_path, straddling_city
):
    lat, lon = straddling_city
    _, tiles_by_xy = _golden_tiles(lat, lon)
    checkpoint = str(tmp_path / "cp")

    _serve(monkeypatch, tiles_by_xy, block_after=0)
    with pytest.raises(HostBlockedError):
        _download(tmp_path, lat, lon, checkpoint, name="run")
    assert not os.path.exists(checkpoint)


# ── The channel is what keeps a walk and a grid run apart ──────────────────


def test_the_walk_and_the_grid_run_get_different_checkpoint_directories():
    """
    Both sweep the identical frozen bbox, so only the channel separates them —
    and the state file refuses a cross-channel resume even if a caller derived
    the path wrong.
    """
    bbox = (-122.0, 47.0, -121.9, 47.1)
    grid = checkpoint_path_for("seattle--washington", bbox, "mapillary")
    walk = checkpoint_path_for("seattle--washington", bbox, "mapillary_streets")
    assert grid != walk
    assert os.path.basename(grid) == os.path.basename(walk)


def test_a_checkpoint_written_by_the_walk_is_refused_by_the_grid_run(
    monkeypatch, tmp_path, straddling_city, caplog
):
    lat, lon = straddling_city
    tiles, tiles_by_xy = _golden_tiles(lat, lon)
    bbox = dm.grid_bbox(lat, lon, 200, 200, 20)
    shared = str(tmp_path / "moved-by-hand")

    _serve(monkeypatch, tiles_by_xy, block_after=1)
    with pytest.raises(HostBlockedError):
        asyncio.run(
            dm.fetch_city_images_async(
                "C",
                bbox,
                "MLY|t",
                connection_limit=1,
                checkpoint_path=shared,
                checkpoint_channel="mapillary_streets",
            )
        )

    served = _serve(monkeypatch, tiles_by_xy)
    with caplog.at_level("WARNING"):
        asyncio.run(
            dm.fetch_city_images_async(
                "C",
                bbox,
                "MLY|t",
                connection_limit=1,
                checkpoint_path=shared,
                checkpoint_channel="mapillary",
            )
        )
    assert "different api_usage ledgers" in caplog.text
    assert len(served) == len(tiles)


def test_the_checkpoint_is_a_seven_day_horizon(monkeypatch, tmp_path, straddling_city):
    """Six days resumes; eight does not. The cap protects an ARTIFACT, not tidiness."""
    lat, lon = straddling_city
    tiles, tiles_by_xy = _golden_tiles(lat, lon)
    checkpoint = str(tmp_path / "cp")

    _serve(monkeypatch, tiles_by_xy, block_after=1)
    with pytest.raises(HostBlockedError):
        _download(tmp_path, lat, lon, checkpoint, name="first")
    _write_state(checkpoint, updated_at=(datetime.now(UTC) - timedelta(days=6)).isoformat())

    served = _serve(monkeypatch, tiles_by_xy)
    _download(tmp_path, lat, lon, checkpoint, name="second")
    assert len(served) == len(tiles) - 1, "six days is still resumable"


# ── The age cap has to be un-resettable, or it protects nothing ────────────


def test_the_age_is_measured_from_the_first_commit_not_the_last_write(
    monkeypatch, tmp_path, straddling_city, caplog
):
    """
    `updated_at` moves on writes that commit NO tile (see `_commit_spend`), and a
    host-blocked night records no `consecutive_failures` — so the same stalest
    city is re-attempted nightly. Ageing from the last write would let it
    refresh its own clock forever and splice rows of any age into a snapshot
    dated today, which is the one thing this cap exists to stop.
    """
    lat, lon = straddling_city
    tiles, tiles_by_xy = _golden_tiles(lat, lon)
    checkpoint = str(tmp_path / "cp")

    _serve(monkeypatch, tiles_by_xy, block_after=1)
    with pytest.raises(HostBlockedError):
        _download(tmp_path, lat, lon, checkpoint, name="first")

    # A crawl whose oldest row is 8 days old, last written a moment ago.
    _write_state(
        checkpoint,
        created_at=(datetime.now(UTC) - timedelta(days=8)).isoformat(),
        updated_at=datetime.now(UTC).isoformat(),
    )

    served = _serve(monkeypatch, tiles_by_xy)
    with caplog.at_level("WARNING"):
        _download(tmp_path, lat, lon, checkpoint, name="second")
    assert "past the 7-day limit" in caplog.text
    assert len(served) == len(tiles), "a stale crawl is refetched, not resumed"


def test_a_night_that_commits_no_tile_does_not_restamp_the_origin(
    monkeypatch, tmp_path, straddling_city
):
    """The write `_commit_spend` makes must move `updated_at` and nothing else."""
    lat, lon = straddling_city
    _, tiles_by_xy = _golden_tiles(lat, lon)
    checkpoint = str(tmp_path / "cp")

    _serve(monkeypatch, tiles_by_xy, block_after=1)
    with pytest.raises(HostBlockedError):
        _download(tmp_path, lat, lon, checkpoint, name="first")

    aged = (datetime.now(UTC) - timedelta(days=6, hours=23)).isoformat()
    _write_state(checkpoint, created_at=aged)
    before = _state(checkpoint)

    # Refused at request 1: nothing new commits, but the spend is still recorded.
    _serve(monkeypatch, tiles_by_xy, block_after=0)
    with pytest.raises(HostBlockedError):
        _download(tmp_path, lat, lon, checkpoint, name="second")

    after = _state(checkpoint)
    assert after["created_at"] == aged, "a spend-only write must not reset the age clock"
    assert after["updated_at"] != before["updated_at"], "but it is still a write"
    assert after["done_tiles"] == before["done_tiles"], "and it commits no tile"


# ── A discarded checkpoint must not cost the run its protection ────────────


def test_a_discarded_checkpoint_leaves_the_rerun_still_protected(
    monkeypatch, tmp_path, straddling_city
):
    """
    An unusable checkpoint here is DELETED, so the directory the run is about to
    commit into is the one the discard removed. Opening it before the load left
    every commit failing, `degraded` latched on the first tile, and the whole
    city fetched unprotected — in exactly the case the age cap produces, i.e.
    the first attempt after a multi-day block.
    """
    lat, lon = straddling_city
    _, tiles_by_xy = _golden_tiles(lat, lon)
    checkpoint = str(tmp_path / "cp")

    _serve(monkeypatch, tiles_by_xy, block_after=1)
    with pytest.raises(HostBlockedError):
        _download(tmp_path, lat, lon, checkpoint, name="first")
    _write_state(checkpoint, created_at=(datetime.now(UTC) - timedelta(days=8)).isoformat())

    # Night 2 discards the stale crawl and is itself interrupted after one tile.
    _serve(monkeypatch, tiles_by_xy, block_after=1)
    with pytest.raises(HostBlockedError):
        _download(tmp_path, lat, lon, checkpoint, name="second")

    assert os.path.exists(_state_file(checkpoint)), "night 2 saved nothing"
    assert len(_state(checkpoint)["done_tiles"]) == 1, "night 2's tile is not durable"


def test_a_part_written_before_any_commit_record_is_swept(monkeypatch, tmp_path, straddling_city):
    """
    The process died between its first `to_parquet` and its first `state.json`,
    so `load_tile_checkpoint` returns at the missing record before it can look —
    which used to mean nothing ever swept what it left.
    """
    lat, lon = straddling_city
    tiles, tiles_by_xy = _golden_tiles(lat, lon)
    checkpoint = str(tmp_path / "cp")
    os.makedirs(checkpoint)
    orphan = dm._tile_part_path(checkpoint, 999, 999)
    pd.DataFrame({"image_id": ["x"]}).to_parquet(orphan, index=False)
    stray = os.path.join(checkpoint, "tile-1-2.parquet.tmp")
    open(stray, "w").close()

    served = _serve(monkeypatch, tiles_by_xy)
    _download(tmp_path, lat, lon, checkpoint, name="run")

    assert len(served) == len(tiles), "no commit record means fetch everything"
    assert not os.path.exists(orphan)
    assert not os.path.exists(stray)


def test_a_committed_part_is_fsynced_before_it_is_renamed(monkeypatch, tmp_path, straddling_city):
    """
    Without it the part-then-state ordering holds against a process crash but
    not a power loss, and what a torn part costs is the WHOLE checkpoint —
    `load_tile_checkpoint` discards on a short one. `download_kartaview`'s
    `_commit_checkpoint` spends the same fsyncs for the same reason.
    """
    lat, lon = straddling_city
    _, tiles_by_xy = _golden_tiles(lat, lon)
    checkpoint = str(tmp_path / "cp")

    events = []
    real_fsync, real_replace = os.fsync, os.replace
    monkeypatch.setattr(os, "fsync", lambda fd: (events.append("fsync"), real_fsync(fd))[1])
    monkeypatch.setattr(
        os,
        "replace",
        lambda a, b: (events.append(("replace", os.path.basename(a))), real_replace(a, b))[1],
    )

    _serve(monkeypatch, tiles_by_xy)
    _download(tmp_path, lat, lon, checkpoint, name="run")

    first_part_rename = next(
        i
        for i, e in enumerate(events)
        if isinstance(e, tuple) and e[1].startswith("tile-") and e[1].endswith(".parquet.tmp")
    )
    assert "fsync" in events[:first_part_rename], (
        "the part reached the directory entry before it reached the disk"
    )


# ── The variant: what separates two walks of one city (issue #256 review) ──


def test_the_two_network_types_get_different_checkpoint_directories():
    """
    Both walks meter into `mapillary_streets` and read the SAME frozen bbox, so
    the channel cannot tell them apart and every geometric check would pass.
    """
    bbox = (-122.0, 47.0, -121.9, 47.1)
    drive = checkpoint_path_for("seattle--washington", bbox, "mapillary_streets", variant="drive")
    broad = checkpoint_path_for(
        "seattle--washington", bbox, "mapillary_streets", variant="all_public"
    )
    grid = checkpoint_path_for("seattle--washington", bbox, "mapillary")

    assert drive != broad
    assert os.path.dirname(drive) == os.path.dirname(broad)
    # A grid run has exactly one crawl per channel, so it passes no variant and
    # its path stays byte-identical to the one #239 shipped.
    assert os.path.basename(grid) == os.path.basename(drive).removesuffix("_drive")


def _state_file(path):
    return os.path.join(path, CHECKPOINT_STATE_FILENAME)


# ── The shared census cache (issue #290) ───────────────────────────────────
#
# A checkpoint protects ONE crawl; the cache lets every other consumer of that
# (provider, city, bbox) observation reuse the finished result. For Mapillary
# that is the grid run, the road walk, and a second walk at another
# --network-type — three identical tile censuses, each previously paid for
# against the same per-IP limit that has blocked this host three times.
#
# What is under test here is not "does it read the file back". It is that a
# REUSED census is the SAME CENSUS — pinned against the same golden fixture the
# uninterrupted and the resumed paths are pinned to — and that the two request
# counters split the way the ledger needs, which is the half a plausible edit
# gets backwards silently.


def _fetch(
    tmp_path,
    lat,
    lon,
    *,
    channel,
    variant=None,
    cache_path,
    reuse=True,
    checkpoint=None,
    run_date=None,
):
    """One census fetch through the real entry point, cache wired up."""
    return asyncio.run(
        dm.fetch_city_images_async(
            "Test City",
            dm.grid_bbox(lat, lon, 200, 200, 20),
            "MLY|test|token",
            connection_limit=1,
            checkpoint_path=checkpoint or str(tmp_path / f"cp-{channel}-{variant}"),
            checkpoint_channel=channel,
            checkpoint_variant=variant,
            census_cache=CensusCache(cache_path, reuse, run_date) if cache_path else None,
        )
    )


@pytest.fixture
def cache_path(tmp_path, straddling_city):
    """The entry the grid run and both walks all resolve to — no channel in it."""
    lat, lon = straddling_city
    return census_cache_path_for("mapillary", "test--city", dm.grid_bbox(lat, lon, 200, 200, 20))


def test_the_second_channel_reads_the_first_channels_census_for_zero_requests(
    monkeypatch, tmp_path, straddling_city, cache_path, caplog
):
    """
    THE test of this feature, and the reason it exists: on a paired night the
    grid run and the road walk fetch the IDENTICAL z14 census over the identical
    frozen bbox — the ledger showed byte-identical daily totals for the two
    channels (#287). One fetch now serves both.
    """
    lat, lon = straddling_city
    tiles, tiles_by_xy = _golden_tiles(lat, lon)

    served = _serve(monkeypatch, tiles_by_xy)
    grid = _fetch(tmp_path, lat, lon, channel="mapillary", cache_path=cache_path)
    assert len(served) == len(tiles)

    served = _serve(monkeypatch, tiles_by_xy)
    with caplog.at_level("WARNING"):
        walk = _fetch(
            tmp_path,
            lat,
            lon,
            channel="mapillary_streets",
            variant="drive",
            cache_path=cache_path,
        )

    assert served == [], "the walk must not touch the tile CDN"
    assert walk["api_requests"] == 0
    pd.testing.assert_frame_equal(walk["census"], grid["census"])
    assert "REUSING the Mapillary census fetched by mapillary" in caplog.text


def test_a_reused_census_writes_the_same_csv_as_a_fetched_one(
    monkeypatch, tmp_path, straddling_city, cache_path
):
    """
    Byte identity against the SAME golden fixture the uninterrupted and resumed
    paths are pinned to (test_mapillary.py, and the resume test at the top of
    this file). A reused census that assembled its tiles in a different order
    would resolve the border duplicate the other way — dedupe_census keeps the
    FIRST position — and every diff of that city would show imagery churn that
    did not happen, against an immutable dated snapshot.
    """
    lat, lon = straddling_city
    _tiles, tiles_by_xy = _golden_tiles(lat, lon)

    _serve(monkeypatch, tiles_by_xy)
    first = asyncio.run(
        dm.download_mapillary_metadata_async(
            "Test City",
            lat,
            lon,
            200,
            200,
            20,
            "MLY|t",
            str(tmp_path / "grid.csv.gz"),
            connection_limit=1,
            checkpoint_path=str(tmp_path / "cp-grid"),
            checkpoint_channel="mapillary",
            census_cache=CensusCache(cache_path),
        )
    )
    served = _serve(monkeypatch, tiles_by_xy)
    second = asyncio.run(
        dm.download_mapillary_metadata_async(
            "Test City",
            lat,
            lon,
            200,
            200,
            20,
            "MLY|t",
            str(tmp_path / "walk.csv.gz"),
            connection_limit=1,
            checkpoint_path=str(tmp_path / "cp-walk"),
            checkpoint_channel="mapillary_streets",
            census_cache=CensusCache(cache_path),
        )
    )
    assert served == []

    with gzip.open(second["filename_with_path"], "rt", encoding="utf-8") as f:
        written = f.read()
    # The reused run stamps its rows with WHEN MAPILLARY WAS OBSERVED, not when
    # this process started: the rows were fetched by the grid run.
    assert second["census_fetched_at"] == first["census_fetched_at"]
    assert second["started_at"] not in written
    written = written.replace(second["census_fetched_at"], GOLDEN_TIMESTAMP_PLACEHOLDER)
    _assert_csv_matches_golden(written, GOLDEN_PATH.read_text(encoding="utf-8"))


def test_a_cross_channel_reuse_records_zero_and_a_same_channel_refinalize_records_the_crawl(
    monkeypatch, tmp_path, straddling_city, cache_path
):
    """
    The one rule here that is easy to get backwards. `api_requests` is always 0
    (this process issued nothing, and the daily ledger is additive by (date,
    provider)) — but `api_requests_total` is NOT.

    A same-(channel, variant) reader is not reusing anything: it is #239/#256's
    re-finalize, a caller that died before its artifact was durable coming back
    to write the row for a crawl IT paid for. That row must still price the
    collection. A different channel's collection genuinely cost nothing.
    """
    lat, lon = straddling_city
    tiles, tiles_by_xy = _golden_tiles(lat, lon)

    _serve(monkeypatch, tiles_by_xy)
    grid = _fetch(tmp_path, lat, lon, channel="mapillary", cache_path=cache_path)
    assert grid["api_requests_total"] == len(tiles)

    _serve(monkeypatch, tiles_by_xy)
    refinalize = _fetch(tmp_path, lat, lon, channel="mapillary", cache_path=cache_path)
    assert refinalize["api_requests"] == 0
    assert refinalize["api_requests_total"] == len(tiles), "the row still prices the collection"
    assert refinalize["census_fetched_by"] == "mapillary"

    _serve(monkeypatch, tiles_by_xy)
    walk = _fetch(
        tmp_path, lat, lon, channel="mapillary_streets", variant="drive", cache_path=cache_path
    )
    assert walk["api_requests"] == 0
    assert walk["api_requests_total"] == 0, "this collection cost nothing"
    assert walk["census_fetched_by"] == "mapillary", "and the row says who did pay"


def test_the_two_network_types_share_one_census(monkeypatch, tmp_path, straddling_city, cache_path):
    """
    `--network-type all_public` is a THIRD identical census (N+1, #287). The
    variant is what keeps the two walks' CHECKPOINTS apart — they agree on the
    ledger, the credential and every geometric parameter — and it is deliberately
    absent from the cache key, because the census they read is the same one.
    """
    lat, lon = straddling_city
    _tiles, tiles_by_xy = _golden_tiles(lat, lon)

    _serve(monkeypatch, tiles_by_xy)
    drive = _fetch(
        tmp_path, lat, lon, channel="mapillary_streets", variant="drive", cache_path=cache_path
    )
    served = _serve(monkeypatch, tiles_by_xy)
    broad = _fetch(
        tmp_path, lat, lon, channel="mapillary_streets", variant="all_public", cache_path=cache_path
    )

    assert served == []
    pd.testing.assert_frame_equal(broad["census"], drive["census"])
    assert broad["census_fetched_by"] == "mapillary_streets"
    # Same channel, DIFFERENT variant: still a reuse, not a re-finalize, so the
    # second walk must not inherit the first's crawl cost into its own row.
    assert broad["api_requests_total"] == 0


def test_the_promoted_checkpoint_is_not_left_behind_for_the_caller_to_discard(
    monkeypatch, tmp_path, straddling_city, cache_path
):
    """
    The directory MOVED. Returning its old path would have the caller's
    `discard_checkpoint` chase a path that is gone — harmless today, but a
    future caller reading it as "still mine" would delete the shared entry.
    """
    lat, lon = straddling_city
    _tiles, tiles_by_xy = _golden_tiles(lat, lon)
    checkpoint = str(tmp_path / "cp")

    _serve(monkeypatch, tiles_by_xy)
    result = _fetch(
        tmp_path, lat, lon, channel="mapillary", cache_path=cache_path, checkpoint=checkpoint
    )
    assert result["checkpoint_path"] is None
    assert not os.path.exists(checkpoint)
    assert os.path.isdir(cache_path)


def test_a_reuse_inherits_the_failed_tiles_rather_than_re_probing_them(
    monkeypatch, tmp_path, straddling_city, cache_path
):
    """
    A CROSS-CHANNEL reuser is publishing the SAME observation, so the same grid
    points must read REQUEST_FAILED in both artifacts. Re-probing the holes
    would mix two moments into one dated snapshot for no gain — and would not
    be free. (The crawl's own channel is the exception; see the next test.)
    """
    lat, lon = straddling_city
    tiles, tiles_by_xy = _golden_tiles(lat, lon)
    hole = tiles[-1]
    # The golden city is 4 tiles, so one hole is 25% and the real 2% tolerance
    # would refuse to finalize. Widened here because what is under test is what
    # a REUSER inherits, not where the tolerance sits (which
    # test_mapillary.py's own tolerance tests pin).
    monkeypatch.setattr(dm, "MAX_FAILED_TILE_FRACTION", 0.5)

    _serve(monkeypatch, tiles_by_xy, failing={hole})
    grid = _fetch(tmp_path, lat, lon, channel="mapillary", cache_path=cache_path)
    assert grid["failed_tiles"] == [hole]

    served = _serve(monkeypatch, tiles_by_xy)
    walk = _fetch(
        tmp_path, lat, lon, channel="mapillary_streets", variant="drive", cache_path=cache_path
    )
    assert served == [], "the hole is inherited, not re-asked"
    assert walk["failed_tiles"] == [hole]


def test_the_crawls_own_channel_re_probes_its_failed_tiles_rather_than_inheriting_them(
    monkeypatch, tmp_path, straddling_city, cache_path
):
    """
    The same (channel, variant) coming back is #239/#256's re-finalize — a tail
    that died between promotion and the catalog row — and on main that path
    resumed a COMPLETE checkpoint, which re-fetches the tiles that failed
    because a refusal is time-varying. Promotion must not silently drop that:
    the entry is handed back to the checkpoint, the resume re-probes the hole
    for exactly one request, and the entry it re-promotes has no hole.
    """
    lat, lon = straddling_city
    tiles, tiles_by_xy = _golden_tiles(lat, lon)
    hole = tiles[-1]
    monkeypatch.setattr(dm, "MAX_FAILED_TILE_FRACTION", 0.5)

    _serve(monkeypatch, tiles_by_xy, failing={hole})
    grid = _fetch(tmp_path, lat, lon, channel="mapillary", cache_path=cache_path)
    assert grid["failed_tiles"] == [hole]

    served = _serve(monkeypatch, tiles_by_xy)
    again = _fetch(tmp_path, lat, lon, channel="mapillary", cache_path=cache_path)
    assert served == [hole], "the hole was re-asked, and only the hole"
    assert again["failed_tiles"] == []
    assert again["api_requests"] == 1
    # The row still prices the whole collection: the crawl's stored spend plus
    # this re-probe (the resume rule the counter tests above pin).
    assert again["api_requests_total"] >= len(tiles)
    assert again["checkpoint_path"] is None, "re-promoted"
    assert load_census_cache_marker(cache_path)["failed"] == []
    # The entry was handed back and re-promoted, so nothing lingers under the
    # checkpoint name.
    assert not os.path.exists(str(tmp_path / "cp-mapillary-None"))


def test_a_hit_discards_this_channels_older_partial_checkpoint(
    monkeypatch, tmp_path, straddling_city, cache_path
):
    """
    Last night's walk was host-blocked after one tile and left its checkpoint;
    the grid run then completed and promoted. The walk's hit supersedes the
    partial, which nothing else would ever touch — a hit returns before the
    checkpoint is opened, and no pruner walks checkpoints/.
    """
    lat, lon = straddling_city
    _tiles, tiles_by_xy = _golden_tiles(lat, lon)
    walk_checkpoint = str(tmp_path / "cp-mapillary_streets-drive")

    _serve(monkeypatch, tiles_by_xy, block_after=1)
    with pytest.raises(HostBlockedError):
        _fetch(
            tmp_path, lat, lon, channel="mapillary_streets", variant="drive", cache_path=cache_path
        )
    assert os.path.exists(os.path.join(walk_checkpoint, CHECKPOINT_STATE_FILENAME))

    _serve(monkeypatch, tiles_by_xy)
    _fetch(tmp_path, lat, lon, channel="mapillary", cache_path=cache_path)

    served = _serve(monkeypatch, tiles_by_xy)
    walk = _fetch(
        tmp_path, lat, lon, channel="mapillary_streets", variant="drive", cache_path=cache_path
    )
    assert served == [], "reused"
    assert walk["census_fetched_by"] == "mapillary"
    assert not os.path.exists(walk_checkpoint), "the superseded partial is gone"


def test_an_interrupted_refetch_is_resumed_rather_than_abandoned_for_the_stale_entry(
    monkeypatch, tmp_path, straddling_city, cache_path
):
    """
    An operator's `--refetch-census` that was host-blocked mid-crawl is the
    NEWER observation and the one they asked for. The next run without the flag
    must resume it — not hit the entry it was started to replace and republish
    that.
    """
    lat, lon = straddling_city
    tiles, tiles_by_xy = _golden_tiles(lat, lon)

    _serve(monkeypatch, tiles_by_xy)
    _fetch(tmp_path, lat, lon, channel="mapillary", cache_path=cache_path)

    _serve(monkeypatch, tiles_by_xy, block_after=1)
    with pytest.raises(HostBlockedError):
        _fetch(
            tmp_path,
            lat,
            lon,
            channel="mapillary_streets",
            variant="drive",
            cache_path=cache_path,
            reuse=False,
        )

    served = _serve(monkeypatch, tiles_by_xy)
    resumed = _fetch(
        tmp_path, lat, lon, channel="mapillary_streets", variant="drive", cache_path=cache_path
    )
    assert 0 < len(served) < len(tiles), "resumed the newer crawl: the rest of it, not a reuse"
    assert resumed["census_fetched_by"] == "mapillary_streets"
    assert load_census_cache_marker(cache_path)["fetched_by"] == "mapillary_streets", (
        "and the refetch replaced the entry it was started to replace"
    )


def test_a_backdated_run_date_refuses_an_entry_observed_after_it(
    monkeypatch, tmp_path, straddling_city, cache_path
):
    """
    `--force --run-date <earlier>` after a real collection must not publish
    rows Mapillary served after the snapshot's own date. The window cannot see
    this (the entry is minutes old), so the consumer's run_date is the guard;
    the refetch then promotes over it, since a refetch is the fresher census.
    """
    lat, lon = straddling_city
    tiles, tiles_by_xy = _golden_tiles(lat, lon)

    _serve(monkeypatch, tiles_by_xy)
    _fetch(tmp_path, lat, lon, channel="mapillary", cache_path=cache_path)

    served = _serve(monkeypatch, tiles_by_xy)
    yesterday = datetime.now(UTC).date() - timedelta(days=1)
    backdated = _fetch(
        tmp_path,
        lat,
        lon,
        channel="mapillary_streets",
        variant="drive",
        cache_path=cache_path,
        run_date=yesterday,
    )
    assert len(served) == len(tiles), "refetched rather than published from its own future"
    assert backdated["census_reused"] is False

    served = _serve(monkeypatch, tiles_by_xy)
    _fetch(
        tmp_path,
        lat,
        lon,
        channel="mapillary_streets",
        variant="all_public",
        cache_path=cache_path,
        run_date=datetime.now(UTC).date(),
    )
    assert served == [], "a consumer dated today reuses it"


def test_an_interrupted_census_is_never_promoted(
    monkeypatch, tmp_path, straddling_city, cache_path
):
    """
    A partial entry reused as a census would publish the missing tiles' grid
    points as genuine no-imagery — absence never observed — in an immutable
    dated snapshot. That is the one way this feature could produce a WRONG
    artifact rather than wasted work, so promotion happens only on the success
    path and only after every `raise` above it.
    """
    lat, lon = straddling_city
    _tiles, tiles_by_xy = _golden_tiles(lat, lon)

    _serve(monkeypatch, tiles_by_xy, block_after=1)
    with pytest.raises(HostBlockedError):
        _fetch(tmp_path, lat, lon, channel="mapillary", cache_path=cache_path)

    assert not os.path.exists(cache_path)
    assert load_census_cache_marker(cache_path) is None


def test_a_degraded_checkpoint_is_never_promoted(
    monkeypatch, tmp_path, straddling_city, cache_path
):
    """
    A checkpoint whose commits latched off is missing tiles it never recorded,
    so `done` no longer describes what is on disk. The fetch still succeeds —
    checkpointing fails OPEN here, deliberately — but what it holds is not a
    complete census and must not become one.
    """
    lat, lon = straddling_city
    tiles, tiles_by_xy = _golden_tiles(lat, lon)

    def refuse_every_commit(*a, **k):
        raise OSError("read-only file system")

    monkeypatch.setattr(dm, "_write_checkpoint_state", refuse_every_commit)
    _serve(monkeypatch, tiles_by_xy)
    result = _fetch(tmp_path, lat, lon, channel="mapillary", cache_path=cache_path)

    assert result["api_requests"] == len(tiles), "the city still collected"
    assert not os.path.exists(cache_path), "but nothing incomplete reached the cache"


def test_an_incomplete_cache_entry_is_refused_and_deleted(
    monkeypatch, tmp_path, straddling_city, cache_path
):
    """
    Completeness is the one check a cache entry makes that a resume does NOT: a
    partial checkpoint is legitimate progress, a partial census is a hole. Here
    the entry is hand-shortened to look complete by its own counters while
    covering fewer tiles than the lattice.
    """
    lat, lon = straddling_city
    tiles, tiles_by_xy = _golden_tiles(lat, lon)

    _serve(monkeypatch, tiles_by_xy)
    _fetch(tmp_path, lat, lon, channel="mapillary", cache_path=cache_path)

    state = _state(cache_path)
    dropped = state["done_tiles"].pop()
    state["census_rows"] -= dropped[2]
    _write_state(cache_path, **state)
    os.remove(dm._tile_part_path(cache_path, dropped[0], dropped[1]))

    served = _serve(monkeypatch, tiles_by_xy)
    result = _fetch(
        tmp_path, lat, lon, channel="mapillary_streets", variant="drive", cache_path=cache_path
    )
    assert len(served) == len(tiles), "an incomplete entry costs a full fetch, not a hole"
    assert result["api_requests"] == len(tiles)


@pytest.mark.parametrize(
    "overrides, expected",
    [
        ({"bbox": [0.0, 0.0, 1.0, 1.0]}, "covers bbox"),
        ({"tile_count": 99}, "covers 99 tiles"),
        ({"zoom": 13}, "fetched at z13"),
    ],
)
def test_an_entry_that_does_not_describe_this_lattice_is_refused_and_deleted(
    monkeypatch, tmp_path, straddling_city, cache_path, caplog, overrides, expected
):
    """
    A promoted entry IS a moved checkpoint, so the same geometric cascade a
    resume makes has to run over it — a re-registered grid (resize_city.py,
    cap_oversized_grids.py) must not be reused onto a lattice it does not
    describe. Deleted rather than left, because a shared entry that will never
    validate would otherwise be re-read by every consumer in turn.
    """
    lat, lon = straddling_city
    tiles, tiles_by_xy = _golden_tiles(lat, lon)

    _serve(monkeypatch, tiles_by_xy)
    _fetch(tmp_path, lat, lon, channel="mapillary", cache_path=cache_path)
    _write_state(cache_path, **overrides)

    served = _serve(monkeypatch, tiles_by_xy)
    with caplog.at_level("WARNING"):
        _fetch(
            tmp_path, lat, lon, channel="mapillary_streets", variant="drive", cache_path=cache_path
        )
    assert expected in caplog.text
    assert len(served) == len(tiles), "the refusal costs a full fetch, never a hole"
    # The entry standing at that path is now the REFETCH's, not the one that was
    # refused: the loader deleted the bad one and the walk promoted its own over
    # the space. (The deletion itself is pinned in test_census_cache.py, where
    # nothing writes a replacement.)
    assert load_census_cache_marker(cache_path)["fetched_by"] == "mapillary_streets"


def test_a_stale_entry_is_refetched_rather_than_spliced_into_todays_snapshot(
    monkeypatch, tmp_path, straddling_city, cache_path
):
    """
    Frozen geometry never changes, so every other check still passes a fortnight
    later. The window is what keeps a census fetched last month out of a
    snapshot dated today, published as one observation of one day.
    """
    lat, lon = straddling_city
    tiles, tiles_by_xy = _golden_tiles(lat, lon)

    _serve(monkeypatch, tiles_by_xy)
    _fetch(tmp_path, lat, lon, channel="mapillary", cache_path=cache_path)

    marker_path = os.path.join(cache_path, dm_checkpointing.CENSUS_CACHE_MARKER)
    with open(marker_path, encoding="utf-8") as f:
        marker = json.load(f)
    marker["crawl_started_at"] = (datetime.now(UTC) - timedelta(days=8)).isoformat()
    with open(marker_path, "w", encoding="utf-8") as f:
        json.dump(marker, f)

    served = _serve(monkeypatch, tiles_by_xy)
    _fetch(tmp_path, lat, lon, channel="mapillary_streets", variant="drive", cache_path=cache_path)
    assert len(served) == len(tiles)


def test_refetch_census_ignores_the_entry_and_replaces_it(
    monkeypatch, tmp_path, straddling_city, cache_path
):
    """
    `--refetch-census` is about the OBSERVATION — take it now — and is
    deliberately separate from `--force`, which is about this run date's
    artifacts. A refetch still PROMOTES, so the consumers behind it get the
    fresher census rather than a re-fetch each.
    """
    lat, lon = straddling_city
    tiles, tiles_by_xy = _golden_tiles(lat, lon)

    _serve(monkeypatch, tiles_by_xy)
    first = _fetch(tmp_path, lat, lon, channel="mapillary", cache_path=cache_path)

    served = _serve(monkeypatch, tiles_by_xy)
    refetched = _fetch(
        tmp_path,
        lat,
        lon,
        channel="mapillary_streets",
        variant="drive",
        cache_path=cache_path,
        reuse=False,
    )
    assert len(served) == len(tiles), "the flag means ask again"
    assert refetched["census_fetched_by"] == "mapillary_streets"
    assert load_census_cache_marker(cache_path)["fetched_by"] == "mapillary_streets", (
        "and the entry it leaves is the fresher one"
    )
    assert first["census_fetched_at"] != refetched["census_fetched_at"]


def test_no_cache_path_is_the_historical_behaviour(monkeypatch, tmp_path, straddling_city):
    """The pre-#290 path, byte for byte: nothing is promoted, nothing is read,
    and the caller still owns its checkpoint."""
    lat, lon = straddling_city
    tiles, tiles_by_xy = _golden_tiles(lat, lon)
    checkpoint = str(tmp_path / "cp")

    _serve(monkeypatch, tiles_by_xy)
    result = _fetch(tmp_path, lat, lon, channel="mapillary", cache_path=None, checkpoint=checkpoint)
    assert result["checkpoint_path"] == checkpoint
    assert os.path.isdir(checkpoint)
    assert not os.path.exists(dm_checkpointing.census_cache_dir()) or not os.listdir(
        dm_checkpointing.census_cache_dir()
    )
    assert result["api_requests"] == len(tiles)


def test_an_unwritable_cache_never_fails_a_city(monkeypatch, tmp_path, straddling_city):
    """
    A city must never fail over its own optimization. When promotion cannot
    happen the caller keeps its checkpoint and discards it exactly as it did
    before this existed — the only cost is that the next consumer refetches.
    """
    lat, lon = straddling_city
    tiles, tiles_by_xy = _golden_tiles(lat, lon)
    checkpoint = str(tmp_path / "cp")
    unwritable = tmp_path / "readonly"
    unwritable.mkdir(mode=0o500)

    _serve(monkeypatch, tiles_by_xy)
    result = _fetch(
        tmp_path,
        lat,
        lon,
        channel="mapillary",
        cache_path=str(unwritable / "sub" / "entry"),
        checkpoint=checkpoint,
    )
    assert result["api_requests"] == len(tiles), "the collection succeeded"
    assert result["checkpoint_path"] == checkpoint, "and its checkpoint is still the caller's"


def test_the_walk_and_the_grid_run_share_the_cache_while_splitting_the_checkpoint():
    """
    The two path builders, side by side, because getting them the same way round
    is the whole design and both mistakes are silent: a cache keyed by channel
    never reuses anything, and a checkpoint NOT keyed by channel lets two crawls
    resume each other's spend into the wrong ledger.
    """
    bbox = (-122.0, 47.0, -121.9, 47.1)
    assert checkpoint_path_for("seattle--washington", bbox, "mapillary") != checkpoint_path_for(
        "seattle--washington", bbox, "mapillary_streets"
    )
    assert census_cache_path_for("mapillary", "seattle--washington", bbox) == census_cache_path_for(
        "mapillary", "seattle--washington", bbox
    )
