"""The Overpass fetch's retry window (issue #357).

Before #357 a road walk gave up on a refusing Overpass after tenacity's three
attempts -- ~12 s of backoff, or ~3 min when osmnx's own 60 s status-fallback
pause sat in front of each attempt -- while the refusals it met on 2026-09-21
cleared on a minutes scale. Each give-up exits 76 and latches the night-level
breaker, so a flap became a stranded night.

What these tests pin, and why each is a measurement rather than a restatement:

* the schedule itself (30, 60, 120, 240 s; upward-only jitter), per failure
  shape -- with and without osmnx's 60 s pause in front of each attempt;
* the two stops, each binding on its own: the attempt cap and the wall-clock
  window, and the invariant the deadline depends on (no attempt STARTS past the
  window, so one full attempt always fits inside the 900 s deadline);
* the usage policy's 30 s floor between a failure and the next request, which
  no configuration can lower;
* that a settled answer (406, a ban page, an empty bbox) is asked once;
* that exhaustion is still a HostBlockedError, and still exit 76 from the
  child;
* and the config hop end to end -- TOML -> SchedulerConfig -> argv -> collect
  -> fetch -- at NON-default values, since a hop that hardcoded the default
  would pass any test that only checked the default.

No real sleeping and no network: a fake clock is installed on the retry loop,
and `ox.graph_from_bbox` is replaced by an in-memory stand-in that can advance
that clock to model osmnx's pre-request pause.
"""

from __future__ import annotations

import inspect
import itertools
import logging
import math
from dataclasses import replace

import networkx as nx
import pytest
import requests
from osmnx._errors import InsufficientResponseError, ResponseStatusCodeError

from streetscape_metadata_tracker import scheduler
from streetscape_metadata_tracker.download_common import (
    HOST_EXIT_CODES,
    HOST_OVERPASS,
    DownloadError,
    HostBlockedError,
)
from streetscape_metadata_tracker.overpass_retry import (
    OVERPASS_CHILD_STARTUP_RESERVE_S,
    OVERPASS_FINAL_ATTEMPT_RESERVE_S,
    OVERPASS_MIN_RETRY_WAIT_S,
    OVERPASS_RETRY_WINDOW_CEILING_S,
    OverpassRetryPolicy,
    RetriesExhausted,
    call_with_retry,
    overpass_retry_argv,
    policy_for_child_timeout,
)
from streetscape_street_analyzer import collect
from streetscape_street_analyzer import download_street_network as dsn

# Captured at IMPORT, which happens before conftest's autouse
# `_no_overpass_retry_sleep` replaces the sleep for every test -- the same
# technique `tests/test_overpass.py` uses for the /status probe. Read after the
# fixture has run, `dsn._retry_sleep` is the suite's no-op and pins nothing.
_PRODUCTION_SLEEP = dsn._retry_sleep
_PRODUCTION_CLOCK = dsn._retry_clock
_PRODUCTION_RANDOM = dsn._retry_random

REFUSED = requests.exceptions.ConnectionError("[Errno 111] Connection refused")
TIMED_OUT = requests.exceptions.ReadTimeout("read timed out")
RETRYABLE = (requests.exceptions.ConnectionError, requests.exceptions.Timeout)

# osmnx's `_get_overpass_pause` falls back to this when /status is unreachable,
# i.e. on every attempt against a host refusing TCP outright.
OSMNX_STATUS_FALLBACK_PAUSE_S = 60.0

# A draw just under 1: the largest jitter `random.random()` can produce.
U_MAX = math.nextafter(1.0, 0.0)


class FakeClock:
    """A monotonic clock that only moves when something sleeps or works."""

    def __init__(self):
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class Host:
    """An Overpass stand-in: fails the first ``failures`` attempts, then serves.

    Each attempt costs ``cost`` seconds of the fake clock before it answers,
    which is how osmnx's 60 s pre-request pause is modelled. Records when each
    attempt STARTED, so gaps and window bounds are read from what happened.
    """

    def __init__(self, clock: FakeClock, *, failures=math.inf, cost=0.0, error=REFUSED):
        self.clock = clock
        self.failures = failures
        self.cost = cost
        self.error = error
        self.starts: list[float] = []

    def __call__(self):
        self.starts.append(self.clock.now)
        self.clock.now += self.cost
        if len(self.starts) <= self.failures:
            raise self.error() if callable(self.error) else self.error
        return "graph"


def _run(host: Host, clock: FakeClock, policy=None, u=0.0):
    return call_with_retry(
        host,
        policy or OverpassRetryPolicy(),
        retryable=RETRYABLE,
        sleep=clock.sleep,
        clock=clock,
        rand=lambda: u,
    )


