"""
Resume and shared-cache behaviour for the Panoramax tile census (issue #316).

The contract is narrower than "it resumes". A checkpoint that resumed but
assembled the census in a different order would be WORSE than no checkpoint at
all: a run file is an immutable dated snapshot, `diff.py` compares one run to the
previous of the same series, and a reordering shows up there as imagery churn
that did not happen.

So the headline is BYTE IDENTITY, asserted three ways against each other rather
than against a committed fixture: an uninterrupted run, a run interrupted after
one tile and resumed, and a run that reads the promoted census out of the shared
cache must all write the same bytes. Three-way rather than against a golden file
because the two grid columns come from a geodesic solve whose last ULP differs
between macOS and glibc (`docs/census.md`), so a committed fixture would have to
carry a numeric tolerance — while comparing runs produced on ONE machine pins
the ordering exactly, which is the property at risk.

The rest fall into three groups: what survives an interruption (only successful
tiles), what the two counters mean (this process vs the whole crawl, which #239
got backwards once), and what an unusable checkpoint does (degrade to a full
fetch, never raise, never a wrong artifact).
"""

import asyncio
import gzip
import json
import os
from datetime import UTC, datetime, timedelta

import mapbox_vector_tile
import pytest

from streetscape_metadata_tracker import download_panoramax as dp
from streetscape_metadata_tracker.checkpointing import (
    CHECKPOINT_STATE_FILENAME,
    CensusCache,
    census_cache_path_for,
    checkpoint_path_for,
    load_census_cache_marker,
)
from streetscape_metadata_tracker.download_common import (
    HOST_PANORAMAX,
    DownloadError,
    HostBlockedError,
    SweepIncompleteError,
)
from tests.test_panoramax import encode_tile, make_picture

SEATTLE = (47.6062, -122.3321)
CITY_ID = "test--wa"
RUN_KWARGS = dict(width=100, height=100, step=20)


@pytest.fixture
def straddling_city():
    """Centred on a z15 tile x-boundary, so the bbox spans two tiles and a
    border picture lands in both. That duplicate is the whole reason reassembly
    order matters — `dedupe_census` keeps the FIRST position."""
    from streetscape_metadata_tracker.download_common import (
        lonlat_to_tile_frac,
        tile_frac_to_lonlat,
    )

    lat = SEATTLE[0]
    fx, fy = lonlat_to_tile_frac(SEATTLE[1], lat, dp.TILE_ZOOM)
    boundary_lon, _ = tile_frac_to_lonlat(int(fx), fy, dp.TILE_ZOOM)
    return lat, boundary_lon


def _tiles(lat, lon):
    return dp.tiles_for_bbox(*dp.grid_bbox(lat, lon, 100, 100, 20))


def _payloads(lat, lon):
    """One picture per tile plus a BORDER picture served by both, which is what
    makes reassembly order observable at all."""
    tiles = _tiles(lat, lon)
    assert len(tiles) >= 2, "this fixture is meant to straddle a tile boundary"
    border = make_picture("border", lon, lat)
    payloads = {}
    for i, tile in enumerate(tiles):
        payloads[tile] = encode_tile(
            [make_picture(f"p{i}", lon + 0.0001 * i, lat + 0.0001 * i), border], *tile
        )
    return payloads


def _stub(monkeypatch, payloads, *, fail=(), record=None):
    """Serve the given tiles; raise for any tile named in ``fail``."""

    async def paced(session, url, timeout, rate_limiter=None, on_request=None, on_empty=None):
        if on_request is not None:
            on_request()
        _, _, tail = url.rpartition("/map/")
        _, x, y = tail.replace(".mvt", "").split("/")
        xy = (int(x), int(y))
        if record is not None:
            record.append(xy)
        if xy in fail:
            raise RuntimeError(f"tile {xy} did not download")
        return payloads.get(xy, mapbox_vector_tile.encode([]))

    monkeypatch.setattr(dp, "_fetch_tile", paced)


