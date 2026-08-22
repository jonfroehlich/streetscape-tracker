"""Policy-layer tests for cli.py (audit 2026-07-11: previously untested).

These drive the real async_main() with sys.argv patched, the city
pre-registered (so no geocoding), and the provider downloaders stubbed —
exercising the skip policy, --force, same-date dedup, the both-provider
fail-fast, the systemic-failure rejection path (rename + nonzero exit +
ledger), the immutable-snapshot overwrite refusal, and ledger recording
for failed downloads. No network.
"""

import asyncio
import os
import sys
from datetime import date

import pandas as pd
import pytest

from streetscape_metadata_tracker import cli, db
from streetscape_metadata_tracker.city_registration import cap_dimensions
from streetscape_metadata_tracker.download_common import (
    HOST_BUSY_EXIT_CODES,
    HOST_EXIT_CODES,
    HOST_MAPILLARY_TILES,
    SWEEP_INCOMPLETE_EXIT_CODE,
    DownloadError,
    HostBlockedError,
    HostBusyError,
)
from streetscape_metadata_tracker.download_kartaview import SweepIncompleteError
from streetscape_metadata_tracker.download_mapillary import DEFAULT_TILE_REQUESTS_PER_MINUTE
from streetscape_metadata_tracker.fileutils import load_city_csv_file
from streetscape_metadata_tracker.naming import generate_run_filename
from tests.conftest import COLUMNS, make_city_df, make_mapillary_city_df, write_city_csv_gz

RUN_DATE = date(2026, 7, 1)

GRID = dict(grid_width_m=100, grid_height_m=100, step_m=20)


@pytest.fixture
def catalog(data_dir):
    """(conn, city_id, data_dir) with one registered city, frozen geometry."""
    conn = db.connect(os.path.join(data_dir, "streetscape_tracker.db"))
    city_id = db.register_city(
        conn,
        city_name="Bend",
        state_name="Oregon",
        state_code="OR",
        country_name="United States",
        country_code="us",
        center_lat=44.05,
        center_lon=-121.31,
        **GRID,
    )
    yield conn, city_id, data_dir
    conn.close()


def run_filename(city_id, provider="gsv", run_date=RUN_DATE):
    base = generate_run_filename(
        city_id,
        GRID["grid_width_m"],
        GRID["grid_height_m"],
        GRID["step_m"],
        run_date,
        provider=provider,
    )
    return f"{base}.csv.gz"


def run_cli(monkeypatch, city_id, data_dir, *extra, provider="gsv"):
    """Invoke the real async_main with patched argv; returns its exit code."""
    argv = [
        "streetscape_tracker.py",
        city_id,
        "--provider",
        provider,
        "--download-dir",
        data_dir,
        "--run-date",
        RUN_DATE.isoformat(),
        "--no-visual",
        "--no-publish-json",
        *extra,
    ]
    monkeypatch.setattr(sys, "argv", argv)
    return asyncio.run(cli.async_main())


def stub_downloader(calls, df_factory=None, api_requests=25, error=None):
    """
    A downloader double honoring the real contract: writes the output
    csv.gz, returns the result dict (or raises `error`). Records each
    call's kwargs in `calls`.
    """

    async def stub(**kwargs):
        calls.append(kwargs)
        if error is not None:
            raise error
        frame = (df_factory or (lambda: make_city_df([("p1", "2020-05-01")])))()
        path = kwargs["output_csv_gz_path"]
        write_city_csv_gz(frame, path)
        return {
            "df": load_city_csv_file(path),
            "filename_with_path": path,
            "api_requests": api_requests,
            "started_at": "2026-07-01T00:00:00+00:00",
            "finished_at": "2026-07-01T00:05:00+00:00",
        }

    return stub


def gsv_configs(monkeypatch):
    monkeypatch.setattr(cli, "load_config", lambda provider: {"api_key": "k", "access_token": "t"})


