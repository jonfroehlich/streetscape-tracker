#!/usr/bin/env python3
"""
Where has one Mapillary user mapped most recently -- and is that imagery
inside a city we track, captured after our last Mapillary run there?

    python scripts/mapillary_user_activity.py uwrapid
    python scripts/mapillary_user_activity.py uwrapid --since 2026-09-01 --until 2026-09-15
    python scripts/mapillary_user_activity.py uwrapid --geojson /tmp/uwrapid.geojson --cell-km 5
    python scripts/mapillary_user_activity.py uwrapid --db ~/prod-catalog-copy.db

An OPERATOR tool for a laptop. It is never run by, or inside, the nightly
scheduler, and it refuses to start on a ``makelab*`` host unless told
otherwise (see ``--allow-collection-host``): a per-IP limit found from a
collection host takes out the nightly batch (docs/provider-access.md).

THE API
-------
One endpoint, the Graph API's image search filtered by creator::

    GET https://graph.mapillary.com/images
        ?creator_username=U&fields=id,captured_at,geometry,sequence,is_pano
        &limit=2000&start_captured_at=ISO&end_captured_at=ISO
    Authorization: OAuth <MAPILLARY_ACCESS_TOKEN>

Measured 2026-09-22/23 (docs/experiments/mapillary-user-activity.md): a page
caps at 2,000 images, the response carries ``paging.next`` (an ``after``
cursor), and following it returns the next 2,000 with NO overlap, newest
first. So the counts this script prints are EXACT when it follows the cursor to
the end, and the newest images when ``--max-requests`` stops it early -- which
is the useful half for "where did they map most recently?". A truncated run
says so, in the text report, in every GeoJSON feature (``complete: false``),
and in its exit status (83, the repo's "crawl incomplete" code).

Never the tile CDN. ``tiles.mapillary.com`` is the per-IP-blocked host the
nightly census depends on; this script touches only ``graph.mapillary.com``.

PACING
------
Single-threaded, at least ``--min-interval`` seconds (default 1.0) between
requests, each gap stretched by a random extra of up to ``JITTER_FRACTION`` of
it -- slower than the floor, never faster. HTTP 429 and 5xx back off
(``Retry-After`` when given, else exponential) for at most ``MAX_TRIES``
attempts; every attempt, retry or not, is paced and counts toward
``--max-requests``.

A 3xx (Mapillary's 302 -> login) or an HTML body on a 200 is how a per-IP
block presents (issue #199). It is NOT retried -- retrying during a block
appears to extend it (the forum report in provider-access.md) -- and the run
stops with exit 75.

GROUPING
--------
Images are grouped by (capture day, grid cell). The day is the MEAN SOLAR day
at the image's own longitude -- UTC shifted by ``lon / 15`` hours -- rather than
the UTC date. That is within about an hour of civil time almost everywhere, is
free (no timezone database), and keeps an afternoon drive in the Americas on
the day it happened: a Spokane capture at 16:30 PDT is 23:30 UTC, which the UTC
date files under the right day only by luck and a 17:30 capture files under
the next one. First/last capture times are still reported in UTC.

A cell is ``--cell-km`` on a side: rows are fixed-height latitude bands, and
each row's columns are widened by ``1 / cos(row-centre latitude)`` so a cell
stays roughly square away from the equator. Coarse on purpose -- this answers
"which city?", not "which street?".

The API's time filter is in UTC, so a group near either end of the window can
be partial; the day grouping and the ``--since/--until`` bounds use different
clocks by design.

CATALOG MATCH
-------------
If a catalog exists (``--db``, default ``data/streetscape_tracker.db``), each
group's centroid is tested against every ENABLED city's frozen grid bbox --
``checkpointing.frozen_bbox``, the one derivation the census keys on -- and
annotated with that city's latest Mapillary run date and the newest capture
date that run saw. Two flags, because they answer different questions:

  after_last_run   the imagery was captured after our last run, so that run
                   could not possibly have seen it
  newer_than_seen  the imagery is newer than anything our last run saw, so
                   that run did not see it -- captured before the run but
                   uploaded (or processed) after it, which the Graph API's
                   capture time cannot tell apart

The catalog is opened READ-ONLY. A checkout's catalog is usually a dev copy
and not production's (it may hold no Mapillary runs at all); the report names
the path it read so nobody quotes a dev copy as production.

Remember that the Mapillary census collects 360-degree panos only (issue
#116), so a group whose ``pano_share`` is 0 would not appear in our runs at
any date.

EXIT STATUS
-----------
  0   complete
  1   a non-retryable API error (bad token, unknown field, retries exhausted)
  64  usage error
  75  the provider refused this IP (302 -> login / HTML on 200); stop and wait
  83  stopped at --max-requests; the report is the newest images only
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import random
import re
import socket
import sqlite3
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any
from urllib.parse import urlparse

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from streetscape_metadata_tracker import db  # noqa: E402
from streetscape_metadata_tracker.checkpointing import frozen_bbox  # noqa: E402
from streetscape_metadata_tracker.paths import get_default_data_dir  # noqa: E402

logger = logging.getLogger("mapillary_user_activity")

GRAPH_HOST = "graph.mapillary.com"
GRAPH_IMAGES_URL = f"https://{GRAPH_HOST}/images"
FIELDS = "id,captured_at,geometry,sequence,is_pano"
PAGE_LIMIT = 2000  # the measured per-page cap

DEFAULT_MAX_REQUESTS = 200
DEFAULT_MIN_INTERVAL_S = 1.0
JITTER_FRACTION = 0.5
MAX_TRIES = 4
BACKOFF_BASE_S = 5.0
BACKOFF_CAP_S = 120.0
REQUEST_TIMEOUT_S = 60.0
DEFAULT_WINDOW_DAYS = 30
DEFAULT_CELL_KM = 10.0
KM_PER_DEG_LAT = 111.32

EXIT_OK = 0
EXIT_ERROR = 1
USAGE_EXIT = 64  # the repo's usage-error code (scheduler.USAGE_EXIT_CODE)
BLOCKED_EXIT = 75  # EX_TEMPFAIL, as the per-IP families use it (download_common)
INCOMPLETE_EXIT = 83  # the repo's "crawl incomplete" code (download_common)

# Mapillary usernames are letters, digits, and _ . -. Validated so a typo'd
# argument fails as a usage error rather than as an API round trip.
USERNAME_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")

USER_AGENT = "streetscape-tracker user-activity (github.com/jonfroehlich/streetscape-tracker)"


# ── Errors ────────────────────────────────────────────────────────────────


class ActivityError(Exception):
    """A non-retryable API failure. Exit 1."""


class BlockedError(ActivityError):
    """The provider is refusing this IP (302 -> login, or HTML on a 200). Exit 75."""


class TransientError(Exception):
    """A transport failure the fetch primitive raises; retried like a 5xx."""


# ── The fetch primitive ───────────────────────────────────────────────────


@dataclass
class HttpResult:
    """Everything the crawl reads from one HTTP response -- nothing more.

    Tests substitute an in-memory ``fetch`` returning these rather than mocking
    ``requests`` (docs/testing.md).
    """

    status: int
    body: str = ""
    content_type: str = "application/json"
    headers: dict[str, str] = field(default_factory=dict)


Fetch = Callable[[str, dict[str, Any] | None], HttpResult]


def make_requests_fetch(access_token: str) -> Fetch:
    """The production fetch: one GET, redirects NOT followed.

    ``allow_redirects=False`` is load-bearing: a followed 302 -> login returns
    200 ``text/html``, and the block would read as a malformed page (#199).
    The token rides in a header, never in a URL, so it cannot leak into a log
    line that prints one.
    """
    import requests

    session = requests.Session()
    session.headers.update({"Authorization": f"OAuth {access_token}", "User-Agent": USER_AGENT})

    def fetch(url: str, params: dict[str, Any] | None) -> HttpResult:
        try:
            resp = session.get(url, params=params, allow_redirects=False, timeout=REQUEST_TIMEOUT_S)
        except requests.RequestException as exc:
            raise TransientError(type(exc).__name__) from None
        return HttpResult(
            status=resp.status_code,
            body=resp.text,
            content_type=resp.headers.get("Content-Type", ""),
            headers=dict(resp.headers),
        )

    return fetch


class Pacer:
    """At least ``min_interval_s`` between request starts, plus up to
    ``JITTER_FRACTION`` of it at random. The first request is not delayed."""

    def __init__(
        self,
        min_interval_s: float,
        *,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        rng: random.Random | None = None,
    ):
        self.min_interval_s = min_interval_s
        self._sleep = sleep
        self._clock = clock
        self._rng = rng or random.Random()
        self._last: float | None = None

    def wait(self) -> None:
        if self._last is not None:
            gap = self.min_interval_s * (1.0 + JITTER_FRACTION * self._rng.random())
            remaining = self._last + gap - self._clock()
            if remaining > 0:
                self._sleep(remaining)
        self._last = self._clock()


# ── The crawl ─────────────────────────────────────────────────────────────


@dataclass
class CrawlResult:
    images: list[dict[str, Any]]
    requests: int
    complete: bool
    pages: int


def _retry_after_s(headers: dict[str, str], attempt: int) -> float:
    """Honour a numeric Retry-After; otherwise exponential from BACKOFF_BASE_S."""
    for key, value in headers.items():
        if key.lower() == "retry-after":
            try:
                return min(BACKOFF_CAP_S, max(0.0, float(value)))
            except ValueError:
                break
    return min(BACKOFF_CAP_S, BACKOFF_BASE_S * (2**attempt))


def _api_error_message(body: str) -> str:
    """Mapillary's own error text, if the body carries one; truncated."""
    try:
        err = json.loads(body).get("error", {})
        msg = err.get("message") or json.dumps(err)
    except (ValueError, AttributeError):
        msg = body
    return str(msg)[:300]


def fetch_page(
    url: str,
    params: dict[str, Any] | None,
    *,
    fetch: Fetch,
    pacer: Pacer,
    budget: list[int],
    max_requests: int,
    sleep: Callable[[float], None],
) -> dict[str, Any] | None:
    """One page, with bounded retries. Returns the parsed JSON, or None if the
    request budget ran out before a page could be fetched.

    ``budget`` is a one-element list holding requests spent so far, so every
    ATTEMPT is charged, not only the successful one.
    """
    for attempt in range(MAX_TRIES):
        if budget[0] >= max_requests:
            return None
        pacer.wait()
        budget[0] += 1
        try:
            res = fetch(url, params)
        except TransientError as exc:
            logger.warning(f"transport error ({exc}); attempt {attempt + 1}/{MAX_TRIES}")
            if attempt + 1 < MAX_TRIES:
                sleep(_retry_after_s({}, attempt))
            continue

        if 300 <= res.status < 400:
            raise BlockedError(
                f"HTTP {res.status} redirect from {GRAPH_HOST} -- how Mapillary presents a "
                f"per-IP block (302 -> login). Not retried: retrying during a block appears "
                f"to extend it. Stop, and do not re-run from this IP for several hours."
            )
        if res.status == 200 and "html" in res.content_type.lower():
            raise BlockedError(
                f"HTTP 200 with an HTML body from {GRAPH_HOST} -- a login/challenge page, "
                f"i.e. a per-IP block. Not retried."
            )
        if res.status == 429 or res.status >= 500:
            wait = _retry_after_s(res.headers, attempt)
            logger.warning(
                f"HTTP {res.status}; attempt {attempt + 1}/{MAX_TRIES}, backing off {wait:.0f}s"
            )
            if attempt + 1 < MAX_TRIES:
                sleep(wait)
            continue
        if res.status != 200:
            raise ActivityError(
                f"HTTP {res.status} from {GRAPH_HOST}: {_api_error_message(res.body)}"
            )
        try:
            return json.loads(res.body)
        except ValueError:
            raise ActivityError(f"unparseable JSON from {GRAPH_HOST}") from None

    raise ActivityError(f"gave up after {MAX_TRIES} attempts (429/5xx/transport errors)")


def first_page_params(username: str, since: date, until: date) -> dict[str, Any]:
    """The query for the first page. ``until`` is inclusive: the window ends at
    the start of the following UTC day."""
    return {
        "creator_username": username,
        "fields": FIELDS,
        "limit": PAGE_LIMIT,
        "start_captured_at": f"{since.isoformat()}T00:00:00Z",
        "end_captured_at": f"{(until + timedelta(days=1)).isoformat()}T00:00:00Z",
    }


def crawl_user_images(
    username: str,
    since: date,
    until: date,
    *,
    fetch: Fetch,
    pacer: Pacer,
    max_requests: int,
    sleep: Callable[[float], None] = time.sleep,
) -> CrawlResult:
    """Follow ``paging.next`` until it runs out or the request budget does.

    The next URL is followed only if it stays on ``graph.mapillary.com``: the
    session sends the token on every request, and a cursor that pointed
    anywhere else would be handed it.
    """
    images: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    budget = [0]
    pages = 0
    url: str = GRAPH_IMAGES_URL
    params: dict[str, Any] | None = first_page_params(username, since, until)
    seen_urls: set[str] = set()

    while True:
        page = fetch_page(
            url,
            params,
            fetch=fetch,
            pacer=pacer,
            budget=budget,
            max_requests=max_requests,
            sleep=sleep,
        )
        if page is None:
            return CrawlResult(images, budget[0], complete=False, pages=pages)
        pages += 1
        data = page.get("data") or []
        for img in data:
            # Defensive: a cursor that re-served an image must not double-count it.
            if img.get("id") in seen_ids:
                continue
            seen_ids.add(img.get("id"))
            images.append(img)
        next_url = (page.get("paging") or {}).get("next")
        if not data or not next_url:
            return CrawlResult(images, budget[0], complete=True, pages=pages)
        if urlparse(next_url).hostname != GRAPH_HOST:
            raise ActivityError(f"paging.next points off {GRAPH_HOST}; refusing to follow it")
        if next_url in seen_urls:
            raise ActivityError("paging.next repeated a cursor already followed; stopping")
        seen_urls.add(next_url)
        url, params = next_url, None


# ── Grouping ──────────────────────────────────────────────────────────────


def solar_day(captured_at_ms: int, lon: float) -> date:
    """Mean solar date at ``lon``: UTC shifted by lon/15 hours (see GROUPING)."""
    seconds = captured_at_ms / 1000.0 + lon / 15.0 * 3600.0
    return datetime.fromtimestamp(seconds, UTC).date()


def cell_of(lat: float, lon: float, cell_km: float) -> tuple[int, int]:
    """(row, col) of the roughly square ``cell_km`` cell containing a point."""
    dlat = cell_km / KM_PER_DEG_LAT
    row = math.floor(lat / dlat)
    row_center_lat = (row + 0.5) * dlat
    # Clamp so a polar row cannot divide by ~0.
    cos_lat = max(math.cos(math.radians(row_center_lat)), 0.01)
    dlon = cell_km / (KM_PER_DEG_LAT * cos_lat)
    col = math.floor(lon / dlon)
    return row, col


def _iso_utc(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000.0, UTC).isoformat().replace("+00:00", "Z")


def group_images(images: list[dict[str, Any]], cell_km: float) -> list[dict[str, Any]]:
    """One record per (solar day, cell), newest day first, then largest first.

    Images without a point geometry or a capture time are skipped (counted by
    the caller); they cannot be placed in either dimension.
    """
    buckets: dict[tuple[date, int, int], list[dict[str, Any]]] = {}
    for img in images:
        coords = (img.get("geometry") or {}).get("coordinates")
        ts = img.get("captured_at")
        if not coords or ts is None:
            continue
        lon, lat = float(coords[0]), float(coords[1])
        day = solar_day(int(ts), lon)
        row, col = cell_of(lat, lon, cell_km)
        buckets.setdefault((day, row, col), []).append(
            {
                "lat": lat,
                "lon": lon,
                "ts": int(ts),
                "seq": img.get("sequence"),
                "pano": img.get("is_pano"),
            }
        )

    groups = []
    for (day, row, col), pts in buckets.items():
        n = len(pts)
        groups.append(
            {
                "date": day.isoformat(),
                "cell": [row, col],
                "lat": round(sum(p["lat"] for p in pts) / n, 5),
                "lon": round(sum(p["lon"] for p in pts) / n, 5),
                "images": n,
                "sequences": len({p["seq"] for p in pts if p["seq"]}),
                "pano_share": round(sum(1 for p in pts if p["pano"] is True) / n, 4),
                "first_capture_utc": _iso_utc(min(p["ts"] for p in pts)),
                "last_capture_utc": _iso_utc(max(p["ts"] for p in pts)),
            }
        )
    groups.sort(key=lambda g: (g["date"], g["images"]), reverse=True)
    return groups


# ── Catalog match ─────────────────────────────────────────────────────────


@dataclass
class CatalogCity:
    city_id: str
    bbox: tuple[float, float, float, float]  # (min_lon, min_lat, max_lon, max_lat)
    last_run_date: str | None
    newest_capture_date: str | None


def open_catalog_readonly(path: str) -> sqlite3.Connection | None:
    """The catalog, read-only, or None if there is no file at ``path``.

    Never ``db.connect``: that creates the file and migrates the schema, and
    this tool must not write to a catalog -- least of all production's.
    """
    if not os.path.isfile(path):
        return None
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def load_catalog_cities(conn: sqlite3.Connection) -> list[CatalogCity]:
    """Every enabled city's frozen bbox and latest Mapillary run."""
    out = []
    for city in db.get_all_cities(conn, enabled_only=True):
        run = db.get_latest_run(conn, city.city_id, provider="mapillary")
        out.append(
            CatalogCity(
                city_id=city.city_id,
                bbox=frozen_bbox(city),
                last_run_date=run.run_date if run else None,
                newest_capture_date=(run.newest_capture_date or None) if run else None,
            )
        )
    return out


def _bbox_area(b: tuple[float, float, float, float]) -> float:
    return (b[2] - b[0]) * (b[3] - b[1])


def match_group(group: dict[str, Any], cities: list[CatalogCity]) -> dict[str, Any]:
    """Catalog fields for one group, keyed on its centroid.

    Frozen grids can overlap (a suburb inside a metro's rectangle); the
    SMALLEST containing bbox wins, as the most specific city, and the others
    are listed in ``also_in``.
    """
    lat, lon = group["lat"], group["lon"]
    hits = [c for c in cities if c.bbox[0] <= lon <= c.bbox[2] and c.bbox[1] <= lat <= c.bbox[3]]
    if not hits:
        return {"city_id": None}
    hits.sort(key=lambda c: (_bbox_area(c.bbox), c.city_id))
    city = hits[0]
    # Compare on the UTC date of the LAST capture: run_date is a UTC-ish date
    # and the question is whether any of this imagery postdates the run.
    last_capture = group["last_capture_utc"][:10]
    newest_seen = city.newest_capture_date[:10] if city.newest_capture_date else None
    return {
        "city_id": city.city_id,
        "also_in": [c.city_id for c in hits[1:]],
        "last_mapillary_run": city.last_run_date,
        "newest_capture_seen": newest_seen,
        "never_collected": city.last_run_date is None,
        "after_last_run": city.last_run_date is not None and last_capture > city.last_run_date,
        "newer_than_seen": city.last_run_date is not None
        and (newest_seen is None or last_capture > newest_seen),
    }


# ── Output ────────────────────────────────────────────────────────────────


def to_geojson(groups: list[dict[str, Any]], meta: dict[str, Any]) -> dict[str, Any]:
    features = []
    for g in groups:
        props = {k: v for k, v in g.items() if k not in ("lat", "lon")}
        props["complete"] = meta["complete"]
        features.append(
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [g["lon"], g["lat"]]},
                "properties": props,
            }
        )
    return {"type": "FeatureCollection", "features": features, "properties": meta}