def _collect(tmp_path, lat, lon, *, name="run", **kwargs):
    out = str(tmp_path / f"{name}.csv.gz")
    result = asyncio.run(
        dp.download_panoramax_metadata_async("Test City", lat, lon, 100, 100, 20, out, **kwargs)
    )
    return result, out


def _bytes(path):
    with gzip.open(path, "rb") as f:
        return f.read()


def _without_timestamps(raw: bytes) -> list[bytes]:
    """
    The CSV with its `query_timestamp` column blanked.

    Two runs on different clocks differ in that column by construction, and it
    is the one column that is SUPPOSED to differ — so it is removed rather than
    tolerated, leaving every other byte compared exactly.
    """
    lines = raw.decode("utf-8").splitlines()
    header = lines[0].split(",")
    idx = header.index("query_timestamp")
    out = [lines[0].encode()]
    for line in lines[1:]:
        cells = line.split(",")
        cells[idx] = ""
        out.append(",".join(cells).encode())
    return out


# ── 1. Byte identity across an interruption and across the cache ───────────


def test_a_resumed_run_and_a_cache_reuse_write_THE_SAME_BYTES_as_one_pass(
    monkeypatch, tmp_path, straddling_city
):
    """
    THE HEADLINE. Reassembly walks `tiles_for_bbox` rather than a directory
    glob or fetch order, so a border picture served by two tiles resolves to the
    same position however the work was split — across an interruption, and
    across a completely different consumer reading the promoted census.

    Get this wrong and `diff.py` reports imagery churn in every Panoramax city
    that straddles a tile edge, indistinguishable from a real re-drive.
    """
    lat, lon = straddling_city
    payloads = _payloads(lat, lon)
    tiles = _tiles(lat, lon)

    # (a) One uninterrupted pass, no checkpoint at all.
    _stub(monkeypatch, payloads)
    _, plain = _collect(tmp_path, lat, lon, name="plain")

    # (b) Interrupted after the first tile, then resumed.
    checkpoint = str(tmp_path / "cp")
    cache = CensusCache(str(tmp_path / "cache"))
    _stub(monkeypatch, payloads, fail=set(tiles[1:]))
    with pytest.raises(DownloadError):
        _collect(
            tmp_path,
            lat,
            lon,
            name="partial",
            checkpoint_path=checkpoint,
            checkpoint_channel="panoramax",
        )
    assert os.path.exists(os.path.join(checkpoint, CHECKPOINT_STATE_FILENAME))

    served = []
    _stub(monkeypatch, payloads, record=served)
    _, resumed = _collect(
        tmp_path,
        lat,
        lon,
        name="resumed",
        checkpoint_path=checkpoint,
        checkpoint_channel="panoramax",
        census_cache=cache,
    )
    assert set(served) == set(tiles[1:]), "the already-committed tile must not be refetched"
    assert _without_timestamps(_bytes(plain)) == _without_timestamps(_bytes(resumed))

    # (c) A second channel reads the promoted census for zero requests.
    served.clear()
    _, reused = _collect(
        tmp_path,
        lat,
        lon,
        name="reused",
        checkpoint_path=str(tmp_path / "cp-walk"),
        checkpoint_channel="panoramax_streets",
        census_cache=cache,
    )
    assert served == [], "a cache hit must issue no tile requests at all"
    assert _without_timestamps(_bytes(plain)) == _without_timestamps(_bytes(reused))


# ── 2. What survives an interruption ───────────────────────────────────────