# ── Skip policy ─────────────────────────────────────────────────────────────


def test_skip_when_recent_run(monkeypatch, catalog):
    conn, city_id, data_dir = catalog
    db.register_run(
        conn,
        city_id=city_id,
        run_date=date(2026, 6, 21),  # 10 days before RUN_DATE
        csv_filename=run_filename(city_id, run_date=date(2026, 6, 21)),
    )
    calls = []
    gsv_configs(monkeypatch)
    monkeypatch.setattr(cli, "download_gsv_metadata_async", stub_downloader(calls))

    assert run_cli(monkeypatch, city_id, data_dir) == 0
    assert calls == [], "skip policy must prevent any download"
    assert db.get_latest_run(conn, city_id).run_date == "2026-06-21"


def test_force_overrides_skip(monkeypatch, catalog):
    conn, city_id, data_dir = catalog
    db.register_run(
        conn,
        city_id=city_id,
        run_date=date(2026, 6, 21),
        csv_filename=run_filename(city_id, run_date=date(2026, 6, 21)),
    )
    calls = []
    gsv_configs(monkeypatch)
    monkeypatch.setattr(cli, "download_gsv_metadata_async", stub_downloader(calls))

    assert run_cli(monkeypatch, city_id, data_dir, "--force") == 0
    assert len(calls) == 1
    latest = db.get_latest_run(conn, city_id)
    assert latest.run_date == RUN_DATE.isoformat()
    # The new run is fully cataloged, with its per-run JSON linked.
    assert latest.json_filename == run_filename(city_id).replace(".csv.gz", ".json.gz")
    assert db.get_api_usage(conn, RUN_DATE) == 25


def test_max_requests_per_minute_threads_to_downloader(monkeypatch, catalog):
    conn, city_id, data_dir = catalog
    calls = []
    gsv_configs(monkeypatch)
    monkeypatch.setattr(cli, "download_gsv_metadata_async", stub_downloader(calls))

    assert run_cli(monkeypatch, city_id, data_dir, "--max-requests-per-minute", "5000") == 0
    assert calls[0]["max_requests_per_minute"] == 5000


def test_max_requests_per_minute_defaults_to_80pct_of_default_quota(monkeypatch, catalog):
    conn, city_id, data_dir = catalog
    calls = []
    gsv_configs(monkeypatch)
    monkeypatch.setattr(cli, "download_gsv_metadata_async", stub_downloader(calls))

    assert run_cli(monkeypatch, city_id, data_dir) == 0
    assert calls[0]["max_requests_per_minute"] == 24_000


def _mapillary_stub(calls):
    return stub_downloader(
        calls, df_factory=lambda: make_mapillary_city_df([("m1", "2023-01-01")]), api_requests=4
    )


def test_mapillary_tile_pace_threads_to_the_downloader(monkeypatch, catalog):
    """The GSV flag above is a project-quota figure three orders of magnitude
    larger and the CLI applies it only to the GSV path, so Mapillary's per-IP
    tile cap needs its own flag reaching its own downloader (issue #198)."""
    conn, city_id, data_dir = catalog
    calls = []
    gsv_configs(monkeypatch)
    monkeypatch.setattr(cli, "download_mapillary_metadata_async", _mapillary_stub(calls))

    exit_code = run_cli(
        monkeypatch,
        city_id,
        data_dir,
        "--mapillary-max-requests-per-minute",
        "30",
        provider="mapillary",
    )
    assert exit_code == 0
    assert calls[0]["max_requests_per_minute"] == 30


def test_mapillary_tile_pace_defaults_to_the_conservative_tile_rate(monkeypatch, catalog):
    """Unset must not mean unpaced, and must not mean the GSV number."""
    conn, city_id, data_dir = catalog
    calls = []
    gsv_configs(monkeypatch)
    monkeypatch.setattr(cli, "download_mapillary_metadata_async", _mapillary_stub(calls))

    assert run_cli(monkeypatch, city_id, data_dir, provider="mapillary") == 0
    assert calls[0]["max_requests_per_minute"] == DEFAULT_TILE_REQUESTS_PER_MINUTE


