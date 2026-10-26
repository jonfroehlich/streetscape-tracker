"""
Historical Street View capture-date harvester (issues #2, #442).

A normal GSV run answers only "what's the nearest pano to point X, and when was
it taken?" — one *current* date per point. Google's own time-travel feature,
though, exposes the FULL capture history at a location: every official
Street View drive-through and its date. That history is not in any documented
Google API (free or paid). It is returned by an **unpublished endpoint**
(the `MapsJsInternalService/SingleImageSearch` RPC, the backend the Maps JS
`StreetViewService.getPanorama().time[]` array reads from), which we query
directly here — no API key, no browser. Its older JSONP shim was turned down
by 2026-10-17; `RPC_URL` below records the route that answers.

Because the endpoint is undocumented there is **no guarantee it keeps working**,
and it is identified by our IP rather than a metered key, so this harvester is
deliberately the opposite of the aggressive per-point run downloader:

  * ONE request in flight, paced by the repo's jittered pacer (mean 1/s,
    coefficient of variation 0.6 by default) — the pace governs, not sockets;
  * a stopping rule written down in advance (#442): the FIRST 429 or 403, the
    first HTML page served with a 200 (an anti-abuse interstitial is a refusal
    wearing a 200), 8 consecutive failed REQUESTS (transport failures, retries
    included, and 200s whose body is not an answer), or failed requests above
    a quarter of the last 50 (a throttle that lets one in a few through never
    fails eight in a row), ends the run and writes a cool-down latch -- one
    per HOST, in the out-dir's parent by default -- that refuses a restart for
    24 h; a per-run request cap lets a timer run it in daily tranches;
  * ONE crawl per host: a lock beside the latch is held for the whole run, so
    a second harvester on the same address (pass 2 beside pass 1, a tranche
    overlapping the next) is refused rather than doubling the paced rate;
  * every response body is kept verbatim, gzipped, before it is parsed — if the
    endpoint changes shape or disappears, the raw store is the only copy; a
    200 that is not an answer is never recorded as "no imagery here";
  * resumable: the queries ledger it appends to IS the checkpoint.

Collection model (#442):
  * The unit of work is a ROAD-WALK SAMPLE POINT from a work list that
    `scripts/build_gsv_history_worklist.py` writes on the catalog host, keyed
    `(edge_id, sample_idx)` on the walk's frozen network and carrying the walk's
    current ("anchor") pano. The crawler runs on a dedicated host that holds
    no catalog and needs nothing but the file.
  * Pass 1 queries each edge's midpoint sample; pass 2 the remaining samples.
  * Each search returns the pano served at the location now, plus every pano
    the service relates to it: neighbours (undated) and the capture HISTORY
    (dated, keyed by index). Every dated pano is kept, the current one first.
    A date no longer marks official imagery — user photospheres come dated
    too — so each row carries `third_party`, from the node's type code (2
    official, 10 user) with the id-shape rule as fallback.
  * Results stay keyed to the sample that found them (one row per sample and
    dated pano), so "how often is THIS street re-captured?" is answerable.
    Whether the anchor pano appears in its own history (`anchor_in_history`)
    is the parser-health signal a pilot reads.

The per-city, per-pano `HISTORY_DTYPES` artifact (`_gsv_history_` CSV, #2) and
its #109 summary are DERIVED from these per-sample results by a later import
step; the grid-point sweep that used to write them directly was retired in
#442 (it queried the wrong unit and kept no link to the place it searched).

History is near-static (Google's archive), so a point is harvested ONCE; this is
not part of the cadenced run series. Re-querying a point whose anchor pano has
changed is a follow-up, not implemented here.

The parsing of the endpoint's (undocumented, deeply-nested) response is adapted
from the MIT-licensed `robolyst/streetview` package.
"""

import asyncio
import csv
import gzip
import io
import json
import logging
import os
import zlib
from collections import deque
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import aiohttp
import backoff
import numpy as np
import pandas as pd
from filelock import FileLock, Timeout

from .download_common import AsyncRateLimiter, DownloadError, standardize_capture_date
from .progress import progress

logger = logging.getLogger(__name__)

# Schema of the per-city `_gsv_history_` artifact — one row per unique dated
# pano. Distinct from config.METADATA_DTYPES (a per-grid-point run sample);
# this is a per-pano historical census, so it gets its own columns. Read by
# fileutils.load_history_csv_file and summarized by json_summarizer (#109); since
# #442 it is DERIVED from the per-sample SAMPLE_HISTORY_COLUMNS output below,
# and the importer that derives it MUST carry `third_party`: on the RPC route
# user photospheres come dated (20 + 179 rows in the pilot), so a present
# date no longer means official imagery and the summarizer filters on the
# flag. A harvest written before the column existed came from the retired
# route, where "dated" was "official" by construction, and reads as all
# official.
HISTORY_DTYPES = {
    "pano_id": str,
    "capture_date": str,  # ISO YYYY-MM-DD (day defaults to 01;
    # the endpoint's precision is a month)
    "pano_lat": np.float64,
    "pano_lon": np.float64,
    "nearest_query_lat": np.float64,  # grid point whose search first
    "nearest_query_lon": np.float64,  # surfaced this pano
    "harvested_at": str,  # UTC ISO 8601
    "third_party": bool,  # a user photosphere, never official Google imagery
}

# The live route (measured 2026-10-17 from the pilot host): the RPC that the
# Maps JavaScript API itself calls. The older JSONP shim this module shipped
# with (`maps/api/js/GeoPhotoService.SingleImageSearch?pb=...&callback=`) now
# answers every query with HTTP 200 and the body
# `[[5,"generic","GeoPhotoService.SingleImageSearch is decommissioned and turned down."]]`
# -- every one of the pilot's first requests, before the parser noticed
# anything. The request
# body and the response offsets follow sk-zk/streetlevel (MIT), the maintained
# client of this RPC; the toggles ask for resolution, street name and date,
# copyright, neighbours + history, and the exact-timestamp field.
RPC_URL = (
    "https://maps.googleapis.com/$rpc/google.internal.maps.mapsjs.v1.MapsJsInternalService"
    "/SingleImageSearch"
)
RPC_CONTENT_TYPE = "application/json+protobuf"
DEFAULT_SEARCH_RADIUS_M = 50.0  # the walk's own match radius

# A realistic browser UA; the endpoint is meant to be called from Maps JS.
_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0 Safari/537.36"
)

# Pano type codes the response carries in each node's first field (and the
# request asks for): 2 = official Google imagery, 10 = third-party/user.
PANO_TYPE_OFFICIAL = 2
PANO_TYPE_THIRD_PARTY = 10


@dataclass(frozen=True)
class HistoricalPano:
    """One panorama the search knows about at this location, with its capture month."""

    pano_id: str
    capture_date: str  # standardized ISO 'YYYY-MM-DD' (month precision, pinned to the 1st)
    lat: float
    lon: float
    third_party: bool = False  # user photosphere rather than official Google imagery
    is_current: bool = False  # the pano the search returned as the one served here now
    # Everything else the node carries, kept because the request was paid for
    # (the raw body holds the rest, byte for byte). Pose is served in degrees;
    # pitch is 90 - served, so 0 is level (sk-zk/streetlevel's reading).
    capture_day: int | None = None  # only when the date node carries a day
    pano_type: int | None = None  # the node's type code as served (2 official, 10 user)
    elevation_m: float | None = None
    heading_deg: float | None = None
    pitch_deg: float | None = None
    roll_deg: float | None = None