def test_only_successful_tiles_are_committed(monkeypatch, tmp_path, straddling_city):
    """
    A tile that failed stays refetchable, which is also what keeps #168's
    tolerance measuring against the FULL tile set rather than against whatever
    one process happened to attempt.
    """
    lat, lon = straddling_city
    tiles = _tiles(lat, lon)
    checkpoint = str(tmp_path / "cp")

    _stub(monkeypatch, _payloads(lat, lon), fail=set(tiles[1:]))
    with pytest.raises(DownloadError):
        _collect(
            tmp_path,
            lat,
            lon,
            checkpoint_path=checkpoint,
            checkpoint_channel="panoramax",
        )

    with open(os.path.join(checkpoint, CHECKPOINT_STATE_FILENAME)) as f:
        state = json.load(f)
    done = {(x, y) for x, y, _ in state["done_tiles"]}
    assert done == {tiles[0]}


def test_an_empty_tile_gets_a_RECORD_AND_NO_FILE(monkeypatch, tmp_path, straddling_city):
    """
    Most z15 tiles over a real bbox hold nothing, and a part file for each would
    mean thousands of files per city to say nothing. The record's row count is
    what separates "empty tile, already fetched" from "not fetched yet".
    """
    lat, lon = straddling_city
    tiles = _tiles(lat, lon)
    checkpoint = str(tmp_path / "cp")

    # Nothing anywhere: every tile decodes to zero rows.
    _stub(monkeypatch, {})
    _collect(tmp_path, lat, lon, checkpoint_path=checkpoint, checkpoint_channel="panoramax")

    with open(os.path.join(checkpoint, CHECKPOINT_STATE_FILENAME)) as f:
        state = json.load(f)
    assert {(x, y) for x, y, _ in state["done_tiles"]} == set(tiles)
    assert all(rows == 0 for _, _, rows in state["done_tiles"])
    assert [n for n in os.listdir(checkpoint) if n.endswith(".parquet")] == []


# ── 3. The two counters ────────────────────────────────────────────────────


def test_a_resume_charges_only_ITS_OWN_requests_to_the_daily_ledger(
    monkeypatch, tmp_path, straddling_city
):
    """
    `api_requests` is THIS process's spend and `api_requests_total` is the
    crawl's, and #239 got this backwards once. `db.add_api_usage` is additive
    and keyed by (date, provider), so a resumed night reporting the whole crawl
    would charge last night's tiles against tonight's budget gate.

    The total is asserted as the SUM of the two nights rather than as the tile
    count, and the difference is the finding: the first night ATTEMPTED every
    tile and committed one, so the crawl cost more requests than the lattice has
    tiles. A total equal to the tile count would mean the failed attempts had
    been dropped from the crawl's price — which is the direction that
    under-reports what a city actually cost.
    """
    lat, lon = straddling_city
    tiles = _tiles(lat, lon)
    checkpoint = str(tmp_path / "cp")

    _stub(monkeypatch, _payloads(lat, lon), fail=set(tiles[1:]))
    with pytest.raises(DownloadError) as first:
        _collect(tmp_path, lat, lon, checkpoint_path=checkpoint, checkpoint_channel="panoramax")
    night_one = first.value.api_requests
    assert night_one == len(tiles), "every tile was attempted; only one came back"

    _stub(monkeypatch, _payloads(lat, lon))
    result, _ = _collect(
        tmp_path,
        lat,
        lon,
        name="resumed",
        checkpoint_path=checkpoint,
        checkpoint_channel="panoramax",
    )
    assert result["api_requests"] == len(tiles) - 1, "only the tiles this call fetched"
    assert result["api_requests_total"] == night_one + result["api_requests"]


