"""Shared fixtures: temp data dir, catalog DB, and a synthetic city CSV factory."""

import gzip
import os
import sys
from datetime import UTC, date, datetime

import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from streetscape_metadata_tracker import db  # noqa: E402
from streetscape_metadata_tracker.config import METADATA_DTYPES  # noqa: E402

# The run CSV schema, from its single source of truth — a column added to
# (or reordered in) METADATA_DTYPES flows into every synthetic fixture.
COLUMNS = list(METADATA_DTYPES)


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
                f"© Mapillary contributor {100 + i % 3}",
                "OK",
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
                f"flat{k}",
                None,  # FLAT_ONLY rows carry no capture date
                f"© Mapillary contributor {200 + k}",
                "FLAT_ONLY",
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
            )
        )
    return pd.DataFrame(rows, columns=COLUMNS)


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
def _no_mapillary_tile_pacing(monkeypatch):
    """
    Disable the Mapillary tile rate limiter (issue #198) for the whole suite.

    The production default is deliberately slow — 60 tile requests/minute
    against a per-IP limit on Mapillary's CDN — so a fixture city of a couple
    hundred tiles would otherwise pace a single test out to several minutes of
    real sleeping. Tests that care about pacing (rather than about what the
    fetch returns) monkeypatch ``AsyncRateLimiter`` themselves, which runs after
    this fixture and so wins.
    """
    from streetscape_metadata_tracker import download_mapillary as dm

    class _NoPacing:
        def __init__(self, max_per_minute):
            pass

        async def acquire(self):
            return None

    monkeypatch.setattr(dm, "AsyncRateLimiter", _NoPacing)


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