def test_same_run_date_is_noop(monkeypatch, catalog):
    conn, city_id, data_dir = catalog
    db.register_run(conn, city_id=city_id, run_date=RUN_DATE, csv_filename=run_filename(city_id))
    calls = []
    gsv_configs(monkeypatch)
    monkeypatch.setattr(cli, "download_gsv_metadata_async", stub_downloader(calls))

    # Even --force must not duplicate/overwrite the same-date snapshot.
    assert run_cli(monkeypatch, city_id, data_dir, "--force") == 0
    assert calls == []


# ── Systemic-failure rejection ──────────────────────────────────────────────


def _denied_df(n=20):
    ts = "2026-07-01T00:00:00+00:00"
    rows = [
        (44.0 + i * 0.001, -121.0, ts, None, None, None, None, None, "REQUEST_DENIED")
        for i in range(n)
    ]
    return pd.DataFrame(rows, columns=COLUMNS)


def test_rejected_run_renames_exits_nonzero_and_still_records_usage(monkeypatch, catalog):
    conn, city_id, data_dir = catalog
    calls = []
    gsv_configs(monkeypatch)
    monkeypatch.setattr(
        cli,
        "download_gsv_metadata_async",
        stub_downloader(calls, df_factory=_denied_df, api_requests=400),
    )

    assert run_cli(monkeypatch, city_id, data_dir) == 1, "scheduler counts failures via exit code"
    out_path = os.path.join(data_dir, run_filename(city_id))
    assert not os.path.exists(out_path), "rejected run must not keep the publishable name"
    assert os.path.exists(f"{out_path}.rejected"), "raw responses kept under .rejected"
    assert db.get_latest_run(conn, city_id) is None, "rejected run must not be cataloged"
    # The requests were really spent: the ledger write precedes the guard.
    assert db.get_api_usage(conn, RUN_DATE) == 400


# ── Fail-fast and per-provider isolation ────────────────────────────────────


def test_both_providers_fail_fast_before_any_download(monkeypatch, catalog):
    conn, city_id, data_dir = catalog
    calls = []

    def configs(provider):
        if provider == "mapillary":
            raise ValueError("MAPILLARY_ACCESS_TOKEN not set")
        return {"api_key": "k"}

    monkeypatch.setattr(cli, "load_config", configs)
    monkeypatch.setattr(cli, "download_gsv_metadata_async", stub_downloader(calls))
    monkeypatch.setattr(cli, "download_mapillary_metadata_async", stub_downloader(calls))

    with pytest.raises(SystemExit) as excinfo:
        run_cli(monkeypatch, city_id, data_dir, provider="both")
    assert excinfo.value.code == 1
    # The whole point of fail-fast: GSV must NOT have collected, or the
    # series would be left unpaired.
    assert calls == []


def test_one_provider_fails_other_continues(monkeypatch, catalog):
    conn, city_id, data_dir = catalog
    gsv_calls, mly_calls = [], []
    gsv_error = DownloadError("Download failed: boom")
    gsv_error.api_requests = 1234
    gsv_configs(monkeypatch)
    monkeypatch.setattr(
        cli, "download_gsv_metadata_async", stub_downloader(gsv_calls, error=gsv_error)
    )
    monkeypatch.setattr(
        cli,
        "download_mapillary_metadata_async",
        stub_downloader(
            mly_calls,
            df_factory=lambda: make_mapillary_city_df([("m1", "2023-01-01")]),
            api_requests=4,
        ),
    )

    assert run_cli(monkeypatch, city_id, data_dir, provider="both") == 1
    assert len(gsv_calls) == 1 and len(mly_calls) == 1
    # Mapillary's run is cataloged despite GSV failing…
    assert db.get_latest_run(conn, city_id, provider="mapillary") is not None
    assert db.get_latest_run(conn, city_id, provider="gsv") is None
    # …and the failed GSV download's spent requests still hit the ledger.
    assert db.get_api_usage(conn, RUN_DATE, provider="gsv") == 1234
    assert db.get_api_usage(conn, RUN_DATE, provider="mapillary") == 4