def format_report(groups: list[dict[str, Any]], meta: dict[str, Any]) -> str:
    lines = [
        f"Mapillary user {meta['username']!r}: {meta['images']} images, "
        f"{meta['since']} .. {meta['until']} (UTC capture window), "
        f"{meta['requests']} requests",
    ]
    if meta["complete"]:
        lines.append("Counts are EXACT (cursor followed to the end).")
    else:
        lines.append(
            f"TRUNCATED at --max-requests {meta['max_requests']}: these are the NEWEST "
            f"{meta['images']} images only, and every count is a LOWER BOUND."
        )
    if meta.get("skipped_unplaceable"):
        lines.append(
            f"{meta['skipped_unplaceable']} images had no geometry or time and were skipped."
        )
    lines.append(meta["catalog_note"])
    lines.append("")
    has_catalog = meta["catalog_path"] is not None
    header = (
        f"{'solar day':10}  {'lat':>9} {'lon':>10}  {'images':>7} {'seqs':>5} {'pano':>5}  "
        f"{'first (UTC)':>20} {'last (UTC)':>20}"
    )
    if has_catalog:
        header += "  city / last mapillary run / newest seen"
    lines.append(header)
    for g in groups:
        row = (
            f"{g['date']:10}  {g['lat']:9.5f} {g['lon']:10.5f}  {g['images']:7d} "
            f"{g['sequences']:5d} {g['pano_share']:5.0%}  "
            f"{g['first_capture_utc'][:19]:>20} {g['last_capture_utc'][:19]:>20}"
        )
        if has_catalog:
            if g.get("city_id") is None:
                row += "  (no tracked city)"
            else:
                flags = []
                if g["never_collected"]:
                    flags.append("NEVER COLLECTED")
                if g["after_last_run"]:
                    flags.append("AFTER LAST RUN")
                if g["newer_than_seen"]:
                    flags.append("NEWER THAN SEEN")
                row += (
                    f"  {g['city_id']} / {g['last_mapillary_run'] or '-'} / "
                    f"{g['newest_capture_seen'] or '-'}"
                )
                if flags:
                    row += "  <- " + ", ".join(flags)
        lines.append(row)
    return "\n".join(lines)


