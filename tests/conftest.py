"""Shared fixtures: temp data dir, catalog DB, and a synthetic city CSV factory."""

import gzip
import os
import sys
from datetime import UTC, date, datetime

import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from streetscape_metadata_tracker import db  # noqa: E402
from streetscape_metadata_tracker.config import (  # noqa: E402
    KARTAVIEW_METADATA_DTYPES,
    MAPILLARY_METADATA_DTYPES,
    METADATA_DTYPES,
    PANORAMAX_METADATA_DTYPES,
)

# The run CSV schema, from its single source of truth — a column added to
# (or reordered in) METADATA_DTYPES flows into every synthetic fixture.
COLUMNS = list(METADATA_DTYPES)

# The census providers carry extra columns past that core: Mapillary seven,
# KartaView nine (issue #225), Panoramax four (issue #316). All three taken
# from the same single source of truth, and in the SAME order their downloaders
# write them, so a fixture cannot disagree with a real run file about the
# column set.
#
# Mapillary's builder was on the bare nine until the #360 review, which is the
# gap that comment described itself as closing: the fixture's "Mapillary run"
# was missing every column MAPILLARY_EXTRA_DTYPES declares. Nothing in `www/`
# reads them, so it cost nothing visible — which is exactly why it survived
# three providers' worth of edits to the file.
MAPILLARY_COLUMNS = list(MAPILLARY_METADATA_DTYPES)
KARTAVIEW_COLUMNS = list(KARTAVIEW_METADATA_DTYPES)
PANORAMAX_COLUMNS = list(PANORAMAX_METADATA_DTYPES)


def make_city_df(
    panos,
    run_date=date(2026, 1, 15),
    grid_origin=(44.0, -121.0),
    n_empty=1,
    copyright_info="© Google",
):
    """
    Build a synthetic run DataFrame.

    Args:
        panos: list of (pano_id, capture_date_str) — one OK grid point each
        run_date: embedded in query_timestamp
        grid_origin: (lat, lon) of the first grid point; points step by 0.001
        n_empty: trailing ZERO_RESULTS points
        copyright_info: value for OK rows; None mimics archival imports
            that never captured copyright (issue #93)

    Returns raw (string-typed) DataFrame, like a freshly written CSV.
    """
    ts = datetime(run_date.year, run_date.month, run_date.day, 12, 0, tzinfo=UTC).isoformat()
    rows = []
    lat0, lon0 = grid_origin
    for i, (pano_id, capture) in enumerate(panos):
        rows.append(
            (
                lat0 + i * 0.001,
                lon0,
                ts,
                lat0 + i * 0.001 + 0.0001,
                lon0 + 0.0001,
                pano_id,
                capture,
                copyright_info,
                "OK",
            )
        )
    for j in range(n_empty):
        rows.append(
            (
                lat0 + (len(panos) + j) * 0.001,
                lon0,
                ts,
                None,
                None,
                None,
                None,
                None,
                "ZERO_RESULTS",
            )
        )
    return pd.DataFrame(rows, columns=COLUMNS)