# ---------------------------------------------------------------------------
# The production bindings
# ---------------------------------------------------------------------------


def test_production_really_sleeps_on_a_real_clock_with_real_jitter():
    """
    The whole schedule below is measured through injected doubles, so nothing
    else in this file can see what the module is actually bound to -- and with
    these unpinned, setting `_retry_sleep = lambda s: None` in production left
    the entire suite green while shipping five attempts back to back at a
    refusing Overpass, i.e. the 30 s usage-policy floor this PR calls enforced,
    unenforced.
    """
    import random
    import time

    assert _PRODUCTION_SLEEP is time.sleep
    assert _PRODUCTION_CLOCK is time.monotonic
    assert _PRODUCTION_RANDOM is random.random
    # And the loop reads them at CALL time (so the fixtures above can swap
    # them), rather than having captured them into a default argument.
    signature = inspect.signature(call_with_retry)
    for name in ("sleep", "clock", "rand"):
        assert signature.parameters[name].default is inspect.Parameter.empty


# ---------------------------------------------------------------------------
# The schedule
# ---------------------------------------------------------------------------


def test_the_default_schedule_doubles_from_the_policy_floor_and_caps():
    policy = OverpassRetryPolicy()
    assert [policy.nominal_wait_s(k) for k in range(1, 7)] == [30, 60, 120, 240, 240, 240]
    assert policy.initial_wait_s == OVERPASS_MIN_RETRY_WAIT_S


@pytest.mark.parametrize("u", [0.0, 0.1, 0.5, 0.9, U_MAX])
@pytest.mark.parametrize("failures", [1, 2, 3, 4, 5, 9])
def test_jitter_only_ever_lengthens_a_wait_and_by_at_most_the_fraction(u, failures):
    """Upward-only jitter is what keeps the 30 s floor a floor. A symmetric
    `1 + j*(2u - 1)` would put the first wait at 22.5 s -- under the usage
    policy's pause after a refusal."""
    policy = OverpassRetryPolicy(jitter=0.25)
    nominal = policy.nominal_wait_s(failures)
    wait = policy.wait_s(failures, u)
    assert nominal <= wait <= nominal * 1.25
    assert wait >= OVERPASS_MIN_RETRY_WAIT_S
    assert wait == pytest.approx(nominal * (1 + 0.25 * u))


def test_jitter_zero_is_exactly_the_nominal_schedule():
    policy = OverpassRetryPolicy(jitter=0.0)
    assert [policy.wait_s(k, U_MAX) for k in (1, 2, 3)] == [30, 60, 120]


# ---------------------------------------------------------------------------
# Per failure shape: attempts, waits and total time, exactly
# ---------------------------------------------------------------------------


def test_a_refused_host_whose_status_is_refused_too_gets_four_attempts_over_450_s():
    """osmnx sits through its 60 s fallback pause before every POST, so the
    window (not the attempt cap) is what binds: the 240 s wait after the fourth
    attempt would end at 690 s, past the 600 s window."""
    clock = FakeClock()
    host = Host(clock, cost=OSMNX_STATUS_FALLBACK_PAUSE_S)
    with pytest.raises(RetriesExhausted) as excinfo:
        _run(host, clock)
    assert host.starts == [0, 90, 210, 390]
    assert clock.sleeps == [30, 60, 120]
    assert excinfo.value.attempts == 4
    assert excinfo.value.elapsed_s == 450


def test_a_refused_interpreter_behind_a_serving_status_gets_five_attempts_over_450_s():
    """The multi-backend shape of 2026-09-21: no osmnx pause, so the attempt cap
    binds -- and the last wait (240 s) still ends inside the window."""
    clock = FakeClock()
    host = Host(clock, cost=0.0)
    with pytest.raises(RetriesExhausted) as excinfo:
        _run(host, clock)
    assert host.starts == [0, 30, 90, 210, 450]
    assert clock.sleeps == [30, 60, 120, 240]
    assert excinfo.value.attempts == 5
    assert excinfo.value.elapsed_s == 450


def test_maximum_jitter_stretches_the_waits_and_still_fits_the_window():
    clock = FakeClock()
    host = Host(clock, cost=0.0)
    with pytest.raises(RetriesExhausted):
        _run(host, clock, u=U_MAX)
    assert len(host.starts) == 5
    assert clock.sleeps == pytest.approx([37.5, 75, 150, 300])
    assert host.starts[-1] == pytest.approx(562.5)