def test_a_blocked_nights_refused_requests_still_reach_the_crawl_total(
    monkeypatch, tmp_path, straddling_city
):
    """
    The requests a block refuses are counted into `api_usage` deliberately — one
    token, one counted request — but they die with the process unless
    `_commit_spend` writes them into the record. Without it a resumed run's
    catalog row prices the city below what it actually cost.
    """
    lat, lon = straddling_city
    tiles = _tiles(lat, lon)
    checkpoint = str(tmp_path / "cp")

    payloads = _payloads(lat, lon)
    blocked_after = {"n": 0}

    async def paced(session, url, timeout, rate_limiter=None, on_request=None, on_empty=None):
        if on_request is not None:
            on_request()
        blocked_after["n"] += 1
        _, _, tail = url.rpartition("/map/")
        _, x, y = tail.replace(".mvt", "").split("/")
        xy = (int(x), int(y))
        if xy != tiles[0]:
            raise HostBlockedError("refused", host=HOST_PANORAMAX)
        return payloads[xy]

    monkeypatch.setattr(dp, "_fetch_tile", paced)
    with pytest.raises(HostBlockedError) as excinfo:
        _collect(tmp_path, lat, lon, checkpoint_path=checkpoint, checkpoint_channel="panoramax")
    spent = excinfo.value.api_requests
    assert spent > 1, "the refusal itself was a request and must be counted"

    with open(os.path.join(checkpoint, CHECKPOINT_STATE_FILENAME)) as f:
        state = json.load(f)
    assert state["api_requests_total"] == spent


# ── 4. An unusable checkpoint degrades; it never raises and never lies ─────


@pytest.mark.parametrize(
    "mutate,why",
    [
        (lambda s: s.update(format_version=99), "a format this build does not write"),
        (lambda s: s.update(zoom=14), "the Mapillary zoom, whose tile indices mean something else"),
        (lambda s: s.update(tile_count=999), "a lattice this run does not have"),
        (lambda s: s.update(channel="mapillary"), "another channel's ledger"),
        (lambda s: s.update(variant="all_public"), "another crawl of this channel"),
        (
            lambda s: s.update(created_at=(datetime.now(UTC) - timedelta(days=30)).isoformat()),
            "rows old enough to be spliced into a snapshot dated today",
        ),
        (lambda s: s.update(bbox=[0.0, 0.0, 1.0, 1.0]), "a different frame"),
    ],
)
def test_an_unusable_checkpoint_is_discarded_and_refetched_without_raising(
    monkeypatch, tmp_path, straddling_city, mutate, why
):
    """
    A checkpoint is not a comparison whose mismatch corrupts an artifact — the
    worst case of ignoring one is a re-spend — so every one of these degrades to
    a full fetch with a warning. Refusing outright would cost a night to protect
    nothing; resuming would splice the wrong rows into a dated snapshot.
    """
    lat, lon = straddling_city
    tiles = _tiles(lat, lon)
    checkpoint = str(tmp_path / "cp")

    _stub(monkeypatch, _payloads(lat, lon), fail=set(tiles[1:]))
    with pytest.raises(DownloadError):
        _collect(tmp_path, lat, lon, checkpoint_path=checkpoint, checkpoint_channel="panoramax")

    state_path = os.path.join(checkpoint, CHECKPOINT_STATE_FILENAME)
    with open(state_path) as f:
        state = json.load(f)
    mutate(state)
    with open(state_path, "w") as f:
        json.dump(state, f)

    served = []
    _stub(monkeypatch, _payloads(lat, lon), record=served)
    result, _ = _collect(
        tmp_path,
        lat,
        lon,
        name="fresh",
        checkpoint_path=checkpoint,
        checkpoint_channel="panoramax",
    )
    assert set(served) == set(tiles), f"a checkpoint describing {why} must be refetched whole"
    assert result["api_requests"] == len(tiles)
    assert result["api_requests_total"] == len(tiles), (
        "a discarded checkpoint's spend must not be carried into the new crawl"
    )


def test_no_checkpoint_path_is_the_pre_checkpoint_behaviour_exactly(
    monkeypatch, tmp_path, straddling_city
):
    """Passing None must be byte-equivalent to the fetch-everything path, and
    must leave nothing on disk to clean up."""
    lat, lon = straddling_city
    _stub(monkeypatch, _payloads(lat, lon))
    result, _ = _collect(tmp_path, lat, lon)
    assert result["checkpoint_path"] is None
    assert not any(p.name == "cp" for p in tmp_path.iterdir())