def make_kartaview_city_df(
    panos,
    run_date=date(2026, 1, 15),
    grid_origin=(44.0, -121.0),
    n_empty=1,
    panos_per_point=1,
    n_flat_only=0,
):
    """
    Build a synthetic KartaView run DataFrame (issue #225).

    A census like Mapillary's and Panoramax's — several OK rows may share one
    grid point — on the wider KARTAVIEW_COLUMNS schema. The three extra things
    worth knowing, all of which the frontend reads:

      * ``sequence_id``/``sequence_index`` are what KartaView's viewer is
        addressed BY (``details/<sequence>/<index>``), not ``pano_id``, so a
        row missing either builds no photo link. Both are populated here; the
        null-sequence case has its own node test.
      * ``is_pano`` is ``projection == "SPHERE"``; the FLAT_ONLY rows are the
        PLANE dashcam imagery that makes up most of the catalog outside the
        Grab fleet markets (issue #116).
      * ``copyright_info`` is ``© KartaView contributor <username>``, an
        attribution requirement (CC BY-SA 4.0) rather than the official-fleet
        marker it is for GSV — so nothing downstream may filter on it.

    ``date_added`` is the server-side upload time and is deliberately AFTER
    every capture date: ``shot_date >= date_added`` is the rejected-as-null
    case (download_kartaview.shot_date_to_iso_date), and a fixture that tripped
    it would silently publish a run with no dates at all.

    Args:
        panos: list of (pano_id, capture_date_str)
        panos_per_point: how many consecutive panos share each grid point
        n_flat_only: trailing FLAT_ONLY points (issue #116) — flat-imagery
            presence markers with a representative pano_id/coords but a null
            capture_date, on grid points distinct from the pano/empty ones
        run_date, grid_origin, n_empty: as in make_city_df

    Returns raw (string-typed) DataFrame, like a freshly written CSV.
    """
    ts = datetime(run_date.year, run_date.month, run_date.day, 12, 0, tzinfo=UTC).isoformat()
    rows = []
    lat0, lon0 = grid_origin
    n_points_used = 0
    # KartaView ids are numeric strings on every column that carries one, and
    # the sequence is the DRIVE these photos came from — one drive, many
    # photos, which is the grouping GSV cannot offer (pano-spacing.md).
    username = "testdriver"
    sequence = "11616154"
    # The first photo's position WITHIN that drive. 1 rather than 0 because
    # this sequence is not invented: `streetscape-utils.js` records the probed
    # real datum "image 2627370567 lives at sequence 11616154, index 1", and
    # the e2e asserts the exact `details/11616154/1` the viewer is addressed
    # by. A fixture that reuses a real id under a different index is a small
    # lie that reads as a finding later.
    first_index = 1
    # Space-separated, not ISO: that is the shape the API returns, and it is
    # why download_kartaview parses the two date columns separately.
    added = f"{run_date.isoformat()} 21:08:37"
    for i, (pano_id, capture) in enumerate(panos):
        point = i // panos_per_point
        n_points_used = point + 1
        rows.append(
            (
                lat0 + point * 0.001,
                lon0,
                ts,
                lat0 + point * 0.001 + 0.0001,
                lon0 + 0.0001,
                pano_id,
                capture,
                f"© KartaView contributor {username}",
                "OK",
                username,
                sequence,
                first_index + i,
                True,
                360.0,
                90.0 + i,
                added,
                "CMNT",
                "12345678",
            )
        )
    for k in range(n_flat_only):
        point = n_points_used + k
        rows.append(
            (
                lat0 + point * 0.001,
                lon0,
                ts,
                lat0 + point * 0.001 + 0.0001,
                lon0 + 0.0001,
                # The real photo id of the flat image picked as this point's
                # representative, as census.build_image_rows copies it — and
                # the row still carries a sequence, so the map fallback link
                # has coordinates and the photo link has an address.
                f"262737055{k}",
                None,  # FLAT_ONLY rows carry no capture date
                f"© KartaView contributor {username}",
                "FLAT_ONLY",
                username,
                sequence,
                first_index + len(panos) + k,
                False,
                120.0,  # PLANE imagery: a lens angle, not a full sphere
                90.0,
                added,
                "CMNT",
                "12345678",
            )
        )
    for j in range(n_empty):
        rows.append(
            (
                lat0 + (n_points_used + n_flat_only + j) * 0.001,
                lon0,
                ts,
                None,
                None,
                None,
                None,
                None,
                "ZERO_RESULTS",
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
            )
        )
    df = pd.DataFrame(rows, columns=KARTAVIEW_COLUMNS)
    # ``sequence_index`` has to survive as an INTEGER. The ZERO_RESULTS rows
    # carry None, which makes pandas infer the whole column float64 — and a
    # written CSV then says "0.0", which is the position inside a drive that
    # KartaView's viewer URL is addressed by, so the link becomes
    # ``details/<sequence>/0.0`` and opens nothing. That is the same float
    # coercion PROVIDER_RUN_DTYPES exists to prevent on the read side; here it
    # would happen on the write side, in a fixture claiming to be a run file.
    #
    # Deleting this line left the whole fast suite green (#360 review): only
    # regenerating the fixture and then running the BROWSER suite caught it,
    # and that job is `continue-on-error: true`. It is pinned now, on the
    # committed bytes and for every provider, by
    # `test_every_committed_run_csv_writes_its_integer_columns_as_integers`.
    df["sequence_index"] = df["sequence_index"].astype("Int64")
    return df


