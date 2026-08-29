"""Provider-agnostic download helpers shared across streetscape imagery
providers (Google Street View, Mapillary, …).

Grid generation, the common download exception, and capture-date normalization
live here so provider-specific downloaders (`download_gsv.py`,
`download_mapillary.py`, `download_gsv_history.py`) can share them without one
provider importing from another's module.
"""

import argparse
import asyncio
import random
import re
from collections.abc import Callable, Iterator
from datetime import datetime

import geopy.distance
import numpy as np

from .progress import progress


class DownloadError(Exception):
    """Custom exception for download-related errors."""

    pass


# Third-party hosts that meter us by IP rather than by credential (issue #208).
#
# Deliberately NOT here: GSV metadata. Google meters the Street View Static API
# per *project*, so two processes on one host share a quota that the daily
# ledger already tracks — serializing them would cost throughput and buy
# nothing. Membership in this list means "a second concurrent process on this
# host is a hazard to the whole host", which is only true of per-IP limits.
#
# This is the set we LOCK, not an exhaustive list of our per-IP third parties.
# Two others are per-IP and are knowingly uncovered, both because they are
# low-volume and out-of-band rather than because they are safe:
#   * Nominatim (geoutils.py) — its usage policy is explicitly per-IP with a
#     hard 1 req/s. A nightly batch of frozen cities never touches it: it runs
#     only when an UNKNOWN city is registered, and once per invocation of
#     `scheduler assess-city` (its boundary pre-flight, issue #215, which asks
#     even for an already-registered city). Both are one operator-initiated
#     call at a time, which is why the volume argument still holds; a bulk
#     register_frame.py run alongside a batch is the same collision shape as
#     the ones below.
#   * GeoPhotoService.SingleImageSearch (download_gsv_history.py) — undocumented
#     and IP-identified rather than key-metered, which is why it already carries
#     its own circuit breaker. A harvest is long-running and manual, i.e. the
#     "detached process that cannot read the rule" profile exactly.
# Add them here if either ever runs on the same schedule as a collection.
HOST_MAPILLARY_TILES = "mapillary_tiles"
HOST_OVERPASS = "overpass"
# KartaView documents an hourly ceiling per API key and returns no rate-limit
# headers of any kind, so a client cannot observe its own budget and nothing
# published says the ENFORCED limit is the documented one. Both of this
# project's prior bans were on limits no document described, which is why this
# is locked on the documented-per-key reading rather than exempted by it.
HOST_KARTAVIEW = "kartaview"

HOST_LABELS = {
    HOST_MAPILLARY_TILES: "Mapillary's tile CDN (tiles.mapillary.com)",
    HOST_OVERPASS: "the Overpass API (overpass-api.de)",
    HOST_KARTAVIEW: "the KartaView API (kartaview.org)",
}


class HostUnavailableError(DownloadError):
    """
    A whole-HOST condition: every remaining request to this host would fail
    identically, so the caller should stop rather than work through its queue.

    Distinct from a per-request failure (a 404 on one tile) and from a
    per-credential failure (a rejected token — that is scoped to the key, and
    our two Mapillary channels hold different keys, so it must not be treated
    as host-wide).
    """

    def __init__(self, message: str, host: str):
        super().__init__(message)
        self.host = host


class HostBusyError(HostUnavailableError):
    """Another process ON THIS MACHINE holds the host lock (issue #208)."""


class HostBlockedError(HostUnavailableError):
    """The third party itself is refusing this host's IP (issues #199/#209)."""


