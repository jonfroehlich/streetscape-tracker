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

from streetscape_metadata_tracker import download_mapillary as dm
from streetscape_metadata_tracker.checkpointing import (
    CHECKPOINT_STATE_FILENAME,
    checkpoint_path_for,
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
            {"updated_at": (datetime.now(UTC) - timedelta(days=8)).isoformat()},
            "spliced\ninto a snapshot dated today".replace("\n", " "),
        ),
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
