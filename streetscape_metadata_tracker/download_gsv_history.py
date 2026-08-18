"""
Historical Street View capture-date harvester (issue #2).

A normal GSV run answers only "what's the nearest pano to point X, and when was
it taken?" — one *current* date per grid point. Google's own time-travel
feature, though, exposes the FULL capture history at a location: every official
Street View drive-through and its date. That history is not in any documented
Google API (free or paid). It is returned by an **unpublished endpoint**
(`GeoPhotoService.SingleImageSearch`, the backend the Maps JS
`StreetViewService.getPanorama().time[]` array reads from), which we query
directly here — no API key, no browser.

Because the endpoint is undocumented there is **no guarantee it keeps working**,
and it is identified by our IP rather than a metered key, so this harvester is
deliberately the opposite of the aggressive per-point run downloader:

  * low concurrency + randomized inter-request jitter (polite, not a firehose);
  * exponential backoff on timeouts / throttle responses (429/403/503);
  * a circuit breaker that aborts gracefully on a run of throttle responses
    rather than hammering a wall (cf. analysis.detect_systemic_failure);
  * a resumable `.harvesting` checkpoint so an abort mid-sweep loses no work.

Collection model:
  * We sweep the city's FROZEN sampling grid, one search per grid point.
  * Each search returns a neighbourhood of panoramas; only those carrying a
    capture DATE are kept — in this endpoint a present date is the signal of
    official Google imagery (user photospheres come back undated). This is the
    endpoint-native analogue of the `© Google` filter used elsewhere.
  * Kept panos are de-duplicated by pano_id across the whole grid, so the
    output is a census of every official Google panorama (with its capture
    month) discoverable in the city — the "all previous capture dates for an
    area" issue #2 asked for.

History is near-static (Google's archive), so a city is harvested ONCE and only
re-swept occasionally; this is not part of the cadenced run series.

The parsing of the endpoint's (undocumented, deeply-nested) response is adapted
from the MIT-licensed `robolyst/streetview` package.
"""

import asyncio
import gzip
import json
import logging
import os
import random
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiohttp
import backoff
import geopy
import numpy as np
import pandas as pd

from .download_common import DownloadError, generate_grid_points, standardize_capture_date
from .progress import progress

logger = logging.getLogger(__name__)

# Output schema for a harvest file — one row per unique official Google pano.
# Distinct from config.METADATA_DTYPES (a per-grid-point run sample); this is a
# per-pano historical census, so it gets its own columns.
HISTORY_DTYPES = {
    "pano_id": str,
    "capture_date": str,  # ISO YYYY-MM-DD (day defaults to 01;
    # the endpoint's precision is a month)
    "pano_lat": np.float64,
    "pano_lon": np.float64,
    "nearest_query_lat": np.float64,  # grid point whose search first
    "nearest_query_lon": np.float64,  # surfaced this pano
    "harvested_at": str,  # UTC ISO 8601
}

_SEARCH_URL = (
    "https://maps.googleapis.com/maps/api/js/GeoPhotoService.SingleImageSearch"
    "?pb=!1m5!1sapiv3!5sUS!11m2!1m1!1b0!2m4!1m2!3d{lat}!4d{lon}!2d50!3m10"
    "!2m2!1sen!2sUS!9m1!1e2!11m4!1m3!1e2!2b1!3e2!4m10!1e1!1e2!1e3!1e4"
    "!1e8!1e6!5m1!1e2!6m1!1e2"
    "&callback=callbackfunc"
)
_CALLBACK_RE = re.compile(r"callbackfunc\( (.*) \)$", re.DOTALL)
_NO_IMAGES_SENTINEL = [[5, "generic", "Search returned no images."]]

# A realistic browser UA; the endpoint is meant to be called from Maps JS.
_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0 Safari/537.36"
)


@dataclass(frozen=True)
class HistoricalPano:
    """One official Google panorama and its capture month."""

    pano_id: str
    capture_date: str  # standardized ISO 'YYYY-MM-DD'
    lat: float
    lon: float


class HarvestBlockedError(DownloadError):
    """
    Raised when the endpoint appears to be throttling/blocking us (a run of
    failed searches tripped the circuit breaker). The `.harvesting` checkpoint
    is left in place so the sweep can resume later.
    """