# ── CLI ───────────────────────────────────────────────────────────────────


def _date_arg(s: str) -> date:
    try:
        return date.fromisoformat(s)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a YYYY-MM-DD date: {s!r}") from None


class _UsageParser(argparse.ArgumentParser):
    """argparse, but a usage error exits 64 (the repo's code), not 2."""

    def error(self, message: str):  # type: ignore[override]
        self.print_usage(sys.stderr)
        print(f"{self.prog}: error: {message}", file=sys.stderr)
        raise SystemExit(USAGE_EXIT)


def parse_args(argv: list[str] | None = None, *, today: date | None = None) -> argparse.Namespace:
    today = today or datetime.now(UTC).date()
    p = _UsageParser(
        description="Where has a Mapillary user mapped most recently, and do our runs have it?"
    )
    p.add_argument("username", help="Mapillary username (creator_username)")
    p.add_argument(
        "--since",
        type=_date_arg,
        default=today - timedelta(days=DEFAULT_WINDOW_DAYS),
        help=f"first UTC capture date, inclusive (default: {DEFAULT_WINDOW_DAYS} days ago)",
    )
    p.add_argument(
        "--until",
        type=_date_arg,
        default=today,
        help="last UTC capture date, inclusive (default: today)",
    )
    p.add_argument("--cell-km", type=float, default=DEFAULT_CELL_KM, help="grid cell side, km")
    p.add_argument("--geojson", default=None, help="write group centroids as a FeatureCollection")
    p.add_argument(
        "--metrics-json",
        default=None,
        help="upsert this run's derived record into a committed experiments metrics file",
    )
    p.add_argument(
        "--max-requests",
        type=int,
        default=DEFAULT_MAX_REQUESTS,
        help=f"stop after this many HTTP requests, retries included (default {DEFAULT_MAX_REQUESTS})",
    )
    p.add_argument(
        "--min-interval",
        type=float,
        default=DEFAULT_MIN_INTERVAL_S,
        help=f"seconds between requests, at least {DEFAULT_MIN_INTERVAL_S} (default)",
    )
    p.add_argument(
        "--db",
        default=None,
        help="catalog to match against (default: data/streetscape_tracker.db); opened read-only",
    )
    p.add_argument(
        "--allow-collection-host",
        action="store_true",
        help="run even on a makelab* host (the nightly batch's IP); think twice",
    )
    p.add_argument("--log-level", default="INFO")
    args = p.parse_args(argv)

    if not USERNAME_RE.match(args.username):
        p.error(f"not a plausible Mapillary username: {args.username!r}")
    if args.since > args.until:
        p.error(f"--since {args.since} is after --until {args.until}")
    if not (args.cell_km > 0 and math.isfinite(args.cell_km)):
        p.error("--cell-km must be a positive number")
    if args.max_requests < 1:
        p.error("--max-requests must be at least 1")
    if not (args.min_interval >= DEFAULT_MIN_INTERVAL_S and math.isfinite(args.min_interval)):
        p.error(f"--min-interval must be at least {DEFAULT_MIN_INTERVAL_S} s")
    return args