def test_a_timeout_waits_on_the_same_schedule_as_a_refusal():
    """No classification by errno (#341, restated in #357): a bare ECONNREFUSED
    is the signature of both a flap and the 2026-08-14 ban, so every retryable
    fault waits alike. Here a Timeout that costs a full 180 s request timeout."""
    refused_clock, timeout_clock = FakeClock(), FakeClock()
    with pytest.raises(RetriesExhausted):
        _run(Host(refused_clock, cost=180, error=REFUSED), refused_clock)
    with pytest.raises(RetriesExhausted):
        _run(Host(timeout_clock, cost=180, error=TIMED_OUT), timeout_clock)
    assert timeout_clock.sleeps == refused_clock.sleeps == [30, 60]


@pytest.mark.parametrize(
    ("failures", "cost"),
    [(1, 60.0), (2, 60.0), (3, 60.0), (1, 0.0), (4, 0.0)],
)
def test_success_after_n_transient_failures(failures, cost):
    """Up to three failures ride out under osmnx's 60 s pause (the window then
    binds, per the shape test above); four are survivable only without it."""
    clock = FakeClock()
    host = Host(clock, failures=failures, cost=cost)
    assert _run(host, clock) == "graph"
    assert len(host.starts) == failures + 1
    assert clock.sleeps == [30, 60, 120, 240][:failures]


def test_a_flap_the_old_policy_escalated_is_now_absorbed():
    """The point of #357, as a number: a refusal lasting 5 minutes. The old
    stack's last attempt started ~12 s (no pause) or ~130 s (60 s pause) in; the
    default policy's fifth attempt, at 450 s, lands after the refusal cleared."""
    clock = FakeClock()

    def refusing_for_five_minutes():
        host.starts.append(clock.now)
        if clock.now < 300:
            raise REFUSED
        return "graph"

    host = Host(clock)
    assert _run(refusing_for_five_minutes, clock) == "graph"
    assert host.starts[-1] == 450


# ---------------------------------------------------------------------------
# The two stops, each binding on its own
# ---------------------------------------------------------------------------


def test_the_attempt_cap_binds_when_the_window_does_not():
    clock = FakeClock()
    host = Host(clock)
    with pytest.raises(RetriesExhausted) as excinfo:
        _run(host, clock, OverpassRetryPolicy(max_attempts=3))
    assert len(host.starts) == 3
    assert clock.sleeps == [30, 60]
    assert excinfo.value.attempts == 3


def test_the_window_binds_when_the_attempt_cap_does_not():
    clock = FakeClock()
    host = Host(clock)
    with pytest.raises(RetriesExhausted):
        _run(host, clock, OverpassRetryPolicy(max_attempts=50, window_s=100))
    # 0, 30, 90; the next wait (120 s) would end at 210 > 100.
    assert host.starts == [0, 30, 90]


def test_one_attempt_means_no_retry_and_no_wait():
    clock = FakeClock()
    host = Host(clock)
    with pytest.raises(RetriesExhausted) as excinfo:
        _run(host, clock, OverpassRetryPolicy(max_attempts=1))
    assert host.starts == [0]
    assert clock.sleeps == []
    assert excinfo.value.attempts == 1


@pytest.mark.parametrize("cost", [0, 1, 29, 30, 60, 179, 180, 250])
@pytest.mark.parametrize("u", [0.0, 0.5, U_MAX])
@pytest.mark.parametrize(
    "policy",
    [
        OverpassRetryPolicy(),
        OverpassRetryPolicy(max_attempts=20, jitter=1.0),
        OverpassRetryPolicy(max_attempts=20, initial_wait_s=30, max_wait_s=30),
        OverpassRetryPolicy(max_attempts=20, window_s=OVERPASS_RETRY_WINDOW_CEILING_S),
    ],
)
def test_no_attempt_ever_starts_past_the_window_or_closer_than_the_floor(cost, u, policy):
    """The invariant the 900 s deadline is derived from, swept over attempt
    costs, jitter draws and policies whose attempt caps cannot bind first.
    Reachable: with the window check deleted, `max_attempts=20` starts
    attempts at 690 s and beyond."""
    clock = FakeClock()
    host = Host(clock, cost=cost)
    with pytest.raises(RetriesExhausted):
        _run(host, clock, policy, u=u)
    assert host.starts[-1] <= policy.window_s
    # Implied by the line above given how OVERPASS_DEADLINE_S is derived, and
    # kept as the standing statement of what that derivation is FOR: it fails
    # if either term of the deadline moves without the other.
    assert (
        host.starts[-1] + dsn.OVERPASS_TIMEOUT_S + dsn.OVERPASS_ATTEMPT_SLACK_S
        <= dsn.OVERPASS_DEADLINE_S
    )
    assert all(wait >= OVERPASS_MIN_RETRY_WAIT_S for wait in clock.sleeps)
    gaps = [later - earlier for earlier, later in itertools.pairwise(host.starts)]
    assert all(gap >= cost + OVERPASS_MIN_RETRY_WAIT_S for gap in gaps)