class HarvestIncompleteError(HarvestBlockedError):
    """
    Raised when isolated point failures survive all retry passes (without
    tripping the breaker). The sweep must NOT finalize: writing the final
    csv.gz and deleting the checkpoint would silently publish a harvest
    with holes — and lose the record that anything was skipped — for an
    endpoint that may disappear before a re-sweep. Subclasses
    HarvestBlockedError so existing resume/alert handling applies.
    """


def build_search_url(lat: float, lon: float) -> str:
    """URL of the unpublished single-image-search endpoint for a coordinate."""
    return _SEARCH_URL.format(lat=lat, lon=lon)


def parse_search_response(text: str) -> list[HistoricalPano]:
    """
    Extract dated official-Google panoramas from one endpoint response.

    The response body is a JS callback wrapping a deeply-nested array. Only
    panoramas that carry a capture date are returned (undated entries are user
    contributions, not the official history). Any structural surprise — a shape
    change from this undocumented endpoint — is swallowed and yields an empty
    list rather than raising, so one odd response can't abort a sweep.

    Adapted from robolyst/streetview (MIT).
    """
    m = _CALLBACK_RE.search(text.strip())
    if not m:
        return []
    try:
        data = json.loads(m.group(1))
    except (ValueError, json.JSONDecodeError):
        return []
    if not data or data == _NO_IMAGES_SENTINEL:
        return []

    try:
        subset = data[1][5][0]
        raw_panos = subset[3][0]
        # Dates cover only the last n panos; reverse both so indices align.
        raw_dates = subset[8] if (len(subset) > 8 and subset[8]) else []
        raw_panos = raw_panos[::-1]
        raw_dates = raw_dates[::-1]
    except (IndexError, KeyError, TypeError):
        return []

    dates: list[str] = []
    for d in raw_dates:
        try:
            dates.append(f"{d[1][0]}-{int(d[1][1]):02d}")
        except (IndexError, KeyError, TypeError, ValueError):
            dates.append("")

    out: list[HistoricalPano] = []
    for i, pano in enumerate(raw_panos):
        raw_date = dates[i] if i < len(dates) else ""
        iso = standardize_capture_date(raw_date)
        if not iso:
            continue  # no date -> not official Google history; skip
        try:
            pano_id = pano[0][1]
            lat = pano[2][0][2]
            lon = pano[2][0][3]
        except (IndexError, KeyError, TypeError):
            continue
        out.append(
            HistoricalPano(pano_id=str(pano_id), capture_date=iso, lat=float(lat), lon=float(lon))
        )
    return out


class _CircuitBreaker:
    """
    Trips after `limit` consecutive failed searches — the signature of being
    throttled/blocked, as opposed to isolated points with genuinely no imagery
    (those succeed with an empty result and reset the counter).
    """

    def __init__(self, limit: int = 8):
        self.limit = limit
        self.consecutive = 0
        self.total_failures = 0

    def record(self, ok: bool) -> None:
        if ok:
            self.consecutive = 0
        else:
            self.consecutive += 1
            self.total_failures += 1

    @property
    def tripped(self) -> bool:
        return self.consecutive >= self.limit


@backoff.on_exception(
    backoff.expo, (asyncio.TimeoutError, aiohttp.ClientError), max_tries=4, max_time=90
)
async def _fetch_search(
    session: aiohttp.ClientSession, url: str, timeout: aiohttp.ClientTimeout
) -> str:
    """
    One search request with retry/backoff. Throttle-ish statuses (429/403/503)
    raise ClientResponseError so backoff retries with growing delay; if the
    server sends Retry-After we wait at least that long first.
    """
    async with session.get(url, timeout=timeout) as resp:
        if resp.status in (429, 403, 503):
            retry_after = resp.headers.get("Retry-After")
            if retry_after:
                try:
                    await asyncio.sleep(min(float(retry_after), 30.0))
                except ValueError:
                    pass
            resp.raise_for_status()
        if resp.status != 200:
            resp.raise_for_status()
        return await resp.text()


# ── Checkpoint (resume) ────────────────────────────────────────────────────