def refuse_on_collection_host(allow: bool) -> None:
    """Refuse a makelab* host: a per-IP block found here stops the nightly batch."""
    host = socket.gethostname().lower()
    if host.startswith("makelab") and not allow:
        print(
            f"Refusing to query Mapillary from {host!r}: it is a production collection host, "
            f"and a per-IP refusal here would take out the nightly batch. Run it from a laptop "
            f"(copy the catalog and pass --db), or pass --allow-collection-host.",
            file=sys.stderr,
        )
        raise SystemExit(USAGE_EXIT)


def docs_generated_by(args: argparse.Namespace) -> str:
    """The command that produced a metrics record, from the real arguments."""
    parts = [
        "scripts/mapillary_user_activity.py",
        args.username,
        "--since",
        args.since.isoformat(),
        "--until",
        args.until.isoformat(),
        "--cell-km",
        f"{args.cell_km:g}",
        "--max-requests",
        str(args.max_requests),
        "--metrics-json",
        str(args.metrics_json),
    ]
    return " ".join(parts)


def upsert_metrics(path: str, record: dict[str, Any]) -> None:
    """Add (or replace) this window's record in a metrics file.

    One file holds several windows because a heavy contributor's single day can
    exceed any sane request budget; each window is keyed by (username, since,
    until) and carries its own ``generated_by``, so every record is traceable
    to one command.
    """
    doc: dict[str, Any] = {"windows": []}
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
    key = (record["username"], record["since"], record["until"])
    doc["windows"] = [
        w for w in doc.get("windows", []) if (w["username"], w["since"], w["until"]) != key
    ]
    doc["windows"].append(record)
    doc["windows"].sort(key=lambda w: (w["username"], w["since"], w["until"]))
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2, sort_keys=True)
        fh.write("\n")


