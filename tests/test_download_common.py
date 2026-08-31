"""Unit tests for the provider-agnostic download helpers extracted into
`download_common.py` (grid generation, capture-date normalization, the shared
download exception). These are consumed by both the GSV and Mapillary
downloaders and the GSV history harvester, so they're tested on their own here
rather than only transitively through a provider.
"""

import argparse

import geopy.distance
import pytest

from streetscape_metadata_tracker.download_common import (
    AsyncRateLimiter,
    DownloadError,
    _unit_exponential,
    coerce_jitter,
    generate_grid_arrays,
    generate_grid_points,
    grid_index_ranges,
    jitter_fraction,
    standardize_capture_date,
)

SEATTLE = geopy.Point(47.6062, -122.3321)


# ── generate_grid_points ────────────────────────────────────────────────────


def test_grid_point_count_is_steps_plus_one_squared():
    # (width_steps + 1) * (height_steps + 1) — a fencepost point on each side.
    points = generate_grid_points(SEATTLE, width_steps=4, height_steps=6, step_length=20)
    assert len(points) == (4 + 1) * (6 + 1)


def test_grid_zero_steps_yields_single_centered_point():
    points = generate_grid_points(SEATTLE, width_steps=0, height_steps=0, step_length=20)
    assert len(points) == 1
    lat, lon, i, j = points[0]
    assert (i, j) == (0, 0)
    assert lat == pytest.approx(SEATTLE.latitude, abs=1e-9)
    assert lon == pytest.approx(SEATTLE.longitude, abs=1e-9)


def test_grid_center_point_is_the_origin():
    points = generate_grid_points(SEATTLE, width_steps=4, height_steps=4, step_length=50)
    center = [(lat, lon) for lat, lon, i, j in points if i == 0 and j == 0]
    assert len(center) == 1
    lat, lon = center[0]
    assert lat == pytest.approx(SEATTLE.latitude, abs=1e-9)
    assert lon == pytest.approx(SEATTLE.longitude, abs=1e-9)


def test_grid_indices_cover_the_full_rectangle():
    w, h = 4, 6
    points = generate_grid_points(SEATTLE, width_steps=w, height_steps=h, step_length=20)
    idx = {(i, j) for _, _, i, j in points}
    expected = {(i, j) for i in range(-h // 2, h // 2 + 1) for j in range(-w // 2, w // 2 + 1)}
    assert idx == expected


def test_grid_coordinates_literal_anchor():
    """Pin exact output coordinates for one known grid.

    The other grid tests verify structure (counts, indices, relative
    spacing) — properties a mirrored bug in the implementation could still
    satisfy. These literals were hand-checked against independent geodesy:
    100 m north of 47.6°N is +0.000899° lat; 100 m east is +0.001330° lon
    (1° lon ≈ 75.1 km at that latitude). A regression in the bearing/
    distance math changes them and fails here.
    """
    points = generate_grid_points(SEATTLE, width_steps=2, height_steps=2, step_length=100)
    by_idx = {(i, j): (lat, lon) for lat, lon, i, j in points}
    assert by_idx[(1, 0)] == (
        pytest.approx(47.607099, abs=5e-6),
        pytest.approx(-122.332100, abs=5e-6),
    )
    assert by_idx[(0, 1)] == (
        pytest.approx(47.606200, abs=5e-6),
        pytest.approx(-122.330770, abs=5e-6),
    )
    assert by_idx[(-1, -1)] == (
        pytest.approx(47.605301, abs=5e-6),
        pytest.approx(-122.333430, abs=5e-6),
    )


def test_grid_step_spacing_matches_step_length():
    step = 100
    points = generate_grid_points(SEATTLE, width_steps=2, height_steps=2, step_length=step)
    by_idx = {(i, j): (lat, lon) for lat, lon, i, j in points}
    # Neighbor one step east (j: 0 -> 1) should be ~step_length meters away.
    center = by_idx[(0, 0)]
    east = by_idx[(0, 1)]
    dist_m = geopy.distance.distance(center, east).meters
    assert dist_m == pytest.approx(step, rel=0.01)


# ── standardize_capture_date ────────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("2024-05-15", "2024-05-15"),  # full date passes through
        ("2024-05", "2024-05-01"),  # year-month → first of month
        ("2024", "2024-01-01"),  # year only → Jan 1
        (None, None),  # missing
        ("", None),  # empty
        ("not-a-date", None),  # unparseable
        ("2024/05/15", None),  # slash format is not accepted
        ("05-15-2024", None),  # US ordering is not accepted
    ],
)
def test_standardize_capture_date(raw, expected):
    assert standardize_capture_date(raw) == expected


# ── DownloadError ───────────────────────────────────────────────────────────