def make_mapillary_city_df(
    panos,
    run_date=date(2026, 1, 15),
    grid_origin=(44.0, -121.0),
    n_empty=1,
    panos_per_point=1,
    n_flat_only=0,
):
    """
    Build a synthetic Mapillary run DataFrame.

    Mapillary runs keep every pano: multiple OK rows can share one grid
    point (query_lat/query_lon), and copyright_info names the contributor.

    On the full MAPILLARY_COLUMNS schema — the nine shared columns plus the
    seven MAPILLARY_EXTRA_DTYPES ones the tile layer hands over for free
    (creator, organization, sequence, is_pano, on_foot, quality_score,
    compass_angle). Nothing in ``www/`` reads any of them, which is how this
    builder stayed on the bare core while its KartaView and Panoramax siblings
    were written against the real schema (#360 review).

    Args:
        panos: list of (pano_id, capture_date_str)
        panos_per_point: how many consecutive panos share each grid point
        n_flat_only: trailing FLAT_ONLY points (issue #116) — flat-imagery
            presence markers with a representative pano_id/coords but a null
            capture_date, on grid points distinct from the pano/empty ones
        run_date, grid_origin, n_empty: as in make_city_df

    Returns raw (string-typed) DataFrame, like a freshly written CSV.
    """
    ts = datetime(run_date.year, run_date.month, run_date.day, 12, 0, tzinfo=UTC).isoformat()
    rows = []
    lat0, lon0 = grid_origin
    n_points_used = 0
    # One capture drive, as the tile layer reports it. `creator_id` is the same
    # contributor the copyright string names — they are two spellings of one
    # fact in a real run, so a fixture where they disagree would make a parity
    # check pass that should not.
    sequence = "s6q8Xt0ZPfDVgHkRYcW3aM"
    for i, (pano_id, capture) in enumerate(panos):
        point = i // panos_per_point
        n_points_used = point + 1
        creator = 100 + i % 3
        rows.append(
            (
                lat0 + point * 0.001,
                lon0,
                ts,
                lat0 + point * 0.001 + 0.0001,
                lon0 + 0.0001,
                pano_id,
                capture,
                f"© Mapillary contributor {creator}",
                "OK",
                str(creator),
                None,  # individual contributor, not an organization fleet
                sequence,
                True,
                False,  # vehicle capture, not on foot
                0.75,
                90.0 + i,
            )
        )
    for k in range(n_flat_only):
        point = n_points_used + k
        creator = 200 + k
        rows.append(
            (
                lat0 + point * 0.001,
                lon0,
                ts,
                lat0 + point * 0.001 + 0.0001,
                lon0 + 0.0001,
                f"flat{k}",
                None,  # FLAT_ONLY rows carry no capture date
                f"© Mapillary contributor {creator}",
                "FLAT_ONLY",
                str(creator),
                None,
                sequence,
                False,  # the flat/perspective imagery issue #116 counts apart
                True,  # ...and on foot, which is what the score penalizes
                0.35,
                90.0,
            )
        )
    for j in range(n_empty):
        rows.append(
            (
                lat0 + (n_points_used + n_flat_only + j) * 0.001,
                lon0,
                ts,
                None,
                None,
                None,
                None,
                None,
                "ZERO_RESULTS",
                None,
                None,
                None,
                None,
                None,
                None,
                None,
            )
        )
    return pd.DataFrame(rows, columns=MAPILLARY_COLUMNS)