def _checkpoint_path(output_csv_gz_path: str) -> str:
    return output_csv_gz_path + ".harvesting"


def _load_checkpoint(path: str) -> tuple[set, dict[str, dict], int]:
    """Return (done grid indices, panos-by-id, api_requests) from a checkpoint."""
    if not os.path.exists(path):
        return set(), {}, 0
    try:
        with open(path, encoding="utf-8") as f:
            state = json.load(f)
        done = {tuple(ij) for ij in state.get("done", [])}
        panos = state.get("panos", {})
        api_requests = int(state.get("api_requests", 0))
        logger.info(
            f"Resuming harvest from {path}: "
            f"{len(done)} grid points already done, "
            f"{len(panos)} panos so far"
        )
        return done, panos, api_requests
    except (ValueError, OSError, KeyError):
        logger.warning(f"Ignoring unreadable checkpoint {path}")
        return set(), {}, 0


def _save_checkpoint(path: str, done: set, panos: dict[str, dict], api_requests: int) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(
            {"done": [list(ij) for ij in done], "panos": panos, "api_requests": api_requests}, f
        )
    os.replace(tmp, path)  # atomic


# ── Harvest ────────────────────────────────────────────────────────────────


async def harvest_gsv_history_async(
    city_name: str,
    center_lat: float,
    center_lon: float,
    grid_width: float,
    grid_height: float,
    step_length: float,
    output_csv_gz_path: str,
    connection_limit: int = 2,
    request_timeout: float = 30,
    jitter_seconds: tuple[float, float] = (0.2, 0.6),
    chunk_size: int = 25,
    circuit_breaker_limit: int = 8,
    retry_passes: int = 2,
) -> dict[str, Any]:
    """
    Sweep a city's frozen grid and harvest every official Google panorama's
    capture date from the unpublished single-image-search endpoint.

    Same calling convention as the run downloaders: the caller decides the
    output filename. Deliberately gentle (low `connection_limit`, per-request
    jitter, chunked with checkpointing). Resumes from a `.harvesting` sibling
    if one exists.

    Returns a dict with: df, filename_with_path, api_requests, grid_points,
    unique_panos, oldest_capture_date, newest_capture_date, started_at,
    finished_at.

    Raises HarvestBlockedError if the endpoint appears to be throttling us (the
    checkpoint is kept for a later resume).
    """
    started_at = datetime.now(UTC).isoformat()
    if not output_csv_gz_path.endswith(".csv.gz"):
        raise ValueError(f"output_csv_gz_path must end in .csv.gz, got: {output_csv_gz_path}")
    Path(os.path.dirname(os.path.abspath(output_csv_gz_path))).mkdir(parents=True, exist_ok=True)

    width_steps = int(grid_width / step_length)
    height_steps = int(grid_height / step_length)
    origin = geopy.Point(center_lat, center_lon)
    grid_points = generate_grid_points(origin, width_steps, height_steps, step_length)

    checkpoint = _checkpoint_path(output_csv_gz_path)
    done, panos, api_requests = _load_checkpoint(checkpoint)
    remaining = [(lat, lon, i, j) for (lat, lon, i, j) in grid_points if (i, j) not in done]
    logger.info(
        f"Harvesting GSV history for {city_name}: {len(remaining)} of "
        f"{len(grid_points)} grid points to query "
        f"(connection_limit={connection_limit}, gentle mode)"
    )

    breaker = _CircuitBreaker(limit=circuit_breaker_limit)
    semaphore = asyncio.Semaphore(connection_limit)
    timeout = aiohttp.ClientTimeout(total=request_timeout)
    progress_bar = progress(
        total=len(grid_points),
        initial=len(done),
        desc=f"Harvesting GSV history for {city_name}",
        unit="point",
        # A full-grid sweep against a deliberately gentle, jittered endpoint, so
        # this is the slowest thing in the repo per request. Ticks are the only
        # way to tell a live harvest from a wedged one in a redirected log.
        logger=logger,
    )

    async def query_point(session, lat, lon, i, j):
        nonlocal api_requests
        async with semaphore:
            await asyncio.sleep(random.uniform(*jitter_seconds))
            api_requests += 1
            try:
                text = await _fetch_search(session, build_search_url(lat, lon), timeout)
            except (TimeoutError, aiohttp.ClientError) as e:
                breaker.record(ok=False)
                return (i, j, lat, lon, None, e)
        breaker.record(ok=True)
        return (i, j, lat, lon, parse_search_response(text), None)

    headers = {"User-Agent": _USER_AGENT}
    blocked = False
    async with aiohttp.ClientSession(headers=headers) as session:
        # Initial sweep plus up to `retry_passes` re-sweeps of the points
        # that failed (isolated failures never re-queried used to be
        # silently finalized as holes in the harvest).
        for sweep in range(1 + retry_passes):
            pending = [(lat, lon, i, j) for (lat, lon, i, j) in remaining if (i, j) not in done]
            if not pending or blocked:
                break
            if sweep > 0:
                logger.info(
                    f"Retry pass {sweep}/{retry_passes}: re-querying {len(pending)} failed points"
                )
            for start in range(0, len(pending), chunk_size):
                chunk = pending[start : start + chunk_size]
                results = await asyncio.gather(
                    *(query_point(session, lat, lon, i, j) for (lat, lon, i, j) in chunk)
                )
                for i, j, lat, lon, found, err in results:
                    if err is not None:
                        continue  # failed point; stays out of `done` for the next pass
                    progress_bar.update(1)
                    done.add((i, j))
                    for p in found:
                        # First grid point to surface a pano wins its query coords;
                        # keep the earliest capture_date if the same id recurs.
                        existing = panos.get(p.pano_id)
                        if existing is None or p.capture_date < existing["capture_date"]:
                            panos[p.pano_id] = {
                                "capture_date": p.capture_date,
                                "pano_lat": p.lat,
                                "pano_lon": p.lon,
                                "nearest_query_lat": lat,
                                "nearest_query_lon": lon,
                            }
                _save_checkpoint(checkpoint, done, panos, api_requests)
                if breaker.tripped:
                    blocked = True
                    break
    progress_bar.close()

    if blocked:
        raise HarvestBlockedError(
            f"Harvest aborted for {city_name}: {breaker.consecutive} consecutive "
            f"failed searches suggest the endpoint is throttling us. Progress "
            f"saved to {checkpoint}; rerun to resume."
        )

    unfinished = len(grid_points) - len(done)
    if unfinished > 0:
        raise HarvestIncompleteError(
            f"Harvest incomplete for {city_name}: {unfinished} grid points "
            f"still failed after {retry_passes} retry passes. Refusing to "
            f"finalize a harvest with holes; progress saved to {checkpoint}; "
            f"rerun to resume."
        )

    harvested_at = datetime.now(UTC).isoformat()
    rows = [
        {
            "pano_id": pid,
            "capture_date": rec["capture_date"],
            "pano_lat": rec["pano_lat"],
            "pano_lon": rec["pano_lon"],
            "nearest_query_lat": rec["nearest_query_lat"],
            "nearest_query_lon": rec["nearest_query_lon"],
            "harvested_at": harvested_at,
        }
        for pid, rec in panos.items()
    ]
    df = pd.DataFrame(rows, columns=list(HISTORY_DTYPES.keys()))
    df = df.sort_values(["capture_date", "pano_id"]).reset_index(drop=True)

    with gzip.open(output_csv_gz_path, "wb") as f:
        f.write(df.to_csv(index=False).encode("utf-8"))
    if os.path.exists(checkpoint):
        os.remove(checkpoint)  # sweep complete; no resume needed

    dates = df["capture_date"].dropna()
    oldest = dates.min() if len(dates) else None
    newest = dates.max() if len(dates) else None
    logger.info(
        f"Harvested {len(df)} unique official Google panos for "
        f"{city_name} ({oldest}..{newest}) from {api_requests} searches "
        f"-> {output_csv_gz_path}"
    )

    return {
        "df": df,
        "filename_with_path": output_csv_gz_path,
        "api_requests": api_requests,
        "grid_points": len(grid_points),
        "unique_panos": len(df),
        "oldest_capture_date": oldest,
        "newest_capture_date": newest,
        "started_at": started_at,
        "finished_at": harvested_at,
    }
