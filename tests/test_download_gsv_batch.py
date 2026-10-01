"""End-to-end tests for the GSV batch downloader's quota-throttling behavior
(`download_gsv.py`), driven with a monkeypatched fetch primitive — the same
served-from-memory pattern as the Mapillary tests. No network.

Covers the 2026-07-16 incident class: Google signals per-minute quota
exhaustion as an HTTP 200 body with status OVER_QUERY_LIMIT. Such responses
say nothing about the grid point itself, so they must be retried rather than
written as a final row while retries remain; a run left with too many holes
after retries must abort (keeping its resume checkpoint) rather than register
a clean-looking snapshot that silently lost coverage. A sub-threshold residual
is written back as a failure row (never dropped) so every grid point stays
present and run-to-run diffs still align.
"""

import asyncio

import pytest

from streetscape_metadata_tracker import download_gsv as dg
from streetscape_metadata_tracker.download_common import DownloadError

CENTER_LAT, CENTER_LON = 44.05, -121.31

THROTTLED = {"status": "OVER_QUERY_LIMIT"}


def _ok_response(lat: float, lon: float) -> dict:
    return {
        "status": "OK",
        "location": {"lat": lat, "lng": lon},
        "pano_id": f"pano_{lat:.6f}_{lon:.6f}",
        "copyright": "© Google",
        "date": "2024-05",
    }


def _point_key(lat: float, lon: float) -> tuple[float, float]:
    return (round(lat, 9), round(lon, 9))


def _patch_instant_retry_sleep(monkeypatch):
    """Skip the real inter-retry-pass waits (20s/40s/…) but keep yielding."""
    real_sleep = asyncio.sleep

    async def instant_sleep(seconds):
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", instant_sleep)


def _run_download(tmp_path, monkeypatch, fake_fetch, grid_m=40, **kwargs):
    """Drive the real download loop with `fake_fetch` as the API. A 40 m
    grid at step 20 is 3x3 = 9 points."""
    out = str(tmp_path / f"bend_width_{grid_m}_height_{grid_m}_step_20_2026-07-16.csv.gz")
    monkeypatch.setattr(dg, "fetch_gsv_pano_metadata_async", fake_fetch)
    result = asyncio.run(
        dg.download_gsv_metadata_async(
            city_name="Bend",
            center_lat=CENTER_LAT,
            center_lon=CENTER_LON,
            grid_width=grid_m,
            grid_height=grid_m,
            step_length=20,
            api_key="TESTKEY",
            output_csv_gz_path=out,
            batch_size=4,
            connection_limit=2,
            **kwargs,
        )
    )
    return result, out


def test_over_query_limit_is_retried_and_never_written(tmp_path, monkeypatch):
    """Every point throttled once, then OK: the run must end fully OK with
    no OVER_QUERY_LIMIT row, and the retry requests must be counted."""
    calls: dict[tuple, int] = {}
    seen_limiter = []

    async def fake_fetch(lat, lon, api_key, session, timeout, limiter=None):
        seen_limiter.append(limiter)
        key = _point_key(lat, lon)
        calls[key] = calls.get(key, 0) + 1
        if calls[key] == 1:
            return dict(THROTTLED)
        return _ok_response(lat, lon)

    _patch_instant_retry_sleep(monkeypatch)  # skip the pre-retry quota-reset wait
    # A limiter far above the request volume: verifies the wiring without
    # ever pacing (its 1-second burst capacity exceeds the whole run).
    result, out = _run_download(
        tmp_path, monkeypatch, fake_fetch, max_requests_per_minute=6_000_000
    )

    df = result["df"]
    assert len(df) == 9  # 3x3 grid, no dropped points
    assert set(df["status"]) == {"OK"}
    assert result["api_requests"] == 18  # 9 initial + 9 retried
    assert all(lim is not None for lim in seen_limiter), (
        "the run's rate limiter must reach every fetch, including retries"
    )


