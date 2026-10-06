"""
Tests for `scheduler assess-city` — the same-day answer for a new city
(issue #215).

Pure logic: geocoding, the boundary probe and every collection subprocess are
substituted. The seam for collection is ``_run_one_city``, which is where the
real command hands off to a subprocess, so these tests exercise the whole
command down to (but not through) that boundary. The collectors' own end-to-end
behaviour is covered by tests/test_streetwalk_collect.py and
tests/test_streetwalk_mapillary.py.
"""

from datetime import UTC, date, datetime

import pytest

from streetscape_metadata_tracker import city_registration as cr
from streetscape_metadata_tracker import db
from streetscape_metadata_tracker import scheduler as _sched
from streetscape_metadata_tracker.boundary_audit import rect_in_boundary_frac
from streetscape_metadata_tracker.download_common import (
    ARGV_REJECTED_EXIT_CODE,
    HOST_BUSY_EXIT_CODES,
    HOST_EXIT_CODES,
    HOST_MAPILLARY_TILES,
    HOST_OVERPASS,
)
from streetscape_metadata_tracker.scheduler import (
    ASSESS_CHANNELS,
    ProviderConfig,
    SchedulerConfig,
    build_parser,
)
from tests.conftest import panoramax_screen_fetch

TODAY = date(2026, 8, 17)
QUERY = "Newport, Kentucky"
CITY_ID = "newport--kentucky--united-states"

# The real probe, saved before the autouse stub below replaces it, so the test
# that exercises its failure path can put it back.
_REAL_BOUNDARY_PREFLIGHT = _sched._boundary_preflight


# --------------------------------------------------------------------------
# fixtures / helpers
# --------------------------------------------------------------------------


def _cfg(tmp_path, **overrides):
    """All four channels enabled, as production has them."""
    providers = {
        "gsv": ProviderConfig(enabled=True, daily_request_budget=10_000_000),
        "gsv_streets": ProviderConfig(enabled=True, daily_request_budget=3_000_000),
        "mapillary": ProviderConfig(
            enabled=True, daily_request_budget=15_000, max_requests_per_minute=60
        ),
        "mapillary_streets": ProviderConfig(
            enabled=True, daily_request_budget=5_000, max_requests_per_minute=60
        ),
    }
    base = dict(
        providers=providers, data_dir=str(tmp_path / "data"), log_dir=str(tmp_path / "logs")
    )
    base.update(overrides)
    return SchedulerConfig(**base)


# The frozen geometry a registered Newport carries, for the tests that need a
# CityRow without going through the command.
_CITY_ROW = dict(
    city_name="Newport",
    state_name="Kentucky",
    state_code="KY",
    country_name="United States",
    country_code="US",
    center_lat=39.0889,
    center_lon=-84.4919,
    grid_width_m=4000,
    grid_height_m=4000,
    step_m=20,
)


class _Loc:
    """The subset of a geoutils location object registration reads."""

    city = "Newport"
    state = "Kentucky"
    state_code = "KY"
    country = "United States"
    country_code = "US"
    latitude = 39.0889
    longitude = -84.4919
    bbox_center = (39.0836, -84.4836)


@pytest.fixture(autouse=True)
def _no_geocode(monkeypatch):
    """
    Registration geocodes exactly once through city_registration; stub both of
    its Nominatim calls, and count them so a test can assert a second
    invocation of assess-city re-uses frozen geometry instead of re-geocoding.
    """
    from streetscape_metadata_tracker import city_registration as cr

    calls = {"loc": 0, "dims": 0}

    def fake_loc(query, *a, **k):
        calls["loc"] += 1
        return _Loc()

    def fake_dims(query, w, h):
        calls["dims"] += 1
        return 3892.0, 4182.0

    monkeypatch.setattr(cr, "get_city_location_data", fake_loc)
    monkeypatch.setattr(cr, "get_search_dimensions", fake_dims)
    return calls


@pytest.fixture(autouse=True)
def _no_boundary_probe(monkeypatch):
    """
    The boundary probe is an unlocked per-IP Nominatim call. Neutralize it for
    every test — the tests that care about the report patch it back.
    """
    monkeypatch.setattr(_sched, "_boundary_preflight", lambda city: (None, None))


@pytest.fixture(autouse=True)
def _no_panoramax_screen_network(monkeypatch):
    """
    A newly registered city is screened against Panoramax to gate its opt-in
    enrolment (issue #374). Every test here gets a screen whose fetch fails as a
    connection fault would — so nothing reaches the host, and the default
    outcome enrols no Panoramax channel. The #374 tests install their own answer.
    """
    import aiohttp

    from streetscape_metadata_tracker import panoramax_screen

    monkeypatch.setattr(
        panoramax_screen,
        "_fetch_tile",
        panoramax_screen_fetch(error=aiohttp.ClientError("the suite never reaches Panoramax")),
    )


@pytest.fixture(autouse=True)
def _no_published_json(monkeypatch):
    """
    The tail regenerates every published artifact. data_dir points at tmp_path
    here, but building them needs per-run JSON on disk that these tests never
    write, so stub the whole helper and let the tail tests assert on the marker.
    """
    monkeypatch.setattr(
        _sched, "_regenerate_published_json", lambda conn, cfg: ("regenerated", True)
    )


def _stub_collection(monkeypatch, conn, *, outcome=None):
    """
    Stand in for the collection subprocess — the seam where the real command
    hands off, so everything above it runs for real.

    Returns the list that collects ``(city_id, provider)`` in launch order;
    ``outcome(city, provider)`` fakes the per-channel result (default success).
    """
    outcome = outcome or (lambda city, provider: True)
    ran = []

    def fake_run(
        cfg,
        city,
        run_today,
        provider="gsv",
        connection_limit=None,
        daily_budget=0,
        conn=None,
        remaining_s=None,
        **_,
    ):
        ran.append((city.city_id, provider))
        return outcome(city, provider)

    monkeypatch.setattr(_sched, "_run_one_city", fake_run)
    monkeypatch.setattr(_sched.db, "connect", lambda path: conn)
    return ran


def _due(conn, provider, today=date(2026, 8, 18)):
    """The city_ids ``provider``'s nightly queue would hold on ``today``."""
    return [
        c.city_id
        for c in db.get_due_cities(
            conn,
            today=today,
            cycle_days=90,
            grace_days=10,
            max_consecutive_failures=5,
            default_membership=_sched.CHANNEL_DEFAULT_MEMBERSHIP[provider],
            provider=provider,
        )
    ]


def _assess(tmp_path, **kwargs):
    """Run the command with confirmation pre-answered and publishing off."""
    kwargs.setdefault("assume_yes", True)
    cfg = kwargs.pop("cfg", None) or _cfg(tmp_path)
    return _sched.cmd_assess_city(cfg, QUERY, today=TODAY, **kwargs)


# --------------------------------------------------------------------------
# argument parsing
# --------------------------------------------------------------------------


def test_assess_city_is_wired_into_the_parser():
    args = build_parser().parse_args(
        ["assess-city", "--provider", "mapillary_streets", "--yes", QUERY]
    )
    assert args.command == "assess-city"
    assert args.city == QUERY
    assert args.providers == ["mapillary_streets"]
    assert args.yes is True
    assert args.estimate is False


def test_global_config_flag_works_on_either_side_of_the_subcommand():
    """Same contract as every other subcommand (see _add_global_flags)."""
    before = build_parser().parse_args(["--config", "/tmp/a.toml", "assess-city", QUERY])
    after = build_parser().parse_args(["assess-city", "--config", "/tmp/a.toml", QUERY])
    assert before.config == after.config == "/tmp/a.toml"


# --------------------------------------------------------------------------
# usage refusals — all must land before the catalog is opened
# --------------------------------------------------------------------------


def _refusal(monkeypatch, tmp_path, conn, **kwargs):
    """Run the command with a db.connect recorder; return (rc, connected)."""
    connected = []
    monkeypatch.setattr(_sched.db, "connect", lambda path: connected.append(path) or conn)
    monkeypatch.setattr(_sched, "_run_one_city", lambda *a, **k: pytest.fail("collected!"))
    rc = _sched.cmd_assess_city(_cfg(tmp_path), QUERY, today=TODAY, assume_yes=True, **kwargs)
    return rc, connected


@pytest.mark.parametrize(
    "kwargs",
    [
        pytest.param({"width": 5000}, id="width-without-height"),
        pytest.param({"height": 5000}, id="height-without-width"),
        pytest.param({"lat": 39.0}, id="lat-without-lng"),
        pytest.param({"lng": -84.0}, id="lng-without-lat"),
        pytest.param({"width": 5000, "height": 5000}, id="size-without-center"),
        pytest.param({"requested_providers": ["gsv"]}, id="grid-gsv"),
        pytest.param({"requested_providers": ["nope"]}, id="unknown-channel"),
        pytest.param({"requested_providers": [","]}, id="no-channel-named"),
        pytest.param({"requested_providers": ["  "]}, id="whitespace-channel"),
    ],
)
def test_bad_arguments_exit_usage_without_opening_the_catalog(conn, monkeypatch, tmp_path, kwargs):
    """
    #214's posture, applied to this command: validate before db.connect so an
    operator typo costs nothing — no catalog, no geocode, no requests.
    """
    rc, connected = _refusal(monkeypatch, tmp_path, conn, **kwargs)
    assert rc == _sched.USAGE_EXIT_CODE
    assert connected == []


def test_the_size_without_center_refusal_explains_itself(conn, monkeypatch, tmp_path, caplog):
    """
    cli.py accepts --width/--height alone and centers the grid on the
    geocoder's reported point (#186), which nobody has verified is downtown
    (#185) — and the geometry is frozen forever. This command refuses instead,
    so the message has to say why and name the flags that fix it.
    """
    with caplog.at_level("ERROR"):
        rc, _connected = _refusal(monkeypatch, tmp_path, conn, width=5000, height=5000)

    assert rc == _sched.USAGE_EXIT_CODE
    assert "geocoder's reported point" in caplog.text
    assert "bounding-box midpoint" not in caplog.text
    assert "--lat/--lng" in caplog.text


def test_the_grid_gsv_refusal_points_at_run_due(conn, monkeypatch, tmp_path, caplog):
    """An operator asking for the grid run is not confused, just in the wrong
    command — say where it lives instead of only refusing."""
    with caplog.at_level("ERROR"):
        rc, _connected = _refusal(monkeypatch, tmp_path, conn, requested_providers=["gsv"])

    assert rc == _sched.USAGE_EXIT_CODE
    assert "run-due --provider gsv" in caplog.text
    # run-due collects only enabled, due cities, so for a city assess-city just
    # registered (DISABLED, #431) the pointer alone is a silent no-op.
    assert "DISABLED until `enable-city`" in caplog.text