def test_a_city_refused_before_committing_anything_leaves_no_empty_directory(
    monkeypatch, tmp_path, straddling_city
):
    """Otherwise every blocked night leaves a directory behind, on every city."""
    lat, lon = straddling_city
    checkpoint = str(tmp_path / "cp")

    async def refuse(session, url, timeout, rate_limiter=None, on_request=None, on_empty=None):
        if on_request is not None:
            on_request()
        raise HostBlockedError("refused", host=HOST_PANORAMAX)

    monkeypatch.setattr(dp, "_fetch_tile", refuse)
    with pytest.raises(HostBlockedError):
        _collect(tmp_path, lat, lon, checkpoint_path=checkpoint, checkpoint_channel="panoramax")
    assert not os.path.exists(checkpoint)


# ── 5. The shared census cache, and who is charged for what ───────────────


def test_the_cache_records_who_paid_and_prices_a_cross_channel_reuse_at_zero(
    monkeypatch, tmp_path, straddling_city
):
    """
    `api_requests` is 0 for every reuser — charging one would bill another
    channel's spend against its budget gate — while `api_requests_total` is the
    crawl's cost only for the (channel, variant) that actually paid. The
    asymmetry is easy to get backwards, so both halves are asserted.
    """
    lat, lon = straddling_city
    tiles = _tiles(lat, lon)
    cache = CensusCache(str(tmp_path / "cache"))

    _stub(monkeypatch, _payloads(lat, lon))
    paid, _ = _collect(
        tmp_path,
        lat,
        lon,
        name="grid",
        checkpoint_path=str(tmp_path / "cp-grid"),
        checkpoint_channel="panoramax",
        census_cache=cache,
    )
    assert paid["api_requests"] == len(tiles)
    assert paid["census_reused"] is False
    # Promotion MOVED the directory, so the caller must not chase it.
    assert paid["checkpoint_path"] is None

    marker = load_census_cache_marker(cache.path, run_date=None)
    assert marker is not None
    assert marker["fetched_by"] == "panoramax"

    served = []
    _stub(monkeypatch, _payloads(lat, lon), record=served)
    reuser, _ = _collect(
        tmp_path,
        lat,
        lon,
        name="walk",
        checkpoint_path=str(tmp_path / "cp-walk"),
        checkpoint_channel="panoramax_streets",
        census_cache=cache,
    )
    assert served == []
    assert reuser["api_requests"] == 0
    assert reuser["api_requests_total"] == 0, "a different channel did not pay for this census"
    assert reuser["census_reused"] is True
    assert reuser["census_fetched_by"] == "panoramax"


def test_refetch_census_ignores_the_entry_and_pays_again(monkeypatch, tmp_path, straddling_city):
    """
    `--refetch-census` is about the OBSERVATION — take it now — which is
    deliberately not what `--force` means. A run that reused the entry here
    would silently republish a week-old observation the operator asked to
    replace.
    """
    lat, lon = straddling_city
    tiles = _tiles(lat, lon)
    cache_path = str(tmp_path / "cache")

    _stub(monkeypatch, _payloads(lat, lon))
    _collect(
        tmp_path,
        lat,
        lon,
        name="grid",
        checkpoint_path=str(tmp_path / "cp-grid"),
        checkpoint_channel="panoramax",
        census_cache=CensusCache(cache_path),
    )

    served = []
    _stub(monkeypatch, _payloads(lat, lon), record=served)
    result, _ = _collect(
        tmp_path,
        lat,
        lon,
        name="again",
        checkpoint_path=str(tmp_path / "cp-again"),
        checkpoint_channel="panoramax",
        census_cache=CensusCache(cache_path, reuse=False),
    )
    assert set(served) == set(tiles)
    assert result["api_requests"] == len(tiles)


