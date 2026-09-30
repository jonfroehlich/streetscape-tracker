"""The GSV collection engine's connection reuse and batch pipelining (issue #304).

`collect_points_async` used to build a fresh ClientSession per batch and wait
for every batch to finish before starting the next. Both cost throughput
(docs/experiments/gsv-throughput.md); neither may change a byte of what the
run writes. These tests pin the three halves of that: the written CSV is
byte-identical to what the sequential engine wrote (golden fixture, with
requests completing out of order and a retry pass), one session serves the
whole run, and batch k+1 is on the wire while batch k is still outstanding.
The PR's review added what pipelining must not break one level out: a queued
request's timeout, the failure-path spend, and the run lock under a second
cancel. No Google traffic: the fetch primitive is monkeypatched, as in
tests/test_download_gsv_batch.py (one test serves it from a local server).
"""

import asyncio
import hashlib
import os
from datetime import UTC, datetime
from pathlib import Path

import aiohttp
import pytest

from streetscape_metadata_tracker import download_gsv as dg
from streetscape_metadata_tracker.download_common import DownloadError

# Captured at import, before any test patches asyncio.sleep: the fake fetch
# needs REAL delays so completion order genuinely differs from submission
# order, while the engine's 20 s pre-retry wait is made instant.
_REAL_SLEEP = asyncio.sleep

# Generated from the SEQUENTIAL engine (origin/main 2a94a68, one session per
# batch, no pipelining) and committed unchanged with the pipelined one: it is
# the contract the rewrite had to satisfy. To change it deliberately, run with
# REGEN_GSV_GOLDEN=1 and review the fixture diff -- a drift here would show up
# as phantom churn in every GSV city's next run-to-run diff.
GOLDEN_PATH = Path(__file__).parent / "fixtures" / "gsv_golden_run.csv"

_FROZEN_NOW = datetime(2026, 9, 30, 12, 0, 0, tzinfo=UTC)


class _FrozenDatetime(datetime):
    """``datetime`` whose ``now()`` is fixed, so query_timestamp is reproducible."""

    @classmethod
    def now(cls, tz=None):
        return _FROZEN_NOW if tz is not None else _FROZEN_NOW.replace(tzinfo=None)