@dataclass(frozen=True)
class SearchResult:
    """
    A parsed search response.

    ``unanswered`` is set when the body is NOT an answer to the search -- not
    JSON, empty, an HTML page served with a 200, a response code the service
    does not use for "no images", or a code 0 with no pano message in it. An
    answer is exactly one of two shapes: code 0 with the pano message, or the
    "no images" code with its message. Anything else is a query the endpoint
    did not answer (the retired route said so in a code-5 body for every one
    of the pilot's first requests, and an anti-abuse page comes as a 200 too),
    so the crawl treats it like a transport failure: not done, re-queried on
    resume, and counted toward the breaker, since a run of them is a refusal
    in disguise. ``refusal`` marks the one shape that is a refusal on its own:
    an HTML page, which no answer ever is, so the crawl latches on the first.
    """

    response_code: int | None  # 0 ok, 5 "Search returned no images.", None unparseable
    panos: tuple[HistoricalPano, ...]  # every DATED pano: the current one plus its history
    panos_total: int | None  # every pano node the response carries, dated or not
    # Per-location fields of the current pano, parsed for convenience.
    copyright: str | None = None  # e.g. "© 2026 Google"
    country_code: str | None = None
    street_name: str | None = None  # the first street label the service attaches
    upload_date: str | None = None  # the raw upload-date node, JSON-encoded, when present
    exact_timestamp: str | None = None  # the raw exact-time field (third-party panos), when present
    unanswered: str | None = None  # why this body is not an answer (see above), else None
    refusal: bool = False  # an HTML page served with a 200: latch on the first one


# The response code the service answers a search that found nothing with, and
# the message it sends beside it. A code 5 WITHOUT that message is what the
# retired JSONP route answered with ("... is decommissioned and turned down."),
# so the code alone is NOT "no images": a code-5 body whose message does not
# say so is `unanswered` (re-queried, counted by the breaker), never silently
# "no imagery here". The cost of being wrong is asymmetric -- a mis-read
# refusal publishes absence nobody observed, a mis-read "no images" costs one
# re-query and an exit 83 -- which is why the message, not the code, decides.
NO_IMAGES_CODE = 5
NO_IMAGES_MESSAGE = "no images"
UNANSWERED_REASON_CHARS = 120


def _is_html(text: str) -> bool:
    """An HTML page (an anti-abuse interstitial): a refusal, whatever its status."""
    return text.lstrip()[:1] == "<"


def _describe_non_answer(text: str) -> str:
    """A short, ledger-sized reason for a body that is not an answer."""
    stripped = text.strip()
    if not stripped:
        return "empty body"
    if _is_html(stripped):
        return f"HTML body ({len(text)} chars): {stripped[:60]!r}"[:UNANSWERED_REASON_CHARS]
    return f"non-JSON body ({len(text)} chars): {stripped[:60]!r}"[:UNANSWERED_REASON_CHARS]


def _message_of(data: Any) -> str | None:
    """The message beside a response code (``data[0][2]``), when it is a string."""
    try:
        message = data[0][2]
    except (IndexError, KeyError, TypeError):
        return None
    return message if isinstance(message, str) else None


class HarvestBlockedError(DownloadError):
    """
    The endpoint refused us -- a 429/403, or a run of failed requests that
    tripped the circuit breaker. The outputs written so far stay on disk and
    the ``blocked.json`` latch records when a restart may be attempted.
    """


class HarvestBusyError(DownloadError):
    """Another harvester on this host holds the crawl lock (one crawl per address)."""


class OutputShapeError(ValueError):
    """
    An output file in the out-dir was written under another column set.

    Rows appended under a header that names other columns land shifted, and
    a resume reads them through that header: on 2026-10-26 a pilot-era
    12-column ledger took 900 rows of this crawler's 15 and read every
    unanswered key back as done. The crawler refuses the out-dir instead.
    """


class _BreakerTripped(Exception):
    """Raised instead of sending a request once the breaker has tripped (never retried)."""


def is_third_party_pano_id(pano_id: str) -> bool:
    """The id-shape rule for user photospheres (sk-zk/streetlevel): 'CIHM0og...' or longer than 22."""
    return pano_id.startswith("CIHM0og") or len(pano_id) > 22


def build_search_body(lat: float, lon: float, radius_m: float = DEFAULT_SEARCH_RADIUS_M) -> str:
    """The JSON+protobuf request body for a location search (official imagery, en-US)."""
    message = [
        ["apiv3", None, None, None, "US"],
        [[None, None, float(lat), float(lon)], float(radius_m)],
        [
            None,
            ["en", "US"],
            None,
            None,
            None,
            None,
            None,
            None,
            [2],
            None,
            [[[PANO_TYPE_OFFICIAL, True, 2]]],
        ],
        [[1, 2, 3, 4, 6, 8, 12], None, None, None, [], []],
    ]
    return json.dumps(message)


def _month_iso(node: Any) -> str:
    """``[year, month]`` or ``[year, month, day]`` -> 'YYYY-MM-01', '' when malformed."""
    try:
        return standardize_capture_date(f"{int(node[0])}-{int(node[1]):02d}")
    except (IndexError, KeyError, TypeError, ValueError):
        return ""


def _day_of(node: Any) -> int | None:
    try:
        return int(node[2]) if len(node) > 2 and node[2] is not None else None
    except (IndexError, KeyError, TypeError, ValueError):
        return None


def _float_or_none(value: Any) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def _pose(geo: Any) -> dict[str, float | None]:
    """``[[_, _, lat, lon], [elevation, ...], [heading, pitch, roll]]`` -> the four pose fields."""
    out: dict[str, float | None] = {
        "elevation_m": None,
        "heading_deg": None,
        "pitch_deg": None,
        "roll_deg": None,
    }
    try:
        out["elevation_m"] = _float_or_none(geo[1][0])
    except (IndexError, KeyError, TypeError):
        pass
    try:
        out["heading_deg"] = _float_or_none(geo[2][0])
        pitch = _float_or_none(geo[2][1])
        out["pitch_deg"] = None if pitch is None else 90.0 - pitch
        out["roll_deg"] = _float_or_none(geo[2][2])
    except (IndexError, KeyError, TypeError):
        pass
    return out


def _str_or_none(value: Any) -> str | None:
    return None if value is None else str(value)