# ---------------------------------------------------------------------------
# Settled answers are asked once
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "error",
    [
        ResponseStatusCodeError("406 Not Acceptable"),
        ResponseStatusCodeError("403 Forbidden"),
        InsufficientResponseError("no drivable ways"),
        ValueError("malformed"),
    ],
    ids=["406", "403", "empty-bbox", "other"],
)
def test_a_settled_answer_propagates_on_its_first_attempt(error):
    clock = FakeClock()
    host = Host(clock, error=error)
    with pytest.raises(type(error)):
        _run(host, clock)
    assert host.starts == [0]
    assert clock.sleeps == []


def test_exhaustion_chains_the_last_fault_as_its_cause():
    clock = FakeClock()
    errors = iter([REFUSED, TIMED_OUT, REFUSED, REFUSED, TIMED_OUT])

    def host():
        raise next(errors)

    with pytest.raises(RetriesExhausted) as excinfo:
        _run(host, clock)
    assert excinfo.value.last is excinfo.value.__cause__
    assert isinstance(excinfo.value.last, requests.exceptions.Timeout)


def test_each_retry_is_logged_with_its_attempt_and_wait(caplog):
    clock = FakeClock()
    with caplog.at_level(logging.WARNING), pytest.raises(RetriesExhausted):
        _run(Host(clock), clock, OverpassRetryPolicy(max_attempts=2))
    assert "attempt 1 of at most 2" in caplog.text
    assert "retrying in 30 s" in caplog.text


# ---------------------------------------------------------------------------
# Validation: the floor and the ceiling are enforced, not defaulted
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kwargs",
    [
        {"initial_wait_s": 29.9},
        {"initial_wait_s": 0},
        {"max_wait_s": 20},
        {"initial_wait_s": 60, "max_wait_s": 45},
        {"jitter": -0.1},
        {"jitter": 1.5},
        {"window_s": 0},
        {"window_s": OVERPASS_RETRY_WINDOW_CEILING_S + 1},
        {"max_attempts": 0},
        {"max_attempts": 2.5},
        {"max_attempts": True},
        {"jitter": True},
        {"window_s": float("nan")},
        {"initial_wait_s": float("inf")},
        {"max_wait_s": "240"},
    ],
)
def test_an_out_of_range_policy_cannot_be_constructed(kwargs):
    with pytest.raises(ValueError):
        OverpassRetryPolicy(**kwargs)


def test_the_boundaries_themselves_are_allowed():
    OverpassRetryPolicy(
        max_attempts=1,
        initial_wait_s=OVERPASS_MIN_RETRY_WAIT_S,
        max_wait_s=OVERPASS_MIN_RETRY_WAIT_S,
        jitter=0,
        window_s=OVERPASS_RETRY_WINDOW_CEILING_S,
    )
    OverpassRetryPolicy(jitter=1)


# ---------------------------------------------------------------------------
# Through the real fetch: typed failure, exit 76, the lock, the pass-through
# ---------------------------------------------------------------------------


@pytest.fixture
def overpass(monkeypatch):
    """The fetch's retry loop on a fake clock, and a refusing Overpass stand-in
    that costs osmnx's 60 s status-fallback pause per attempt."""
    clock = FakeClock()
    monkeypatch.setattr(dsn, "_retry_clock", clock)
    monkeypatch.setattr(dsn, "_retry_sleep", clock.sleep)
    monkeypatch.setattr(dsn, "_retry_random", lambda: 0.0)
    host = Host(clock, cost=OSMNX_STATUS_FALLBACK_PAUSE_S)

    def graph_from_bbox(**kwargs):
        host()
        graph = nx.MultiDiGraph()
        graph.add_edge(1, 2)
        return graph

    monkeypatch.setattr(dsn.ox, "graph_from_bbox", graph_from_bbox)
    monkeypatch.setattr(dsn.ox, "save_graphml", lambda g, p: open(p, "w").close())
    return host