def test_a_config_with_no_assess_channel_is_refused(conn, monkeypatch, tmp_path):
    """
    The prod-shaped case: while the Mapillary channels are off after a per-IP
    block AND gsv_streets is off, assess-city has nothing to collect. Accepting
    it would publish, exit 0, and look like an answer.
    """
    cfg = _cfg(tmp_path, providers={"gsv": ProviderConfig(enabled=True)})
    connected = []
    monkeypatch.setattr(_sched.db, "connect", lambda path: connected.append(path) or conn)

    rc = _sched.cmd_assess_city(cfg, QUERY, today=TODAY, assume_yes=True)

    assert rc == _sched.USAGE_EXIT_CODE
    assert connected == []


def test_non_interactive_stdin_without_yes_refuses_rather_than_hanging(conn, monkeypatch, tmp_path):
    """
    input() on a pipe raises EOFError or reads garbage; either way a cron-style
    invocation must not collect unconfirmed. It refuses AFTER the free pre-flight
    (registration + costs), which is the useful half.
    """
    monkeypatch.setattr(_sched.db, "connect", lambda path: conn)
    monkeypatch.setattr(_sched, "_run_one_city", lambda *a, **k: pytest.fail("collected!"))
    monkeypatch.setattr(_sched.sys.stdin, "isatty", lambda: False, raising=False)

    rc = _sched.cmd_assess_city(_cfg(tmp_path), QUERY, today=TODAY, assume_yes=False)

    assert rc == _sched.USAGE_EXIT_CODE
    assert db.resolve_city(conn, QUERY) is not None  # the pre-flight still registered it


# --------------------------------------------------------------------------
# registration
# --------------------------------------------------------------------------


def test_registers_an_unknown_city_with_capped_geometry_and_an_alias(
    conn, monkeypatch, tmp_path, _no_geocode
):
    _stub_collection(monkeypatch, conn)

    _assess(tmp_path)

    city = db.resolve_city(conn, CITY_ID)
    assert city is not None
    assert (city.grid_width_m, city.grid_height_m) == (3892, 4182)
    # Issue #431: an unvetted boundary stays out of the rotation until
    # `enable-city`, even on the collecting (--yes) path.
    assert not city.enabled
    # The user's query slug resolves too, so a second invocation never re-geocodes.
    assert db.resolve_city(conn, QUERY).city_id == CITY_ID
    assert _no_geocode["loc"] == 1


def test_a_second_run_reuses_frozen_geometry_and_never_re_geocodes(
    conn, monkeypatch, tmp_path, _no_geocode
):
    _stub_collection(monkeypatch, conn)

    _assess(tmp_path)
    _assess(tmp_path)

    assert _no_geocode["loc"] == 1
    assert _no_geocode["dims"] == 1


def test_derived_dimensions_are_capped_at_the_40km_ceiling(conn, monkeypatch, tmp_path):
    """Same cap a real collection applies (issue #166) — this command must not
    be a way around it."""
    from streetscape_metadata_tracker import city_registration as cr

    monkeypatch.setattr(cr, "get_search_dimensions", lambda q, w, h: (66_453.0, 63_475.0))
    _stub_collection(monkeypatch, conn)

    _assess(tmp_path)

    city = db.resolve_city(conn, CITY_ID)
    assert (city.grid_width_m, city.grid_height_m) == (
        _sched.MAX_GRID_DIM_M,
        _sched.MAX_GRID_DIM_M,
    )


def test_an_unresolvable_city_fails_without_collecting(conn, monkeypatch, tmp_path):
    from streetscape_metadata_tracker import city_registration as cr

    monkeypatch.setattr(cr, "get_city_location_data", lambda q, *a, **k: None)
    ran = _stub_collection(monkeypatch, conn)

    rc = _assess(tmp_path)

    assert rc == 1
    assert ran == []


# --------------------------------------------------------------------------
# --estimate
# --------------------------------------------------------------------------


def test_estimate_registers_but_issues_no_provider_request(conn, monkeypatch, tmp_path, capsys):
    """
    Named --estimate rather than --dry-run on purpose: it matches
    `collect --estimate`, which also does real work (registration here, an OSM
    fetch there) and only refuses to spend provider requests. In this repo
    --dry-run means "no writes at all".
    """
    ran = _stub_collection(monkeypatch, conn)

    rc = _assess(tmp_path, estimate_only=True)

    assert rc == 0
    assert ran == []
    assert db.resolve_city(conn, CITY_ID) is not None
    assert db.get_api_usage(conn, TODAY, "mapillary") == 0
    out = capsys.readouterr().out
    for channel in ASSESS_CHANNELS:
        assert channel in out
    assert "no provider request was issued" in out
    # Issue #431: registered DISABLED, says so with the one way in, and so the
    # next night's gsv queue does not hold it.
    assert not db.resolve_city(conn, CITY_ID).enabled
    assert cr.enable_hint(CITY_ID) in out
    assert _due(conn, "gsv") == []


def test_the_estimate_stop_text_names_enable_city_only_for_a_disabled_city(
    conn, monkeypatch, tmp_path, capsys
):
    """
    The --estimate stop text points at `enable-city` (#431), and only when that
    is true: an already-enabled city printed no hint above and is already in
    the rotation, so "registered DISABLED, see above" would point at nothing.
    """
    _stub_collection(monkeypatch, conn)

    _assess(tmp_path, estimate_only=True)
    out = capsys.readouterr().out
    assert f"`enable-city {CITY_ID}` puts it into the nightly rotation" in out

    db.set_city_enabled(conn, CITY_ID, True)
    _assess(tmp_path, estimate_only=True)
    out = capsys.readouterr().out
    assert "no provider request was issued" in out
    assert "DISABLED" not in out
    assert "enable-city" not in out


def test_an_estimate_registered_city_becomes_due_only_after_enable_city(
    conn, monkeypatch, tmp_path
):
    """
    The documented operator path end to end (issue #431): `--estimate` leaves
    nothing due, and `enable-city` is what puts the gsv grid run at the head of
    the next night. The second half is what keeps the first `== []` from being
    vacuous -- the same query does see the city once it is enabled.
    """
    _stub_collection(monkeypatch, conn)

    _assess(tmp_path, estimate_only=True)
    assert _due(conn, "gsv") == []

    rc = _sched.cmd_enable_city(_cfg(tmp_path), CITY_ID, opt_in=False)

    assert rc == 0
    assert _due(conn, "gsv") == [CITY_ID]


def test_the_preflight_prices_every_channel_against_todays_remaining_budget(
    conn, monkeypatch, tmp_path, capsys
):
    """The only volume governor the Mapillary GRID run gets: cli.py records
    spend via add_api_usage but never checks it."""
    _stub_collection(monkeypatch, conn)
    cid = db.register_city(
        conn,
        city_name="Newport",
        state_name="Kentucky",
        state_code="KY",
        country_name="United States",
        country_code="US",
        center_lat=39.0836,
        center_lon=-84.4836,
        grid_width_m=3892,
        grid_height_m=4182,
        step_m=20,
    )
    db.add_api_usage(conn, TODAY, 14_995, "mapillary")

    _assess(tmp_path, estimate_only=True)

    out = capsys.readouterr().out
    assert cid == CITY_ID
    assert "14,995 of 15,000 spent today" in out
    assert "OVER REMAINING BUDGET, deferred" in out


def _hosts_cfg(tmp_path, host_budget=3000):
    """``_cfg``'s four channels plus a [hosts.mapillary_tiles] budget, THROUGH the loader.

    The host budget is the one setting here a hand-built ``SchedulerConfig``
    could hold in a shape the loader would refuse (issue #385 review), so these
    tests read it from TOML the way production does.
    """
    path = tmp_path / "assess-hosts.toml"
    path.write_text(
        "[providers.gsv]\nenabled = true\ndaily_request_budget = 10000000\n\n"
        "[providers.gsv_streets]\nenabled = true\ndaily_request_budget = 3000000\n\n"
        "[providers.mapillary]\nenabled = true\ndaily_request_budget = 15000\n"
        "max_requests_per_minute = 60\n\n"
        "[providers.mapillary_streets]\nenabled = true\ndaily_request_budget = 5000\n"
        "max_requests_per_minute = 60\n\n"
        f'[paths]\ndata_dir = "{tmp_path / "data"}"\nlog_dir = "{tmp_path / "logs"}"\n'
        f'backup_dir = "{tmp_path / "backups"}"\n\n'
        f"[hosts.mapillary_tiles]\nrolling_24h_request_budget = {host_budget}\n"
    )
    cfg = _sched.load_scheduler_config(str(path))
    assert cfg.host_budgets == {HOST_MAPILLARY_TILES: host_budget}
    assert cfg.host_budget_errors == []
    return cfg


def _seed_tile_window(conn, n, at="2026-08-17T09:00:00+00:00"):
    conn.execute(
        "INSERT INTO host_usage VALUES (?, 'mapillary_tiles', 'mapillary_streets', ?)", (at, n)
    )
    conn.commit()


def test_the_preflight_shows_the_tile_cdns_rolling_window(
    conn, monkeypatch, tmp_path, capsys, frozen_utc_clock
):
    """Issue #385: beside each Mapillary channel's daily line, the host's last 24 h.

    2,995 of 3,000 are gone, so the 9-tile grid run does not fit what the HOST
    has left even though its own daily budget is untouched -- and the pre-flight
    says which ceiling that is. gsv_streets shares no budgeted host and gets no
    host line.
    """
    _stub_collection(monkeypatch, conn)
    frozen_utc_clock(datetime(2026, 8, 17, 12, 0, tzinfo=UTC))
    _seed_tile_window(conn, 2995)
    cfg = _hosts_cfg(tmp_path)

    _sched.cmd_assess_city(cfg, QUERY, today=TODAY, assume_yes=True, estimate_only=True)

    lines = capsys.readouterr().out.splitlines()
    host_lines = [
        line for line in lines if "mapillary_tiles: 2,995 of 3,000 in the last 24 h" in line
    ]
    assert len(host_lines) == sum(1 for c in ASSESS_CHANNELS if c.startswith("mapillary"))
    grid = next(line for line in lines if line.strip().startswith("mapillary "))
    assert "OVER mapillary_tiles's rolling-24h budget" in grid
    assert "0 of 15,000 spent today" in grid