def parse_search(text: str) -> SearchResult:
    """
    Parse one RPC response into the dated panos at a location.

    Shape (offsets after sk-zk/streetlevel): ``data[0][0]`` is the response
    code; ``msg = data[1]``; ``msg[1][1]`` the current pano's id, ``msg[6][7]``
    its ``[year, month]``, ``msg[5][0][1][0][2:4]`` its position; ``msg[5][0][3][0]``
    the list of every pano node the search relates to it (neighbours AND
    history, the current pano included), each ``[[type, id], _, [[_, _, lat, lon], ...]]``;
    and ``msg[5][0][8]`` the history list, ``[index, [year, month], ...]`` keyed
    by index into that node list. Any structural surprise yields an empty
    result rather than raising, so one odd response cannot abort a crawl --
    the raw body is kept verbatim for a later re-parse.

    A date no longer marks official imagery (third-party photospheres come
    dated here), so each pano carries ``third_party`` from the node's type
    code, with the id-shape rule as fallback.

    A body that is not an answer at all (not JSON, empty, HTML, an unknown
    response code, a code 0 with no pano message) comes back with
    ``unanswered`` set to a short reason -- never as "no imagery here".
    """
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return SearchResult(
            None, (), None, unanswered=_describe_non_answer(text), refusal=_is_html(text)
        )
    try:
        code = int(data[0][0])
    except (IndexError, KeyError, TypeError, ValueError):
        return SearchResult(None, (), None, unanswered=_describe_non_answer(text))
    if code != 0:
        message = _message_of(data)
        if code == NO_IMAGES_CODE and message and NO_IMAGES_MESSAGE in message.lower():
            return SearchResult(code, (), 0)
        reason = f"response code {code}: {message or 'no message'}"[:UNANSWERED_REASON_CHARS]
        return SearchResult(code, (), None, unanswered=reason)
    try:
        msg = data[1]
        main_id = str(msg[1][1])
        nodes = msg[5][0][3][0] or []
    except (IndexError, KeyError, TypeError):
        return SearchResult(code, (), None, unanswered="response code 0 without a pano message")
    try:
        main_latlon = (float(msg[5][0][1][0][2]), float(msg[5][0][1][0][3]))
    except (IndexError, KeyError, TypeError, ValueError):
        main_latlon = None
    try:
        main_geo = msg[5][0][1]
    except (IndexError, KeyError, TypeError):
        main_geo = None
    extras: dict[str, Any] = {}
    for key, getter in (
        ("copyright", lambda: msg[4][0][0][0][0]),
        ("country_code", lambda: msg[5][0][1][4]),
        ("street_name", lambda: msg[5][0][12][0][0][0][2][0]),
        ("upload_date", lambda: json.dumps(msg[6][8]) if msg[6][8] is not None else None),
        ("exact_timestamp", lambda: msg[12][0] if msg[12] else None),
    ):
        try:
            extras[key] = _str_or_none(getter())
        except (IndexError, KeyError, TypeError):
            extras[key] = None
    try:
        main_date = _month_iso(msg[6][7])
    except (IndexError, KeyError, TypeError):
        main_date = ""
    try:
        raw_dates = msg[5][0][8] or []
    except (IndexError, KeyError, TypeError):
        raw_dates = []
    dates: dict[int, str] = {}
    for entry in raw_dates:
        try:
            dates[int(entry[0])] = _month_iso(entry[1])
        except (IndexError, KeyError, TypeError, ValueError):
            continue

    def _node(node) -> dict[str, Any] | None:
        try:
            pano_id = str(node[0][1])
        except (IndexError, KeyError, TypeError):
            return None
        try:
            kind: int | None = int(node[0][0])
            third = kind == PANO_TYPE_THIRD_PARTY
        except (IndexError, KeyError, TypeError, ValueError):
            kind = None
            third = is_third_party_pano_id(pano_id)
        try:
            lat, lon = float(node[2][0][2]), float(node[2][0][3])
        except (IndexError, KeyError, TypeError, ValueError):
            lat = lon = None
        try:
            geo = node[2]
        except (IndexError, KeyError, TypeError):
            geo = None
        return {
            "pano_id": pano_id,
            "third_party": third or is_third_party_pano_id(pano_id),
            "lat": lat,
            "lon": lon,
            "pano_type": kind,
            **_pose(geo),
        }

    out: list[HistoricalPano] = []
    seen: set[str] = set()
    if main_date and main_latlon is not None:
        # The current pano's own node (type code, pose) is the one in the
        # list that carries its id -- normally the first.
        own = next((n for n in (_node(x) for x in nodes) if n and n["pano_id"] == main_id), None)
        pose = (
            {k: own[k] for k in ("elevation_m", "heading_deg", "pitch_deg", "roll_deg")}
            if own
            else _pose(main_geo)
        )
        out.append(
            HistoricalPano(
                pano_id=main_id,
                capture_date=main_date,
                lat=main_latlon[0],
                lon=main_latlon[1],
                third_party=(own["third_party"] if own else is_third_party_pano_id(main_id)),
                is_current=True,
                capture_day=_day_of(msg[6][7]),
                pano_type=own["pano_type"] if own else None,
                **pose,
            )
        )
        seen.add(main_id)
    days: dict[int, int | None] = {}
    for entry in raw_dates:
        try:
            days[int(entry[0])] = _day_of(entry[1])
        except (IndexError, KeyError, TypeError, ValueError):
            continue
    for idx, iso in sorted(dates.items()):
        if idx >= len(nodes) or not iso:
            continue
        parsed = _node(nodes[idx])
        if parsed is None or parsed["pano_id"] in seen or parsed["lat"] is None:
            continue
        seen.add(parsed["pano_id"])
        out.append(
            HistoricalPano(
                pano_id=parsed["pano_id"],
                capture_date=iso,
                lat=parsed["lat"],
                lon=parsed["lon"],
                third_party=parsed["third_party"],
                capture_day=days.get(idx),
                pano_type=parsed["pano_type"],
                elevation_m=parsed["elevation_m"],
                heading_deg=parsed["heading_deg"],
                pitch_deg=parsed["pitch_deg"],
                roll_deg=parsed["roll_deg"],
            )
        )
    return SearchResult(code, tuple(out), len(nodes), **extras)


def parse_search_response(text: str) -> list[HistoricalPano]:
    """Every dated pano in one response (the current one first); see ``parse_search``."""
    return list(parse_search(text).panos)


class _CircuitBreaker:
    """
    Trips after `limit` consecutive failed REQUESTS — the signature of being
    throttled/blocked, as opposed to isolated points with genuinely no imagery
    (those succeed with an empty result and reset the counter) — or when the
    failed share of the last `window` requests exceeds `share` (a throttle
    that lets one request in a few through never fails `limit` in a row, and
    without the window such a run would spend its whole cap into it: measured
    375 requests for 46 answers under the consecutive rule alone, in a
    simulated throttle).

    A request fails when its transport fails (a timeout, a connection error,
    a non-200 the driver retries) or when its 200 is not an answer
    (``SearchResult.unanswered``). The count is per request, retries included:
    ``harvest_worklist_async`` records every attempt and sends no attempt once
    the breaker has tripped, so "8 consecutive failed requests" means the
    ninth is never made, whatever ``fetch_tries`` is. The share rule reads
    only once the window is full, so a lone early failure cannot trip it.
    """

    def __init__(
        self,
        limit: int = 8,
        window: int = 50,
        share: float = 0.25,
    ):
        self.limit = limit
        self.window = window
        self.share = share
        self.consecutive = 0
        self.total_failures = 0
        self._recent: deque[bool] = deque(maxlen=window)

    def record(self, ok: bool) -> None:
        self._recent.append(not ok)
        if ok:
            self.consecutive = 0
        else:
            self.consecutive += 1
            self.total_failures += 1

    @property
    def recent_failures(self) -> int:
        return sum(self._recent)

    @property
    def share_exceeded(self) -> bool:
        return len(self._recent) >= self.window and self.recent_failures > self.share * self.window

    @property
    def tripped(self) -> bool:
        return self.consecutive >= self.limit or self.share_exceeded

    @property
    def reason(self) -> str:
        if self.consecutive >= self.limit:
            return f"{self.consecutive} consecutive failed requests"
        return f"{self.recent_failures} of the last {self.window} requests failed"