def test_an_INCOMPLETE_crawl_is_never_promoted(monkeypatch, tmp_path, straddling_city):
    """
    Completeness is the whole difference between a checkpoint and a cache entry.
    A resume is allowed to be partial by definition; a partial entry reused as a
    census would publish the missing tiles' grid points as genuine no-imagery —
    absence never observed — in an immutable dated snapshot.
    """
    lat, lon = straddling_city
    tiles = _tiles(lat, lon)
    cache = CensusCache(str(tmp_path / "cache"))

    _stub(monkeypatch, _payloads(lat, lon), fail=set(tiles[1:]))
    with pytest.raises(DownloadError):
        _collect(
            tmp_path,
            lat,
            lon,
            checkpoint_path=str(tmp_path / "cp"),
            checkpoint_channel="panoramax",
            census_cache=cache,
        )
    assert load_census_cache_marker(cache.path, run_date=None) is None


def test_the_crawl_store_paths_are_channel_keyed_date_free_and_outside_data(tmp_path):
    """
    Both halves of #290's key rule, from the one derivation every consumer uses.
    A cache entry keyed by channel would reuse nothing with nothing failing; a
    checkpoint NOT keyed by channel would let two crawls resume each other into
    the wrong ledger. And a date in either path would restart the crawl nightly,
    since a run is dated on the day it COMPLETES.
    """
    from streetscape_metadata_tracker.checkpointing import CENSUS_PROVIDERS

    assert "panoramax" in CENSUS_PROVIDERS

    bbox = dp.grid_bbox(*SEATTLE, 100, 100, 20)
    grid_cp = checkpoint_path_for(CITY_ID, bbox, "panoramax")
    walk_cp = checkpoint_path_for(CITY_ID, bbox, "panoramax_streets")
    entry = census_cache_path_for("panoramax", CITY_ID, bbox)

    assert grid_cp != walk_cp, "two channels must not resume each other's crawl"
    assert census_cache_path_for("panoramax", CITY_ID, bbox) == entry
    for path in (grid_cp, walk_cp, entry):
        assert "2026" not in os.path.basename(path), "a dated path restarts every night"
        assert f"{os.sep}data{os.sep}" not in path, (
            "a partial census must never reach the publisher"
        )


# ── 5. Stopping at a request cap, and continuing (issue #318) ───────────────
#
# Panoramax's crawl is Mapillary's, so the cap is the same code reached through
# a different module -- which is exactly why it is tested here rather than
# assumed from the Mapillary suite. The two censuses have diverged before (the
# zoom, the 404-is-empty rule, `type` being two-state), and a cap that raised
# the wrong exception or promoted a partial entry would be silent in both.
#
# Nothing SCHEDULES Panoramax yet -- CHANNEL_RESUMABLE keeps it False because no
# launch arm forwards a cap -- so these pin the collector alone, which is the
# half that has to be right before that flag can flip.


def test_a_census_stopped_at_its_cap_pauses_rather_than_failing(
    monkeypatch, tmp_path, straddling_city
):
    """The exception is the exit code: SweepIncompleteError becomes 83, which a
    scheduler amnesties, where a plain DownloadError becomes 1 and counts a
    consecutive_failure against the city."""
    lat, lon = straddling_city
    tiles = _tiles(lat, lon)
    checkpoint = str(tmp_path / "cp")
    record = []
    _stub(monkeypatch, _payloads(lat, lon), record=record)

    with pytest.raises(SweepIncompleteError) as excinfo:
        _collect(
            tmp_path,
            lat,
            lon,
            connection_limit=1,
            max_requests=1,
            checkpoint_path=checkpoint,
            checkpoint_channel="panoramax",
        )

    error = excinfo.value
    assert len(record) == 1, "the cap must stop dispatching, not merely be recorded"
    assert (error.units_done, error.unit_count, error.unit_name) == (1, len(tiles), "tiles")
    assert error.checkpoint_path == checkpoint
    assert error.api_requests == 1