# Exit codes a collection child uses to tell the scheduler *which* host is
# unavailable, and in WHICH of the two senses. The message never crosses the
# process boundary — the scheduler only sees `subprocess.run(...).returncode` —
# so both facts have to ride in the status.
#
# The distinction is not cosmetic; the two conditions have opposite lifetimes
# and the scheduler reacts to them differently:
#
#   BLOCKED — the third party is refusing this machine's IP. Durable. Asking
#     again with the next city cannot answer differently, so the first one trips
#     a night-level breaker that skips that host's channels for the rest of the
#     run (scheduler.CHANNEL_HOSTS).
#   BUSY — another process on this box holds the lock. Transient, and it
#     resolves itself the moment that process finishes. Escalating it to the
#     breaker would mean a 90-second manual run costs the batch every Mapillary
#     city of the night, so the scheduler skips only that one channel of that
#     one city — and still alerts, because a city that quietly did not collect
#     is the failure mode #145 exists to prevent.
#
# 75/76 sit in sysexits.h's EX_TEMPFAIL (75) range by analogy: a retryable,
# environmental condition rather than a bug. The busy codes deliberately sit
# PAST the end of sysexits (which stops at 78) so they carry no false analogy —
# 77/78 would silently read as EX_NOPERM/EX_CONFIG to anyone who looks them up.
#
# 77 and 78 are skipped for the same reason: they are EX_NOPERM and EX_CONFIG,
# so a third host taking 77 would read to anyone looking it up as "permission
# denied" — a plausible-sounding wrong answer, which is worse than an
# unallocated number. The families are defined by these dicts rather than by
# being contiguous, so the numbering has a gap and each entry is justified.
HOST_EXIT_CODES = {
    HOST_MAPILLARY_TILES: 75,
    HOST_OVERPASS: 76,
    HOST_KARTAVIEW: 81,
}
HOST_BUSY_EXIT_CODES = {
    HOST_MAPILLARY_TILES: 79,
    HOST_OVERPASS: 80,
    HOST_KARTAVIEW: 82,
}
HOST_BY_EXIT_CODE = {code: host for host, code in HOST_EXIT_CODES.items()}
HOST_BY_BUSY_EXIT_CODE = {code: host for host, code in HOST_BUSY_EXIT_CODES.items()}

# A collection stopped part-way and CHECKPOINTED what it had (issue #239). Not a
# host condition in either sense above: the third party answered every question
# it was asked, and nothing about this machine is refusing anything. What ran out
# was the night — a request budget, a deadline, a supervisor's stop — and the
# work is on disk waiting for tomorrow.
#
# It is a third family rather than an entry in either dict for exactly that
# reason. A BLOCKED code trips the night-level breaker and skips that host's
# remaining cities, which would be badly wrong here: the next city's sweep is
# unaffected by this city's budget. A BUSY code says "another local process holds
# the lock", which is a different fact an operator would chase differently.
#
# 83 continues past the busy family and past sysexits.h's end (78), so like 79-82
# it carries no false EX_* analogy. The scheduler branch this exists for does not
# exist yet — KartaView is deliberately unwired — but the number is allocated
# here, beside the rationale for the numbering, rather than chosen in a later PR
# that would have to rediscover it. What that branch must do when it lands:
# `continue` WITHOUT `record_attempt(success=False)`, exactly like the busy
# branch, because `get_due_cities` filters on `consecutive_failures <
# max_consecutive_failures` (5) and nothing resets that counter but a success —
# so a city that legitimately needs three nights would quarantine itself for a
# whole cycle for making progress.
SWEEP_INCOMPLETE_EXIT_CODE = 83


def host_exit_code(error: HostUnavailableError) -> int:
    """
    The exit code a collection child should return for ``error``.

    The single place the busy/blocked distinction becomes a number, so the three
    child entry points (``cli.py``, ``collect.py``, ``analyze.py``) cannot drift
    from each other or from the scheduler's reverse maps.
    """
    if isinstance(error, HostBusyError):
        return HOST_BUSY_EXIT_CODES[error.host]
    return HOST_EXIT_CODES[error.host]


def _unit_exponential() -> float:
    """A draw from the unit exponential distribution (mean 1, CV 1).

    The default jitter sampler for :class:`AsyncRateLimiter` (issue #292).
    ``lambd`` is passed explicitly because it only became optional in Python
    3.12 and this project still runs on 3.11.
    """
    return random.expovariate(1.0)