def make_panoramax_city_df(
    panos,
    run_date=date(2026, 1, 15),
    grid_origin=(44.0, -121.0),
    n_empty=1,
    panos_per_point=1,
    n_flat_only=0,
):
    """
    Build a synthetic Panoramax run DataFrame (issue #316).

    A census like Mapillary's — several OK rows may share one grid point — on
    the wider PANORAMAX_COLUMNS schema. The two extra things worth knowing,
    both of which the frontend reads:

      * ``image_type`` is the provider's RAW word, kept beside the boolean it
        produced. ``equirectangular`` on 360° rows and ``flat`` on FLAT_ONLY
        ones here, matching the only two values measured across the
        federation — but a run file records what the provider said, so a
        consumer must tolerate a third.
      * ``copyright_info`` is ``© Panoramax contributor <uuid>``: a
        contributor id, not an official-fleet marker, which is why the
        provider declares no copyright filter.

    Every id column is a UUID, FLAT_ONLY rows included — that is the shape a
    real run carries, and ``pano_id`` is what the frontend interpolates into
    the federation viewer's permalink.

    Args:
        panos: list of (pano_id, capture_date_str)
        panos_per_point: how many consecutive panos share each grid point
        n_flat_only: trailing FLAT_ONLY points (issue #116) — flat-imagery
            presence markers with a representative pano_id/coords but a null
            capture_date, on grid points distinct from the pano/empty ones
        run_date, grid_origin, n_empty: as in make_city_df

    Returns raw (string-typed) DataFrame, like a freshly written CSV.
    """
    ts = datetime(run_date.year, run_date.month, run_date.day, 12, 0, tzinfo=UTC).isoformat()
    rows = []
    lat0, lon0 = grid_origin
    n_points_used = 0
    # Panoramax ids are UUIDs on every column that carries one, which is what
    # makes a `pano_id` usable as a viewer permalink at all.
    account = "cddcb3f2-9b2f-454b-8f03-da7dd6a966e2"
    sequence = "af768120-d9b0-483e-a10a-08a2f69753b6"
    for i, (pano_id, capture) in enumerate(panos):
        point = i // panos_per_point
        n_points_used = point + 1
        rows.append(
            (
                lat0 + point * 0.001,
                lon0,
                ts,
                lat0 + point * 0.001 + 0.0001,
                lon0 + 0.0001,
                pano_id,
                capture,
                f"© Panoramax contributor {account}",
                "OK",
                account,
                sequence,
                True,
                "equirectangular",
            )
        )
    for k in range(n_flat_only):
        point = n_points_used + k
        rows.append(
            (
                lat0 + point * 0.001,
                lon0,
                ts,
                lat0 + point * 0.001 + 0.0001,
                lon0 + 0.0001,
                # A UUID like every other Panoramax id, not a ``flat{k}``
                # placeholder: census.build_image_rows copies the REAL picture
                # id of the flat image it picked as the point's representative,
                # and this is the one column a consumer turns into a viewer
                # permalink (``?focus=pic&pic=``). A placeholder here is a
                # fixture that does not look like a run file at exactly the
                # spot where that matters.
                f"6e4f0b{k:02x}-9c31-4f6a-bd47-7c1e0a5b93{k:02x}",
                None,  # FLAT_ONLY rows carry no capture date
                f"© Panoramax contributor {account}",
                "FLAT_ONLY",
                account,
                sequence,
                False,
                "flat",
            )
        )
    for j in range(n_empty):
        rows.append(
            (
                lat0 + (n_points_used + n_flat_only + j) * 0.001,
                lon0,
                ts,
                None,
                None,
                None,
                None,
                None,
                "ZERO_RESULTS",
                None,
                None,
                None,
                None,
            )
        )
    return pd.DataFrame(rows, columns=PANORAMAX_COLUMNS)


def write_city_csv_gz(df, path):
    """Write a synthetic df the way the downloader does (gzipped CSV)."""
    with gzip.open(path, "wt", encoding="utf-8", newline="") as f:
        df.to_csv(f, index=False)
    return path


@pytest.fixture
def data_dir(tmp_path):
    d = tmp_path / "data"
    d.mkdir()
    return str(d)


@pytest.fixture
def conn(data_dir):
    connection = db.connect(os.path.join(data_dir, "streetscape_tracker.db"))
    yield connection
    connection.close()


@pytest.fixture
def city_df_factory():
    return make_city_df