def test_download_error_is_an_exception():
    assert issubclass(DownloadError, Exception)
    with pytest.raises(DownloadError, match="boom"):
        raise DownloadError("boom")


# ── AsyncRateLimiter ────────────────────────────────────────────────────────
#
# Deterministic: the limiter takes an injectable clock (time_func) and its
# only sleep call is monkeypatched to advance that fake clock, so no test
# here waits on real time.


def _make_clock():
    clock = {"t": 0.0}
    return clock, (lambda: clock["t"])


def _patch_sleep(monkeypatch, clock, sleeps):
    import asyncio as aio

    async def fake_sleep(seconds):
        sleeps.append(seconds)
        clock["t"] += seconds

    monkeypatch.setattr(aio, "sleep", fake_sleep)


def _run(coro):
    import asyncio as aio

    return aio.run(coro)


def test_rate_limiter_paces_to_configured_rate(monkeypatch):
    clock, now = _make_clock()
    sleeps = []
    _patch_sleep(monkeypatch, clock, sleeps)
    limiter = AsyncRateLimiter(60, time_func=now)  # 1 token/s, burst 1

    async def scenario():
        for _ in range(3):
            await limiter.acquire()

    _run(scenario())
    # First acquisition spends the initial token; each subsequent one must
    # wait a full second for the next token to accrue.
    assert sleeps == [pytest.approx(1.0), pytest.approx(1.0)]


def test_rate_limiter_allows_one_second_burst(monkeypatch):
    clock, now = _make_clock()
    sleeps = []
    _patch_sleep(monkeypatch, clock, sleeps)
    limiter = AsyncRateLimiter(600, time_func=now)  # 10 tokens/s, burst 10

    async def scenario():
        for _ in range(10):
            await limiter.acquire()  # burst capacity: no waiting
        await limiter.acquire()  # 11th must wait one token interval

    _run(scenario())
    assert sleeps == [pytest.approx(0.1)]


def test_rate_limiter_refills_while_idle_but_caps_at_capacity(monkeypatch):
    clock, now = _make_clock()
    sleeps = []
    _patch_sleep(monkeypatch, clock, sleeps)
    limiter = AsyncRateLimiter(600, time_func=now)  # 10 tokens/s, burst 10

    async def scenario():
        for _ in range(10):
            await limiter.acquire()  # drain the bucket
        clock["t"] += 3600.0  # a long idle refills at most to capacity
        for _ in range(10):
            await limiter.acquire()  # burst again without waiting
        await limiter.acquire()  # ...but the 11th still waits

    _run(scenario())
    assert sleeps == [pytest.approx(0.1)]


def test_rate_limiter_zero_or_negative_disables(monkeypatch):
    clock, now = _make_clock()
    sleeps = []
    _patch_sleep(monkeypatch, clock, sleeps)

    async def scenario():
        for max_per_minute in (0, -5):
            limiter = AsyncRateLimiter(max_per_minute, time_func=now)
            for _ in range(100):
                await limiter.acquire()

    _run(scenario())
    assert sleeps == []


# ── AsyncRateLimiter jitter (issue #292) ─────────────────────────────────────
#
# The spaced pacer is the fourth per-IP hypothesis under test, and the property
# it exists to change is the metronome: a saturated token bucket sleeps exactly
# 60/max_per_minute between requests. The gap is a SHIFTED EXPONENTIAL —
# mean * ((1 - j) + j * Exponential(1)) — so these pin (a) that the gaps really
# are that shifted draw, (b) that the mean rate is unchanged and the CV is
# `jitter`, (c) that there is no burst and no ceiling, and (d) that jitter=0 is
# byte-for-byte the old bucket.


def _draws(values):
    """A deterministic stand-in for the unit-exponential sampler."""
    it = iter(values)
    return lambda: next(it)


def test_jittered_limiter_spaces_requests_by_the_shifted_draw(monkeypatch):
    clock, now = _make_clock()
    sleeps = []
    _patch_sleep(monkeypatch, clock, sleeps)
    # mean gap 1.5 s (40/min), jitter 0.6: gap = 1.5 * (0.4 + 0.6 * draw).
    # Every acquisition draws the gap the NEXT one will wait, so four requests
    # draw four times and the last draw is never slept on.
    limiter = AsyncRateLimiter(
        40, time_func=now, jitter=0.6, draw_func=_draws([0.0, 3.0, 1.0, 1.0])
    )

    async def scenario():
        for _ in range(4):
            await limiter.acquire()

    _run(scenario())
    # The first request never waits; each later one waits out the PREVIOUS
    # request's drawn gap. draw=0 lands exactly on the floor, and draw=3 is a
    # gap the old uniform draw could not have produced at all (its ceiling was
    # 1.6 * mean = 2.4 s) — which is the point of the change.
    assert sleeps == [pytest.approx(0.6), pytest.approx(3.3), pytest.approx(1.5)]


