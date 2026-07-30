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
_CREDENTIAL_PATTERN = re.compile(r"(?i)\b(key|access_token|token)=[^&\s'\"]+")


def redact_credentials(text: str) -> str:
    """
    Strip API credentials from text destined for logs, exceptions, or
    alert emails.

    Provider APIs carry credentials in URL query parameters, and HTTP
    client exceptions (e.g. aiohttp.ClientResponseError) stringify with the
    full request URL — so any raw ``str(e)`` that gets logged can leak the
    key, and the scheduler pastes log tails into operator alert emails.
    Every log/raise of provider-HTTP error text must pass through here.

    >>> redact_credentials("HTTP 403 for https://x/tile?access_token=MLY123")
    'HTTP 403 for https://x/tile?access_token=REDACTED'
    """
    return _CREDENTIAL_PATTERN.sub(r"\1=REDACTED", str(text))


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

    with tqdm(total=total_points, desc="Generating search grid points") as pbar:
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
