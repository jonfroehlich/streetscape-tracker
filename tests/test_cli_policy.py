"""Policy-layer tests for cli.py (audit 2026-07-11: previously untested).

These drive the real async_main() with sys.argv patched, the city
pre-registered (so no geocoding), and the provider downloaders stubbed —
exercising the skip policy, --force, same-date dedup, --provider parsing with
its multi-provider fail-fast and its per-provider downloader dispatch, the
credential-free --check-boundary preview, the systemic-failure rejection path
(rename + nonzero exit + ledger), the immutable-snapshot overwrite refusal,
and ledger recording for failed downloads. No network.
"""

import asyncio
import os
import sys
from datetime import date

import pandas as pd
import pytest

from streetscape_metadata_tracker import cli, db, naming
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
from streetscape_metadata_tracker.naming import KNOWN_PROVIDERS, generate_run_filename
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
    """
    Invoke the real async_main with patched argv; returns its exit code.

    `provider=None` OMITS the flag entirely, which is the only way to exercise
    argparse's default — see
    test_omitting_provider_runs_the_type_function_over_the_default.
    """
    argv = [
        "streetscape_tracker.py",
        city_id,
        *(() if provider is None else ("--provider", provider)),
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


def test_multiple_providers_fail_fast_before_any_download(monkeypatch, catalog):
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
        run_cli(monkeypatch, city_id, data_dir, provider="gsv,mapillary")
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

    assert run_cli(monkeypatch, city_id, data_dir, provider="gsv,mapillary") == 1
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

    assert run_cli(monkeypatch, city_id, data_dir, provider="gsv,mapillary") == 1


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
        run_cli(monkeypatch, city_id, data_dir, provider="gsv,mapillary")
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

    NOTE WHAT THIS CAN AND CANNOT REACH TODAY. `--provider` takes one of
    {both, gsv, mapillary, kartaview} and `both` is gsv+mapillary, so kartaview
    always runs ALONE and the mixed case is currently unreachable through argv
    — the guard is `len(incomplete) == len(failed)`, which today is either
    trivially true or vacuous. It exists for #247, which makes `--provider all`
    collect every provider in one invocation and turns this into a live path.

    So this drives the aggregation directly, by stubbing the per-provider entry
    point rather than the downloaders. Testing it through a `both` run of two
    ordinary failures would exit 1 for reasons that have nothing to do with the
    branch under test, and would pass just as happily if the branch were
    deleted.
    """
    conn, city_id, data_dir = catalog
    gsv_configs(monkeypatch)

    async def one_pauses_one_breaks(conn_, args, city_row, run_date, provider, config, vis_path):
        if provider == "mapillary":
            raise SweepIncompleteError("budget", checkpoint_path="/cp", roots_done=2, root_count=16)
        raise DownloadError("genuinely broken")

    monkeypatch.setattr(cli, "_collect_one_run", one_pauses_one_breaks)
    assert run_cli(monkeypatch, city_id, data_dir, provider="gsv,mapillary") == 1

    # ...and with the pause as the ONLY thing that went wrong, the same code
    # path reports it as progress.
    async def only_pauses(conn_, args, city_row, run_date, provider, config, vis_path):
        raise SweepIncompleteError("budget", checkpoint_path="/cp", roots_done=2, root_count=16)

    monkeypatch.setattr(cli, "_collect_one_run", only_pauses)
    assert (
        run_cli(monkeypatch, city_id, data_dir, provider="gsv,mapillary")
        == SWEEP_INCOMPLETE_EXIT_CODE
    )


def test_a_paused_sweep_prints_paused_not_failed(monkeypatch, catalog, capsys):
    """
    The except clause calls a pause "progress, not breakage", so stdout must
    not announce FAILED and then contradict itself with PAUSED — a wrapper
    grepping stdout would escalate (or --force re-run) every legitimately
    budget-capped night of a multi-night sweep. The message also names the
    provider that actually paused rather than hardcoding KartaView
    (PR #251 review).
    """
    conn, city_id, data_dir = catalog
    kartaview_configs(monkeypatch)
    error = SweepIncompleteError("budget", checkpoint_path="/cp", roots_done=2, root_count=16)

    async def stub(**kwargs):
        raise error

    monkeypatch.setattr(cli, "download_kartaview_metadata_async", stub)
    assert (
        run_cli(monkeypatch, city_id, data_dir, provider="kartaview") == SWEEP_INCOMPLETE_EXIT_CODE
    )
    out = capsys.readouterr().out
    assert "PAUSED: kartaview checkpointed at 2/16 root cells" in out
    assert "FAILED" not in out


def test_the_checkpoint_is_discarded_only_after_the_runs_row_commits(monkeypatch, catalog):
    """
    The discard moved out of the downloader (PR #251 review): the CSV landing
    is not enough, because a crash between the CSV write and register_run
    leaves an orphan whose remedy is "delete it and re-run" — which must
    re-finalize from the checkpoint for ~0 requests, not re-pay the sweep. So
    the CLI discards, and only once the runs row is already in the catalog.
    """
    conn, city_id, data_dir = catalog
    kartaview_configs(monkeypatch)
    inner = kartaview_stub()

    async def with_checkpoint(**kwargs):
        result = await inner(**kwargs)
        result["checkpoint_path"] = "/cp"
        return result

    monkeypatch.setattr(cli, "download_kartaview_metadata_async", with_checkpoint)
    seen = {}

    def fake_discard(path):
        seen["path"] = path
        seen["run_cataloged"] = db.get_latest_run(conn, city_id, provider="kartaview") is not None

    monkeypatch.setattr(cli, "discard_checkpoint", fake_discard)

    assert run_cli(monkeypatch, city_id, data_dir, provider="kartaview") == 0
    assert seen["path"] == "/cp"
    assert seen["run_cataloged"] is True, "discard must come after register_run, never before"


def test_a_register_run_failure_leaves_the_checkpoint_alive(monkeypatch, catalog):
    """
    The failure that motivated the move: the DB refusing the runs row (locked
    by a concurrent process, a schema migration mid-run — the class that cost
    the 611k-request Berlin walk its row). The CSV is on disk, nothing is
    cataloged, and the checkpoint must survive as the only thing that makes
    the re-run cheap.
    """
    conn, city_id, data_dir = catalog
    kartaview_configs(monkeypatch)
    inner = kartaview_stub()

    async def with_checkpoint(**kwargs):
        result = await inner(**kwargs)
        result["checkpoint_path"] = "/cp"
        return result

    monkeypatch.setattr(cli, "download_kartaview_metadata_async", with_checkpoint)
    discarded = []
    monkeypatch.setattr(cli, "discard_checkpoint", lambda p: discarded.append(p))

    def boom(*args, **kwargs):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(cli.db, "register_run", boom)
    assert run_cli(monkeypatch, city_id, data_dir, provider="kartaview") == 1
    assert discarded == []


def test_a_non_downloaderror_failure_with_a_spend_still_reaches_the_ledger(monkeypatch, catalog):
    """
    PR #251 review: a KartaView sweep whose post-fetch tail dies (ENOSPC on
    the gzip write) raises OSError, not DownloadError — and because the
    checkpoint survives and the resume re-finalizes for ~0 new requests, a
    spend missed here would never land in ANY api_usage row.
    """
    conn, city_id, data_dir = catalog
    kartaview_configs(monkeypatch)
    error = OSError("No space left on device")
    error.api_requests = 9

    async def stub(**kwargs):
        raise error

    monkeypatch.setattr(cli, "download_kartaview_metadata_async", stub)
    assert run_cli(monkeypatch, city_id, data_dir, provider="kartaview") == 1
    assert db.get_api_usage(conn, RUN_DATE, provider="kartaview") == 9


def test_kartaview_gets_its_own_timeout_default(monkeypatch, catalog):
    """
    DEFAULT_REQUEST_TIMEOUT_S is 60 because nearby-photos is a database query
    whose heaviest documented shape is a 2,000-row page; passing the CLI's
    30 s default straight through silently halved it on every real sweep
    (PR #251 review). An explicit --timeout still wins.
    """
    conn, city_id, data_dir = catalog
    kartaview_configs(monkeypatch)
    calls = []
    inner = kartaview_stub()

    async def capture(**kwargs):
        calls.append(kwargs)
        return await inner(**kwargs)

    monkeypatch.setattr(cli, "download_kartaview_metadata_async", capture)
    assert run_cli(monkeypatch, city_id, data_dir, provider="kartaview") == 0
    assert calls[0]["request_timeout"] == 60.0

    assert (
        run_cli(
            monkeypatch,
            city_id,
            data_dir,
            "--run-date",
            "2026-07-02",
            "--timeout",
            "45",
            "--force",
            provider="kartaview",
        )
        == 0
    )
    assert calls[-1]["request_timeout"] == 45.0

    gsv_calls = []
    gsv_configs(monkeypatch)
    monkeypatch.setattr(cli, "download_gsv_metadata_async", stub_downloader(gsv_calls))
    assert run_cli(monkeypatch, city_id, data_dir, provider="gsv") == 0
    assert gsv_calls[0]["request_timeout"] == 30.0, "the other providers keep their 30 s default"


def test_kartaview_max_requests_refuses_nonpositive_values(monkeypatch, catalog):
    """
    `--kartaview-max-requests 0` used to spend the full calibration ladder,
    checkpoint roots_done=0, exit 83 and print "re-run the same command to
    resume" — an infinite loop the message actively encourages (PR #251
    review). Refused at parse time, before any work, following #214's posture
    for `run-due --limit`.
    """
    conn, city_id, data_dir = catalog
    kartaview_configs(monkeypatch)
    calls = []

    async def never(**kwargs):
        calls.append(kwargs)
        raise AssertionError("no downloader call may happen on a refused value")

    monkeypatch.setattr(cli, "download_kartaview_metadata_async", never)
    for bad in ("0", "-5"):
        with pytest.raises(SystemExit) as excinfo:
            run_cli(
                monkeypatch,
                city_id,
                data_dir,
                "--kartaview-max-requests",
                bad,
                provider="kartaview",
            )
        assert excinfo.value.code == 2
    assert calls == []


# ── --provider is a channel LIST (issue #247) ───────────────────────────────


def test_provider_default_is_the_two_production_channels():
    """
    Typing nothing must still mean gsv+mapillary, and must NOT acquire a third
    provider. This is the whole reason `both` became a stated list rather than
    a redefined keyword: `both` named two of two when it was written and names
    two of three now, so redefining it in place would have added KartaView's
    mandatory credential AND its 16 req/min serial sweep to every bare
    `streetscape_tracker.py "City"` invocation.
    """
    parser_default = cli.DEFAULT_PROVIDERS_ARG
    assert cli._parse_provider_list(parser_default) == ["gsv", "mapillary"]
    assert "kartaview" not in cli._parse_provider_list(parser_default)


def test_omitting_provider_runs_the_type_function_over_the_default(monkeypatch, catalog, capsys):
    """
    Typing nothing must land in async_main as the canonical parsed channel
    list, quietly.

    argparse runs `type` over a *string* default and hands a non-string default
    through untouched, so `default=DEFAULT_PROVIDERS_ARG` is load-bearing and
    invisible — `default=DEFAULT_PROVIDERS` (the tuple) skips the type function
    and delivers a tuple to code that has been told it holds a list. Nothing
    pinned any of it before, because run_cli always passed --provider, so the
    whole suite stayed green over a default the CLI never once parsed (PR #263
    review). Three things are asserted: the parse, the silence (a default of
    `both` would nag on every bare invocation), and the collection it drives.
    """
    monkeypatch.setattr(sys, "argv", ["streetscape_tracker.py", "Bend, Oregon"])
    assert cli.parse_args().provider == ["gsv", "mapillary"]
    assert "deprecated" not in capsys.readouterr().err

    conn, city_id, data_dir = catalog
    gsv_calls, mly_calls, kv_calls = [], [], []
    gsv_configs(monkeypatch)
    monkeypatch.setattr(cli, "download_gsv_metadata_async", stub_downloader(gsv_calls))
    monkeypatch.setattr(cli, "download_mapillary_metadata_async", stub_downloader(mly_calls))
    monkeypatch.setattr(cli, "download_kartaview_metadata_async", stub_downloader(kv_calls))

    assert run_cli(monkeypatch, city_id, data_dir, provider=None) == 0
    assert len(gsv_calls) == 1 and len(mly_calls) == 1
    # The half that costs money if it regresses: no KartaView sweep for typing
    # nothing.
    assert kv_calls == []


def test_provider_all_covers_the_provider_the_default_leaves_out():
    """
    What `all` has to do that the default must not: reach KartaView.

    Asserted against the literal token rather than against KNOWN_PROVIDERS,
    which is what the previous version of this test did —
    `_parse_provider_list("all") == list(KNOWN_PROVIDERS)` restates the
    implementation (update from KNOWN_PROVIDERS, filter by KNOWN_PROVIDERS) and
    cannot fail (PR #263 review). The derivation is still what makes `all`
    correct for a FOURTH provider; the test that keeps that honest is
    test_every_known_provider_reaches_its_own_downloader, where set equality
    against KNOWN_PROVIDERS has something real to catch.
    """
    every = cli._parse_provider_list("all")
    default = cli._parse_provider_list(cli.DEFAULT_PROVIDERS_ARG)

    assert "kartaview" in every
    assert "kartaview" not in default
    assert set(default) < set(every)


def test_provider_both_is_accepted_and_warns(capsys):
    """
    Cron entries, shell history and `run_cities.py` pass-through arguments all
    carry the retired spelling, so it keeps working — it just says so. The
    notice goes to stderr because parse time is before logging is configured.

    The alias resolves through the DEFAULT_PROVIDERS tuple, not by re-splitting
    its argv spelling. That is the fix for a real narrowing: the string form is
    also help text, so reformatting it to "gsv, mapillary" is an ordinary edit,
    and the unstripped split this branch used to do then resolved `both` to
    ['gsv'] and exited 0 (PR #263 review).
    """
    assert cli._parse_provider_list("both") == ["gsv", "mapillary"]
    assert cli._parse_provider_list("both") == cli._parse_provider_list(cli.DEFAULT_PROVIDERS_ARG)
    err = capsys.readouterr().err
    assert "deprecated" in err
    assert cli.DEFAULT_PROVIDERS_ARG in err


def test_provider_list_collapses_duplicates_and_orders_canonically():
    """
    Ordered by KNOWN_PROVIDERS, not by what was typed — the same rule
    `scheduler._select_providers` follows when it filters out of
    `enabled_providers()`, so the canonical gsv-first ranking survives whatever
    order an operator types. Duplicates (including `all` beside a name it
    already covers) collapse rather than collecting a provider twice.
    """
    assert cli._parse_provider_list("mapillary,gsv") == ["gsv", "mapillary"]
    assert cli._parse_provider_list(" mapillary , gsv , gsv ") == ["gsv", "mapillary"]
    assert cli._parse_provider_list("all,gsv,both") == list(KNOWN_PROVIDERS)


@pytest.mark.parametrize("value", ["", ",", " , ", "bogus", "gsv,bogus", "gsv_streets"])
def test_provider_refuses_unusable_selections(monkeypatch, catalog, value):
    """
    An unknown name, and an empty selection, both exit 2 before any work.

    The empty cases are reachable — argparse hands the type function whatever
    string it was given — and falling through with an empty list would collect
    nothing while exiting 0. That is the zero-channel no-op
    `scheduler._select_providers` refuses for the nightly run, reached here
    through the sibling flag.

    `gsv_streets` is in the list on purpose: street channels are budget
    channels of `streetscape_street_analyzer.collect`, not grid providers, so
    naming one here is a mistake rather than a shorthand.
    """
    conn, city_id, data_dir = catalog
    calls = []

    async def never(**kwargs):
        calls.append(kwargs)
        raise AssertionError("no downloader call may happen on a refused selection")

    for attr in (
        "download_gsv_metadata_async",
        "download_mapillary_metadata_async",
        "download_kartaview_metadata_async",
    ):
        monkeypatch.setattr(cli, attr, never)

    with pytest.raises(SystemExit) as excinfo:
        run_cli(monkeypatch, city_id, data_dir, provider=value)
    assert excinfo.value.code == 2
    assert calls == []


def test_provider_all_fails_fast_on_every_credential(monkeypatch, catalog):
    """
    `all` means all: a missing credential for ANY named provider aborts before
    a single request, exactly as the two-provider default already did.

    Deliberate, and the alternative was live at plan time: `all` could have
    skipped a credential-less provider with a warning. It doesn't, because a
    run that silently collected two of three while exiting 0 is the same
    quiet-narrowing failure this codebase refuses everywhere else (a disabled
    channel in `_select_providers`, `--limit 0`, an unwired `[providers.*]`
    block). The escape hatch is naming the list instead.
    """
    conn, city_id, data_dir = catalog
    calls = []

    def configs(provider):
        if provider == "kartaview":
            raise ValueError("KARTAVIEW_ACCESS_TOKEN not set")
        return {"api_key": "k", "access_token": "t"}

    monkeypatch.setattr(cli, "load_config", configs)
    for attr in (
        "download_gsv_metadata_async",
        "download_mapillary_metadata_async",
        "download_kartaview_metadata_async",
    ):
        monkeypatch.setattr(cli, attr, stub_downloader(calls))

    with pytest.raises(SystemExit) as excinfo:
        run_cli(monkeypatch, city_id, data_dir, provider="all")
    assert excinfo.value.code == 1
    # The point of fail-fast: the two providers that DO have keys must not have
    # collected, or the series is left unpaired against the one that couldn't.
    assert calls == []


# The downloader each provider token must reach. Hand-kept ON PURPOSE — it is
# the half test_every_known_provider_reaches_its_own_downloader cannot derive,
# and its whole value is that adding a provider to naming.KNOWN_PROVIDERS fails
# the set-equality assertion until someone names a downloader for it here (at
# which point they find out whether one exists).
EXPECTED_DOWNLOADER = {
    "gsv": "download_gsv_metadata_async",
    "mapillary": "download_mapillary_metadata_async",
    "kartaview": "download_kartaview_metadata_async",
}


def test_every_known_provider_reaches_its_own_downloader(monkeypatch, catalog):
    """
    `_collect_one_run`'s dispatch must fail CLOSED on a token it doesn't wire.

    GSV used to be the `else` arm, so a provider added to KNOWN_PROVIDERS but
    not wired would have collected as GSV — a Google-keyed grid sweep written
    into a file named for the new provider, then cataloged, diffed and
    published as that provider's series in an immutable dated snapshot. Nobody
    had to type the new token to get there either, because `--provider all`
    expands from KNOWN_PROVIDERS: `all` is the fourth consumer of that tuple and
    was the only one without a reachability pin, where PROVIDER_RUN_DTYPES,
    vis.PROVIDER_DISPLAY and scheduler.CHANNEL_HOSTS each have one precisely so
    a token cannot fail open (PR #263 review).

    Set equality, following test_every_scheduled_channel_declares_its_per_ip_hosts
    and test_every_known_provider_has_a_run_schema.
    """
    assert set(EXPECTED_DOWNLOADER) == set(KNOWN_PROVIDERS)

    conn, city_id, data_dir = catalog
    gsv_configs(monkeypatch)

    for provider, expected in EXPECTED_DOWNLOADER.items():
        calls = {attr: [] for attr in EXPECTED_DOWNLOADER.values()}
        for attr, recorded in calls.items():
            monkeypatch.setattr(cli, attr, stub_downloader(recorded))

        assert run_cli(monkeypatch, city_id, data_dir, provider=provider) == 0
        called = [attr for attr, recorded in calls.items() if recorded]
        assert called == [expected], f"{provider} dispatched to {called}, expected [{expected}]"

        # Each provider is its own run series, so the next iteration would hit
        # the same-run-date dedup rather than the dispatch it means to test.
        os.remove(os.path.join(data_dir, run_filename(city_id, provider=provider)))
        conn.execute("DELETE FROM runs WHERE provider = ?", (provider,))
        conn.commit()


def test_an_unwired_provider_is_refused_rather_than_collected_as_gsv(monkeypatch, catalog, caplog):
    """
    The other direction, with the dispatch's own guard as the subject.

    test_every_known_provider_reaches_its_own_downloader keeps this branch
    unreachable; this one pins what happens if it ever is reached, because a
    fail-open dispatch is silent by construction — the failure it produces is a
    published snapshot, not an error. A wiring bug has to surface as a failed
    run instead.

    Both KNOWN_PROVIDERS bindings are patched — cli's for `--provider`, naming's
    for `generate_run_filename` — so the run gets far enough to reach the
    dispatch. The log assertion is what makes that load-bearing: without it this
    test would pass just as well on naming's own guard, several steps earlier.
    """
    conn, city_id, data_dir = catalog
    gsv_calls = []
    gsv_configs(monkeypatch)
    monkeypatch.setattr(cli, "download_gsv_metadata_async", stub_downloader(gsv_calls))
    monkeypatch.setattr(cli, "KNOWN_PROVIDERS", (*KNOWN_PROVIDERS, "newprovider"))
    monkeypatch.setattr(naming, "KNOWN_PROVIDERS", (*KNOWN_PROVIDERS, "newprovider"))

    with caplog.at_level("ERROR"):
        assert run_cli(monkeypatch, city_id, data_dir, provider="newprovider") == 1
    assert "_collect_one_run has no arm for it" in caplog.text
    assert gsv_calls == []
    assert db.get_latest_run(conn, city_id, provider="newprovider") is None


def test_check_boundary_previews_without_any_credential(monkeypatch, catalog):
    """
    A boundary preview contacts no provider API — it geocodes and draws a
    rectangle — so it must not require the named providers' keys.

    It did: the credential check ran ahead of the early return, so
    `--provider all --check-boundary` demanded all three keys to preview a
    search area (PR #263 review). CLAUDE.md documents --check-boundary as the
    cheap way to see what a run would cover, which is exactly what an operator
    reaches for on a host that has not been given every credential yet.
    """
    conn, city_id, data_dir = catalog
    saved = []

    def refuse(provider):
        raise AssertionError(f"--check-boundary must not load {provider} credentials")

    class FakeMap:
        def save(self, path):
            saved.append(path)

    monkeypatch.setattr(cli, "load_config", refuse)
    monkeypatch.setattr(cli, "display_search_area", lambda *a, **k: FakeMap())
    monkeypatch.setattr(cli, "open_in_browser", lambda path: (True, ""))
    monkeypatch.setattr(cli, "get_default_vis_dir", lambda: data_dir)

    assert run_cli(monkeypatch, city_id, data_dir, "--check-boundary", provider="all") == 0
    assert len(saved) == 1