def test_the_floor_is_exactly_one_minus_jitter_and_a_gap_is_never_zero(monkeypatch):
    """`jitter < 1` is enforced precisely because it is what keeps the floor
    above zero — at 1 the draw admits an unpaced burst against the tile CDN."""
    clock, now = _make_clock()
    sleeps = []
    _patch_sleep(monkeypatch, clock, sleeps)
    limiter = AsyncRateLimiter(60, time_func=now, jitter=0.99, draw_func=_draws([0.0] * 5))

    async def scenario():
        for _ in range(3):
            await limiter.acquire()

    _run(scenario())
    # Even the smallest possible draw leaves (1 - jitter) * mean of spacing.
    assert sleeps == [pytest.approx(0.01), pytest.approx(0.01)]
    assert all(gap > 0 for gap in sleeps)


def test_jittered_limiter_keeps_the_mean_rate_and_reaches_the_target_cv(monkeypatch):
    """Two properties in one draw sample, because they trade off against each
    other: jitter reshapes the gaps but must not quietly slow or speed the
    channel (the daily budget and the scheduler's timeout are both derived from
    max_requests_per_minute as a MEAN), and the reshaping has to be large
    enough to matter — an organic client's Poisson arrivals sit at CV 1.0, and
    the uniform draw this replaced reached only jitter/sqrt(3) = 0.35."""
    import random as _random
    import statistics

    clock, now = _make_clock()
    sleeps = []
    _patch_sleep(monkeypatch, clock, sleeps)
    rng = _random.Random(292)
    limiter = AsyncRateLimiter(
        60, time_func=now, jitter=0.6, draw_func=lambda: rng.expovariate(1.0)
    )

    async def scenario():
        for _ in range(20_000):
            await limiter.acquire()

    _run(scenario())
    assert len(sleeps) == 19_999
    mean = statistics.fmean(sleeps)
    assert mean == pytest.approx(1.0, rel=0.02), "the mean rate must survive the reshaping"
    assert statistics.pstdev(sleeps) / mean == pytest.approx(0.6, rel=0.05), "CV == jitter"
    assert min(sleeps) >= 0.4, "the (1 - jitter) floor holds"
    # No ceiling: the old uniform draw could never exceed (1 + jitter) * mean,
    # and that hard bound is exactly what a scorer reading gap statistics would
    # still have seen. This is the assertion that the distribution changed.
    assert max(sleeps) > 1.6


def test_jittered_limiter_has_no_burst_capacity(monkeypatch):
    """The bucket lets ~1 s of requests through unpaced after an idle period;
    the pacer must not, since a burst is exactly the shape a per-IP scorer
    would notice first."""
    clock, now = _make_clock()
    sleeps = []
    _patch_sleep(monkeypatch, clock, sleeps)
    limiter = AsyncRateLimiter(600, time_func=now, jitter=0.6, draw_func=_draws([1.0] * 20))

    async def scenario():
        await limiter.acquire()
        clock["t"] += 3600.0  # a long idle earns no credit
        for _ in range(3):
            await limiter.acquire()

    _run(scenario())
    # After the idle the first acquisition goes straight out (its slot is long
    # past), and every one after it waits the full gap — never zero.
    assert sleeps == [pytest.approx(0.1), pytest.approx(0.1)]


def test_zero_jitter_is_exactly_the_token_bucket(monkeypatch):
    clock, now = _make_clock()
    sleeps = []
    _patch_sleep(monkeypatch, clock, sleeps)

    def never():  # pragma: no cover - the assertion is that it is unused
        raise AssertionError("jitter=0 must not draw randomness")

    limiter = AsyncRateLimiter(60, time_func=now, jitter=0.0, draw_func=never)

    async def scenario():
        for _ in range(3):
            await limiter.acquire()

    _run(scenario())
    assert sleeps == [pytest.approx(1.0), pytest.approx(1.0)]


def test_disabled_pacing_ignores_jitter(monkeypatch):
    clock, now = _make_clock()
    sleeps = []
    _patch_sleep(monkeypatch, clock, sleeps)
    limiter = AsyncRateLimiter(0, time_func=now, jitter=0.6)

    async def scenario():
        for _ in range(50):
            await limiter.acquire()

    _run(scenario())
    assert sleeps == []


def test_the_default_sampler_is_the_unit_exponential():
    """The mean-rate guarantee is `E[draw] == 1`; a differently-scaled default
    would silently re-rate every Mapillary channel."""
    import statistics

    draws = [_unit_exponential() for _ in range(50_000)]
    assert statistics.fmean(draws) == pytest.approx(1.0, rel=0.02)
    assert statistics.pstdev(draws) == pytest.approx(1.0, rel=0.02)  # CV 1.0
    assert min(draws) >= 0.0