class AsyncRateLimiter:
    """
    Token-bucket rate limiter for provider APIs with a per-minute quota
    (e.g. GSV metadata's 30,000 requests/minute project cap).

    Tokens refill continuously at ``max_per_minute / 60`` per second with a
    burst capacity of ~1 second's worth, so short spikes are smoothed rather
    than letting a fast host blow through the provider's minute window.
    ``max_per_minute <= 0`` disables limiting entirely.

    Waiters queue on an internal lock, so acquisition order is FIFO and the
    aggregate rate holds no matter how many tasks call ``acquire()``
    concurrently.

    With ``jitter > 0`` the bucket is replaced by a **spaced pacer**
    (issue #292): each acquisition waits out the previous one's gap, and the
    gap is drawn from a *shifted exponential* around the mean gap
    ``m = 60 / max_per_minute``::

        gap = m * ((1 - jitter) + jitter * Exponential(1))

    i.e. a fixed floor of ``(1 - jitter) * m`` plus an exponential tail scaled
    to ``jitter * m``. A saturated token bucket emits at an exact ``m`` cadence,
    which is the one property of our Mapillary traffic that three per-IP blocks
    left untested (rate and daily volume were both falsified, #286).

    Why *this* distribution rather than the uniform draw #292 first shipped:
    an organic client's request arrivals are Poisson, whose inter-arrival gaps
    are exponential with a **coefficient of variation of 1.0**. A uniform draw
    on ``[1 - j, 1 + j]`` reaches only ``j / sqrt(3)`` (0.35 at j = 0.6) and,
    worse, keeps a hard ceiling — under it no pause ever exceeds ``(1 + j) * m``,
    so a scorer reading gap *statistics* rather than gap *equality* sees nearly
    what the metronome showed it. The shifted exponential is memoryless above
    the floor, is unbounded above, and has ``CV = jitter`` exactly.

    Every invariant the uniform version had is preserved, which is why the
    parameter did not have to change meaning:

    * **Mean gap is exactly** ``m`` (``E[Exponential(1)] = 1``), so the mean rate
      is still ``max_per_minute`` and the daily budgets and the rate-derived
      scheduler timeout keep their arithmetic. The tail widens the *variance* of
      a run's wall clock, not its expectation: over a 1,750-request night the
      standard deviation of the total is ``sqrt(N) * jitter * m`` — under a
      minute against ~44, far inside ``_TIMEOUT_HEADROOM``. The gap is
      deliberately **not** capped: a cap is a ceiling, and the ceiling is the
      artifact being removed.
    * **Minimum gap is exactly** ``(1 - jitter) * m``, so ``jitter`` still reads
      as "how far below the mean a gap may fall" and the ``jitter < 1`` bound is
      literally what keeps the gap above zero — at ``jitter = 1`` the floor
      vanishes and the draw admits an unpaced burst.
    * ``jitter = 0`` collapses the draw to a constant ``m``, so the pacer is
      continuous with the token bucket it replaces (and is short-circuited to it
      anyway, byte for byte, so GSV and KartaView are untouched).

    Because the pacer is FIFO under one lock, ``connection_limit`` concurrency
    cannot smooth the jitter back out.

    Usage:
        limiter = AsyncRateLimiter(24_000)  # 80% of the 30k/min quota
        await limiter.acquire()             # before each request

        # 40/min: gaps floored at 0.6 s, mean 1.5 s, p99 ~4.7 s, no ceiling
        limiter = AsyncRateLimiter(40, jitter=0.6)
    """

    def __init__(
        self,
        max_per_minute: int,
        time_func: Callable[[], float] | None = None,
        *,
        jitter: float = 0.0,
        draw_func: Callable[[], float] | None = None,
    ):
        """
        Args:
            max_per_minute: Maximum acquisitions per minute; <= 0 disables.
            time_func: Monotonic clock returning seconds (defaults to the
                running event loop's clock). Injectable for tests.
            jitter: Fraction in ``[0, 1)``. 0 keeps the token bucket; anything
                above switches to the spaced pacer, and is both the exponential
                tail's share of the mean gap and the resulting coefficient of
                variation. 1 or more would erase the floor and admit a zero gap,
                i.e. an unpaced burst, so it is refused.
            draw_func: ``() -> float`` sampler from the **unit exponential**
                distribution (mean 1), used for the jitter. Defaults to
                :func:`random.expovariate`. Injectable for deterministic tests;
                a caller that substitutes a differently-scaled distribution
                breaks the mean-rate guarantee above.
        """
        if not 0.0 <= jitter < 1.0:
            raise ValueError(f"jitter must be in [0, 1), got {jitter!r}")
        self._enabled = max_per_minute > 0
        self._rate = max_per_minute / 60.0  # tokens per second
        self._capacity = max(self._rate, 1.0)  # ~1 second of burst
        self._tokens = self._capacity
        self._time_func = time_func
        self._last_refill: float | None = None
        self._lock = asyncio.Lock()
        self.jitter = jitter
        self._draw = draw_func or _unit_exponential
        # Spaced-pacer state: the clock time the next request may go out. None
        # until the first acquisition, which never waits.
        self._next_at: float | None = None

    def _now(self) -> float:
        if self._time_func is not None:
            return self._time_func()
        return asyncio.get_running_loop().time()

    async def acquire(self) -> None:
        """Block until a request token is available (no-op when disabled)."""
        if not self._enabled:
            return
        if self.jitter > 0:
            await self._acquire_spaced()
            return
        async with self._lock:
            now = self._now()
            if self._last_refill is None:
                self._last_refill = now
            self._tokens = min(
                self._capacity, self._tokens + (now - self._last_refill) * self._rate
            )
            self._last_refill = now
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return
            # Sleep while holding the lock: later waiters must queue behind
            # this one anyway, and releasing would let them busy-cycle.
            wait = (1.0 - self._tokens) / self._rate
            await asyncio.sleep(wait)
            self._last_refill = self._now()
            self._tokens = 0.0

    async def _acquire_spaced(self) -> None:
        # Sleep while holding the lock, for the same reason as the bucket: the
        # waiters behind this one must queue behind its gap anyway, and the
        # gap is drawn AFTER the sleep so every request's delay is fresh
        # randomness rather than one draw replayed by a burst of waiters.
        async with self._lock:
            now = self._now()
            if self._next_at is not None and self._next_at > now:
                await asyncio.sleep(self._next_at - now)
                now = self._now()
            # Shifted exponential: a fixed (1 - jitter) floor plus an
            # exponential tail scaled to jitter. Mean is exactly the mean gap
            # because E[Exponential(1)] = 1; see the class docstring.
            mean_gap = 1.0 / self._rate
            gap = mean_gap * ((1.0 - self.jitter) + self.jitter * self._draw())
            self._next_at = now + gap