def test_a_capped_census_resumes_to_the_same_bytes_as_an_uninterrupted_one(
    monkeypatch, tmp_path, straddling_city
):
    """The file's headline contract, reached by the new route.

    A cap skips its remaining tiles by RETURNING rather than raising, which is a
    different path through the settle loop and the reassembly than the block
    this file already pins. Landing on the same bytes is what says the choice
    changed nothing about the artifact -- including the border duplicate, whose
    winner `dedupe_census` picks by position.
    """
    lat, lon = straddling_city
    payloads = _payloads(lat, lon)

    _stub(monkeypatch, payloads)
    _whole, whole_path = _collect(tmp_path, lat, lon, name="whole")

    checkpoint = str(tmp_path / "cp")
    _stub(monkeypatch, payloads)
    with pytest.raises(SweepIncompleteError):
        _collect(
            tmp_path,
            lat,
            lon,
            name="capped",
            connection_limit=1,
            max_requests=1,
            checkpoint_path=checkpoint,
            checkpoint_channel="panoramax",
        )
    assert not os.path.exists(str(tmp_path / "capped.csv.gz")), "nothing is finalized"

    _stub(monkeypatch, payloads)
    _resumed, resumed_path = _collect(
        tmp_path,
        lat,
        lon,
        name="resumed",
        connection_limit=1,
        checkpoint_path=checkpoint,
        checkpoint_channel="panoramax",
    )
    assert _without_timestamps(_bytes(resumed_path)) == _without_timestamps(_bytes(whole_path))


def test_a_capped_census_is_never_promoted_into_the_shared_cache(
    monkeypatch, tmp_path, straddling_city
):
    """A partial entry reused as a census publishes absence nobody observed."""
    lat, lon = straddling_city
    cache_path = census_cache_path_for("panoramax", CITY_ID, dp.grid_bbox(lat, lon, 100, 100, 20))
    _stub(monkeypatch, _payloads(lat, lon))

    with pytest.raises(SweepIncompleteError):
        _collect(
            tmp_path,
            lat,
            lon,
            connection_limit=1,
            max_requests=1,
            checkpoint_path=str(tmp_path / "cp"),
            checkpoint_channel="panoramax",
            census_cache=CensusCache(cache_path, True, None),
        )

    assert not os.path.exists(cache_path)
    assert load_census_cache_marker(cache_path) is None


def test_a_cap_without_a_checkpoint_is_refused_before_any_request(
    monkeypatch, tmp_path, straddling_city
):
    """Capped and uncheckpointed, a crawl discards everything it paid for."""
    lat, lon = straddling_city
    record = []
    _stub(monkeypatch, _payloads(lat, lon), record=record)

    with pytest.raises(ValueError, match="max_requests needs a checkpoint_path"):
        _collect(tmp_path, lat, lon, connection_limit=1, max_requests=1)
    assert record == [], "refused before a single tile was asked for"


# The two refusal arms and the overshoot, duplicated from the Mapillary census
# rather than assumed to hold here (#318 review). The code is currently
# identical, which is the argument for the duplication and not against it: this
# file exists because the two censuses HAVE diverged before, and a shared
# invariant tested on only one of them is exactly how the next divergence ships.