def test_a_host_deferral_leaves_the_inquiry_incomplete_and_says_why(
    conn, monkeypatch, tmp_path, capsys, frozen_utc_clock
):
    """Issue #385 review: assess-city's COLLECTION path, not just its pre-flight.

    The morning after a full night the tile CDN's window has 1 of 3,000 left:
    both Mapillary channels are deferred under the launch floor by the host,
    and only the GSV walk runs. The summary has to name the host's window, and
    the command must not exit "complete" -- an inquiry answered without its
    Mapillary half is not answered. Fails if the summary drops
    ``_host_budget_note`` or ``nothing_deferred`` drops its host term.
    """
    ran = _stub_collection(monkeypatch, conn)
    frozen_utc_clock(datetime(2026, 8, 17, 12, 0, tzinfo=UTC))
    _seed_tile_window(conn, 2999)
    cfg = _hosts_cfg(tmp_path)

    rc = _sched.cmd_assess_city(cfg, QUERY, today=TODAY, assume_yes=True, publish=False)

    assert [p for _, p in ran] == ["gsv_streets"]
    summary = next(
        line for line in capsys.readouterr().out.splitlines() if "channel(s) collected" in line
    )
    assert summary.strip().startswith("1/1 channel(s) collected")
    assert "2 deferred for the rolling-24h budget of Mapillary's tile CDN" in summary
    assert "skipped on budget" not in summary
    assert rc == 1, "a host deferral is not a complete answer"


def test_a_channel_over_the_whole_budget_is_not_called_deferred(
    conn, monkeypatch, tmp_path, capsys
):
    """
    The two budget guards in _run_city_channels mean different things and the
    operator's next move differs. "Deferred to a later run" is right for work
    that does not fit what is LEFT today; for work that exceeds the whole budget
    it promises a later run that never comes, because tomorrow's budget is the
    same size. That one needs a config change or a smaller grid.
    """
    _stub_collection(monkeypatch, conn)
    cfg = _cfg(tmp_path)
    cfg.providers["mapillary"].daily_request_budget = 2  # below the 9-tile estimate

    _sched.cmd_assess_city(cfg, QUERY, today=TODAY, assume_yes=True, estimate_only=True)

    out = capsys.readouterr().out
    assert "EXCEEDS THE ENTIRE DAILY BUDGET" in out
    assert "deferred to a later run" not in out


def test_the_preflight_names_the_combined_per_ip_mapillary_exposure(
    conn, monkeypatch, tmp_path, capsys
):
    """Two censuses on one address: the block is per-IP, so the sum is what is
    on the line, not either channel's own budget."""
    _stub_collection(monkeypatch, conn)

    _assess(tmp_path, estimate_only=True)

    out = capsys.readouterr().out
    assert "tile requests from THIS HOST's IP" in out
    assert "paced at 60/min" in out


def test_differently_paced_mapillary_channels_are_both_reported(
    conn, monkeypatch, tmp_path, capsys
):
    """The two channels hold independent [providers.*] blocks and run
    back-to-back, so one rate beside a summed total would misreport a config
    that paces them apart.

    The rate is reported as a MEAN with its jitter beside it (issue #292): since
    the pacer stopped being a metronome, a bare "60/min" reads exactly like the
    cadence it replaced, and the shape is the whole change an operator is here
    to see. An explicit 0 must read as the exact cadence it is.
    """
    _stub_collection(monkeypatch, conn)
    cfg = _cfg(tmp_path)
    cfg.providers["mapillary_streets"].max_requests_per_minute = 30

    _sched.cmd_assess_city(cfg, QUERY, today=TODAY, assume_yes=True, estimate_only=True)

    out = capsys.readouterr().out
    # Unset jitter means the child's own default, which is jittered.
    assert "paced at 30/min (mean, gaps at CV 0.60) and 60/min (mean, gaps at CV 0.60)" in out

    cfg.providers["mapillary_streets"].jitter = 0
    _sched.cmd_assess_city(cfg, QUERY, today=TODAY, assume_yes=True, estimate_only=True)
    assert "30/min (exact cadence)" in capsys.readouterr().out


# --------------------------------------------------------------------------
# boundary pre-flight
# --------------------------------------------------------------------------


def _square(south, north, west, east):
    return {
        "type": "Polygon",
        "coordinates": [
            [[west, south], [east, south], [east, north], [west, north], [west, south]]
        ],
    }


def test_rect_in_boundary_frac_measures_the_rectangle_not_the_city():
    """The reciprocal of rect_polygon_coverage: what share of what we SAMPLE is
    actually this place. Northern Kentucky's county rectangles scored 0.49-0.69,
    with the remainder largely Cincinnati."""
    # A rectangle strictly inside a big polygon: all of it is in-boundary.
    assert (
        rect_in_boundary_frac(_square(38.0, 40.0, -85.0, -83.0), (39.0, 39.1, -84.6, -84.4)) == 1.0
    )
    # Polygon covers the western half of the rectangle.
    half = rect_in_boundary_frac(_square(39.0, 39.1, -84.6, -84.5), (39.0, 39.1, -84.6, -84.4))
    assert half == pytest.approx(0.5, abs=0.01)
    # Disjoint.
    assert rect_in_boundary_frac(_square(10.0, 11.0, 10.0, 11.0), (39.0, 39.1, -84.6, -84.4)) == 0.0


def test_rect_in_boundary_frac_is_none_without_a_usable_polygon():
    """Nominatim answers with a Point for many places; None means "unknown",
    which the report must not render as 0%."""
    rect = (39.0, 39.1, -84.6, -84.4)
    assert rect_in_boundary_frac({"type": "Point", "coordinates": [-84.5, 39.05]}, rect) is None
    assert rect_in_boundary_frac(None, rect) is None
    assert rect_in_boundary_frac(_square(39.0, 39.1, -84.6, -84.4), None) is None


def test_a_low_in_boundary_fraction_warns_before_anything_is_spent(
    conn, monkeypatch, tmp_path, capsys
):
    monkeypatch.setattr(_sched, "_boundary_preflight", lambda city: (0.46, 1.0))
    _stub_collection(monkeypatch, conn)

    _assess(tmp_path, estimate_only=True)

    out = capsys.readouterr().out
    assert "46% of the sampled rectangle is inside the boundary" in out
    assert "Northern Kentucky" in out
    assert "--lat/--lng" in out