def jitter_fraction(value: str) -> float:
    """
    argparse type for a pacing jitter: a float in ``[0, 1)`` (issue #292).

    Shared by both CLIs' ``--mapillary-jitter`` so the two cannot drift on what
    the bound is, and by ``scheduler.load_scheduler_config`` through
    :func:`coerce_jitter` so a config file cannot smuggle past it either. 1 or
    more erases :class:`AsyncRateLimiter`'s gap floor and admits a zero gap — an
    unpaced burst against the per-IP tile CDN — so it is refused at parse time
    rather than at the first request.

    Usage:
        parser.add_argument("--mapillary-jitter", type=jitter_fraction, default=0.6)
    """
    try:
        number = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"must be a number, got {value!r}") from None
    if not 0.0 <= number < 1.0:
        raise argparse.ArgumentTypeError(f"must be in [0, 1), got {number}")
    return number


def coerce_jitter(value: object) -> float | None:
    """
    The scheduler-config half of :func:`jitter_fraction`: coerce a TOML value to
    a valid jitter, or ``None`` if it is not one (issue #292).

    Lives here, beside the argparse type and the limiter's own guard, so the
    three cannot disagree about what a valid jitter is. Returns ``None`` rather
    than raising because the caller's fallback for a bad field is "use the
    collector's own default", which is what ``None`` means downstream — and
    never ``0``, which means "restore the exact cadence".

    Usage:
        jitter = coerce_jitter(p.get("jitter"))  # None on absent or invalid
    """
    if value is None:
        return None
    # bool BEFORE float(): a bool is an int, so `jitter = false` would coerce to
    # a perfectly valid 0.0 and silently mean "restore the metronome" — a
    # reading of that TOML nobody writing it intends, and the one wrong answer
    # here that runs a whole night without complaining.
    if isinstance(value, bool):
        return None
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if not 0.0 <= number < 1.0:
        return None
    return number