def run(
    args: argparse.Namespace,
    *,
    fetch: Fetch,
    pacer: Pacer,
    sleep: Callable[[float], None] = time.sleep,
    out=None,
) -> int:
    """Everything after argument parsing and credential loading."""
    out = out or sys.stdout
    status = EXIT_OK
    try:
        crawl = crawl_user_images(
            args.username,
            args.since,
            args.until,
            fetch=fetch,
            pacer=pacer,
            max_requests=args.max_requests,
            sleep=sleep,
        )
    except BlockedError as exc:
        print(f"BLOCKED: {exc}", file=sys.stderr)
        return BLOCKED_EXIT
    except ActivityError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_ERROR

    groups = group_images(crawl.images, args.cell_km)
    placed = sum(g["images"] for g in groups)

    db_path = args.db or db.get_default_db_path(get_default_data_dir())
    conn = open_catalog_readonly(db_path)
    if conn is None:
        catalog_note = f"No catalog at {db_path}: catalog match skipped."
        catalog_path = None
    else:
        try:
            cities = load_catalog_cities(conn)
        finally:
            conn.close()
        for g in groups:
            g.update(match_group(g, cities))
        n_runs = sum(1 for c in cities if c.last_run_date)
        catalog_note = (
            f"Catalog: {db_path} (read-only; {len(cities)} enabled cities, {n_runs} with a "
            f"Mapillary run). A checkout's catalog is usually a DEV copy, not production's."
        )
        catalog_path = db_path

    meta = {
        "username": args.username,
        "since": args.since.isoformat(),
        "until": args.until.isoformat(),
        "cell_km": args.cell_km,
        "requests": crawl.requests,
        "pages": crawl.pages,
        "max_requests": args.max_requests,
        "complete": crawl.complete,
        "images": len(crawl.images),
        "skipped_unplaceable": len(crawl.images) - placed,
        "catalog_path": catalog_path,
        "catalog_note": catalog_note,
    }
    print(format_report(groups, meta), file=out)

    if args.geojson:
        with open(args.geojson, "w", encoding="utf-8") as fh:
            json.dump(to_geojson(groups, meta), fh, indent=2)
            fh.write("\n")
    if args.metrics_json:
        record = {k: v for k, v in meta.items() if k != "catalog_note"}
        record["generated_by"] = docs_generated_by(args)
        record["generated_at"] = datetime.now(UTC).isoformat(timespec="seconds")
        record["groups"] = groups
        upsert_metrics(args.metrics_json, record)

    if not crawl.complete:
        print(
            f"Stopped at --max-requests {args.max_requests}: counts are lower bounds "
            f"(newest images only). Raise --max-requests or narrow --since/--until.",
            file=sys.stderr,
        )
        status = INCOMPLETE_EXIT
    return status


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=args.log_level, format="%(asctime)s %(levelname)s %(message)s")
    refuse_on_collection_host(args.allow_collection_host)

    # The repo convention: bare load_dotenv() to LOAD (it walks up from this
    # module, so any cwd works); find_dotenv only for the permission warning.
    from dotenv import find_dotenv, load_dotenv

    from streetscape_metadata_tracker import config as cfg

    load_dotenv()
    cfg.warn_if_credentials_world_readable(find_dotenv(usecwd=True))
    try:
        token = cfg.load_config("mapillary")["access_token"]
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return USAGE_EXIT

    return run(args, fetch=make_requests_fetch(token), pacer=Pacer(args.min_interval))


if __name__ == "__main__":
    sys.exit(main())