# ── Immutable snapshots ─────────────────────────────────────────────────────


def test_refuses_to_overwrite_existing_uncataloged_snapshot(monkeypatch, catalog):
    conn, city_id, data_dir = catalog
    out_path = os.path.join(data_dir, run_filename(city_id))
    with open(out_path, "wb") as f:
        f.write(b"orphan or concurrent run in flight")
    calls = []
    gsv_configs(monkeypatch)
    monkeypatch.setattr(cli, "download_gsv_metadata_async", stub_downloader(calls))

    assert run_cli(monkeypatch, city_id, data_dir) == 1
    assert calls == [], "must refuse before issuing any request"
    with open(out_path, "rb") as f:
        assert f.read() == b"orphan or concurrent run in flight", "existing file untouched"


# ── Host-level exit codes (issue #208) ──────────────────────────────────────
#
# The child half of the breaker's contract, and the only place it is testable:
# the scheduler sees nothing but `returncode`, so if this layer returns a plain
# 1 the whole night-level breaker is silently inert and the symptom is the
# pre-#208 behaviour it was built to remove — cities marked failed, and
# `consecutive_failures` climbing toward a 90-day quarantine.


def test_a_blocked_host_exits_with_that_hosts_code(monkeypatch, catalog):
    _, city_id, data_dir = catalog
    gsv_configs(monkeypatch)
    monkeypatch.setattr(
        cli,
        "download_mapillary_metadata_async",
        stub_downloader(
            [], error=HostBlockedError("tile CDN redirected", host=HOST_MAPILLARY_TILES)
        ),
    )

    rc = run_cli(monkeypatch, city_id, data_dir, provider="mapillary")
    assert rc == HOST_EXIT_CODES[HOST_MAPILLARY_TILES]


def test_a_busy_lock_exits_with_the_busy_code_not_the_blocked_one(monkeypatch, catalog):
    """A local collision must not present as a provider refusal — the scheduler
    would skip that host's channels for the whole night over a process that is
    about to finish."""
    _, city_id, data_dir = catalog
    gsv_configs(monkeypatch)
    monkeypatch.setattr(
        cli,
        "download_mapillary_metadata_async",
        stub_downloader([], error=HostBusyError("pid 123 holds it", host=HOST_MAPILLARY_TILES)),
    )

    rc = run_cli(monkeypatch, city_id, data_dir, provider="mapillary")
    assert rc == HOST_BUSY_EXIT_CODES[HOST_MAPILLARY_TILES]
    assert rc != HOST_EXIT_CODES[HOST_MAPILLARY_TILES]


def test_a_mixed_failure_exits_1_rather_than_a_host_code(monkeypatch, catalog):
    """
    Mapillary blocked AND GSV genuinely broken. Reporting the host code would
    let the breaker's "no city failure recorded" posture swallow a real bug in
    the GSV path, which is nothing to do with the host.
    """
    conn, city_id, data_dir = catalog
    gsv_configs(monkeypatch)
    monkeypatch.setattr(
        cli, "download_gsv_metadata_async", stub_downloader([], error=DownloadError("boom"))
    )
    monkeypatch.setattr(
        cli,
        "download_mapillary_metadata_async",
        stub_downloader([], error=HostBlockedError("refused", host=HOST_MAPILLARY_TILES)),
    )

    assert run_cli(monkeypatch, city_id, data_dir, provider="both") == 1