def test_a_cap_reached_with_a_degraded_checkpoint_fails_rather_than_pausing(
    monkeypatch, tmp_path, straddling_city
):
    """83 tells an operator to re-run; with nothing to resume from, that loops.

    A checkpoint whose commits latched off mid-crawl holds nothing the next
    invocation can continue from, so the pause line would send an operator to a
    command that spends the same requests and stops in the same place, forever
    — and exit 83 is amnestied, so no failure is counted and no alert fires.
    A plain DownloadError takes none of that amnesty.

    Set up by patching `_open_tile_checkpoint` rather than by omitting the path,
    because omitting it is what the up-front `ValueError` refuses: testing the
    runtime arm through the path the caller guard already blocks would exercise
    neither.
    """
    lat, lon = straddling_city
    real_open = dp._open_tile_checkpoint

    def degraded_open(*args, **kwargs):
        cp = real_open(*args, **kwargs)
        if cp is not None:
            cp.degraded = True
        return cp

    monkeypatch.setattr(dp, "_open_tile_checkpoint", degraded_open)
    _stub(monkeypatch, _payloads(lat, lon))

    with pytest.raises(DownloadError) as excinfo:
        _collect(
            tmp_path,
            lat,
            lon,
            connection_limit=1,
            max_requests=1,
            checkpoint_path=str(tmp_path / "cp"),
            checkpoint_channel="panoramax",
        )
    assert not isinstance(excinfo.value, SweepIncompleteError)
    assert "nothing can be resumed" in str(excinfo.value).lower()
    # The spend still reaches the ledger: the request was made either way.
    assert excinfo.value.api_requests == 1


def test_a_cap_that_commits_no_tile_is_a_failure_not_a_pause(
    monkeypatch, tmp_path, straddling_city
):
    """The subtle third way to have nothing to resume: a LIVE, healthy, EMPTY store.

    `_commit_spend` returns early on an empty `done`, deliberately, so a crawl
    that commits no tile writes no `state.json` at all. A cap whose one in-flight
    tile fails transiently reaches exactly that state — and it is not
    hypothetical for Panoramax, whose launch floor is one tile's retries.
    """
    lat, lon = straddling_city
    tiles = _tiles(lat, lon)
    # The only tile the cap allows is also the one that fails, so `done` is empty
    # while the checkpoint itself is perfectly healthy.
    _stub(monkeypatch, _payloads(lat, lon), fail=(tiles[0],))

    with pytest.raises(DownloadError) as excinfo:
        _collect(
            tmp_path,
            lat,
            lon,
            connection_limit=1,
            max_requests=1,
            checkpoint_path=str(tmp_path / "cp"),
            checkpoint_channel="panoramax",
        )
    assert not isinstance(excinfo.value, SweepIncompleteError), (
        "an empty checkpoint must not be reported as resumable progress"
    )
    assert "nothing can be resumed" in str(excinfo.value).lower()


def test_the_cap_is_not_overshot_by_the_tasks_already_in_flight(
    monkeypatch, tmp_path, straddling_city
):
    """Measured as an EQUALITY, which needs a stub that actually suspends.

    `_stub` calls `on_request()` and returns without ever awaiting, so the tasks
    never interleave and any overshoot is invisible to it. A real fetch awaits
    the rate limiter first, and every task the semaphore admitted used to clear
    a check reading a stale `api_requests` before queueing behind it —
    `connection_limit - 1` requests over the cap, on a host with no credential
    and no documented rate limit at all.
    """
    lat, lon = straddling_city
    # Wide enough that a runaway would be unmistakable.
    bbox = dp.grid_bbox(lat, lon, 2000, 2000, 20)
    tiles = dp.tiles_for_bbox(*bbox)
    assert len(tiles) > 8, "needs enough tiles for a runaway to be visible"

    record = []

    async def suspending(session, url, timeout, rate_limiter=None, on_request=None, on_empty=None):
        # Where a real limiter yields: between the cap check and the count.
        await asyncio.sleep(0)
        if on_request is not None:
            on_request()
        record.append(url)
        return mapbox_vector_tile.encode([])

    monkeypatch.setattr(dp, "_fetch_tile", suspending)

    connection_limit = 10
    max_requests = 3
    with pytest.raises(SweepIncompleteError):
        asyncio.run(
            dp.fetch_city_images_async(
                "Test City",
                bbox,
                connection_limit=connection_limit,
                max_requests=max_requests,
                checkpoint_path=str(tmp_path / "cp"),
                checkpoint_channel="panoramax",
            )
        )
    assert len(record) == max_requests, "a capped crawl must spend its cap and no more"
    assert len(record) < len(tiles), "the cap must stop the city, not merely dent it"