def test_a_healthy_in_boundary_fraction_does_not_warn(conn, monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(_sched, "_boundary_preflight", lambda city: (0.93, 0.88))
    _stub_collection(monkeypatch, conn)

    _assess(tmp_path, estimate_only=True)

    out = capsys.readouterr().out
    assert "93% of the sampled rectangle is inside the boundary" in out
    assert "Northern Kentucky" not in out


def test_a_failing_boundary_probe_never_fails_the_run(conn, monkeypatch, tmp_path, capsys):
    """
    Advisory only. A geocoder timeout, an unexpected payload — anything — must
    degrade to "unknown" rather than cost a collection, the same posture as the
    Overpass /status probe.
    """
    from streetscape_metadata_tracker import geoutils

    def boom(*a, **k):
        raise TimeoutError("nominatim down")

    # Put the REAL probe back (the autouse fixture stubbed it out) and break the
    # geocoder underneath it, so this exercises the actual except-Exception path
    # rather than a stub that returns None.
    monkeypatch.setattr(_sched, "_boundary_preflight", _REAL_BOUNDARY_PREFLIGHT)
    monkeypatch.setattr(geoutils, "geocode_boundary_raw", boom)
    ran = _stub_collection(monkeypatch, conn)

    rc = _assess(tmp_path)

    assert rc == 0
    assert [p for _, p in ran] == list(ASSESS_CHANNELS)
    assert "in-boundary fraction unknown" in capsys.readouterr().out


# --------------------------------------------------------------------------
# channel selection and collection
# --------------------------------------------------------------------------


def test_collects_the_two_walks_plus_the_mapillary_grid_run(conn, monkeypatch, tmp_path):
    """
    The scope decision, pinned. The two road walks are the answer; the Mapillary
    grid run is the same tile census and is what puts the city in
    cities.json.gz, without which there is no city page to link. The GSV grid
    run is NOT here — it is the expensive half and the nightly batch will pick
    it up.
    """
    ran = _stub_collection(monkeypatch, conn)

    _assess(tmp_path)

    assert [p for _, p in ran] == ["gsv_streets", "mapillary", "mapillary_streets"]


def test_provider_filter_narrows_to_one_channel(conn, monkeypatch, tmp_path):
    ran = _stub_collection(monkeypatch, conn)

    _assess(tmp_path, requested_providers=["mapillary_streets"])

    assert [p for _, p in ran] == ["mapillary_streets"]


def test_the_comma_form_means_the_repeated_form_and_keeps_canonical_order(
    conn, monkeypatch, tmp_path
):
    ran = _stub_collection(monkeypatch, conn)

    _assess(tmp_path, requested_providers=["mapillary_streets,gsv_streets"])

    assert [p for _, p in ran] == ["gsv_streets", "mapillary_streets"]


def test_a_disabled_channel_is_not_collected_even_unfiltered(conn, monkeypatch, tmp_path):
    cfg = _cfg(tmp_path)
    cfg.providers["gsv_streets"] = ProviderConfig(enabled=False)
    ran = _stub_collection(monkeypatch, conn)

    _assess(tmp_path, cfg=cfg)

    assert [p for _, p in ran] == ["mapillary", "mapillary_streets"]


def test_the_daily_budget_ledger_still_governs_a_manual_run(conn, monkeypatch, tmp_path):
    """
    Inheriting the ledger is a large part of why this is a scheduler subcommand
    and not a script: a channel with the day already spent must be skipped here
    exactly as it would be on a nightly run, and counted as a budget skip rather
    than a failure.

    A budget of ZERO, and the zero is load-bearing since #318. Mapillary is
    resumable now, so a small-but-nonzero budget no longer skips the channel --
    it launches the crawl with that many requests as its cap, which is the whole
    point of the issue. What still refuses a launch is having nothing left to
    cap at, so an exhausted ledger is the case this test has to encode.
    """
    cfg = _cfg(tmp_path)
    cfg.providers["mapillary"] = ProviderConfig(enabled=True, daily_request_budget=0)
    ran = _stub_collection(monkeypatch, conn)

    rc = _assess(tmp_path, cfg=cfg)

    assert [p for _, p in ran] == ["gsv_streets", "mapillary_streets"]
    assert "mapillary" not in _state(conn)  # skipped, not failed
    assert rc == 1  # an incomplete answer is never a clean run


def test_declining_the_confirmation_collects_nothing(conn, monkeypatch, tmp_path):
    monkeypatch.setattr(_sched.db, "connect", lambda path: conn)
    monkeypatch.setattr(_sched, "_run_one_city", lambda *a, **k: pytest.fail("collected!"))
    monkeypatch.setattr(_sched.sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr("builtins.input", lambda prompt="": "n")

    rc = _sched.cmd_assess_city(_cfg(tmp_path), QUERY, today=TODAY, assume_yes=False)

    assert rc == 0  # declining is not a failure


# --------------------------------------------------------------------------
# cadence bookkeeping
# --------------------------------------------------------------------------


def _state(conn):
    return {
        r["provider"]: r
        for r in conn.execute(
            "SELECT provider, last_success_at, consecutive_failures FROM schedule_state"
        )
    }


def test_success_starts_only_the_collected_channels_clocks(conn, monkeypatch, tmp_path):
    """
    Recording success is what stops the next nightly batch from re-spending the
    same crawl hours later. Leaving `gsv` untouched is what makes the GSV grid
    run still happen — that is the division of labour this command relies on.
    """
    _stub_collection(monkeypatch, conn)

    _assess(tmp_path)

    state = _state(conn)
    # Only the clocks: since #374 a new city also gains opt-in MEMBERSHIP rows,
    # which carry no success and start nothing.
    started = {c for c, row in state.items() if row["last_success_at"]}
    assert started == set(ASSESS_CHANNELS)
    assert "gsv" not in state


def test_the_closing_note_says_which_channels_are_actually_due(conn, monkeypatch, tmp_path, capsys):
    """
    The report used to close with "this city is now due on every channel", which
    is the opposite of true for the three channels it just collected: recording a
    success stamps last_success_at, get_due_cities reads only that, and so the
    collected channels are the LEAST stale rows in the catalog. Only `gsv` — the
    one deliberately left alone — is due. Asserted against get_due_cities rather
    than against the wording alone, so the sentence cannot drift back out of
    agreement with the scheduler.
    """
    _stub_collection(monkeypatch, conn)

    _assess(tmp_path)

    out = capsys.readouterr().out
    due = {p: _due(conn, p) for p in ("gsv", *ASSESS_CHANNELS)}
    # Disabled (#431): nothing is due until enable-city...
    assert due["gsv"] == []
    assert all(due[c] == [] for c in ASSESS_CHANNELS)
    assert "this city is DISABLED" in out
    assert f"enable-city {CITY_ID}" in out
    # ...and once it is enabled, gsv alone: the channels collected here have
    # their clocks started, so the wording below holds in both states.
    db.set_city_enabled(conn, CITY_ID, True)
    due = {p: _due(conn, p) for p in ("gsv", *ASSESS_CHANNELS)}
    assert due["gsv"] == [CITY_ID]
    assert all(due[c] == [] for c in ASSESS_CHANNELS)

    assert "due on every channel" not in out
    assert "The GSV grid run is not part of this command" in out
    # ...and it no longer claims the grid run LEADS the queue unconditionally:
    # that is true of gsv's own list, and only the union's order while the city
    # is a gsv member (#301).
    assert "leads gsv's OWN stalest-first list" in out
    assert "NOT due tonight" in out
    # And the paired-snapshot cost is named, the way run-due --provider names it
    # (issue #214) rather than leaving it to be discovered.
    assert "no longer share one run date" in out


def test_the_paired_snapshot_note_is_absent_when_nothing_was_collected(
    conn, monkeypatch, tmp_path, capsys
):
    """No success, no clock started, nothing un-paired — so claiming otherwise
    would send an operator looking for a desync that never happened."""
    _stub_collection(monkeypatch, conn, outcome=lambda city, provider: False)

    _assess(tmp_path)

    out = capsys.readouterr().out
    assert "no longer share one run date" not in out
    # The GSV note still applies: that channel was never given a row either way.
    assert "The GSV grid run is not part of this command" in out


def test_a_failed_channel_records_no_failure(conn, monkeypatch, tmp_path):
    """
    get_due_cities filters on `consecutive_failures < max_consecutive_failures`
    and NOTHING resets that counter except a success, so letting an operator's
    ad-hoc probe increment it would let a few of them quarantine a city for a
    whole 90-day cycle — recoverable only by hand-written SQL.
    """
    _stub_collection(
        monkeypatch, conn, outcome=lambda city, provider: provider != "mapillary_streets"
    )

    rc = _assess(tmp_path)

    state = _state(conn)
    assert rc == 1  # the run is still reported unhealthy
    assert "mapillary_streets" not in state or state["mapillary_streets"]["last_success_at"] is None
    assert all(row["consecutive_failures"] == 0 for row in state.values())


def test_repeated_failures_leave_the_city_collectable_by_the_nightly_batch(
    conn, monkeypatch, tmp_path
):
    """The corollary of the rule above, over the failure threshold."""
    _stub_collection(monkeypatch, conn, outcome=lambda city, provider: False)

    for _ in range(6):
        _assess(tmp_path)
    # The city is registered disabled (#431); enabling it is the operator step
    # that hands it to the nightly batch, which is what this test is about.
    db.set_city_enabled(conn, CITY_ID, True)

    _due_cfg = _cfg(tmp_path)
    due = _sched._collect_due(
        conn,
        _due_cfg,
        TODAY,
        ["gsv"],
        max_opt_in=_sched._opt_in_reservation(_due_cfg, _due_cfg.max_cities_per_day),
        max_cities=_due_cfg.max_cities_per_day,
    ).cities
    assert [c.city_id for c in due] == [CITY_ID]


# --------------------------------------------------------------------------
# host conditions (inherited from _run_city_channels, pinned for this caller)
# --------------------------------------------------------------------------


def _blocked_outcome(host):
    """A child that reports "this third party refused our IP" (issue #208).

    Uses the production CollectionOutcome rather than a look-alike, so a change
    to what the loop reads off it fails here too.
    """
    return _sched.CollectionOutcome(False, "stubbed", exit_code=HOST_EXIT_CODES[host])


def _busy_outcome(host):
    """A child that reports "another local process holds that host's lock"."""
    return _sched.CollectionOutcome(False, "stubbed", exit_code=HOST_BUSY_EXIT_CODES[host])


def test_a_mapillary_tile_block_still_lets_the_gsv_walk_run(conn, monkeypatch, tmp_path):
    """
    The channels' hosts differ: gsv_streets needs only Overpass. So a per-IP
    tile block must not cost the GSV road walk, which is the number a deployment
    decision most turns on.
    """
    blocked = _blocked_outcome(HOST_MAPILLARY_TILES)
    ran = _stub_collection(
        monkeypatch,
        conn,
        outcome=lambda city, provider: blocked if provider.startswith("mapillary") else True,
    )

    rc = _assess(tmp_path)

    # The GSV walk completes. mapillary_streets is never even attempted: the
    # tile host just refused this IP, so asking again cannot answer differently.
    assert [p for _, p in ran] == ["gsv_streets", "mapillary"]
    assert _state(conn)["gsv_streets"]["last_success_at"]
    assert "mapillary_streets" not in _state(conn)
    assert rc == 1  # a refused host is never a clean run


def test_an_overpass_refusal_skips_every_channel_that_needs_it(conn, monkeypatch, tmp_path):
    """
    Overpass is the first step of both road walks, so once it refuses this host
    there is nothing to learn by asking again. The Mapillary GRID run does not
    need it and still runs.
    """
    blocked = _blocked_outcome(HOST_OVERPASS)
    ran = _stub_collection(
        monkeypatch,
        conn,
        outcome=lambda city, provider: blocked if provider == "gsv_streets" else True,
    )

    _assess(tmp_path)

    assert [p for _, p in ran] == ["gsv_streets", "mapillary"]


def test_a_busy_host_skips_one_channel_without_tripping_the_breaker(conn, monkeypatch, tmp_path):
    """
    Busy means another local process holds the lock — the ordinary case now that
    a manual run alongside the nightly batch is a supported thing to do. It ends
    when that process does, so it must not become a run-wide skip.
    """
    busy = _busy_outcome(HOST_MAPILLARY_TILES)
    ran = _stub_collection(
        monkeypatch,
        conn,
        outcome=lambda city, provider: busy if provider == "mapillary" else True,
    )

    rc = _assess(tmp_path)

    # mapillary_streets is still attempted: the lock may have freed by then.
    assert [p for _, p in ran] == ["gsv_streets", "mapillary", "mapillary_streets"]
    assert "mapillary" not in _state(conn)
    assert rc == 1


def test_assess_city_reports_an_argv_rejection_and_exits_1(conn, monkeypatch, tmp_path, capsys):
    """
    Our own CLI refusing the argv the scheduler built (issue #359) records no
    attempt, so without its own term a rejected channel beside two collected
    ones scores 2/2 and exits 0 -- an inquiry answered with a channel missing.
    """
    rejected = _sched.CollectionOutcome(False, "stubbed", exit_code=ARGV_REJECTED_EXIT_CODE)
    _stub_collection(
        monkeypatch,
        conn,
        outcome=lambda city, provider: rejected if provider == "mapillary" else True,
    )

    rc = _assess(tmp_path)

    assert rc == 1
    assert "1 channel(s) REJECTED by our own CLI" in capsys.readouterr().out
    row = _state(conn).get("mapillary")
    assert row is None or row["consecutive_failures"] == 0


# --------------------------------------------------------------------------
# the tail
# --------------------------------------------------------------------------


def test_the_tail_regenerates_then_publishes(conn, monkeypatch, tmp_path):
    order = []
    monkeypatch.setattr(
        _sched,
        "_regenerate_published_json",
        lambda c, cfg: (order.append("regen") or "ok", True),
    )
    monkeypatch.setattr(_sched, "_publish", lambda cfg, ctx: order.append("publish") or 0)
    _stub_collection(monkeypatch, conn)

    rc = _assess(tmp_path, cfg=_cfg(tmp_path, publish_enabled=True))

    assert order == ["regen", "publish"]
    assert rc == 0


def test_nothing_collected_means_nothing_published(conn, monkeypatch, tmp_path):
    """Mirrors _finish_batch's `succeeded > 0` gate: an rsync that ships no new
    artifact is pure noise, and a failed one would email an alert for it."""
    published = []
    monkeypatch.setattr(_sched, "_publish", lambda cfg, ctx: published.append("publish") or 0)
    _stub_collection(monkeypatch, conn, outcome=lambda city, provider: False)

    rc = _assess(tmp_path, cfg=_cfg(tmp_path, publish_enabled=True))

    assert published == []
    assert rc == 1


def test_a_partial_failure_still_publishes_what_succeeded(conn, monkeypatch, tmp_path):
    """#167's posture: collected work stays invisible until the aggregate is
    rebuilt, so a partial answer must still reach the site."""
    published = []
    monkeypatch.setattr(_sched, "_publish", lambda cfg, ctx: published.append("publish") or 0)
    _stub_collection(monkeypatch, conn, outcome=lambda city, provider: provider == "mapillary")

    rc = _assess(tmp_path, cfg=_cfg(tmp_path, publish_enabled=True))

    assert published == ["publish"]
    assert rc == 1


def test_no_publish_regenerates_but_does_not_rsync(conn, monkeypatch, tmp_path):
    published = []
    monkeypatch.setattr(_sched, "_publish", lambda cfg, ctx: published.append("publish") or 0)
    _stub_collection(monkeypatch, conn)

    rc = _assess(tmp_path, cfg=_cfg(tmp_path, publish_enabled=True), publish=False)

    assert published == []
    assert rc == 0


def test_publishing_disabled_in_config_says_so_beside_the_link(conn, monkeypatch, tmp_path, capsys):
    """
    The city-page link is built from the CATALOG, not from what is live, so with
    publishing switched off it reads exactly like an answer while pointing at
    stale or absent data — and the only other signal is the ABSENCE of
    "; published" from the summary, which nobody reads as "not published". The
    case that matters is not a dev laptop but prod with publishing off during a
    block or a maintenance window, where the operator emails a partner a dead
    link. Same class of bug as the false publish-FAILED alert this PR fixes:
    publishing decided by config with no feedback at the point of use.
    """
    published = []
    monkeypatch.setattr(_sched, "_publish", lambda cfg, ctx: published.append("publish") or 0)
    _stub_collection(monkeypatch, conn)

    rc = _assess(tmp_path, cfg=_cfg(tmp_path, publish_enabled=False))

    out = capsys.readouterr().out
    assert published == []
    assert "[publish].enabled is false" in out
    assert "not what is live" in out
    # A config that declares this host does not publish is not a failure — the
    # same reasoning that makes --no-publish exit 0. Only an ATTEMPTED publish
    # that failed is, which is why the notice above has to exist.
    assert rc == 0


def test_no_publish_does_not_print_the_publishing_disabled_notice(
    conn, monkeypatch, tmp_path, capsys
):
    """The operator asked for it, so it is not a surprise worth a warning — the
    notice is for the case they did not choose."""
    monkeypatch.setattr(_sched, "_publish", lambda cfg, ctx: 0)
    _stub_collection(monkeypatch, conn)

    _assess(tmp_path, cfg=_cfg(tmp_path, publish_enabled=True), publish=False)

    assert "[publish].enabled is false" not in capsys.readouterr().out


def test_assess_city_has_no_publish_override_flag():
    """
    Deliberate asymmetry with `regenerate-aggregate --publish`, pinned so it
    reads as a decision rather than an omission (issue #215). [publish].enabled
    is the host's own declaration, and moving publishing OUT of ambient state and
    INTO config is what the rest of this change does; an override belongs to the
    command whose job genuinely is "push the catalog to the site right now", not
    to a collection command whose publish is a consequence. It is also the one
    flag that would let a non-prod checkout overwrite prod's cities.json.gz,
    which _regenerate_published_json rebuilds from the LOCAL catalog.
    """
    args = build_parser().parse_args(["assess-city", "--yes", QUERY])

    assert args.no_publish is False
    assert not hasattr(args, "publish")


# --------------------------------------------------------------------------
# the answer report
# --------------------------------------------------------------------------


def _register_walk(conn, provider, **overrides):
    row = dict(
        city_id=CITY_ID,
        provider=provider,
        run_date=TODAY,
        csv_filename=f"{CITY_ID}_{provider}_streetwalk_sp15_{TODAY}.csv.gz",
        coverage_filename=f"{CITY_ID}_{provider}_streetwalk_sp15_{TODAY}_coverage.json.gz",
        network_type="drive",
        spacing_m=15,
        match_dist_m=25.0,
        sample_points=1000,
        edges_total=100,
        edges_fully_covered=50,
        mean_edge_coverage=0.5,
        coverage_pct_by_length=58.2,
        coverage_pct_by_length_any=76.8,
        length_km=200.1,
        length_km_covered=116.3,
        length_km_covered_any=153.7,
        median_covered_age_years=1.3,
        api_requests=9,
    )
    row.update(overrides)
    return db.register_street_walk(conn, **row)


def _register_grid_run(conn, coverage_rate_pct=17.0):
    """The Mapillary grid run assess-city collects, and its CSV name — which is
    how city.html is addressed."""
    csv_name = f"{CITY_ID}_width_3892_height_4182_step_20_mapillary_{TODAY}.csv.gz"
    db.register_run(
        conn,
        city_id=CITY_ID,
        run_date=TODAY,
        csv_filename=csv_name,
        provider="mapillary",
        coverage_rate_pct=coverage_rate_pct,
    )
    return csv_name


def _report(conn, tmp_path, **cfg_overrides):
    """The answer report for the registered city, as a string.

    _assess_answer_report RETURNS its text (the command prints it), so these
    tests read the return value rather than round-tripping through capsys.
    """
    return _sched._assess_answer_report(
        _cfg(tmp_path, **cfg_overrides), conn, db.resolve_city(conn, CITY_ID)
    )


def test_the_answer_leads_with_street_coverage_and_disclaims_grid_coverage(
    conn, monkeypatch, tmp_path
):
    """
    The finding this command exists to encode: Highland Heights reads 55.6% of
    grid points but 92.8% of street-km, because grid points land on river, rail,
    parkland and rooftops. Quoting grid coverage to a partner is the mistake, so
    the output has to label it.
    """
    _stub_collection(monkeypatch, conn)
    _assess(tmp_path)
    _register_walk(conn, "gsv", coverage_pct_by_length=92.8, coverage_pct_by_length_any=92.8)
    _register_walk(conn, "mapillary")
    # 55.6% grid vs 92.8% street-km is the real Highland Heights pair.
    _register_grid_run(conn, coverage_rate_pct=55.6)
    out = _report(conn, tmp_path)
    assert "Street coverage (drive network)" in out
    assert "92.8% of street-km" in out
    assert "116.3 of 200.1 km" in out
    assert "76.8% including flat imagery" in out  # Mapillary-only information
    assert "median covered imagery 1.3 y old" in out
    assert "55.6% of grid points" in out
    assert "NOT the deployment number" in out
    # Street coverage comes first, so the number a partner reads first is the
    # one a deployment decision turns on.
    assert out.index("Street coverage") < out.index("Grid coverage")


def test_the_any_imagery_split_is_not_printed_for_gsv(conn, monkeypatch, tmp_path, capsys):
    """GSV emits no FLAT_ONLY, so its any-value equals its 360° value by
    construction — printing it as a second figure would invent a distinction."""
    _stub_collection(monkeypatch, conn)
    _assess(tmp_path)
    _register_walk(conn, "gsv", coverage_pct_by_length=92.8, coverage_pct_by_length_any=92.8)
    assert "including flat imagery" not in _report(conn, tmp_path)


def test_a_missing_walk_reads_as_not_walked_rather_than_zero(conn, monkeypatch, tmp_path, capsys):
    """Not-measured is never 0% — a partner would read 0% as "no imagery"."""
    _stub_collection(monkeypatch, conn)
    _assess(tmp_path)
    _register_walk(conn, "mapillary")
    assert "gsv        not walked" in _report(conn, tmp_path)


def test_null_walk_stats_do_not_crash_the_report(conn, monkeypatch, tmp_path, capsys):
    """A walk that covered nothing has no age to take a median of, and a pre-v12
    row has no lengths at all."""
    _stub_collection(monkeypatch, conn)
    _assess(tmp_path)
    _register_walk(
        conn,
        "mapillary",
        coverage_pct_by_length=0.0,
        coverage_pct_by_length_any=None,
        length_km=None,
        length_km_covered=None,
        median_covered_age_years=None,
    )
    out = _report(conn, tmp_path)
    assert "0.0% of street-km" in out
    assert "including flat imagery" not in out


def test_the_city_page_link_uses_the_grid_runs_filename(conn, monkeypatch, tmp_path, capsys):
    """
    city.html is addressed by run-CSV filename (never by city_id), which is the
    entire reason the Mapillary grid run is in ASSESS_CHANNELS.
    """
    _stub_collection(monkeypatch, conn)
    _assess(tmp_path)
    csv_name = _register_grid_run(conn, coverage_rate_pct=17.0)

    out = _report(conn, tmp_path, site_url="https://example.test/tracker/")
    assert f"https://example.test/tracker/city.html?file={csv_name}&network=drive" in out
    assert "17.0% of grid points" in out


def test_without_a_grid_run_on_any_provider_the_report_says_there_is_no_city_page(
    conn, monkeypatch, tmp_path, capsys
):
    """A walk-only city is absent from cities.json.gz (generate_aggregate_v2
    skips a city with no runs), so promising a link would be a broken one."""
    _stub_collection(monkeypatch, conn)
    _assess(tmp_path, requested_providers=["gsv_streets"])
    assert "no city page to link" in _report(conn, tmp_path)


def test_the_no_city_page_sentence_says_when_the_link_arrives_in_both_states(
    conn, monkeypatch, tmp_path
):
    """
    The no-link sentence tells the operator when to come back for the link, so
    it must agree with get_due_cities. For a DISABLED city (every ad-hoc
    registration since #431) "the next nightly batch" never comes, and the
    closing note of the same output says so -- one block of output must not
    claim both. Asserted against the scheduler's own query in each state, the
    shape test_the_closing_note_says_which_channels_are_actually_due uses.
    """
    _stub_collection(monkeypatch, conn)
    _assess(tmp_path, requested_providers=["gsv_streets"])

    out = _report(conn, tmp_path)
    assert _due(conn, "gsv") == []
    assert "no city page to link" in out
    assert "next nightly batch" not in out
    assert "This city is DISABLED" in out
    assert f"enable-city {CITY_ID}" in out

    db.set_city_enabled(conn, CITY_ID, True)
    out = _report(conn, tmp_path)
    assert _due(conn, "gsv") == [CITY_ID]
    assert "lands on the next nightly batch" in out
    assert "DISABLED" not in out


def test_the_city_page_link_falls_back_to_the_gsv_grid_run(conn, monkeypatch, tmp_path):
    """
    The Mapillary grid run is what USUALLY supplies the link, but it is routinely
    absent from an assess run — the channel switched off after a per-IP block,
    narrowed away by --provider, over budget, or skipped by the host breaker. An
    already-tracked city still has a GSV run, and city.html is addressed by
    run-CSV filename whatever the provider, so the link exists. Reporting "no
    city page" there sent the operator away without the one thing they ran this
    command for, and told them to wait for a nightly batch that already ran.
    """
    _stub_collection(monkeypatch, conn)
    _assess(tmp_path, requested_providers=["gsv_streets"])
    csv_name = f"{CITY_ID}_width_3892_height_4182_step_20_2026-08-01.csv.gz"
    db.register_run(
        conn,
        city_id=CITY_ID,
        run_date=date(2026, 8, 1),
        csv_filename=csv_name,
        provider="gsv",
        coverage_rate_pct=61.2,
    )

    out = _report(conn, tmp_path, site_url="https://example.test/tracker/")

    assert f"https://example.test/tracker/city.html?file={csv_name}&network=drive" in out
    # Named, because the page it opens shows that provider's imagery — and the
    # grid-coverage line above stays Mapillary-only, so an unlabelled link would
    # read as belonging to a Mapillary run that does not exist.
    assert "City page (gsv run)" in out
    assert "no city page to link" not in out
    # Grid coverage stays a Mapillary-only figure: GSV grid coverage is exactly
    # the number this command exists to stop anyone quoting.
    assert "61.2%" not in out


def test_the_mapillary_grid_run_still_wins_the_link_when_both_exist(conn, monkeypatch, tmp_path):
    """It is the run this command collects, and the one whose page shows the
    Mapillary walk beside it."""
    _stub_collection(monkeypatch, conn)
    _assess(tmp_path)
    mapillary_csv = _register_grid_run(conn, coverage_rate_pct=17.0)
    db.register_run(
        conn,
        city_id=CITY_ID,
        run_date=date(2026, 8, 1),
        csv_filename=f"{CITY_ID}_width_3892_height_4182_step_20_2026-08-01.csv.gz",
        provider="gsv",
        coverage_rate_pct=61.2,
    )

    out = _report(conn, tmp_path)

    assert f"file={mapillary_csv}" in out
    assert "City page (mapillary run)" in out


def test_a_site_url_without_a_trailing_slash_still_builds_a_valid_link(conn, monkeypatch, tmp_path):
    """Link building is bare concatenation, so an operator omitting the slash
    would otherwise get '…/streetscape-trackercity.html'."""
    _stub_collection(monkeypatch, conn)
    _assess(tmp_path)
    csv_name = _register_grid_run(conn)

    out = _report(conn, tmp_path, site_url="https://example.test/tracker")

    assert f"https://example.test/tracker/city.html?file={csv_name}" in out


# --------------------------------------------------------------------------
# [publish].local — the false-alert trap this fixes
# --------------------------------------------------------------------------


@pytest.mark.parametrize("publish_local", [True, False])
def test_publish_passes_local_iff_configured(monkeypatch, tmp_path, publish_local):
    """
    A hand-run publish on makelab2 took the SSH path and emailed a
    publish-FAILED alert, because STREETSCAPE_PUBLISH_LOCAL is set in the systemd
    unit and not in an operator shell — so this is config, not ambient env. The
    False case matters just as much: a host that genuinely publishes over SSH
    must not have --local forced on it.
    """
    calls = []

    class _Proc:
        pid = 424242

        def __init__(self, cmd, **kw):
            calls.append(cmd)

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr(_sched.subprocess, "Popen", _Proc)

    _sched._publish(_cfg(tmp_path, publish_local=publish_local), "ctx")

    assert ("--local" in calls[0]) is publish_local


def test_publish_local_is_read_from_the_toml(tmp_path):
    cfg_path = tmp_path / "s.toml"
    cfg_path.write_text('[publish]\nenabled = true\nlocal = true\nsite_url = "https://x.test/"\n')

    cfg = _sched.load_scheduler_config(str(cfg_path))

    assert cfg.publish_local is True
    assert cfg.site_url == "https://x.test/"


def test_production_config_publishes_locally(tmp_path):
    """
    makelab2 NFS-mounts the docroot. Asserted against the checked-in prod config
    for the same reason test_makelab1_production_config_is_wired exists: the
    setting is invisible until a publish fails.
    """
    import os

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cfg = _sched.load_scheduler_config(os.path.join(root, "config", "scheduler.makelab1.toml"))

    assert cfg.publish_local is True
    assert cfg.site_url.startswith("https://")


def test_assess_channels_never_includes_the_gsv_grid_run():
    """
    The expensive half stays on the nightly cycle, where the batch deadline and
    city cap bound it. A newly registered city is DISABLED until `enable-city`
    (#431); once enabled, its last_success_at is NULL, so it leads the next
    night's stalest-first queue on its own.
    """
    assert set(ASSESS_CHANNELS) == {"gsv_streets", "mapillary", "mapillary_streets"}
    # And every assess channel must be a channel the scheduler knows how to run.
    assert all(c in _sched.CHANNEL_HOSTS for c in ASSESS_CHANNELS)


def test_assess_city_inherits_the_lane_scheduler_from_the_config_knob(conn, monkeypatch, tmp_path):
    """The operator path runs the same lane scheduler, gates included (issue #240).

    assess-city calls _run_city_channels, so it fans out the moment prod raises
    the knob — intended, and safe for the same reason the nightly batch is: the
    affinity rule is in the scheduler, not in the caller. Pinned here because
    ASSESS_CHANNELS is exactly the shape that would hide a missing rule by luck
    — gsv_streets and mapillary_streets share Overpass, mapillary and
    mapillary_streets share the tile CDN, and a canonical-order sequential run
    happens to get that right for the wrong reason.
    """
    import threading
    from collections import Counter
    from datetime import date

    lock = threading.Lock()
    events = []
    pair = threading.Barrier(2)

    def fake_run(cfg, city, today, provider="gsv", **kwargs):
        with lock:
            events.append(("start", provider, len(events) + 1))
        try:
            # The two host-disjoint channels must be able to meet here; the
            # third shares a host with each of them and must not.
            if provider != "mapillary_streets":
                pair.wait(10.0)
            return True
        finally:
            with lock:
                events.append(("end", provider, len(events) + 1))

    monkeypatch.setattr(_sched, "_run_one_city", fake_run)
    cfg = _cfg(tmp_path, max_concurrent_channels=4)
    city = db.resolve_city(conn, db.register_city(conn, **_CITY_ROW))

    attempted, succeeded, skipped = _sched._run_city_channels(
        cfg,
        conn,
        city,
        date(2026, 8, 17),
        list(ASSESS_CHANNELS),
        blocked_hosts=_sched.HostBreaker(),
        busy_hosts=Counter(),
        deferred_channels=Counter(),
        deferred_host_budget=Counter(),
        rejected_argv=_sched.ArgvRejections(),
        deadline_deferred=Counter(),
        batch_deadline=None,
        stop_requested=None,
        record_failures=False,
    )

    assert (attempted, succeeded, skipped) == (3, 3, 0)
    seq = {(kind, provider): n for kind, provider, n in events}
    assert seq[("start", "mapillary_streets")] > seq[("end", "gsv_streets")]
    assert seq[("start", "mapillary_streets")] > seq[("end", "mapillary")]


# --------------------------------------------------------------------------
# opt-in enrolment of a new city (issue #374)
# --------------------------------------------------------------------------

OPT_IN = ("kartaview", "kartaview_streets", "panoramax", "panoramax_streets")


def _members(conn, city_id=CITY_ID):
    """``{channel: member}`` for every schedule_state row this city has."""
    return {
        r["provider"]: r["member"]
        for r in conn.execute(
            "SELECT provider, member FROM schedule_state WHERE city_id = ?", (city_id,)
        )
    }


def _screen(monkeypatch, upper_bound=None, *, error=None):
    """Answer the one-city screen's fetch primitive; returns the URL log."""
    from streetscape_metadata_tracker import panoramax_screen

    calls = []
    monkeypatch.setattr(
        panoramax_screen,
        "_fetch_tile",
        panoramax_screen_fetch(upper_bound, error=error, calls=calls),
    )
    return calls


def _newport(conn):
    return db.resolve_city(conn, db.register_city(conn, **_CITY_ROW))


def _enrol(cfg, conn, city, *, over=False, dry_run=False, today=TODAY):
    return _sched.enroll_opt_in_channels(
        cfg, conn, city, enroll_kartaview_over_threshold=over, dry_run=dry_run, today=today
    )


def test_a_new_city_ends_enrolled_on_all_four_opt_in_channels(conn, monkeypatch, tmp_path, capsys):
    """
    The #374 happy path: a positive screen and a KartaView estimate under the
    ceiling (Newport's is 16, the #225 median) enrol all four, and the screen's
    tiles are charged to the ledger on the ASSESSMENT's run date.
    """
    _stub_collection(monkeypatch, conn)
    calls = _screen(monkeypatch, 42)

    assert _assess(tmp_path) == 0

    members = _members(conn)
    assert {c: members.get(c) for c in OPT_IN} == dict.fromkeys(OPT_IN, 1)
    # Newport's grown bbox straddles the z6 seam at -84.375°, so two tiles —
    # both charged, and on `today` (the command's pinned date, never the wall
    # clock), since that is the date the rest of this run's spend lands on.
    assert len(calls) == 2
    assert db.get_api_usage(conn, TODAY, "panoramax") == 2
    out = capsys.readouterr().out
    assert "arrive with this city's first nightly run" in out


def test_a_zero_screen_enrols_neither_panoramax_channel(conn, tmp_path, monkeypatch):
    _screen(monkeypatch, 0)
    report = _enrol(_cfg(tmp_path), conn, _newport(conn))

    members = _members(conn)
    assert members.get("panoramax") is None and members.get("panoramax_streets") is None
    assert report.decision("panoramax").decision == "skipped"
    assert report.decision("panoramax_streets").decision == "skipped"
    # KartaView is decided independently, so a zero here costs it nothing.
    assert members["kartaview"] == members["kartaview_streets"] == 1


def _serve_429(monkeypatch):
    """The REAL fetch primitive, answered 429 by a fake session (a per-IP refusal)."""
    import aiohttp

    from streetscape_metadata_tracker import download_panoramax, panoramax_screen
    from tests.test_panoramax_screen import _AsyncCM, _FakeResponse

    class _Always429:
        def get(self, url, **kwargs):
            class _Ctx:
                async def __aenter__(self):
                    return _FakeResponse(429)

                async def __aexit__(self, *exc):
                    return False

            return _Ctx()

    monkeypatch.setattr(panoramax_screen, "_fetch_tile", download_panoramax._fetch_tile)
    monkeypatch.setattr(aiohttp, "ClientSession", lambda **kw: _AsyncCM(_Always429()))


@pytest.mark.parametrize("failure", ["http_429", "connection_error"])
def test_a_failed_screen_enrols_neither_and_says_screen_failed(
    conn, tmp_path, monkeypatch, failure
):
    """
    Never enrol on "unknown" — and never report a failure as a zero, which is
    what the DISTINCT decision below pins: a zero is conclusive, a failure is not.
    """
    import aiohttp

    if failure == "http_429":
        _serve_429(monkeypatch)
    else:
        _screen(monkeypatch, error=aiohttp.ClientError("connection reset"))

    report = _enrol(_cfg(tmp_path), conn, _newport(conn))

    members = _members(conn)
    assert members.get("panoramax") is None and members.get("panoramax_streets") is None
    assert report.decision("panoramax").decision == "screen_failed"
    assert report.decision("panoramax_streets").decision == "screen_failed"
    assert conn.execute("SELECT COUNT(*) FROM provider_screen").fetchone()[0] == 0
    assert "screen_failed" in report.render()
    # A failure is terminal for this call, so the line names its remedy.
    reason = report.decision("panoramax").reason
    assert f"enroll-city {CITY_ID} --channel panoramax`" in reason
    assert f"enroll-city {CITY_ID} --channel panoramax_streets`" in reason
    # The request that got the refusal (or the fault) was SENT, so it is charged:
    # a ledger that forgot refused attempts lets the next process walk back in.
    assert db.get_api_usage(conn, TODAY, "panoramax") == 1


def test_an_unforeseen_failure_after_the_screen_returns_still_charges_what_it_sent(
    conn, tmp_path, monkeypatch
):
    """
    The catch-all arm: the screen RETURNED (its requests were sent) but reading
    the result failed in a way nobody typed. The decision is still
    `screen_failed`, and the tiles sent are still charged, from the result's
    own count — the exception carries none.
    """
    from streetscape_metadata_tracker import panoramax_screen

    calls = _screen(monkeypatch, 42)
    monkeypatch.setattr(panoramax_screen, "screen_row", lambda *a, **k: {"unexpected": "shape"})

    report = _enrol(_cfg(tmp_path), conn, _newport(conn))

    assert report.decision("panoramax").decision == "screen_failed"
    assert "KeyError" in report.decision("panoramax").reason
    assert len(calls) == 2
    assert db.get_api_usage(conn, TODAY, "panoramax") == 2


def test_a_busy_panoramax_lock_is_reported_and_not_retried(conn, tmp_path, monkeypatch):
    from filelock import FileLock

    from streetscape_metadata_tracker import host_lock
    from streetscape_metadata_tracker.download_common import HOST_PANORAMAX

    calls = _screen(monkeypatch, 42)
    other = FileLock(host_lock.lock_path(HOST_PANORAMAX))
    other.acquire()
    try:
        report = _enrol(_cfg(tmp_path), conn, _newport(conn))
    finally:
        other.release()

    assert calls == [], "a busy lock must not be waited out or retried"
    assert report.decision("panoramax").decision == "screen_failed"
    # Said as LOCAL contention, never as a refusal: a busy lock is our own
    # process, and reading it as a per-IP block would send an operator to the
    # provider-access log for nothing. (Prefix, not substring: the lock path in
    # the message sits under this test's tmp dir, whose name contains "busy".)
    assert report.decision("panoramax").reason.startswith("not enrolled — host lock busy")
    assert "refused this host" not in report.decision("panoramax").reason
    assert _members(conn).get("panoramax") is None


@pytest.mark.parametrize(
    ("estimate", "over", "expected"),
    [
        (1_000, False, 1),  # AT the ceiling enrols: `<=`, not `<`
        (1_001, False, None),  # one over does not
        (1_001, True, 1),  # ... unless --enroll-kartaview accepts it
    ],
)
def test_the_kartaview_ceiling_is_inclusive_and_only_the_flag_crosses_it(
    conn, tmp_path, monkeypatch, estimate, over, expected
):
    monkeypatch.setattr(_sched, "estimate_kartaview_requests", lambda conn, city: estimate)
    report = _enrol(_cfg(tmp_path), conn, _newport(conn), over=over)

    members = _members(conn)
    assert members.get("kartaview") == expected
    assert members.get("kartaview_streets") == expected
    if expected is None:
        assert report.decision("kartaview").decision == "needs_flag"
        assert "--enroll-kartaview" in report.decision("kartaview").reason


@pytest.mark.parametrize("enroll_kartaview", [False, True])
def test_yes_alone_never_accepts_an_over_ceiling_kartaview_estimate(
    conn, monkeypatch, tmp_path, enroll_kartaview
):
    _stub_collection(monkeypatch, conn)
    monkeypatch.setattr(_sched, "estimate_kartaview_requests", lambda conn, city: 5_000)

    _assess(tmp_path, assume_yes=True, enroll_kartaview=enroll_kartaview)

    members = _members(conn)
    want = 1 if enroll_kartaview else None
    assert (members.get("kartaview"), members.get("kartaview_streets")) == (want, want)


def test_no_opt_in_writes_no_opt_in_row_and_screens_nothing(conn, monkeypatch, tmp_path):
    _stub_collection(monkeypatch, conn)
    calls = _screen(monkeypatch, 42)

    _assess(tmp_path, opt_in=False)

    # The row SET, not `member is None`: a row written with a NULL member would
    # satisfy the weaker check while still being a write this flag forbids.
    assert set(_members(conn)) == set(ASSESS_CHANNELS)
    assert calls == []
    assert conn.execute("SELECT COUNT(*) FROM provider_screen").fetchone()[0] == 0


def test_re_assessing_a_city_with_only_default_channel_history_keeps_an_explicit_pair(
    conn, monkeypatch, tmp_path
):
    """
    Since round 1 of the #374 review "new" is keyed on the OPT-IN channels: a
    city with gsv history but no opt-in attempt, run or walk IS enrolled —
    behind the same gates — while an operator's explicit `--remove` on one pair
    survives as `already_set`, pair-wise.
    """
    _stub_collection(monkeypatch, conn)
    _assess(tmp_path, opt_in=False)  # default-channel history only
    db.record_attempt(conn, CITY_ID, success=True, provider="gsv")
    db.set_channel_membership(conn, CITY_ID, "panoramax", False, cycle_days=90)
    _screen(monkeypatch, 42)

    _assess(tmp_path)

    members = _members(conn)
    assert (members["panoramax"], members.get("panoramax_streets")) == (0, None)
    assert (members["kartaview"], members["kartaview_streets"]) == (1, 1)


def _opt_in_touch_attempt(conn):
    db.record_attempt(conn, CITY_ID, success=False, provider="kartaview", error="boom")


def _opt_in_touch_run(conn):
    db.register_run(
        conn,
        city_id=CITY_ID,
        run_date=TODAY,
        csv_filename=f"{CITY_ID}_kartaview_{TODAY}.csv.gz",
        provider="kartaview",
    )


def _opt_in_touch_walk(conn):
    _register_walk(conn, "panoramax")


@pytest.mark.parametrize(
    "touch",
    [_opt_in_touch_attempt, _opt_in_touch_run, _opt_in_touch_walk],
    ids=["opt_in_attempt", "opt_in_run", "opt_in_walk"],
)
def test_a_city_touched_on_any_opt_in_channel_is_not_re_enrolled(
    conn, monkeypatch, tmp_path, touch
):
    """Each of the three opt-in history signals, on its own, is enough to leave the city alone."""
    _stub_collection(monkeypatch, conn)
    _assess(tmp_path, opt_in=False)
    touch(conn)
    before = {c: _members(conn).get(c) for c in OPT_IN}
    calls = _screen(monkeypatch, 42)

    _assess(tmp_path)

    assert {c: _members(conn).get(c) for c in OPT_IN} == before
    assert calls == []


def test_a_nightly_gsv_attempt_between_estimate_and_yes_still_enrols(conn, monkeypatch, tmp_path):
    """
    The gap round 1 of the review found: a never-collected enabled city leads
    gsv's queue, so a 02:00 run between `--estimate` and `--yes` stamps
    `last_attempt_at`. An any-channel history gate then read the `--yes` run as
    a re-assessment — the Montréal/Ottawa failure. Before #431 `--estimate`
    alone opened the gap (it registered the city enabled); since then the
    documented order `--estimate` -> `enable-city` -> a night -> `--yes` still
    reaches it, so the keying must survive.
    """
    _stub_collection(monkeypatch, conn)
    _screen(monkeypatch, 42)

    _assess(tmp_path, estimate_only=True)
    db.set_city_enabled(conn, CITY_ID, True)
    db.record_attempt(conn, CITY_ID, success=True, provider="gsv")
    _assess(tmp_path)

    members = _members(conn)
    assert {c: members.get(c) for c in OPT_IN} == dict.fromkeys(OPT_IN, 1)


def test_declining_the_confirmation_writes_no_membership_and_screens_nothing(
    conn, monkeypatch, tmp_path
):
    monkeypatch.setattr(_sched.db, "connect", lambda path: conn)
    monkeypatch.setattr(_sched, "_run_one_city", lambda *a, **k: pytest.fail("collected!"))
    monkeypatch.setattr(_sched.sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr("builtins.input", lambda prompt="": "n")
    calls = _screen(monkeypatch, 42)

    assert _sched.cmd_assess_city(_cfg(tmp_path), QUERY, today=TODAY, assume_yes=False) == 0

    assert not any(c in _members(conn) for c in OPT_IN)
    assert calls == []
    assert db.get_api_usage(conn, TODAY, "panoramax") == 0


def test_a_one_city_screen_publishes_no_screen_series_point(conn, tmp_path, monkeypatch):
    """
    `provider_screen` is a series of WHOLE-CATALOG observations, grouped by
    date and published with `latest_screen_date`. A one-city row would publish
    a point claiming one city was screened that day, so the enrolment decides
    from the in-memory result and writes no row.
    """
    city = _newport(conn)
    db.record_provider_screen(
        conn,
        provider="panoramax",
        screen_date=date(2026, 8, 10),
        rows=[
            dict(
                city_id=city.city_id,
                cells=1,
                pictures_upper_bound=5,
                pictures_360_upper_bound=5,
                pictures_flat_upper_bound=0,
            )
        ],
    )

    def snapshot():
        return (
            [tuple(r) for r in db.get_provider_screen_series(conn, "panoramax")],
            [tuple(r) for r in db.get_latest_provider_screen(conn, "panoramax")],
        )

    before = snapshot()
    _screen(monkeypatch, 42)

    report = _enrol(_cfg(tmp_path), conn, city, today=TODAY)

    assert report.decision("panoramax").decision == "enrolled"
    assert snapshot() == before


def test_a_pair_write_is_all_or_nothing(conn, tmp_path, monkeypatch):
    """
    Both halves of a pair land in ONE transaction: half a pair would read as
    `already_set` forever after, so a failure on the second write must leave
    neither. Injected with a trigger that aborts the walk channel's insert.
    """
    import sqlite3

    _screen(monkeypatch, 0)  # Panoramax skipped: only the KartaView pair writes
    city = _newport(conn)
    conn.execute(
        """CREATE TEMP TRIGGER abort_walk BEFORE INSERT ON schedule_state
           WHEN NEW.provider = 'kartaview_streets'
           BEGIN SELECT RAISE(ABORT, 'injected failure on the second write'); END"""
    )

    with pytest.raises(sqlite3.DatabaseError, match="injected"):
        _enrol(_cfg(tmp_path), conn, city)

    conn.execute("DROP TRIGGER abort_walk")
    members = _members(conn)
    assert members.get("kartaview") is None
    assert members.get("kartaview_streets") is None


def test_an_estimate_then_a_collection_still_enrols_the_new_city(conn, monkeypatch, tmp_path):
    """
    `--estimate` registers the city, so the follow-up run is not "newly
    registered" — yet it is the documented order, and must not read as a
    re-assessment that silently skips enrolment.
    """
    _stub_collection(monkeypatch, conn)
    calls = _screen(monkeypatch, 42)

    _assess(tmp_path, estimate_only=True)
    assert calls == [], "--estimate issues no provider request, the screen included"
    assert not any(c in _members(conn) for c in OPT_IN), "and writes no membership"

    _assess(tmp_path)

    members = _members(conn)
    assert {c: members.get(c) for c in OPT_IN} == dict.fromkeys(OPT_IN, 1)


def test_the_estimate_previews_the_enrolment_decisions(conn, monkeypatch, tmp_path, capsys):
    _stub_collection(monkeypatch, conn)
    _screen(monkeypatch, 42)

    _assess(tmp_path, estimate_only=True)

    out = capsys.readouterr().out
    assert "DRY RUN, nothing written" in out
    kartaview_line = next(
        line for line in out.splitlines() if line.strip().startswith("kartaview ")
    )
    assert "would enrol" in kartaview_line
    assert "pending_screen" in out


def test_a_pair_is_enrolled_whole_even_when_the_walk_is_not_configured(conn, tmp_path, monkeypatch):
    """
    The walk rides the grid's census for 0 (#290), so a provider's two channels
    move together — including when only the grid channel has a config block,
    since enrolment before configuration is supported on purpose.
    """
    _screen(monkeypatch, 42)
    cfg = _cfg(tmp_path)
    cfg.providers["kartaview"] = ProviderConfig(enabled=True)
    cfg.providers["panoramax"] = ProviderConfig(enabled=True)

    report = _enrol(cfg, conn, _newport(conn))

    members = _members(conn)
    assert {c: members.get(c) for c in OPT_IN} == dict.fromkeys(OPT_IN, 1)
    notes = "\n".join(report.notes)
    assert "[providers.kartaview_streets] is not enabled" in notes
    assert "[providers.panoramax_streets] is not enabled" in notes
    assert "[providers.kartaview] is not" not in notes


def test_one_explicit_half_leaves_the_whole_pair_alone(conn, tmp_path, monkeypatch):
    """An explicit value on EITHER channel of a pair: the other is never enrolled alone."""
    _screen(monkeypatch, 42)
    city = _newport(conn)
    db.set_channel_membership(conn, city.city_id, "kartaview", False, cycle_days=90)

    report = _enrol(_cfg(tmp_path), conn, city)

    members = _members(conn)
    assert members["kartaview"] == 0
    assert members.get("kartaview_streets") is None
    assert report.decision("kartaview_streets").decision == "already_set"
    assert members["panoramax"] == members["panoramax_streets"] == 1


def test_a_dry_run_writes_nothing_and_issues_no_request(conn, tmp_path, monkeypatch):
    calls = _screen(monkeypatch, 42)
    city = _newport(conn)

    report = _enrol(_cfg(tmp_path), conn, city, dry_run=True)

    assert _members(conn) == {}
    assert calls == []
    assert report.decision("kartaview").decision == "enrolled"  # what it WOULD do
    assert report.decision("panoramax").decision == "pending_screen"


def test_every_opt_in_provider_has_an_enrolment_gate():
    """A new opt-in provider must bring its own gate, never be silently skipped."""
    pairs = _sched.opt_in_pairs()
    assert set(pairs) == set(_sched._OPT_IN_GATES)
    assert {c for pair in pairs.values() for c in pair} == {
        c for c in _sched.CHANNEL_DEFAULT_MEMBERSHIP if _sched.is_opt_in_channel(c)
    }
    assert set(OPT_IN) == {c for pair in pairs.values() for c in pair}
    assert all(d in _sched.OPT_IN_DECISIONS for d in ("enrolled", "screen_failed"))


def test_assess_city_parses_the_opt_in_flags():
    args = build_parser().parse_args(["assess-city", "--no-opt-in", "--enroll-kartaview", QUERY])
    assert args.no_opt_in is True
    assert args.enroll_kartaview is True
    defaults = build_parser().parse_args(["assess-city", QUERY])
    assert defaults.no_opt_in is False and defaults.enroll_kartaview is False


def test_assess_city_never_defers_for_a_deadline(conn, monkeypatch, tmp_path):
    """assess-city passes batch_deadline=None, so the #373 deferral can never
    fire here: even with every channel's derived need past any night, each
    listed channel -- gsv_streets, the non-resumable one, included -- is
    collected."""
    monkeypatch.setattr(_sched, "city_timeout_estimate_seconds", lambda *a, **k: 10**9)
    ran = _stub_collection(monkeypatch, conn)

    rc = _assess(tmp_path)

    assert rc == 0
    assert sorted(provider for _cid, provider in ran) == sorted(ASSESS_CHANNELS)


# --------------------------------------------------------------------------
# the GSV-key guard (issue #412)
# --------------------------------------------------------------------------

_NIGHTLY = (
    "/v/bin/python -m streetscape_metadata_tracker.scheduler --config "
    "config/scheduler.makelab1.toml run-due"
)


def _nightly_in_ps(monkeypatch):
    asked = []
    found = [
        _sched.RunDueProcess(pid=4242, args=_NIGHTLY, argv=tuple(_sched._split_ps_args(_NIGHTLY)))
    ]
    monkeypatch.setattr(_sched, "_scan_run_due_processes", lambda: asked.append(1) or found)
    return asked


def test_assess_city_refuses_gsv_streets_beside_a_run_due_on_that_key(
    conn, monkeypatch, tmp_path, caplog
):
    """assess-city spends the gsv_streets key with no rate override, exactly
    like a hand run-due, so it is refused the same way and before anything is
    registered."""
    _nightly_in_ps(monkeypatch)
    rc, connected = _refusal(monkeypatch, tmp_path, conn)
    assert rc == _sched.USAGE_EXIT_CODE
    assert connected == []
    assert "pid 4242 (shared key: gsv_streets)" in caplog.text
    assert "may already be past its GSV channels" in caplog.text
    assert "re-run with --force" in caplog.text
    # assess-city is never the nightly, so it has no identification to explain.
    assert "nightly unit" not in caplog.text


@pytest.mark.parametrize(
    "kwargs",
    [
        pytest.param({"estimate_only": True}, id="estimate-spends-nothing"),
        pytest.param({"force": True}, id="force"),
        pytest.param({"requested_providers": ["mapillary_streets"]}, id="no-gsv-key"),
    ],
)
def test_assess_city_passes_the_guard_when_it_spends_no_shared_key(
    conn, monkeypatch, tmp_path, kwargs
):
    """Past the guard means the catalog is opened; none of these read `ps`."""
    asked = _nightly_in_ps(monkeypatch)

    class _PastTheGuard(Exception):
        pass

    def connect(path):
        raise _PastTheGuard

    monkeypatch.setattr(_sched.db, "connect", connect)
    with pytest.raises(_PastTheGuard):
        _sched.cmd_assess_city(_cfg(tmp_path), QUERY, today=TODAY, assume_yes=True, **kwargs)
    assert asked == []


# --------------------------------------------------------------------------
# a new city is registered DISABLED (issue #431)
# --------------------------------------------------------------------------


def test_assessing_an_already_disabled_city_prints_the_enable_hint_once(
    conn, monkeypatch, tmp_path, capsys
):
    """
    Keyed on `not city.enabled`, not `newly_registered`: a frame city
    (register_frame.py) or an earlier --estimate also left the city disabled,
    and the second run on an unvetted city must say so too.
    """
    db.register_city(conn, **_CITY_ROW, enabled=False)
    _stub_collection(monkeypatch, conn)

    _assess(tmp_path)

    out = capsys.readouterr().out
    assert "Already registered" in out
    assert out.count(cr.enable_hint(CITY_ID)) == 1
    assert "this city is DISABLED" in out


def test_assessing_an_enabled_city_prints_no_enable_hint(conn, monkeypatch, tmp_path, capsys):
    """Every #431 sentence is conditional on the city being disabled."""
    db.register_city(conn, **_CITY_ROW)  # the default: enabled
    _stub_collection(monkeypatch, conn)
    _screen(monkeypatch, 42)

    _assess(tmp_path)

    out = capsys.readouterr().out
    assert "Already registered" in out
    assert cr.enable_hint(CITY_ID) not in out
    assert "this city is DISABLED" not in out
    # The opt-in pairs were enrolled, so the timing sentence was printed -- in
    # its enabled form.
    assert "arrive with this city's first nightly run," in out
    assert "first nightly run after" not in out


def test_enable_city_after_an_assessment_reports_the_pairs_already_set(
    conn, monkeypatch, tmp_path, capsys
):
    """
    `assess-city --yes` enrols the opt-in pairs on a city it left disabled
    (#374, #431); `enable-city` afterwards must compose with that -- report the
    pairs `already_set`, screen nothing a second time, and flip `enabled`.
    """
    _stub_collection(monkeypatch, conn)
    calls = _screen(monkeypatch, 42)
    _assess(tmp_path)
    out = capsys.readouterr().out
    assert "first nightly run after `enable-city" in out
    n = len(calls)
    assert n > 0  # the assessment's own screen, so the equality below is not vacuous

    rc = _sched.cmd_enable_city(_cfg(tmp_path), CITY_ID)

    out = capsys.readouterr().out
    assert rc == 0
    assert out.count("already_set") >= len(OPT_IN)
    assert db.resolve_city(conn, CITY_ID).enabled is True
    assert len(calls) == n
    assert {c: _members(conn).get(c) for c in OPT_IN} == dict.fromkeys(OPT_IN, 1)