def test_a_host_failure_alongside_a_success_still_reports_the_host(monkeypatch, catalog):
    """GSV collected fine; only Mapillary hit the host. Every FAILURE was the
    host, so the breaker should hear about it."""
    conn, city_id, data_dir = catalog
    gsv_configs(monkeypatch)
    monkeypatch.setattr(cli, "download_gsv_metadata_async", stub_downloader([]))
    monkeypatch.setattr(
        cli,
        "download_mapillary_metadata_async",
        stub_downloader([], error=HostBlockedError("refused", host=HOST_MAPILLARY_TILES)),
    )

    assert (
        run_cli(monkeypatch, city_id, data_dir, provider="both")
        == (HOST_EXIT_CODES[HOST_MAPILLARY_TILES])
    )
    assert db.get_latest_run(conn, city_id, provider="gsv") is not None


def test_a_blocked_host_still_records_what_it_spent(monkeypatch, catalog):
    """The refused requests were issued and the CDN counted them (#198/#203's
    one-token-one-increment invariant), so the ledger must learn about them."""
    conn, city_id, data_dir = catalog
    gsv_configs(monkeypatch)
    error = HostBlockedError("refused", host=HOST_MAPILLARY_TILES)
    error.api_requests = 5
    monkeypatch.setattr(cli, "download_mapillary_metadata_async", stub_downloader([], error=error))

    run_cli(monkeypatch, city_id, data_dir, provider="mapillary")
    assert db.get_api_usage(conn, RUN_DATE, provider="mapillary") == 5


# ── Registration-time grid cap (issue #166) ─────────────────────────────────


def test_cap_dimensions_clamps_each_side_to_40km():
    """Auto-derived grids are capped at 40 km/side at registration (issue
    #166), so newly registered cities can't recreate the oversized grids that
    scripts/cap_oversized_grids.py had to fix retroactively."""
    # Cairo's real pre-cap OSM-derived geometry
    w, h = cap_dimensions(66_453, 63_475, "Cairo, Egypt")
    assert (w, h) == (40_000, 40_000)

    # One long side clamps independently; the sane side is untouched
    w, h = cap_dimensions(51_568, 25_146, "Caracas, Venezuela")
    assert (w, h) == (40_000, 25_146)

    # At or under the cap passes through unchanged
    assert cap_dimensions(40_000, 12_000, "Fine") == (40_000, 12_000)


# ── The two KartaView request counts (issues #225, #239) ────────────────────


def kartaview_configs(monkeypatch):
    monkeypatch.setattr(cli, "load_config", lambda provider: {"access_token": "t"})


def kartaview_stub(*, api_requests=25, api_requests_total=33):
    async def stub(**kwargs):
        path = kwargs["output_csv_gz_path"]
        write_city_csv_gz(make_mapillary_city_df([("p1", "2023-05-01")]), path)
        return {
            "df": load_city_csv_file(path),
            "filename_with_path": path,
            "api_requests": api_requests,
            "api_requests_total": api_requests_total,
            "num_flat_images": 0,
            "started_at": "2026-07-01T00:00:00+00:00",
            "finished_at": "2026-07-01T00:05:00+00:00",
        }

    return stub


def test_the_run_row_takes_the_sweeps_cost_and_the_ledger_this_processs(monkeypatch, catalog):
    """
    A resumed KartaView sweep reports two different numbers, and they go to two
    different places (#239). Getting this backwards is silent in both
    directions, and it WAS backwards until a real Krabi sweep showed it.

    `runs.api_requests` describes the RUN, so it must say what the whole sweep
    cost -- 25 tonight after 8 on an earlier night is a run that cost 33.
    `db.add_api_usage` is additive and keyed by (date, provider), so it takes
    only tonight's 25: feeding it the cumulative figure would charge the earlier
    night's requests against tonight's budget gate a second time, and the gate
    would start deferring cities that fit.
    """
    conn, city_id, data_dir = catalog
    kartaview_configs(monkeypatch)
    monkeypatch.setattr(cli, "download_kartaview_metadata_async", kartaview_stub())

    assert run_cli(monkeypatch, city_id, data_dir, provider="kartaview") == 0
    run = db.get_latest_run(conn, city_id, provider="kartaview")
    assert run.api_requests == 33, "the run row must carry the sweep's cost"
    assert db.get_api_usage(conn, RUN_DATE, provider="kartaview") == 25, (
        "the additive daily ledger must carry only this process's spend"
    )