async def _fetch_search(
    session: aiohttp.ClientSession, body: str, timeout: aiohttp.ClientTimeout
) -> tuple[int, str]:
    """
    One search request, UNRETRIED. Returns ``(http_status, body)``.

    A 429 or 403 is returned to the caller -- under the #442 stopping rule the
    first one ends the run, so retrying here would be a second request into a
    refusal. A 503 and any other non-200 raise ``ClientResponseError``, which
    the driver's paced retry wrapper catches beside transport errors; every
    retry attempt takes its own pacer slot there, which is why the retry is
    not a decorator on this function.
    """
    async with session.post(
        RPC_URL, data=body, headers={"content-type": RPC_CONTENT_TYPE}, timeout=timeout
    ) as resp:
        if resp.status in (429, 403):
            return resp.status, await _body_of(resp)
        if resp.status == 503:
            retry_after = resp.headers.get("Retry-After")
            if retry_after:
                try:
                    await asyncio.sleep(min(float(retry_after), 30.0))
                except ValueError:
                    pass
        resp.raise_for_status()
        return resp.status, await _body_of(resp)


async def _body_of(resp) -> str:
    """
    The response body as text, or a transport failure.

    ``resp.text()`` raises ``UnicodeDecodeError`` on a body that does not match
    its declared charset and ``LookupError`` on a charset Python does not know;
    neither is an ``aiohttp.ClientError``, so unwrapped it would escape the
    driver's retry and breaker (exit 1 with a traceback, no raw line, no latch,
    and the same request re-sent on every restart). Undecodable bytes are
    replaced so a mis-declared charset still yields a body to keep, and what
    still fails is re-raised as the payload error the driver already handles.
    """
    try:
        return await resp.text(errors="replace")
    except (UnicodeError, LookupError) as exc:
        raise aiohttp.ClientPayloadError(f"undecodable body: {exc}") from exc


# ── Work-list schema (issue #442) ───────────────────────────────────────────
#
# The unit of work is a road-walk SAMPLE POINT, keyed (edge_id, sample_idx) on
# the walk's frozen network, with the walk's current ("anchor") pano beside it.
# scripts/build_gsv_history_worklist.py writes these files on the catalog host;
# the crawler needs nothing but the file. Pass 1 rows are each edge's midpoint
# sample, pass 2 the rest, so the two passes query disjoint points and together
# cost no more than pass 2 alone.

WORKLIST_COLUMNS = [
    "city_id",
    "walk_csv_filename",
    "network_type",
    "spacing_m",
    "edge_id",
    "sample_idx",
    "edge_samples",
    "pass",
    "query_lat",
    "query_lon",
    "anchor_status",
    "anchor_pano_id",
    "anchor_capture_date",
    "anchor_copyright",
]

# One row per query made (the resume ledger). A key is DONE when a row for it
# has an empty `unanswered` field; a row with that field set records a 200
# whose body was not an answer (the reason, ledger-sized) and leaves the key
# to be re-queried. `upload_date` and `exact_timestamp` are the current pano's,
# as served (the raw node, JSON-encoded, and the raw exact-time field).
QUERY_COLUMNS = [
    "edge_id",
    "sample_idx",
    "http_status",
    "response_code",
    "panos_total",
    "panos_dated",
    "panos_third_party",
    "anchor_in_history",
    "current_copyright",
    "country_code",
    "street_name",
    "upload_date",
    "exact_timestamp",
    "fetched_at",
    "unanswered",
]
# The fields a ledger row must carry to count at all. `unanswered` is read
# when present and treated as empty when the column is missing, so a ledger
# written before the column existed (every key in it was answered) still
# resumes; the two per-location fields are optional the same way.
QUERY_REQUIRED_COLUMNS = [
    c for c in QUERY_COLUMNS if c not in ("unanswered", "upload_date", "exact_timestamp")
]

# One row per (sample point, dated pano the search returned).
SAMPLE_HISTORY_COLUMNS = [
    "city_id",
    "walk_csv_filename",
    "pass",
    "edge_id",
    "sample_idx",
    "query_lat",
    "query_lon",
    "anchor_pano_id",
    "anchor_capture_date",
    "pano_id",
    "capture_date",
    "pano_lat",
    "pano_lon",
    "third_party",
    "is_current",
    "capture_day",
    "pano_type",
    "elevation_m",
    "heading_deg",
    "pitch_deg",
    "roll_deg",
    "harvested_at",
]

# Sits beside the blocked family (75/76/81/84) but outside HOST_EXIT_CODES on
# purpose: the scheduler never runs this program and it takes no host lock, so
# nothing classifies the code as a night-level host verdict. 83 (crawl
# incomplete, resume) keeps its repo-wide meaning.
HISTORY_BLOCKED_EXIT_CODE = 86
# Another harvester holds this host's crawl lock. 87 is the Mapillary census's
# throttled code (#444), so this continues past it; download_common's numbering
# note records both skips.
HISTORY_BUSY_EXIT_CODE = 88

LATCH_FILENAME = "blocked.json"
LOCK_FILENAME = "crawl.lock"
DEFAULT_COOLDOWN_HOURS = 24.0
DEFAULT_RATE_PER_MINUTE = 60  # 1 request/s mean
DEFAULT_JITTER = 0.6  # coefficient of variation, as the Mapillary pacer (#292)
DEFAULT_BREAKER_LIMIT = 8
DEFAULT_BREAKER_WINDOW = 50
DEFAULT_BREAKER_SHARE = 0.25