def test_exhaustion_is_a_host_block_that_says_how_long_it_asked(overpass):
    with pytest.raises(HostBlockedError) as excinfo:
        dsn._download_graph_named((0, 0, 1, 1), "drive")
    assert excinfo.value.host == HOST_OVERPASS
    assert len(overpass.starts) == 4
    assert "4 attempt(s) over 450 s" in str(excinfo.value)
    assert "[overpass]" in str(excinfo.value), "must name the knob that sets the window"
    assert isinstance(excinfo.value.__cause__, requests.exceptions.ConnectionError)


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (ResponseStatusCodeError("406 Not Acceptable"), HostBlockedError),
        (InsufficientResponseError("no drivable ways"), DownloadError),
    ],
)
def test_a_settled_answer_through_the_fetch_costs_one_round_trip(overpass, error, expected):
    overpass.error = error
    with pytest.raises(expected):
        dsn._download_graph_named((0, 0, 1, 1), "drive")
    assert overpass.starts == [0]
    assert overpass.clock.sleeps == []


def test_a_flap_that_clears_is_frozen_like_any_other_fetch(overpass, tmp_path):
    from tests.test_host_lock import _city_row

    overpass.failures = 2
    city = _city_row()
    graph = dsn.fetch_graph(city, str(tmp_path))
    assert graph.number_of_edges() == 1
    assert overpass.starts == [0, 90, 210]
    assert (tmp_path / "osm_cache").is_dir()
    assert dsn.os.path.exists(dsn.network_cache_path(city.city_id, str(tmp_path)))


def test_fetch_graph_hands_the_policy_it_was_given_to_the_retry_loop(overpass, tmp_path):
    """Pass-through at a NON-default value: a fetch_graph that dropped the
    argument would run the default five attempts and this would see four."""
    from tests.test_host_lock import _city_row

    overpass.cost = 0.0
    with pytest.raises(HostBlockedError):
        dsn.fetch_graph(
            _city_row(), str(tmp_path), overpass_retry=OverpassRetryPolicy(max_attempts=2)
        )
    assert len(overpass.starts) == 2


def test_fetch_street_edges_hands_the_policy_through(overpass, tmp_path):
    from tests.test_host_lock import _city_row

    overpass.cost = 0.0
    with pytest.raises(HostBlockedError):
        dsn.fetch_street_edges(
            _city_row(), str(tmp_path), overpass_retry=OverpassRetryPolicy(max_attempts=3)
        )
    assert len(overpass.starts) == 3


def test_every_retry_happens_inside_the_overpass_host_lock(overpass, tmp_path, monkeypatch):
    """A competing process slipping in between our attempts would be a second
    talker against a host already refusing this IP (#208), so the whole window
    runs under one acquisition of the lock."""
    import contextlib

    from tests.test_host_lock import _city_row

    acquisitions = []
    inside = {"now": False}

    @contextlib.contextmanager
    def fake_lock(host):
        acquisitions.append(host)
        inside["now"] = True
        try:
            yield
        finally:
            inside["now"] = False

    monkeypatch.setattr(dsn, "host_lock", fake_lock)
    locked_at_attempt = []

    def graph_from_bbox(**kwargs):
        locked_at_attempt.append(inside["now"])
        overpass()

    monkeypatch.setattr(dsn.ox, "graph_from_bbox", graph_from_bbox)
    with pytest.raises(HostBlockedError):
        dsn.fetch_graph(_city_row(), str(tmp_path))
    assert acquisitions == [HOST_OVERPASS]
    assert locked_at_attempt == [True, True, True, True]


def test_an_exhausted_walk_child_still_exits_76(overpass, tmp_path, monkeypatch):
    """The breaker's whole input is the child's return code: exhaustion must
    still produce the Overpass blocked code, after the full window."""
    from tests.test_streetwalk_collect import _args, _setup

    data_dir = _setup(tmp_path, monkeypatch)
    monkeypatch.setattr(collect, "fetch_street_edges", dsn.fetch_street_edges)
    monkeypatch.setenv("GMAPS_STREETS_API_KEY", "TESTKEY")

    assert collect.run_collect(_args(data_dir)) == HOST_EXIT_CODES[HOST_OVERPASS] == 76
    assert len(overpass.starts) == 4


# ---------------------------------------------------------------------------
# The config hop: TOML -> SchedulerConfig -> argv -> collect -> fetch
# ---------------------------------------------------------------------------

_NON_DEFAULT_TOML = """
[overpass]
retry_max_attempts = 7
retry_initial_wait_s = 45.0
retry_max_wait_s = 90
retry_jitter = 0.5
retry_window_s = 480
"""

_NON_DEFAULT = OverpassRetryPolicy(
    max_attempts=7, initial_wait_s=45.0, max_wait_s=90, jitter=0.5, window_s=480
)