def test_a_provider_without_a_cumulative_count_still_records_its_spend(monkeypatch, catalog):
    """
    The fallback matters: gsv and mapillary publish no `api_requests_total`, so
    a bare subscript would make every one of their runs raise KeyError.
    """
    conn, city_id, data_dir = catalog
    gsv_configs(monkeypatch)
    monkeypatch.setattr(cli, "download_gsv_metadata_async", stub_downloader([], api_requests=17))

    assert run_cli(monkeypatch, city_id, data_dir, provider="gsv") == 0
    assert db.get_latest_run(conn, city_id, provider="gsv").api_requests == 17


def test_an_incomplete_sweep_exits_83_and_publishes_nothing(monkeypatch, catalog):
    """
    83 means "this made progress, run it again", which is a different
    instruction from 1. The scheduler branch that will read it must `continue`
    WITHOUT record_attempt(success=False): get_due_cities filters on
    consecutive_failures and nothing but a success resets it, so a city that
    legitimately needs three nights would quarantine itself for a whole cycle
    for making progress.
    """
    conn, city_id, data_dir = catalog
    kartaview_configs(monkeypatch)
    error = SweepIncompleteError("budget", checkpoint_path="/cp", roots_done=2, root_count=16)
    error.api_requests = 4

    async def stub(**kwargs):
        raise error

    monkeypatch.setattr(cli, "download_kartaview_metadata_async", stub)
    assert (
        run_cli(monkeypatch, city_id, data_dir, provider="kartaview") == SWEEP_INCOMPLETE_EXIT_CODE
    )
    # Nothing published -- a partial census dated today would diff as "every
    # pano in the rest of the city removed" -- but the spend is on the ledger,
    # because those requests were issued and KartaView counted them.
    assert db.get_latest_run(conn, city_id, provider="kartaview") is None
    assert db.get_api_usage(conn, RUN_DATE, provider="kartaview") == 4


def test_a_paused_sweep_alongside_a_real_failure_exits_1_not_83(monkeypatch, catalog):
    """
    The same reasoning as the mixed-host case above: 83 tells the caller to just
    run it again, so an invocation where something ALSO genuinely broke must not
    wear it and have the real failure read as progress.
    """
    conn, city_id, data_dir = catalog
    monkeypatch.setattr(cli, "load_config", lambda provider: {"api_key": "k", "access_token": "t"})

    async def paused(**kwargs):
        raise SweepIncompleteError("budget", checkpoint_path="/cp", roots_done=2, root_count=16)

    monkeypatch.setattr(cli, "download_kartaview_metadata_async", paused)
    assert (
        run_cli(monkeypatch, city_id, data_dir, provider="kartaview") == SWEEP_INCOMPLETE_EXIT_CODE
    )

    # Now the same pause with a genuinely broken channel beside it. 'both' is
    # gsv+mapillary and deliberately excludes kartaview, so this drives the
    # aggregation directly rather than through a provider list that cannot
    # contain all three.
    conn2, city2, data_dir2 = catalog
    monkeypatch.setattr(
        cli, "download_gsv_metadata_async", stub_downloader([], error=DownloadError("broken"))
    )
    monkeypatch.setattr(
        cli, "download_mapillary_metadata_async", stub_downloader([], error=DownloadError("broken"))
    )
    assert run_cli(monkeypatch, city2, data_dir2, provider="both") == 1