@pytest.fixture(autouse=True)
def _no_tile_census_pacing(monkeypatch):
    """
    Disable BOTH tile censuses' rate limiters for the whole suite.

    Every production default here is deliberately slow — 60 tile requests/minute
    for Mapillary against a per-IP limit on its CDN (issue #198), 30/min for
    Panoramax against a host that documents no limit at all (issue #316) — so a
    fixture city of a couple of hundred tiles would otherwise pace a single test
    out to minutes of real sleeping. Panoramax is the worse of the two, being
    both slower per request and at a zoom with ~4x the tiles.

    Tests that care about pacing (rather than about what the fetch returns)
    monkeypatch ``AsyncRateLimiter`` themselves, which runs after this fixture
    and so wins.

    THREE modules, not two: the Panoramax growth screen (issue #316) imports
    the limiter into its OWN namespace, so patching the collector's name leaves
    the screen pacing for real. It is the cheapest caller in the repo — 113
    requests — and at 30/min that is still nearly four minutes of sleeping per
    test that drives it.
    """
    from streetscape_metadata_tracker import download_mapillary as dm
    from streetscape_metadata_tracker import download_panoramax as dp
    from streetscape_metadata_tracker import panoramax_screen as ps

    class _NoPacing:
        def __init__(self, max_per_minute, *args, **kwargs):
            pass

        async def acquire(self):
            return None

    monkeypatch.setattr(dm, "AsyncRateLimiter", _NoPacing)
    monkeypatch.setattr(dp, "AsyncRateLimiter", _NoPacing)
    monkeypatch.setattr(ps, "AsyncRateLimiter", _NoPacing)


@pytest.fixture(autouse=True)
def _isolate_host_locks(tmp_path, monkeypatch):
    """
    Point the per-host locks (issue #208) at a per-test directory.

    Without this the suite would take the SAME lock files a real collection
    uses, so running pytest during a nightly batch — or two test processes at
    once — would fail tests for reasons that have nothing to do with the code
    under test. ``timeout=0`` means the symptom is a spurious ``HostBusyError``
    rather than a hang, but it is still a false failure.

    ``tmp_path`` is per-test, so tests cannot contend with each other either.
    Tests that exercise the lock take a second ``FileLock`` on the same path,
    which behaves exactly like a competing process (see
    ``tests/test_host_lock.py``).
    """
    from streetscape_metadata_tracker import host_lock

    monkeypatch.setenv(host_lock.LOCK_DIR_ENV, str(tmp_path / "locks"))


@pytest.fixture(autouse=True)
def _isolate_checkpoints(tmp_path, monkeypatch):
    """
    Point crawl checkpoints at a per-test directory.

    Both census providers checkpoint — KartaView's radius sweep (#239) and
    Mapillary's tile census (#256) — through one ``STREETSCAPE_CHECKPOINT_DIR``.

    ``checkpoint_dir()`` defaults to a ``checkpoints/`` sibling of the project
    root, so without this any test that drives a census provider's CLI path
    would write fixture-sized checkpoint directories into the working tree — the
    same mistake the catalog backup made with ``logs/`` before #145 grew its own
    autouse stub.

    Worse than untidy, it would also make tests share state: the path key is
    (city, grid geometry, channel) and deliberately carries no date, so two
    tests using one fixture city would resume each other's half-swept lattice.
    ``tmp_path`` is per-test, which is what rules that out.
    """
    from streetscape_metadata_tracker import checkpointing

    monkeypatch.setenv(checkpointing.CHECKPOINT_DIR_ENV, str(tmp_path / "checkpoints"))


@pytest.fixture(autouse=True)
def _isolate_census_cache(tmp_path, monkeypatch):
    """
    Point the shared census cache (issue #290) at a per-test directory.

    The sibling of ``_isolate_checkpoints``, and needed more urgently than it.
    A checkpoint is deleted by its caller once the artifact lands; a COMPLETED
    census is now promoted into ``census_cache/`` and deliberately left there
    for the next consumer — so without this, every test that drives a census
    provider to completion would deposit a fixture-sized entry in the working
    tree and leave it.

    Worse than untidy, it would make tests share state in the one way the
    checkpoint isolation was written to prevent, and MORE easily: the cache key
    is (provider, city, bbox) with no channel, no variant and no date, so any
    two tests using one fixture city would hand each other a census — and the
    reader is silent about it beyond a log line, because reuse is the feature.
    A test asserting "N tile requests" would pass alone and see 0 in a suite run.
    """
    from streetscape_metadata_tracker import checkpointing

    monkeypatch.setenv(checkpointing.CENSUS_CACHE_DIR_ENV, str(tmp_path / "census_cache"))