# Credential-bearing query parameters (GSV's key=, Mapillary's
# access_token=) as they appear inside request URLs.
#
# The `%3f`/`%26`/`%3d` alternatives cover a URL that has been percent-encoded
# *into another URL's query string*, which is exactly the shape a redirect's
# Location takes: `.../login/?next=https%3A%2F%2F…%3Faccess_token%3DMLY…`
# (issue #199). Matching only the literal `?key=` form let that through
# unscrubbed — and unlike an ordinary log line, a Location is echoed straight
# back from the provider with the token still in it.
#
# The value pattern deliberately does not stop at an encoded `&` (`%26`), so an
# encoded URL is redacted through to its end. Over-redaction is the safe
# direction: this text reaches logs and cleartext alert emails.
_CREDENTIAL_PATTERN = re.compile(r"(?i)(\b|%3f|%26)(key|access_token|token)(?:=|%3d)[^&\s'\"]+")


def redact_credentials(text: str) -> str:
    """
    Strip API credentials from text destined for logs, exceptions, or
    alert emails.

    Provider APIs carry credentials in URL query parameters, and HTTP
    client exceptions (e.g. aiohttp.ClientResponseError) stringify with the
    full request URL — so any raw ``str(e)`` that gets logged can leak the
    key, and the scheduler pastes log tails into operator alert emails.
    Every log/raise of provider-HTTP error text must pass through here.

    Handles the percent-encoded form too, since a redirect's Location carries
    the request URL nested inside its own query string:

    >>> redact_credentials("HTTP 403 for https://x/tile?access_token=MLY123")
    'HTTP 403 for https://x/tile?access_token=REDACTED'
    >>> redact_credentials("https://x/login/?next=https%3A%2F%2Fy%3Faccess_token%3DMLY123")
    'https://x/login/?next=https%3A%2F%2Fy%3Faccess_token=REDACTED'
    """
    return _CREDENTIAL_PATTERN.sub(r"\1\2=REDACTED", str(text))