def _load(tmp_path, text):
    path = tmp_path / "scheduler.toml"
    path.write_text(text)
    return scheduler.load_scheduler_config(str(path))


def test_every_field_differs_from_its_default_so_the_hop_tests_mean_something():
    defaults = OverpassRetryPolicy()
    for name in ("max_attempts", "initial_wait_s", "max_wait_s", "jitter", "window_s"):
        assert getattr(_NON_DEFAULT, name) != getattr(defaults, name), name


def test_the_loader_reads_every_overpass_key(tmp_path):
    assert _load(tmp_path, _NON_DEFAULT_TOML).overpass_retry == _NON_DEFAULT


def test_no_overpass_table_means_the_defaults(tmp_path):
    assert _load(tmp_path, "[schedule]\ncycle_days = 90\n").overpass_retry == OverpassRetryPolicy()


@pytest.mark.parametrize(
    ("bad", "sentinel"),
    [
        # Each case pairs the offending key with a VALID line on a different
        # key, so "fell back whole" is distinguishable from "dropped the bad
        # key and kept the rest" -- which is what the assertion is about.
        ("retry_initial_wait_s = 10", "retry_max_attempts = 7"),  # under the 30 s floor
        ("retry_window_s = 1200", "retry_max_attempts = 7"),  # past the 900 s deadline
        ("retry_max_attempts = 0", "retry_jitter = 0.5"),
        ("retry_jitter = true", "retry_max_attempts = 7"),
        ('retry_max_wait_s = "long"', "retry_max_attempts = 7"),
    ],
)
def test_an_invalid_table_warns_and_falls_back_to_the_defaults_whole(
    tmp_path, caplog, bad, sentinel
):
    """Whole, not per key: the fields constrain each other, so keeping the valid
    ones would run a schedule nobody wrote down. And not raised: a load-time
    ValueError takes down every subcommand, backup-status included."""
    with caplog.at_level(logging.WARNING):
        cfg = _load(tmp_path, f"[overpass]\n{sentinel}\n{bad}\n")
    assert cfg.overpass_retry == OverpassRetryPolicy(), "the sentinel key must not survive"
    assert "[overpass]" in caplog.text


@pytest.mark.parametrize("text", ["overpass = 5\n", 'overpass = "on"\n', "overpass = [1, 2]\n"])
def test_an_overpass_key_that_is_not_a_table_warns_rather_than_crashing(tmp_path, caplog, text):
    """`[overpass]` mistyped as a scalar reaches the loader as a non-dict. Left
    to `.items()` that is an AttributeError out of `load_scheduler_config`,
    i.e. EVERY subcommand down -- `backup-status` and `restore-backup`, the
    incident-time handles, included -- over one line of one section."""
    with caplog.at_level(logging.WARNING):
        cfg = _load(tmp_path, text)
    assert cfg.overpass_retry == OverpassRetryPolicy()
    assert "[overpass]" in caplog.text


def test_an_unknown_key_is_named_and_ignored(tmp_path, caplog):
    with caplog.at_level(logging.WARNING):
        cfg = _load(tmp_path, "[overpass]\nretry_attempts = 9\nretry_jitter = 0.5\n")
    assert cfg.overpass_retry == OverpassRetryPolicy(jitter=0.5)
    assert "retry_attempts" in caplog.text
    assert "retry_max_attempts" in caplog.text, "the warning must name the real keys"


@pytest.mark.parametrize("channel", sorted(scheduler.STREET_CHANNELS))
def test_every_walk_channel_hands_the_configured_policy_to_its_child(tmp_path, channel):
    """Every walk may be the one that goes to Overpass for its network. The
    argv is parsed back by collect's own parser, so this pins both ends of the
    hop against each other, at non-default values."""
    from datetime import date

    from tests.test_host_lock import _city_row

    cfg = _load(tmp_path, _NON_DEFAULT_TOML)
    cmd = scheduler._street_collect_cmd(cfg, _city_row(), date(2026, 9, 22), channel, 8, 9_000)
    module_at = cmd.index("streetscape_street_analyzer.collect")
    args = collect.build_parser().parse_args(cmd[module_at + 1 :])
    assert collect.overpass_retry_from_args(args) == _NON_DEFAULT


def test_the_argv_round_trips_exactly():
    args = collect.build_parser().parse_args(["City", *overpass_retry_argv(_NON_DEFAULT)])
    assert collect.overpass_retry_from_args(args) == _NON_DEFAULT


def test_no_flags_mean_the_policy_defaults():
    args = collect.build_parser().parse_args(["City"])
    assert collect.overpass_retry_from_args(args) == OverpassRetryPolicy()