def stamp_census_cache(
    cache_path,
    provider="mapillary",
    *,
    fetched_by=None,
    fetched_variant=None,
    age_days=0,
    api_requests_total=7,
    failed=(),
    **overrides,
):
    """
    A marker-only cache entry -- what ``census_cache_probe`` reads (issue #290).

    Goes through the production builder (``checkpointing.census_cache_marker``)
    rather than spelling the dict out, so the marker's shape has ONE spelling
    across the suite: a field the builder gains reaches every test that stamps
    an entry, and a test cannot hand a reader an entry no writer would produce.
    ``age_days`` backdates both the crawl's start and its completion; keyword
    ``overrides`` land on the finished marker for the tests that need a
    malformed one.
    """
    import json
    from datetime import timedelta

    from streetscape_metadata_tracker.checkpointing import (
        CENSUS_CACHE_MARKER,
        census_cache_marker,
    )

    os.makedirs(cache_path, exist_ok=True)
    stamp = (datetime.now(UTC) - timedelta(days=age_days)).isoformat()
    marker = census_cache_marker(
        provider,
        fetched_by=fetched_by or provider,
        fetched_variant=fetched_variant,
        crawl_started_at=stamp,
        api_requests_total=api_requests_total,
        failed=list(failed),
    )
    marker["completed_at"] = stamp
    marker.update(overrides)
    with open(os.path.join(cache_path, CENSUS_CACHE_MARKER), "w", encoding="utf-8") as fh:
        json.dump(marker, fh)
    return cache_path


@pytest.fixture(autouse=True)
def _no_overpass_status_probe(monkeypatch):
    """
    Stub the Overpass /status pre-flight (issue #209) for the whole suite.

    It is a real HTTP GET that runs before every uncached graph fetch, so
    without this the street tests would hit overpass-api.de — breaking the
    suite's no-network rule, making it fail offline or in CI, and pointing
    avoidable traffic at a volunteer-run service from every developer machine
    and every CI job.

    Returning None means "nothing looks wrong, proceed", which is the same
    answer the real probe gives when it cannot tell. Tests that exercise the
    probe monkeypatch ``requests.get`` (or the probe itself) directly, which
    runs after this fixture and so wins.
    """
    from streetscape_street_analyzer import download_street_network as dsn

    monkeypatch.setattr(dsn, "_overpass_refusing", lambda url=None: None)


@pytest.fixture(autouse=True)
def _no_overpass_retry_sleep(monkeypatch):
    """
    Never really sleep out the Overpass retry backoff (issue #357).

    The default policy waits 30 s, then 60, 120 and 240 between attempts, so any
    street test whose stubbed fetch raises a transport fault would otherwise
    spend minutes asleep. A no-op keeps every attempt; the clock does not move,
    so the window never binds and a test that wants to measure the schedule
    installs its own fake clock and sleep (see tests/test_overpass_retry.py),
    which runs after this fixture and so wins.
    """
    from streetscape_street_analyzer import download_street_network as dsn

    monkeypatch.setattr(dsn, "_retry_sleep", lambda seconds: None)


@pytest.fixture(autouse=True)
def _no_host_recheck_probe(monkeypatch):
    """
    Pin every breaker re-check (issue #341) to "still refusing" for the suite.

    ``scheduler.HOST_RECHECKS`` maps a per-IP host to a real HTTP probe the
    breaker runs on a cooldown once that host has refused a child. Nothing in
    the suite should ever reach one (the default cooldown is 45 min of
    monotonic time), but the fixture makes that structural rather than a
    property of the clock: a test that trips the breaker and runs long, or
    one that shrinks the cooldown, still sends no request anywhere.

    "Still refusing" is the fail-closed answer the real predicate gives when
    it cannot see a positive signal, so a test that does not override this
    keeps the pre-#341 all-night latch. Tests of the recovery path override
    it deliberately (``monkeypatch.setitem`` on the same dict), which is the
    point: recovery must be something a test asked for, never something the
    network happened to grant.
    """
    from streetscape_metadata_tracker import scheduler as sched

    for host in list(sched.HOST_RECHECKS):
        monkeypatch.setitem(sched.HOST_RECHECKS, host, lambda: False)
