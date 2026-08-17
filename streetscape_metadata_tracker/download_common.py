"""Provider-agnostic download helpers shared across streetscape imagery
providers (Google Street View, Mapillary, …).

Grid generation, the common download exception, and capture-date normalization
live here so provider-specific downloaders (`download_gsv.py`,
`download_mapillary.py`, `download_gsv_history.py`) can share them without one
provider importing from another's module.
"""

import asyncio
import re
from collections.abc import Callable, Iterator
from datetime import datetime

import geopy.distance
import numpy as np
from tqdm import tqdm


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

HOST_LABELS = {
    HOST_MAPILLARY_TILES: "Mapillary's tile CDN (tiles.mapillary.com)",
    HOST_OVERPASS: "the Overpass API (overpass-api.de)",
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
HOST_EXIT_CODES = {
    HOST_MAPILLARY_TILES: 75,
    HOST_OVERPASS: 76,
}
HOST_BUSY_EXIT_CODES = {
    HOST_MAPILLARY_TILES: 79,
    HOST_OVERPASS: 80,
}
HOST_BY_EXIT_CODE = {code: host for host, code in HOST_EXIT_CODES.items()}
HOST_BY_BUSY_EXIT_CODE = {code: host for host, code in HOST_BUSY_EXIT_CODES.items()}


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

    Usage:
        limiter = AsyncRateLimiter(24_000)  # 80% of the 30k/min quota
        await limiter.acquire()             # before each request
    """

    def __init__(self, max_per_minute: int, time_func: Callable[[], float] | None = None):
        """
        Args:
            max_per_minute: Maximum acquisitions per minute; <= 0 disables.
            time_func: Monotonic clock returning seconds (defaults to the
                running event loop's clock). Injectable for tests.
        """
        self._enabled = max_per_minute > 0
        self._rate = max_per_minute / 60.0  # tokens per second
        self._capacity = max(self._rate, 1.0)  # ~1 second of burst
        self._tokens = self._capacity
        self._time_func = time_func
        self._last_refill: float | None = None
        self._lock = asyncio.Lock()

    def _now(self) -> float:
        if self._time_func is not None:
            return self._time_func()
        return asyncio.get_running_loop().time()

    async def acquire(self) -> None:
        """Block until a request token is available (no-op when disabled)."""
        if not self._enabled:
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

    with tqdm(total=total_points, desc="Generating search grid points", disable=None) as pbar:
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