def _args_argv(data_dir):
    from tests.test_streetwalk_collect import CITY_QUERY, RUN_DATE

    return [CITY_QUERY, "--data-dir", data_dir, "--run-date", RUN_DATE, "--spacing", "15"]


def test_collect_hands_the_parsed_policy_to_the_network_fetch(tmp_path, monkeypatch):
    from tests.test_streetwalk_collect import _setup

    data_dir = _setup(tmp_path, monkeypatch)
    seen = {}

    def capture(*a, **k):
        seen.update(k)
        raise HostBlockedError("stop here", host=HOST_OVERPASS)

    monkeypatch.setattr(collect, "fetch_street_edges", capture)
    args = collect.build_parser().parse_args(
        [*_args_argv(data_dir), *overpass_retry_argv(_NON_DEFAULT)]
    )
    assert collect.run_collect(args) == 76
    assert seen["overpass_retry"] == _NON_DEFAULT


def test_collect_refuses_a_wait_under_the_policy_floor_before_touching_anything(
    tmp_path, monkeypatch
):
    from tests.test_streetwalk_collect import _args, _setup

    data_dir = _setup(tmp_path, monkeypatch)
    called = []
    monkeypatch.setattr(collect, "fetch_street_edges", lambda *a, **k: called.append(1))
    args = _args(data_dir)
    args.overpass_retry_initial_wait_s = 5.0
    assert collect.run_collect(args) == 2
    assert called == []


@pytest.mark.parametrize("name", ["scheduler.toml", "scheduler.makelab1.toml"])
def test_both_shipped_configs_declare_a_valid_overpass_table(name, caplog):
    """Production reads scheduler.makelab1.toml, and an invalid [overpass] there
    would fall back SILENTLY but for a warning; assert it loads as written."""
    import tomllib

    path = scheduler._PROJECT_ROOT / "config" / name
    with open(path, "rb") as fh:
        table = tomllib.load(fh)["overpass"]
    with caplog.at_level(logging.WARNING):
        cfg = scheduler.load_scheduler_config(str(path))
    assert "[overpass]" not in caplog.text
    assert cfg.overpass_retry == OverpassRetryPolicy(
        max_attempts=table["retry_max_attempts"],
        initial_wait_s=table["retry_initial_wait_s"],
        max_wait_s=table["retry_max_wait_s"],
        jitter=table["retry_jitter"],
        window_s=table["retry_window_s"],
    )


# ---------------------------------------------------------------------------
# The end-of-night clamp: a refusal must still EXIT, not be SIGKILLed (#357 review)
#
# `city_timeout_seconds` clamps a child's timeout down to what is left of the
# batch deadline, floored at `_MIN_CLAMPED_TIMEOUT_S` (300 s). A refusal now
# costs >= ~450 s, so without this a walk launched late is killed mid-window --
# and a SIGKILL carries NO exit code, so it counts a `consecutive_failure` and
# the breaker never learns the host refused us. Before this PR a refusal died in
# ~12 s or ~3.2 min, both inside the floor, so the hazard is new.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("timeout_s", "expected"),
    [
        (180 * 60, OverpassRetryPolicy()),  # the unclamped floor: untouched
        (960, OverpassRetryPolicy()),  # exactly enough for the full window
        (1200, OverpassRetryPolicy()),  # more than enough
        (900, OverpassRetryPolicy(window_s=540)),  # shortened
        (600, OverpassRetryPolicy(window_s=240)),
        (400, OverpassRetryPolicy(window_s=40)),
        (360, OverpassRetryPolicy(max_attempts=1)),  # nothing left: one attempt
        (300, OverpassRetryPolicy(max_attempts=1)),  # _MIN_CLAMPED_TIMEOUT_S
        (1, OverpassRetryPolicy(max_attempts=1)),
    ],
)
def test_the_window_is_shortened_to_what_the_childs_timeout_can_hold(timeout_s, expected):
    assert policy_for_child_timeout(OverpassRetryPolicy(), timeout_s) == expected