def test_persistent_throttle_aborts_and_keeps_checkpoint(tmp_path, monkeypatch):
    """All but one point throttled on every attempt: the run must refuse to
    finalize, keep the .downloading checkpoint for resume, and report the
    requests it spent (budget ledger)."""
    succeeded: set = set()

    async def fake_fetch(lat, lon, api_key, session, timeout, limiter=None):
        key = _point_key(lat, lon)
        if not succeeded or key in succeeded:
            succeeded.add(key)  # exactly one point ever succeeds
            return _ok_response(lat, lon)
        return dict(THROTTLED)

    _patch_instant_retry_sleep(monkeypatch)
    with pytest.raises(DownloadError) as excinfo:
        _run_download(tmp_path, monkeypatch, fake_fetch, max_requests_per_minute=0)

    assert "refusing to finalize" in str(excinfo.value)
    # 9 initial + 3 retry passes over the 8 still-throttled points
    assert excinfo.value.api_requests == 9 + 3 * 8

    base = "bend_width_40_height_40_step_20_2026-07-16"
    assert not (tmp_path / f"{base}.csv.gz").exists(), "no immutable snapshot may be finalized"
    assert (tmp_path / f"{base}.csv.downloading").exists(), "checkpoint must survive for resume"


def test_small_residual_throttle_finalizes_with_failure_row(tmp_path, monkeypatch):
    """One permanently throttled point out of 121 (0.8% < the 1% threshold)
    finalizes: the point is written back as an OVER_QUERY_LIMIT failure row
    (never dropped) so the grid stays complete, and it is also recorded in the
    failed-points sidecar."""
    unlucky: set = set()

    async def fake_fetch(lat, lon, api_key, session, timeout, limiter=None):
        key = _point_key(lat, lon)
        if not unlucky:
            unlucky.add(key)  # first point requested never succeeds
        if key in unlucky:
            return dict(THROTTLED)
        return _ok_response(lat, lon)

    _patch_instant_retry_sleep(monkeypatch)
    # 200 m grid at step 20 -> 11x11 = 121 points
    result, out = _run_download(
        tmp_path, monkeypatch, fake_fetch, grid_m=200, max_requests_per_minute=0
    )

    df = result["df"]
    assert len(df) == 121  # every grid point present — no hole
    assert set(df["status"]) == {"OK", "OVER_QUERY_LIMIT"}
    assert (df["status"] == "OVER_QUERY_LIMIT").sum() == 1
    assert result["api_requests"] == 121 + 3 * 1
    sidecar = tmp_path / "bend_width_200_height_200_step_20_2026-07-16_failed_points.csv"
    assert sidecar.exists()
    assert len(sidecar.read_text().strip().splitlines()) == 2  # header + 1 point


def test_residual_network_failure_written_as_request_failed(tmp_path, monkeypatch):
    """A point whose request keeps raising (no body status) is exhausted as a
    REQUEST_FAILED row, still keeping the grid complete for diff alignment."""
    unlucky: set = set()

    async def fake_fetch(lat, lon, api_key, session, timeout, limiter=None):
        key = _point_key(lat, lon)
        if not unlucky:
            unlucky.add(key)
        if key in unlucky:
            raise DownloadError("connection reset")
        return _ok_response(lat, lon)

    _patch_instant_retry_sleep(monkeypatch)
    result, out = _run_download(
        tmp_path, monkeypatch, fake_fetch, grid_m=200, max_requests_per_minute=0
    )

    df = result["df"]
    assert len(df) == 121
    assert set(df["status"]) == {"OK", "REQUEST_FAILED"}
    assert (df["status"] == "REQUEST_FAILED").sum() == 1


def test_the_returned_frame_applies_the_query_radius_but_the_file_does_not(tmp_path, monkeypatch):
    """Issue #367, on the collector's side of the diff. The frame the downloader
    hands back feeds the catalog stats, the new run's diff and its JSON, so it
    must come through the query-radius seam exactly as the previous run's
    reload does -- or a far pano would be churn on one side only. The file on
    disk still records what Google said."""
    from streetscape_metadata_tracker.analysis import OUT_OF_RADIUS
    from streetscape_metadata_tracker.fileutils import load_city_csv_file

    far_point = []

    async def fake_fetch(lat, lon, api_key, session, timeout, limiter=None):
        response = _ok_response(lat, lon)
        if not far_point:
            far_point.append(_point_key(lat, lon))
            response["location"] = {"lat": lat + 0.01, "lng": lon}  # ~1.1 km away
        return response

    result, out = _run_download(tmp_path, monkeypatch, fake_fetch, max_requests_per_minute=0)

    assert (result["df"]["status"] == OUT_OF_RADIUS).sum() == 1
    assert (result["df"]["status"] == "OK").sum() == 8
    assert set(load_city_csv_file(out, raw=True)["status"]) == {"OK"}