def load_worklist(path: str) -> pd.DataFrame:
    """Read a work-list file and check its schema; keys become (str, int)."""
    df = pd.read_csv(
        path,
        dtype={
            "city_id": str,
            "walk_csv_filename": str,
            "edge_id": str,
            "anchor_status": str,
            "anchor_pano_id": str,
            "anchor_capture_date": str,
            "anchor_copyright": str,
        },
    )
    missing = [c for c in WORKLIST_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(f"{path}: work list is missing columns {missing}")
    df["sample_idx"] = df["sample_idx"].astype(int)
    df["pass"] = df["pass"].astype(int)
    if df.duplicated(["edge_id", "sample_idx"]).any():
        raise ValueError(f"{path}: duplicate (edge_id, sample_idx) keys")
    return df


def output_paths(out_dir: str, stem: str, pass_no: int) -> dict[str, str]:
    """The three per-pass output files for a work list."""
    base = os.path.join(out_dir, f"{stem}.pass{pass_no}")
    return {
        "raw": base + ".raw.jsonl.gz",
        "queries": base + ".queries.csv.gz",
        "history": base + ".history.csv.gz",
    }


def _append_gz(path: str, text: str) -> None:
    """Append one gzip MEMBER. Readers see every earlier member even if this one is cut."""
    if not text:
        return
    with gzip.open(path, "ab") as f:
        f.write(text.encode("utf-8"))


# How much of a gzip file the member scan reads at a time, and how much of
# that it hands a decompressor per call. One chunk is all it ever holds,
# whatever the file size; the piece bounds how far past a member's end the
# scan can have fed before it notices, which is what keeps the scan linear.
_SCAN_CHUNK = 1 << 18
_SCAN_PIECE = 1 << 12


def _complete_members_end(fobj) -> int:
    """
    Byte offset just past the last COMPLETE gzip member readable from ``fobj``
    (a binary file positioned at its start).

    LINEAR in the file size: the file is read chunk by chunk and fed to a
    decompressor a piece at a time; when a member ends inside a piece, the
    piece's unused remainder (at most one piece) starts the next member's
    decompressor. So every byte is handed over once, plus at most a piece per
    member, and nothing is sliced from or held as the whole file. A first
    version sliced ``data[pos:]`` per member and so read a file of N members
    about N/2 times over -- about 30 minutes per start on a 1 M-sample city's
    raw store, with the whole file in RAM.
    """
    good_end = 0
    read_to = 0  # file offset just past the last chunk read
    chunk = memoryview(b"")
    off = 0  # into chunk: the first byte not yet fed
    carry = b""  # a finished member's unused remainder, fed before the chunk continues
    d = zlib.decompressobj(16 + zlib.MAX_WBITS)
    while True:
        if carry:
            piece, carry = carry, b""
        else:
            if off >= len(chunk):
                data = fobj.read(_SCAN_CHUNK)
                if not data:
                    break
                chunk, off = memoryview(data), 0
                read_to += len(data)
            piece = chunk[off : off + _SCAN_PIECE]
            off += len(piece)
        piece_end = read_to - (len(chunk) - off)  # file offset just past this piece
        try:
            d.decompress(piece)
        except zlib.error:
            break  # damage: nothing past the last whole member is trusted
        if d.eof:
            carry = d.unused_data  # what followed the member: the next one, or nothing
            good_end = piece_end - len(carry)
            d = zlib.decompressobj(16 + zlib.MAX_WBITS)
    return good_end


def _scan_gz(path: str) -> tuple[int, int]:
    """``(bytes of complete members, bytes of a cut or damaged tail)`` of a gzip file."""
    with open(path, "rb") as f:
        good_end = _complete_members_end(f)
        size = os.fstat(f.fileno()).st_size
    return good_end, size - good_end


class _PrefixReader(io.RawIOBase):
    """The first ``limit`` bytes of ``fobj``, as a raw stream."""

    def __init__(self, fobj, limit: int):
        self._fobj = fobj
        self._left = limit

    def readable(self) -> bool:
        return True

    def readinto(self, b) -> int:
        n = min(len(b), self._left)
        if n <= 0:
            return 0
        data = self._fobj.read(n)
        b[: len(data)] = data
        self._left -= len(data)
        return len(data)


@contextmanager
def open_complete_members(path: str) -> Iterator[tuple[io.TextIOBase, int]]:
    """
    A text stream over the COMPLETE gzip members of ``path``, and the bytes
    of a cut or damaged tail that were left out.

    This is how every reader of the crawl's files sees a file a crash cut
    short (or one copied while the crawler was writing): the members before
    the cut, whole, and nothing of the cut one. Streams, never materializing
    the file; one linear scan finds the cut, then the decompressor reads up to
    it.
    """
    with open(path, "rb") as f:
        good_end = _complete_members_end(f)
        dropped = os.fstat(f.fileno()).st_size - good_end
        f.seek(0)
        prefix = io.BufferedReader(_PrefixReader(f, good_end))
        with gzip.GzipFile(fileobj=prefix, mode="rb") as gz:
            with io.TextIOWrapper(gz, encoding="utf-8", newline="") as text:
                yield text, dropped


def repair_gz_tail(path: str) -> int:
    """
    Cut a gzip file back to the end of its last COMPLETE member.

    A crash mid-flush leaves the last member short. Appending after it would
    bury every later member behind damage no sequential reader can cross, so
    each run repairs its three files before it appends. Returns the bytes
    dropped (0 when the file is whole or absent). The rows in the dropped
    member were never recorded as done -- ``read_done_keys`` reads complete
    members only, and the ledger is written last -- so they are re-queried.
    A file whose only member was cut is left at 0 bytes, which ``flush``
    treats as new (the header is written again), never as a headerless file.
    """
    if not os.path.exists(path):
        return 0
    good_end, dropped = _scan_gz(path)
    if dropped:
        with open(path, "r+b") as f:
            f.truncate(good_end)
        logger.warning("Dropped %d damaged trailing bytes from %s", dropped, path)
    return dropped


def _first_header(path: str) -> list[str] | None:
    """The header row of ``path``'s first complete member, or None for no complete row."""
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        return None
    with open_complete_members(path) as (text, _dropped):
        line = text.readline()
    return next(csv.reader([line])) if line.strip() else None


def check_output_shapes(paths: dict[str, str]) -> None:
    """
    Refuse an out-dir whose history or queries file was written under other columns.

    An append writes no header, and every reader maps rows through the FIRST
    member's header, so rows of this crawler's shape appended to a file of
    another land shifted and read back wrong (a pilot-era 12-column ledger read
    900 of this crawler's 15-column rows with ``upload_date`` in ``fetched_at``
    and ``unanswered`` under no name at all, so every unanswered key was done).
    Checked before the first request; the operator starts a fresh out-dir or
    rebuilds the files from the raw store with ``scripts/reparse_gsv_history_raw.py``.
    """
    for name, columns in (("queries", QUERY_COLUMNS), ("history", SAMPLE_HISTORY_COLUMNS)):
        header = _first_header(paths[name])
        if header is not None and header != columns:
            raise OutputShapeError(
                f"{paths[name]}: written under another column set -- its header is "
                f"{','.join(header)} and this crawler writes {','.join(columns)}. Appending "
                "would shift every row; use a fresh --out-dir, or rebuild the files from the "
                "raw store with scripts/reparse_gsv_history_raw.py."
            )


def read_done_keys(queries_path: str) -> set[tuple[str, int]]:
    """
    Keys already answered, read back from the queries ledger.

    Only COMPLETE gzip members count: a member a crash cut short is ignored
    whole (and ``repair_gz_tail`` drops it before the next append), so what
    this returns and what a plain reader sees after the repair agree. A row
    whose ``unanswered`` field is set (a 200 that was not an answer) does not
    make its key done -- the row records why, and the key is re-queried on
    resume. A ledger whose header lacks a required column, or a row with more
    or fewer fields than that header (a member written under another column
    set, which a plain ``DictReader`` would map through the first member's
    header, shifted), is REFUSED with ``OutputShapeError`` rather than read.
    """
    done: set[tuple[str, int]] = set()
    if not os.path.exists(queries_path):
        return done
    with open_complete_members(queries_path) as (text, _dropped):
        reader = csv.DictReader(text)
        header = reader.fieldnames or []
        missing = [c for c in QUERY_REQUIRED_COLUMNS if c not in header]
        if header and missing:
            raise OutputShapeError(
                f"{queries_path}: the ledger header lacks {','.join(missing)} "
                f"(header: {','.join(header)}); not a ledger this crawler can resume"
            )
        for lineno, row in enumerate(reader, start=2):
            if None in row or any(v is None for v in row.values()):
                fields = len(header) - sum(v is None for v in row.values()) + len(row.get(None, []))
                raise OutputShapeError(
                    f"{queries_path} line {lineno}: {fields} fields under a {len(header)}-field "
                    f"header ({','.join(header)}) -- a member written under another column set"
                )
            if row["edge_id"] == "edge_id":
                raise OutputShapeError(
                    f"{queries_path} line {lineno}: a second header row "
                    f"({','.join(row.values())}) -- a member written under another column set"
                )
            if row["edge_id"] == "" or row["sample_idx"] == "":
                continue
            if row.get("unanswered"):
                continue
            try:
                done.add((row["edge_id"], int(row["sample_idx"])))
            except ValueError:
                continue
    return done


def load_queries_ledger(queries_path: str) -> pd.DataFrame:
    """
    Every row of a queries ledger: one per REQUEST, unanswered rows included.

    For an analyzer counting requests; keys done come from ``read_done_keys``.
    Complete members only, through the streaming reader, so a ledger a crash
    cut short (or copied mid-write) reads to the cut instead of raising.
    """
    with open_complete_members(queries_path) as (text, _dropped):
        return pd.read_csv(
            text, dtype={"edge_id": str, "unanswered": str, "street_name": str, "country_code": str}
        )


def default_latch_dir(out_dir: str) -> str:
    """
    Where the latch lives unless told otherwise: the out-dir's PARENT.

    A refusal is per ADDRESS, not per work list, so the latch has to be shared
    by every crawl this host runs. Each work list writes its three files under
    its own out-dir, and those sit under one results directory, so the parent
    is the one place they all see; a timer that gave each city its own out-dir
    would otherwise leave the next city unlatched on the same address.
    """
    return os.path.dirname(os.path.abspath(out_dir))


@contextmanager
def crawl_lock(latch_dir: str) -> Iterator[None]:
    """
    Hold this host's crawl lock (``crawl.lock`` beside the latch) for the block.

    "One request in flight" is a property of one process; two harvesters on
    one address (pass 2 started beside pass 1, two cities, a tranche still
    running when a daily timer fires the next) present double the paced rate
    to an IP-identified endpoint. The lock lives in the latch dir for the
    same reason the latch does -- a refusal, and the pace, are the address's
    -- and is a ``flock`` (released by the kernel when the holder dies, so a
    stale file never wedges a restart). Raises ``HarvestBusyError`` at once,
    never queues: the second crawl is wrong to start, not early.
    """
    os.makedirs(latch_dir, exist_ok=True)
    path = os.path.join(latch_dir, LOCK_FILENAME)
    lock = FileLock(path, timeout=0)
    try:
        lock.acquire()
    except Timeout:
        raise HarvestBusyError(
            f"another harvester holds this host's crawl lock ({path}); one crawl per address "
            "-- wait for it to finish rather than doubling the paced rate. (The lock is the "
            "flock, not the file: if its holder is gone the lock is already free.)"
        ) from None
    try:
        yield
    finally:
        lock.release()


def _latch_path(latch_dir: str) -> str:
    return os.path.join(latch_dir, LATCH_FILENAME)


def write_latch(
    latch_dir: str,
    *,
    reason: str,
    http_status: int | None,
    cooldown_hours: float,
    now: datetime | None = None,
) -> str:
    """Record a refusal and when a restart may be attempted."""
    now = now or datetime.now(UTC)
    Path(latch_dir).mkdir(parents=True, exist_ok=True)
    path = _latch_path(latch_dir)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(
            {
                "at": now.isoformat(),
                "cooldown_until": (now + timedelta(hours=cooldown_hours)).isoformat(),
                "reason": reason,
                "http_status": http_status,
            },
            f,
            indent=2,
        )
    os.replace(tmp, path)
    return path


def latch_active(latch_dir: str, now: datetime | None = None) -> dict[str, Any] | None:
    """
    The latch record if its cool-down has not expired, else None.

    A hand-edited ``cooldown_until`` with no zone is read as UTC (the latch is
    written in UTC), rather than raising on the comparison with an aware now.
    """
    path = _latch_path(latch_dir)
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            record = json.load(f)
        until = datetime.fromisoformat(record["cooldown_until"])
    except (ValueError, KeyError, OSError, TypeError):
        # An unreadable latch still means "something refused us"; treat it as
        # active rather than silently restarting.
        return {"cooldown_until": None, "reason": f"unreadable latch at {path}"}
    if until.tzinfo is None:
        until = until.replace(tzinfo=UTC)
    now = now or datetime.now(UTC)
    return record if until > now else None


def clear_latch(latch_dir: str) -> bool:
    path = _latch_path(latch_dir)
    if os.path.exists(path):
        os.remove(path)
        return True
    return False


def _anchor_of(row) -> tuple[str | None, str | None]:
    anchor = (
        row.anchor_pano_id if isinstance(row.anchor_pano_id, str) and row.anchor_pano_id else None
    )
    anchor_date = row.anchor_capture_date if isinstance(row.anchor_capture_date, str) else None
    return anchor, anchor_date


def query_row(row, result: SearchResult, http_status: int, fetched_at: str) -> dict[str, Any]:
    """One queries-ledger record for a work-list row and its parsed response."""
    anchor, _ = _anchor_of(row)
    return {
        "edge_id": row.edge_id,
        "sample_idx": int(row.sample_idx),
        "http_status": http_status,
        "response_code": result.response_code,
        "panos_total": result.panos_total,
        "panos_dated": len(result.panos),
        "panos_third_party": sum(1 for p in result.panos if p.third_party),
        "anchor_in_history": (
            None if anchor is None else any(p.pano_id == anchor for p in result.panos)
        ),
        "current_copyright": result.copyright,
        "country_code": result.country_code,
        "street_name": result.street_name,
        "upload_date": result.upload_date,
        "exact_timestamp": result.exact_timestamp,
        "fetched_at": fetched_at,
        "unanswered": result.unanswered,
    }


def history_rows(row, result: SearchResult, pass_no: int, fetched_at: str) -> list[dict[str, Any]]:
    """The per-sample history records (one per dated pano) for a work-list row."""
    anchor, anchor_date = _anchor_of(row)
    return [
        {
            "city_id": row.city_id,
            "walk_csv_filename": row.walk_csv_filename,
            "pass": pass_no,
            "edge_id": row.edge_id,
            "sample_idx": int(row.sample_idx),
            "query_lat": float(row.query_lat),
            "query_lon": float(row.query_lon),
            "anchor_pano_id": anchor,
            "anchor_capture_date": anchor_date,
            "pano_id": p.pano_id,
            "capture_date": p.capture_date,
            "pano_lat": p.lat,
            "pano_lon": p.lon,
            "third_party": p.third_party,
            "is_current": p.is_current,
            "capture_day": p.capture_day,
            "pano_type": p.pano_type,
            "elevation_m": p.elevation_m,
            "heading_deg": p.heading_deg,
            "pitch_deg": p.pitch_deg,
            "roll_deg": p.roll_deg,
            "harvested_at": fetched_at,
        }
        for p in result.panos
    ]


def rebuild_from_raw(
    raw_path: str, worklist: pd.DataFrame, pass_no: int
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    Re-parse a raw store with the CURRENT parser into (history, queries) frames.

    The raw file is the irreplaceable copy; the parsed files are a view of it.
    A better parser is applied by re-running this, never by re-querying. A key
    that appears more than once in the raw store (a re-query after a cut
    ledger, or after an unanswered 200) keeps its LAST ANSWER; a refusal line
    (429/403) is never an answer and never replaces one, and an unanswered
    body is kept in the queries frame with its reason only when the key has
    no answer at all, as the crawl's ledger records it. Only COMPLETE gzip
    members are read, so a raw file whose final member a crash cut short (or
    that was copied mid-write) rebuilds from everything before the cut;
    ``raw_store_cut_bytes`` says how much was left out.

    The frames are object-typed so that what the crawl wrote as an int (a
    served capture day, a response code) is still an int, and ``None`` is
    still ``None``: written back through ``write_csv_gz`` the text is
    byte-identical to the crawl's own.
    """
    rows = worklist.loc[worklist["pass"] == pass_no]
    by_key = {(r.edge_id, int(r.sample_idx)): r for r in rows.itertuples(index=False)}
    answers: dict[tuple[str, int], dict] = {}
    unanswered: dict[tuple[str, int], dict] = {}
    with open_complete_members(raw_path) as (text, _dropped):
        for line in text:
            rec = json.loads(line)
            if rec["http_status"] in (429, 403):
                continue  # a refusal is not an answer and never replaces one
            key = (rec["edge_id"], int(rec["sample_idx"]))
            if parse_search(rec["body"]).unanswered:
                unanswered[key] = rec
            else:
                answers[key] = rec
    history: list[dict] = []
    queries: list[dict] = []
    for key, rec in {**unanswered, **answers}.items():
        row = by_key.get(key)
        if row is None:
            continue  # a raw line from another work list; not this pass's
        result = parse_search(rec["body"])
        queries.append(query_row(row, result, rec["http_status"], rec["fetched_at"]))
        history.extend(history_rows(row, result, pass_no, rec["fetched_at"]))
    return (
        pd.DataFrame(history, columns=SAMPLE_HISTORY_COLUMNS, dtype=object),
        pd.DataFrame(queries, columns=QUERY_COLUMNS, dtype=object),
    )


def raw_store_cut_bytes(raw_path: str) -> int:
    """Bytes of a cut or damaged final member a reader of ``raw_path`` leaves out."""
    return _scan_gz(raw_path)[1]


def write_csv_gz(frame: pd.DataFrame, path: str, columns: list[str]) -> None:
    """
    Write an object-typed frame as ONE gzip member through the crawl's own
    CSV writer, so the text equals what the crawl would have appended.
    """
    records = [
        dict(zip(columns, vals, strict=True)) for vals in frame[columns].itertuples(index=False)
    ]
    with gzip.open(path, "wb") as f:
        f.write(_csv_text(records, columns, header=True).encode("utf-8"))


def load_sample_history(history_path: str, queries_path: str | None = None) -> pd.DataFrame:
    """
    Read a per-sample history file as the crawl meant it: one answer per key.

    The crawl appends history rows BEFORE the ledger row that marks their key
    done, so a crash between the two leaves the rows on disk, the key
    un-done, and the resume appends the key's rows a second time. The file is
    never rewritten; this reader applies the rule instead: for each
    ``(edge_id, sample_idx)`` only the rows of its LAST query (the greatest
    ``harvested_at``) are kept -- the same "last answer wins" as
    ``rebuild_from_raw`` -- and, when the queries ledger is given, rows whose
    key the ledger does not hold as done are dropped (rows a crash stranded
    before any ledger row). Only complete gzip members are read. Every
    importer and analyzer reads through this, never ``pd.read_csv`` directly.
    """
    with open_complete_members(history_path) as (text, _dropped):
        df = pd.read_csv(
            text, dtype={"edge_id": str, "pano_id": str, "anchor_pano_id": str, "city_id": str}
        )
    if df.empty:
        return df
    last = df.groupby(["edge_id", "sample_idx"])["harvested_at"].transform("max")
    df = df[df["harvested_at"] == last]
    if queries_path is not None:
        done = read_done_keys(queries_path)
        keep = [(e, int(i)) in done for e, i in zip(df["edge_id"], df["sample_idx"], strict=True)]
        df = df[keep]
    return df.reset_index(drop=True)


class HarvestIncompleteError(DownloadError):
    """
    The run stopped with rows unanswered and its outputs on disk: the request
    cap was reached, or rows kept failing after the retry pass. Re-run to
    resume -- the queries ledger is the checkpoint. Deliberately NOT a
    HarvestBlockedError: a cap is the opposite of a refusal.
    """

    def __init__(self, message: str, *, api_requests: int, remaining: int, capped: bool):
        super().__init__(message)
        self.api_requests = api_requests
        self.remaining = remaining
        self.capped = capped


async def harvest_worklist_async(
    worklist: pd.DataFrame,
    out_dir: str,
    *,
    stem: str,
    pass_no: int,
    rate_per_minute: float = DEFAULT_RATE_PER_MINUTE,
    jitter: float = DEFAULT_JITTER,
    max_requests: int | None = None,
    breaker_limit: int = DEFAULT_BREAKER_LIMIT,
    breaker_window: int = DEFAULT_BREAKER_WINDOW,
    breaker_share: float = DEFAULT_BREAKER_SHARE,
    chunk_size: int = 50,
    request_timeout: float = 30,
    cooldown_hours: float = DEFAULT_COOLDOWN_HOURS,
    retry_passes: int = 1,
    fetch_tries: int = 4,
    fetch_backoff_factor: float = 1.0,
    latch_dir: str | None = None,
    now_func=None,
) -> dict[str, Any]:
    """
    Query the endpoint once per work-list row of ``pass_no`` and stream the
    answers to three gzip files under ``out_dir`` (see ``output_paths``).

    ONE request in flight, paced by ``AsyncRateLimiter`` at ``rate_per_minute``
    with coefficient of variation ``jitter``; the pace, not sockets, governs.
    Stops on the FIRST 429/403, on the first HTML page served with a 200 (an
    interstitial is a refusal in disguise), on ``breaker_limit`` consecutive
    failed REQUESTS (transport failures, retries included, and 200s whose
    body is not an answer -- once the breaker has tripped no further attempt
    is sent) or on failed requests above ``breaker_share`` of the last
    ``breaker_window``, writes the ``blocked.json`` latch under ``latch_dir``
    (default ``default_latch_dir(out_dir)``, the out-dir's parent: one latch
    per host) and raises HarvestBlockedError; a latch already in force
    refuses the run before its first request, as does another harvester
    holding the host's crawl lock (``HarvestBusyError``), and an out-dir
    whose files were written under another column set (``OutputShapeError``,
    before any request). Stops cleanly at ``max_requests`` (no latch)
    with HarvestIncompleteError, which is also raised when rows still fail
    after ``retry_passes``. The cap is checked before each row, so it is a
    SOFT ceiling: the row in flight may still retry, at most
    ``fetch_tries - 1`` requests past it.

    Every request's verbatim body goes to the raw file first -- the one copy
    that survives a parser that no longer matches the endpoint. A 200 whose
    body is not an answer (``SearchResult.unanswered``) is kept there and
    recorded in the ledger with its reason, but its key is NOT done: it is
    re-queried in the retry pass and on resume.

    Returns the counters when every row of the pass is answered.
    """
    now_func = now_func or (lambda: datetime.now(UTC))
    latch_dir = latch_dir or default_latch_dir(out_dir)
    with crawl_lock(latch_dir):
        # The latch is read UNDER the lock: a crawl that trips writes
        # blocked.json while holding it, so a second crawl that read the latch
        # first and took the lock second would send after the refusal.
        latch = latch_active(latch_dir, now=now_func())
        if latch is not None:
            raise HarvestBlockedError(
                f"a block latch is in force ({latch.get('reason')}); cool-down until "
                f"{latch.get('cooldown_until')} ({_latch_path(latch_dir)})"
            )
        rows = worklist.loc[worklist["pass"] == pass_no].reset_index(drop=True)
        paths = output_paths(out_dir, stem, pass_no)
        Path(out_dir).mkdir(parents=True, exist_ok=True)
        for path in paths.values():
            repair_gz_tail(path)
        check_output_shapes(paths)
        done = read_done_keys(paths["queries"])
        pending = [
            r for r in rows.itertuples(index=False) if (r.edge_id, int(r.sample_idx)) not in done
        ]
        logger.info(
            "Harvesting GSV history for %s pass %d: %d of %d rows to query (%d already answered), "
            "%s/min mean, jitter %.2f, one in flight",
            stem,
            pass_no,
            len(pending),
            len(rows),
            len(done),
            rate_per_minute,
            jitter,
        )

        limiter = AsyncRateLimiter(rate_per_minute, jitter=jitter)
        breaker = _CircuitBreaker(limit=breaker_limit, window=breaker_window, share=breaker_share)
        timeout = aiohttp.ClientTimeout(total=request_timeout)
        bar = progress(
            total=len(rows),
            initial=len(done),
            desc=f"GSV history {stem} pass {pass_no}",
            unit="sample",
            logger=logger,
        )

        api_requests = 0
        answered = 0
        last_failure = "none"

        def _record_failed_attempt(details) -> None:
            # backoff's on_backoff (before a retry) and on_giveup (after the last
            # try) between them see every failed attempt exactly once.
            nonlocal last_failure
            breaker.record(ok=False)
            last_failure = f"{type(details['exception']).__name__}: {details['exception']}"[:80]

        @backoff.on_exception(
            backoff.expo,
            (asyncio.TimeoutError, aiohttp.ClientError),
            max_tries=fetch_tries,
            max_time=90,
            factor=fetch_backoff_factor,
            on_backoff=_record_failed_attempt,
            on_giveup=_record_failed_attempt,
        )
        async def paced_fetch(session, request_body):
            # Every attempt, retries included, waits for its own pacer slot and is
            # counted: a retry is a request the endpoint sees (issue #198). None is
            # sent once the breaker has tripped -- the retry wrapper does not catch
            # _BreakerTripped, so it ends the row at once.
            nonlocal api_requests
            if breaker.tripped:
                raise _BreakerTripped(last_failure)
            await limiter.acquire()
            api_requests += 1
            return await _fetch_search(session, request_body, timeout)

        blocked: tuple[str, int | None] | None = None
        capped = False
        raw_buf: list[str] = []
        query_buf: list[dict] = []
        history_buf: list[dict] = []

        def _is_new(path: str) -> bool:
            # A file repair_gz_tail cut back to nothing (its only member was cut)
            # exists at 0 bytes; it needs its header exactly as an absent one does,
            # or every later row is appended headerless and no reader sees a key.
            return not os.path.exists(path) or os.path.getsize(path) == 0

        def flush() -> None:
            # Raw first, history second, the queries LEDGER last: a key counts as
            # done only once everything it stands for is on disk, so a crash
            # between two appends can leave a key unrecorded (re-queried, and its
            # history rows appended a second time -- `load_sample_history` keeps
            # a key's last query only) but never recorded with holes.
            _append_gz(paths["raw"], "".join(raw_buf))
            raw_buf.clear()
            if history_buf:
                _append_gz(
                    paths["history"],
                    _csv_text(
                        history_buf, SAMPLE_HISTORY_COLUMNS, header=_is_new(paths["history"])
                    ),
                )
                history_buf.clear()
            if query_buf:
                _append_gz(
                    paths["queries"],
                    _csv_text(query_buf, QUERY_COLUMNS, header=_is_new(paths["queries"])),
                )
                query_buf.clear()

        def _tripped_reason() -> str:
            return f"{breaker.reason} (last: {last_failure})"

        headers = {"User-Agent": _USER_AGENT}
        try:
            async with aiohttp.ClientSession(headers=headers) as session:
                for sweep in range(1 + retry_passes):
                    todo = [r for r in pending if (r.edge_id, int(r.sample_idx)) not in done]
                    if not todo or blocked or capped:
                        break
                    if sweep > 0:
                        logger.info(
                            "Retry pass %d/%d: %d failed rows", sweep, retry_passes, len(todo)
                        )
                    for r in todo:
                        if max_requests is not None and api_requests >= max_requests:
                            capped = True
                            break
                        key = (r.edge_id, int(r.sample_idx))
                        request_body = build_search_body(float(r.query_lat), float(r.query_lon))
                        fetched_at = now_func().isoformat()
                        try:
                            status, body = await paced_fetch(session, request_body)
                        except (TimeoutError, aiohttp.ClientError, _BreakerTripped) as e:
                            logger.debug("transport failure at %s: %s", key, e)
                            if breaker.tripped:
                                blocked = (_tripped_reason(), None)
                                break
                            continue
                        raw_buf.append(
                            json.dumps(
                                {
                                    "edge_id": key[0],
                                    "sample_idx": key[1],
                                    "query_lat": float(r.query_lat),
                                    "query_lon": float(r.query_lon),
                                    "url": RPC_URL,
                                    "request": request_body,
                                    "http_status": status,
                                    "fetched_at": fetched_at,
                                    "body": body,
                                }
                            )
                            + "\n"
                        )
                        if status in (429, 403):
                            blocked = (f"HTTP {status} from the endpoint", status)
                            break
                        result = parse_search(body)
                        if result.unanswered:
                            # A 200 that answered nothing: the body is in the raw
                            # store and the reason in the ledger, the key stays
                            # un-done, and it counts as a failed request -- a run
                            # of these is a refusal wearing a 200.
                            logger.warning("Unanswered 200 at %s: %s", key, result.unanswered)
                            breaker.record(ok=False)
                            last_failure = result.unanswered
                            query_buf.append(query_row(r, result, status, fetched_at))
                            if result.refusal:
                                # An HTML page is never an answer and never
                                # imagery: it is the endpoint refusing, with a
                                # 200 on the envelope. The first one latches.
                                blocked = (
                                    f"HTML page served with a 200: {result.unanswered}",
                                    None,
                                )
                                break
                            if breaker.tripped:
                                blocked = (_tripped_reason(), None)
                                break
                        else:
                            breaker.record(ok=True)
                            query_buf.append(query_row(r, result, status, fetched_at))
                            history_buf.extend(history_rows(r, result, pass_no, fetched_at))
                            done.add(key)
                            answered += 1
                            bar.update(1)
                        if len(query_buf) >= chunk_size:
                            flush()
        finally:
            flush()
            bar.close()

        remaining = len(rows) - len(done)
        if blocked:
            reason, status = blocked
            latch = write_latch(
                latch_dir,
                reason=reason,
                http_status=status,
                cooldown_hours=cooldown_hours,
                now=now_func(),
            )
            raise HarvestBlockedError(
                f"Harvest stopped for {stem} pass {pass_no}: {reason}. {remaining} rows remain; "
                f"latch written to {latch} (cool-down {cooldown_hours:g} h)."
            )
        if capped:
            raise HarvestIncompleteError(
                f"Request cap {max_requests} reached for {stem} pass {pass_no}: {remaining} rows "
                "remain; re-run to resume.",
                api_requests=api_requests,
                remaining=remaining,
                capped=True,
            )
        if remaining:
            raise HarvestIncompleteError(
                f"{remaining} rows of {stem} pass {pass_no} still failed after {retry_passes} "
                "retry pass(es); re-run to resume.",
                api_requests=api_requests,
                remaining=remaining,
                capped=False,
            )
        logger.info(
            "Completed %s pass %d: %d rows answered this run from %d requests",
            stem,
            pass_no,
            answered,
            api_requests,
        )
        return {
            "rows": int(len(rows)),
            "answered": answered,
            "already_done": int(len(rows) - answered - remaining),
            "api_requests": api_requests,
            "paths": paths,
        }


def _csv_text(records: list[dict], columns: list[str], *, header: bool) -> str:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=columns, lineterminator="\n")
    if header:
        w.writeheader()
    for rec in records:
        w.writerow({c: ("" if rec.get(c) is None else rec.get(c)) for c in columns})
    return buf.getvalue()