@pytest.mark.parametrize("bad", [-0.1, 1.0, 1.5])
def test_jitter_outside_the_unit_interval_is_refused(bad):
    """>= 1 erases the gap floor, i.e. an unpaced burst against the tile CDN."""
    with pytest.raises(ValueError, match="jitter"):
        AsyncRateLimiter(60, jitter=bad)


def test_jitter_fraction_argparse_type():
    assert jitter_fraction("0") == 0.0
    assert jitter_fraction("0.6") == pytest.approx(0.6)
    for bad in ("1", "1.2", "-0.5", "lots", "inf", "nan"):
        with pytest.raises(argparse.ArgumentTypeError):
            jitter_fraction(bad)


def test_coerce_jitter_agrees_with_the_argparse_type():
    """The config loader's half of the same guard (issue #292). It must accept
    exactly what `jitter_fraction` accepts, and answer None — "use the
    collector's default" — rather than 0, which means "restore the metronome"."""
    assert coerce_jitter(None) is None
    assert coerce_jitter(0.6) == pytest.approx(0.6)
    assert coerce_jitter(0) == 0.0
    assert coerce_jitter("0.25") == pytest.approx(0.25)
    # `False` is the one that has to be named: it is an int, so it would coerce
    # to a valid 0.0 and silently mean "restore the metronome".
    for bad in (1, 1.5, -0.1, "banana", True, False, [], float("nan"), float("inf")):
        assert coerce_jitter(bad) is None, bad


# ── generate_grid_arrays (issue #157) ───────────────────────────────────────


def _reference_grid(origin, width_steps, height_steps, step_length):
    """The original nested-loop grid, kept verbatim as the frozen-geometry oracle.

    Grid geometry is immutable by design: every future run of a city re-derives
    these coordinates so its diffs align on an identical rectangle. Any drift
    here silently misaligns a city against its own history, which no other test
    would catch — so the fast paths are checked against the slow original rather
    than against each other.
    """
    points = []
    for i in range(-height_steps // 2, height_steps // 2 + 1):
        for j in range(-width_steps // 2, width_steps // 2 + 1):
            north = geopy.distance.distance(meters=i * step_length).destination(origin, 0)
            point = geopy.distance.distance(meters=j * step_length).destination(north, 90)
            points.append((point.latitude, point.longitude, i, j))
    return points


@pytest.mark.parametrize(
    ("origin", "w", "h", "step"),
    [
        (SEATTLE, 11, 7, 20),  # odd/odd: exercises the // 2 fencepost asymmetry
        (SEATTLE, 4, 6, 20),
        (geopy.Point(-33.8688, 151.2093), 8, 8, 20),  # southern hemisphere
        (geopy.Point(30.0444, 31.2357), 5, 5, 50),
        (geopy.Point(64.1466, -21.9426), 3, 8, 15),  # high latitude
        (SEATTLE, 0, 0, 20),  # degenerate single point
    ],
)
def test_both_generators_match_the_original_loop_exactly(origin, w, h, step):
    reference = _reference_grid(origin, w, h, step)

    assert generate_grid_points(origin, w, h, step) == reference

    lats, lons, i_idx, j_idx = generate_grid_arrays(origin, w, h, step)
    as_tuples = [
        (lat, lon, int(i), int(j))
        for lat, lon, i, j in zip(
            lats.tolist(), lons.tolist(), i_idx.tolist(), j_idx.tolist(), strict=True
        )
    ]
    assert as_tuples == reference, "array grid must be bit-identical, not merely close"


def test_grid_index_ranges_match_the_generated_order():
    w, h = 11, 7
    i_values, j_values = grid_index_ranges(w, h)
    _, _, i_idx, j_idx = generate_grid_arrays(SEATTLE, w, h, 20)

    # The ordinal formula the Mapillary downloader uses to replace its
    # (i, j) -> (lat, lon) dict depends on this exact ordering.
    expected = [(i, j) for i in i_values for j in j_values]
    assert list(zip(i_idx.tolist(), j_idx.tolist(), strict=True)) == expected


def test_grid_ordinal_arithmetic_locates_every_point():
    """The downloader indexes grid points by arithmetic instead of a dict."""
    w, h = 9, 5
    i_values, j_values = grid_index_ranges(w, h)
    lats, lons, i_idx, j_idx = generate_grid_arrays(SEATTLE, w, h, 20)
    n_j = len(j_values)

    for ordinal, (i, j) in enumerate(zip(i_idx.tolist(), j_idx.tolist(), strict=True)):
        computed = (i - i_values[0]) * n_j + (j - j_values[0])
        assert computed == ordinal
        assert (lats[computed], lons[computed]) == (lats[ordinal], lons[ordinal])