@pytest.mark.parametrize("timeout_s", [300, 330, 400, 500, 600, 750, 900, 960, 1800, 10_800])
@pytest.mark.parametrize("cost", [0.0, OSMNX_STATUS_FALLBACK_PAUSE_S])
def test_a_refusal_always_finishes_before_the_child_would_be_killed(timeout_s, cost):
    """The property the clamp exists for, measured rather than asserted from the
    arithmetic: play the whole refusal out on a fake clock at maximum jitter and
    check the last attempt could still finish -- start, plus a full request
    timeout and osmnx's slot pause, plus what the child spent before the fetch
    -- inside the timeout it will be SIGKILLed at."""
    policy = policy_for_child_timeout(OverpassRetryPolicy(), timeout_s)
    clock = FakeClock()
    host = Host(clock, cost=cost)
    with pytest.raises(RetriesExhausted):
        _run(host, clock, policy, u=U_MAX)
    # `cost` is NOT added on top: osmnx's pre-request pause is what the
    # reserve's 120 s of slack is for, and counting it twice would demand a
    # timeout the fetch's own 900 s deadline does not.
    worst_case_end = (
        host.starts[-1] + OVERPASS_FINAL_ATTEMPT_RESERVE_S + OVERPASS_CHILD_STARTUP_RESERVE_S
    )
    if timeout_s >= OVERPASS_FINAL_ATTEMPT_RESERVE_S + OVERPASS_CHILD_STARTUP_RESERVE_S:
        assert worst_case_end <= timeout_s
    else:
        # Nothing fits; one attempt is the least the fetch can cost, and the
        # 900 s deadline is what still bounds it.
        assert host.starts == [0]


def _walk_cmd(cfg, channel="gsv_streets", **kwargs):
    from datetime import date

    from tests.test_host_lock import _city_row

    return scheduler._street_collect_cmd(
        cfg, _city_row(), date(2026, 9, 22), channel, 8, 9_000, **kwargs
    )


def _policy_in(cmd):
    module_at = cmd.index("streetscape_street_analyzer.collect")
    return collect.overpass_retry_from_args(collect.build_parser().parse_args(cmd[module_at + 1 :]))


def test_the_argv_carries_the_shortened_window_for_a_clamped_child(tmp_path):
    cfg = _load(tmp_path, _NON_DEFAULT_TOML)
    assert _policy_in(_walk_cmd(cfg, child_timeout_s=10_800)) == _NON_DEFAULT
    assert _policy_in(_walk_cmd(cfg)) == _NON_DEFAULT, "an unknown timeout leaves it alone"
    clamped = _policy_in(_walk_cmd(cfg, child_timeout_s=600))
    assert clamped == policy_for_child_timeout(_NON_DEFAULT, 600)
    assert clamped == replace(_NON_DEFAULT, window_s=240)
    assert _policy_in(_walk_cmd(cfg, child_timeout_s=300)).max_attempts == 1


@pytest.mark.parametrize("channel", sorted(scheduler.STREET_CHANNELS))
def test_the_production_dispatch_shrinks_the_window_for_a_clamped_child(
    tmp_path, monkeypatch, channel
):
    """`_run_one_city` is the only production caller, so the clamp is worth
    nothing unless IT passes the timeout it is about to kill the child at.
    Derives the timeout BEFORE the argv, which is the edit this pins."""
    from datetime import date

    from tests.test_host_lock import _city_row

    cfg = _load(tmp_path, _NON_DEFAULT_TOML)
    seen = {}

    def capture(cfg_, cmd, timeout_s, city, provider, today):
        seen["cmd"], seen["timeout_s"] = cmd, timeout_s
        return scheduler.CollectionOutcome(True, "stubbed")

    monkeypatch.setattr(scheduler, "_run_collection_subprocess", capture)
    scheduler._run_one_city(
        cfg,
        _city_row(),
        date(2026, 9, 22),
        provider=channel,
        timeout_s=scheduler._MIN_CLAMPED_TIMEOUT_S,
        estimated_requests=0,
    )
    assert seen["timeout_s"] == scheduler._MIN_CLAMPED_TIMEOUT_S
    assert _policy_in(seen["cmd"]) == policy_for_child_timeout(
        _NON_DEFAULT, scheduler._MIN_CLAMPED_TIMEOUT_S
    )
    assert _policy_in(seen["cmd"]).max_attempts == 1


def test_an_unclamped_production_dispatch_carries_the_configured_window(tmp_path, monkeypatch):
    from datetime import date

    from tests.test_host_lock import _city_row

    cfg = _load(tmp_path, _NON_DEFAULT_TOML)
    seen = {}

    def capture(cfg_, cmd, timeout_s, city, provider, today):
        seen["cmd"] = cmd
        return scheduler.CollectionOutcome(True, "stubbed")

    monkeypatch.setattr(scheduler, "_run_collection_subprocess", capture)
    scheduler._run_one_city(
        cfg,
        _city_row(),
        date(2026, 9, 22),
        provider="gsv_streets",
        timeout_s=cfg.city_timeout_minutes * 60,
        estimated_requests=0,
    )
    assert _policy_in(seen["cmd"]) == _NON_DEFAULT