def grid_index_ranges(width_steps: int, height_steps: int) -> tuple[range, range]:
    """The (i, j) index ranges of a grid, in the order points are generated."""
    return (
        range(-height_steps // 2, height_steps // 2 + 1),
        range(-width_steps // 2, width_steps // 2 + 1),
    )


def _grid_rows(
    origin: geopy.Point, width_steps: int, height_steps: int, step_length: float
) -> Iterator[tuple[int, list[float], list[float]]]:
    """
    Yield one grid ROW at a time as ``(i, row_lats, row_lons)``.

    Shared by the two public generators below so the per-point geodesic math
    lives in exactly one place. Grid geometry is FROZEN — every future run of a
    city re-derives these same coordinates so its diffs align on an identical
    rectangle — so this math must never drift. The one change from the original
    nested loop is hoisting the northward displacement out of the inner loop,
    where it never depended on ``j``: identical output, half the geodesic
    solves. That is worth real time on a big city (the solve is pure-Python
    geographiclib, and a 10M-point grid spent ~13 minutes here before a single
    tile was fetched).
    """
    i_values, j_values = grid_index_ranges(width_steps, height_steps)
    total_points = (width_steps + 1) * (height_steps + 1)

    with progress(total=total_points, desc="Generating search grid points") as pbar:
        for i in i_values:
            north_point = geopy.distance.distance(meters=i * step_length).destination(origin, 0)
            row_lats: list[float] = []
            row_lons: list[float] = []
            for j in j_values:
                point = geopy.distance.distance(meters=j * step_length).destination(north_point, 90)
                row_lats.append(point.latitude)
                row_lons.append(point.longitude)
            pbar.update(len(j_values))
            yield i, row_lats, row_lons


def generate_grid_points(
    origin: geopy.Point, width_steps: int, height_steps: int, step_length: float
) -> list[tuple[float, float, int, int]]:
    """
    Generate all grid points for the search area with progress bar.

    Args:
        origin: Center point of the grid
        width_steps: Number of steps in width direction
        height_steps: Number of steps in height direction
        step_length: Distance between points in meters

    Returns:
        List of tuples containing (latitude, longitude, i, j) for each point
    """
    _, j_values = grid_index_ranges(width_steps, height_steps)
    points = []
    for i, row_lats, row_lons in _grid_rows(origin, width_steps, height_steps, step_length):
        points.extend(
            (lat, lon, i, j) for lat, lon, j in zip(row_lats, row_lons, j_values, strict=True)
        )
    return points


def generate_grid_arrays(
    origin: geopy.Point, width_steps: int, height_steps: int, step_length: float
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """
    The same grid as :func:`generate_grid_points`, as ``(lats, lons, i, j)``
    arrays in the same order.

    For a caller that only needs coordinates, this never materializes the list
    of Python tuples — which at Cairo's ~10.5M points is about 2.2 GB on its
    own, against a cgroup capped at 8 GB (issue #157). The arrays are ~24 bytes
    per point instead of ~120.
    """
    i_values, j_values = grid_index_ranges(width_steps, height_steps)
    n_i, n_j = len(i_values), len(j_values)

    lats = np.empty(n_i * n_j, dtype=np.float64)
    lons = np.empty(n_i * n_j, dtype=np.float64)
    pos = 0
    for _i, row_lats, row_lons in _grid_rows(origin, width_steps, height_steps, step_length):
        lats[pos : pos + n_j] = row_lats
        lons[pos : pos + n_j] = row_lons
        pos += n_j

    i_idx = np.repeat(np.fromiter(i_values, dtype=np.int32, count=n_i), n_j)
    j_idx = np.tile(np.fromiter(j_values, dtype=np.int32, count=n_j), n_i)
    return lats, lons, i_idx, j_idx


# ── Grid geometry: shared by every provider ──────────────────
#
# These three lived in download_mapillary.py, which made them unreachable to a
# second census provider without a provider-to-provider import (KartaView had
# resorted to a function-local one). They are pure geodesy over the frozen grid
# and know nothing about tiles, so they belong beside generate_grid_arrays.


# Meters per degree of latitude (WGS84 mean). Kept for rough offset math in
# tests/estimates; the actual grid assignment uses the latitude-local series
# below (the mean constant mis-assigned edge panos by whole grid rows —
# ~0.7% error at the equator is +1 row at 2.5 km from center).
_M_PER_DEG_LAT = 111320.0


def _meters_per_degree(lat_deg):
    """
    (m_per_deg_lat, m_per_deg_lon) at a latitude, via the standard WGS84
    series expansion. Accepts scalars or numpy arrays. Matches the geodesic
    math that builds the grid to well under a meter over a city-sized area,
    so nearest-grid-point assignment can't drift by rows near the edges.
    """
    phi = np.radians(lat_deg)
    m_lat = (
        111132.92 - 559.82 * np.cos(2 * phi) + 1.175 * np.cos(4 * phi) - 0.0023 * np.cos(6 * phi)
    )
    m_lon = 111412.84 * np.cos(phi) - 93.5 * np.cos(3 * phi) + 0.118 * np.cos(5 * phi)
    return m_lat, m_lon


def grid_bbox(
    center_lat: float, center_lon: float, grid_width: float, grid_height: float, step_length: float
) -> tuple[float, float, float, float]:
    """
    (min_lon, min_lat, max_lon, max_lat) covering the sampling grid plus a
    half-step margin, computed with the same geodesic math that builds the
    grid so the two always agree. The margin admits images that lie just
    outside the outermost grid points but are still nearest to them.
    """
    origin = geopy.Point(center_lat, center_lon)
    half_h = grid_height / 2 + step_length / 2
    half_w = grid_width / 2 + step_length / 2
    north = geopy.distance.distance(meters=half_h).destination(origin, 0)
    south = geopy.distance.distance(meters=half_h).destination(origin, 180)
    east = geopy.distance.distance(meters=half_w).destination(origin, 90)
    west = geopy.distance.distance(meters=half_w).destination(origin, 270)
    return west.longitude, south.latitude, east.longitude, north.latitude


def assign_to_grid(
    image_lats: np.ndarray,
    image_lons: np.ndarray,
    center_lat: float,
    center_lon: float,
    width_steps: int,
    height_steps: int,
    step_length: float,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """
    Vectorized nearest-grid-point assignment.

    The grid is a regular lattice of step_length meters indexed by
    (i, j) = (north, east) offsets from the center (see
    generate_grid_points), so the nearest point is just a rounded division
    in a local equirectangular projection — no spatial index needed.

    Returns (i, j, in_grid) arrays; in_grid is False for images farther
    than half a step beyond the outermost grid points, which the caller
    drops.
    """
    # Latitude-local scales: the grid is built geodesically, so a global
    # mean m/° mis-assigns by whole rows near the grid edges. dy uses the
    # series at the center↔image midpoint latitude; dx uses each image's
    # own latitude (grid rows are constant-latitude, and their east-west
    # spacing shrinks with cos φ at THAT row, not at the center).
    m_lat_mid, _ = _meters_per_degree((image_lats + center_lat) / 2)
    _, m_lon_local = _meters_per_degree(image_lats)
    dy_m = (image_lats - center_lat) * m_lat_mid
    dx_m = (image_lons - center_lon) * m_lon_local
    i = np.rint(dy_m / step_length).astype(int)
    j = np.rint(dx_m / step_length).astype(int)

    # Replicate generate_grid_points' index ranges exactly (note: Python
    # floor division makes the ranges asymmetric for odd step counts).
    i_min, i_max = -height_steps // 2, height_steps // 2
    j_min, j_max = -width_steps // 2, width_steps // 2
    in_grid = (i >= i_min) & (i <= i_max) & (j >= j_min) & (j <= j_max)
    return i, j, in_grid


def standardize_capture_date(date_str: str | None) -> str | None:
    """Standardizes a capture date string to ISO 8601 format (YYYY-MM-DD).

    Providers return capture dates in various granularities (YYYY-MM-DD, YYYY-MM,
    or YYYY). This function attempts to parse the input date string using several
    common formats and converts it to a standard ISO 8601 date string.

    Args:
        date_str: The capture date string from the API response. Can be None.

    Returns:
        A string representing the date in ISO 8601 format (YYYY-MM-DD), or None if
        the input is None or if no matching format is found.
    """
    if not date_str:  # Handle None or empty strings
        return None

    formats_to_try = [
        "%Y-%m-%d",  # Most precise format (YYYY-MM-DD), try first
        "%Y-%m",  # Year and month (YYYY-MM)
        "%Y",  # Year only (YYYY)
    ]

    for fmt in formats_to_try:
        try:
            date_obj = datetime.strptime(date_str, fmt).date()  # Parse the date
            return date_obj.isoformat()  # Convert to ISO 8601 format (YYYY-MM-DD)
        except ValueError:
            continue  # If parsing fails, try the next format

    return None  # Return None if no format matches