def _points(n: int) -> list[tuple[float, float, int, int]]:
    # Literal coordinates, not a geodesic grid: no libm last-ULP drift between
    # platforms, so the fixture can be compared byte for byte.
    side = 20
    return [
        (47.6 + (k // side) * 0.00018, -122.3 + (k % side) * 0.00027, k // side, k % side)
        for k in range(n)
    ]


def _h(lat: float, lon: float) -> float:
    return int(hashlib.md5(f"{lat},{lon}".encode()).hexdigest()[:8], 16) / 0xFFFFFFFF


def _make_fetch(permanent_failure_key=None):
    """A fake API with every response class the row builder distinguishes.

    Each request sleeps a hash-derived real 0-4 ms, so requests complete out of
    submission order. ~3% answer OVER_QUERY_LIMIT and ~2% raise on the FIRST
    attempt only (both go to the retry pass); one optional point fails forever
    (a residual REQUEST_FAILED row, under the 1% abort threshold).
    """
    attempts: dict = {}

    async def fake_fetch(lat, lon, api_key, session, timeout, limiter=None):
        key = (lat, lon)
        n = attempts.get(key, 0)
        attempts[key] = n + 1
        h = _h(lat, lon)
        await _REAL_SLEEP(int(h * 1e6) % 5 / 1000)
        if key == permanent_failure_key:
            raise aiohttp.ClientError("simulated permanent network failure")
        if n == 0 and h > 0.97:
            return {"status": "OVER_QUERY_LIMIT"}
        if n == 0 and 0.95 < h <= 0.97:
            raise aiohttp.ClientError("simulated transient network failure")
        if h < 0.55:
            date = (
                "2023-07"
                if h < 0.35
                else ("2021-03-05" if h < 0.45 else ("2019" if h < 0.5 else ""))
            )
            return {
                "status": "OK",
                "copyright": "© Google" if h < 0.5 else "© Someone Else",
                "date": date,
                "location": {"lat": lat + 1.25e-5, "lng": lon - 7.5e-6},
                "pano_id": hashlib.md5(f"{lat},{lon}".encode()).hexdigest()[:22],
            }
        return {"status": "ZERO_RESULTS"}

    return fake_fetch


@pytest.fixture
def instant_pass_sleep(monkeypatch):
    async def instant(_seconds):
        await _REAL_SLEEP(0)

    monkeypatch.setattr(asyncio, "sleep", instant)


def _collect(tmp_path, points, **kwargs):
    out = str(tmp_path / "golden_width_400_height_300_step_20_2026-09-30.csv.gz")
    params = dict(batch_size=7, connection_limit=3, max_requests_per_minute=0)
    params.update(kwargs)
    return asyncio.run(dg.collect_points_async(points, "TESTKEY", out, **params)), out


def test_written_csv_is_byte_identical_to_the_sequential_engine(
    tmp_path, monkeypatch, instant_pass_sleep
):
    """Out-of-order completion + a retry pass + a residual failure row: the
    gunzipped CSV must equal the sequential engine's output byte for byte."""
    import gzip

    points = _points(300)
    monkeypatch.setattr(dg, "datetime", _FrozenDatetime)
    monkeypatch.setattr(dg, "fetch_gsv_pano_metadata_async", _make_fetch(points[123][:2]))

    result, out = _collect(tmp_path, points)
    written = gzip.open(out, "rb").read()

    if os.environ.get("REGEN_GSV_GOLDEN"):
        GOLDEN_PATH.write_bytes(written)
    golden = GOLDEN_PATH.read_bytes()
    assert written == golden, (
        "the GSV run CSV changed; if deliberate, regenerate with REGEN_GSV_GOLDEN=1 and "
        "review the fixture diff -- shipping it makes every GSV city's next diff report "
        "changes that did not happen"
    )
    # 300 first attempts + the retry passes' re-requests (measured on the
    # sequential engine), so the api_usage ledger counts exactly what it did.
    assert result["api_requests"] == 320
    # The fixture must actually exercise what it claims to.
    text = golden.decode()
    for status in ("OK", "NO_DATE", "ZERO_RESULTS", "REQUEST_FAILED"):
        assert f",{status}\n" in text, status
    assert text.count("\n") == 301


def test_one_session_serves_the_whole_run(tmp_path, monkeypatch, instant_pass_sleep):
    """A fresh ClientSession per batch re-handshook TLS on every socket every
    batch (6,000 connects per 12,000 requests, docs/experiments/gsv-throughput.md);
    one must serve every batch AND the retry passes, and be closed at the end."""
    made = []

    class CountingSession(aiohttp.ClientSession):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            made.append(self)

    monkeypatch.setattr(dg.aiohttp, "ClientSession", CountingSession)
    monkeypatch.setattr(dg, "fetch_gsv_pano_metadata_async", _make_fetch())

    _collect(tmp_path, _points(100))  # 15 batches, plus a retry pass

    assert len(made) == 1
    assert made[0].closed


def test_the_next_batch_is_in_flight_before_the_previous_one_finishes(tmp_path, monkeypatch):
    """The barrier is the other half of #304: batch 0's last request completes
    only once a batch-1 request has STARTED. A sequential engine deadlocks
    here (it starts batch 1 only after writing batch 0), which the timeout
    turns into a failure rather than a hung suite."""
    points = _points(4)  # batch_size 2 -> batches [p0, p1], [p2, p3]
    batch1_started = asyncio.Event()

    async def fake_fetch(lat, lon, api_key, session, timeout, limiter=None):
        if (lat, lon) in {points[2][:2], points[3][:2]}:
            batch1_started.set()
        if (lat, lon) == points[1][:2]:
            await batch1_started.wait()
        return {"status": "ZERO_RESULTS"}

    monkeypatch.setattr(dg, "fetch_gsv_pano_metadata_async", fake_fetch)
    out = str(tmp_path / "overlap_width_40_height_40_step_20_2026-09-30.csv.gz")

    async def run():
        return await asyncio.wait_for(
            dg.collect_points_async(
                points, "K", out, batch_size=2, connection_limit=2, max_requests_per_minute=0
            ),
            timeout=5,
        )

    result = asyncio.run(run())
    assert len(result["df"]) == 4


def test_a_write_failure_cancels_the_batches_fetched_ahead(tmp_path, monkeypatch):
    """Batches fetched ahead of a failing write must be cancelled, not left to
    run against the API after the run has already failed -- and awaited, so
    no task outlives the run.

    Batch k's requests take 0.05 * (k + 1) s, so the write of batch 1 fails at
    ~0.10 s while batches 2-4 (due at 0.15-0.25 s) are still outstanding.
    Cancelled, only batches 0 and 1 ever complete; merely awaited, all five do.
    """
    points = _points(40)
    batch_of = {p[:2]: k // 5 for k, p in enumerate(points)}
    completed = []

    async def staggered_fetch(lat, lon, api_key, session, timeout, limiter=None):
        await _REAL_SLEEP(0.05 * (batch_of[(lat, lon)] + 1))
        completed.append((lat, lon))
        return {"status": "ZERO_RESULTS"}

    real_process = dg.process_batch_async
    calls = {"n": 0}

    async def failing_process(*a, **kw):
        calls["n"] += 1
        if calls["n"] == 2:
            raise DownloadError("simulated write failure")
        return await real_process(*a, **kw)

    monkeypatch.setattr(dg, "fetch_gsv_pano_metadata_async", staggered_fetch)
    monkeypatch.setattr(dg, "process_batch_async", failing_process)
    out = str(tmp_path / "fail_width_40_height_40_step_20_2026-09-30.csv.gz")

    async def run():
        with pytest.raises(DownloadError) as failure:
            await dg.collect_points_async(
                points, "K", out, batch_size=5, connection_limit=5, max_requests_per_minute=0
            )
        leftover = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        return leftover, failure.value

    leftover, error = asyncio.run(run())
    assert leftover == []
    assert len(completed) == 10  # batches 0 and 1 only
    # The spend the caller records in api_usage: batches 0-3 are scheduled up
    # front, batch 4 after batch 0's write, then batch 1's write fails. Counting
    # at write time instead would report 5 or 10, and under-charge the ledger.
    assert error.api_requests == 25


def test_a_queued_request_does_not_time_out_waiting_for_a_socket(tmp_path, monkeypatch):
    """#304 review: aiohttp's ClientTimeout(total=...) includes the wait for a
    pooled connection. Pipelining queues PIPELINE_DEPTH x batch_size requests
    (40 here) on connection_limit sockets (5), so without the engine's slots
    the tail of the queue waits ~7 latencies for a socket and times out at
    1.0 s against a 0.2 s server -- where the sequential engine's queue of one
    batch (10 on 5) never did. Each such timeout is also a backoff retry that
    reaches the server uncounted.

    A real local HTTP server and the engine's real ClientSession; the fetch has
    the production one's shape (same backoff decorator, same session.get with
    the engine's timeout) pointed at localhost, since the production URL is
    hard-coded to Google.
    """
    import backoff
    from aiohttp import web

    arrivals = []
    port = {}

    async def meta(_request):
        arrivals.append(1)
        await _REAL_SLEEP(0.2)
        return web.json_response({"status": "ZERO_RESULTS"})

    @backoff.on_exception(
        backoff.expo, (asyncio.TimeoutError, aiohttp.ClientError), max_tries=3, max_time=60
    )
    async def local_fetch(lat, lon, api_key, session, timeout, limiter=None):
        url = f"http://127.0.0.1:{port['n']}/m?location={lat},{lon}"
        async with session.get(url, timeout=timeout) as response:
            return await response.json()

    monkeypatch.setattr(dg, "fetch_gsv_pano_metadata_async", local_fetch)
    points = _points(40)
    out = str(tmp_path / "queue_width_40_height_40_step_20_2026-09-30.csv.gz")

    async def run():
        app = web.Application()
        app.router.add_get("/m", meta)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", 0).start()
        port["n"] = runner.addresses[0][1]
        try:
            return await dg.collect_points_async(
                points,
                "K",
                out,
                batch_size=10,
                connection_limit=5,
                request_timeout=1.0,
                max_retries=1,
                max_requests_per_minute=0,
            )
        finally:
            await runner.cleanup()

    result = asyncio.run(run())
    assert (result["df"]["status"] == "ZERO_RESULTS").all()
    assert len(arrivals) == 40  # no backoff retry reached the server
    assert result["api_requests"] == 40


def test_a_second_cancel_during_close_does_not_leak_the_run_lock(tmp_path, monkeypatch):
    """#304 review: a cancellation delivered while the session was closing
    skipped ``run_lock.release()``, holding the run lock for as long as the
    exception's traceback stayed referenced, so a retry in the same process
    that still held the error was refused as "another process is already
    collecting". close() raising CancelledError stands in for that second
    cancel.

    The excinfo is held ACROSS the lock check on purpose: filelock releases
    on garbage collection, so letting the exception go first would release a
    leaked lock and pass against the unguarded code (measured: reverting the
    try/finally passed until the excinfo was held)."""
    from filelock import FileLock

    class CancelledOnClose(aiohttp.ClientSession):
        async def close(self):
            await super().close()
            raise asyncio.CancelledError

    async def zero_fetch(lat, lon, api_key, session, timeout, limiter=None):
        return {"status": "ZERO_RESULTS"}

    monkeypatch.setattr(dg.aiohttp, "ClientSession", CancelledOnClose)
    monkeypatch.setattr(dg, "fetch_gsv_pano_metadata_async", zero_fetch)
    out = str(tmp_path / "lock_width_40_height_40_step_20_2026-09-30.csv.gz")

    with pytest.raises(asyncio.CancelledError) as held:
        asyncio.run(
            dg.collect_points_async(
                _points(10), "K", out, batch_size=5, connection_limit=5, max_requests_per_minute=0
            )
        )

    lock = FileLock(out[: -len(".gz")] + ".downloading.runlock", timeout=0)
    lock.acquire()  # raises filelock.Timeout if the run lock leaked
    lock.release()
    assert held.value is not None  # keeps the traceback (and any leaked lock) alive to here


def test_a_close_failure_does_not_mask_the_download_error_or_its_spend(
    tmp_path, monkeypatch, caplog
):
    """#304 review: if session.close() raised in the ``finally`` while a
    DownloadError was propagating, the close error replaced it and the caller
    lost ``api_requests``, the spend it records in the api_usage ledger. The
    close error is logged instead, and the run lock is still released."""
    import logging

    from filelock import FileLock

    class FailingClose(aiohttp.ClientSession):
        async def close(self):
            await super().close()
            raise RuntimeError("simulated close failure")

    async def zero_fetch(lat, lon, api_key, session, timeout, limiter=None):
        return {"status": "ZERO_RESULTS"}

    async def failing_process(*a, **kw):
        raise DownloadError("simulated write failure")

    monkeypatch.setattr(dg.aiohttp, "ClientSession", FailingClose)
    monkeypatch.setattr(dg, "fetch_gsv_pano_metadata_async", zero_fetch)
    monkeypatch.setattr(dg, "process_batch_async", failing_process)
    out = str(tmp_path / "close_width_40_height_40_step_20_2026-09-30.csv.gz")

    with caplog.at_level(logging.WARNING, logger=dg.logger.name):
        with pytest.raises(DownloadError) as held:
            asyncio.run(
                dg.collect_points_async(
                    _points(20),
                    "K",
                    out,
                    batch_size=5,
                    connection_limit=5,
                    max_requests_per_minute=0,
                )
            )

    # Four batches of five are scheduled before the first write fails.
    assert held.value.api_requests == 20
    assert "simulated write failure" in str(held.value)
    assert "simulated close failure" in caplog.text
    lock = FileLock(out[: -len(".gz")] + ".downloading.runlock", timeout=0)
    lock.acquire()
    lock.release()
