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
  * a stopping rule written down in advance (#442): the FIRST 429 or 403, or a
    run of transport failures, ends the run and writes a cool-down latch that
    refuses a restart for 24 h; a per-run request cap lets a timer run it in
    daily tranches;
  * every response body is kept verbatim, gzipped, before it is parsed — if the
    endpoint changes shape or disappears, the raw store is the only copy;
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

History is near-static (Google's archive), so a point is harvested ONCE and only
re-queried when its anchor pano changes; this is not part of the cadenced run
series.

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
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import aiohttp
import backoff
import numpy as np
import pandas as pd

from .download_common import AsyncRateLimiter, DownloadError, standardize_capture_date
from .progress import progress

logger = logging.getLogger(__name__)

# Schema of the per-city `_gsv_history_` artifact — one row per unique official
# Google pano. Distinct from config.METADATA_DTYPES (a per-grid-point run sample);
# this is a per-pano historical census, so it gets its own columns. Read by
# fileutils.load_history_csv_file and summarized by json_summarizer (#109); since
# #442 it is DERIVED from the per-sample SAMPLE_HISTORY_COLUMNS output below.
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

# The live route (measured 2026-10-17 from the pilot host): the RPC that the
# Maps JavaScript API itself calls. The older JSONP shim this module shipped
# with (`maps/api/js/GeoPhotoService.SingleImageSearch?pb=...&callback=`) now
# answers every query with HTTP 200 and the body
# `[[5,"generic","GeoPhotoService.SingleImageSearch is decommissioned and turned down."]]`
# -- 100 of 100 pilot requests, before the parser noticed anything. The request
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


@dataclass(frozen=True)
class SearchResult:
    """A parsed search response."""

    response_code: int | None  # 0 ok, 5 "Search returned no images.", None unparseable
    panos: tuple[HistoricalPano, ...]  # every DATED pano: the current one plus its history
    panos_total: int | None  # every pano node the response carries, dated or not


class HarvestBlockedError(DownloadError):
    """
    The endpoint refused us -- a 429/403, or a run of transport failures that
    tripped the circuit breaker. The outputs written so far stay on disk and
    the ``blocked.json`` latch records when a restart may be attempted.
    """


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
    """
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return SearchResult(None, (), None)
    try:
        code = int(data[0][0])
    except (IndexError, KeyError, TypeError, ValueError):
        return SearchResult(None, (), None)
    if code != 0:
        return SearchResult(code, (), 0)
    try:
        msg = data[1]
        main_id = str(msg[1][1])
        nodes = msg[5][0][3][0] or []
    except (IndexError, KeyError, TypeError):
        return SearchResult(code, (), None)
    try:
        main_latlon = (float(msg[5][0][1][0][2]), float(msg[5][0][1][0][3]))
    except (IndexError, KeyError, TypeError, ValueError):
        main_latlon = None
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

    def _node(node) -> tuple[str, bool, float | None, float | None] | None:
        try:
            pano_id = str(node[0][1])
        except (IndexError, KeyError, TypeError):
            return None
        try:
            kind = int(node[0][0])
            third = kind == PANO_TYPE_THIRD_PARTY
        except (IndexError, KeyError, TypeError, ValueError):
            third = is_third_party_pano_id(pano_id)
        try:
            lat, lon = float(node[2][0][2]), float(node[2][0][3])
        except (IndexError, KeyError, TypeError, ValueError):
            lat = lon = None
        return pano_id, third or is_third_party_pano_id(pano_id), lat, lon

    out: list[HistoricalPano] = []
    seen: set[str] = set()
    if main_date and main_latlon is not None:
        out.append(
            HistoricalPano(
                pano_id=main_id,
                capture_date=main_date,
                lat=main_latlon[0],
                lon=main_latlon[1],
                third_party=is_third_party_pano_id(main_id),
                is_current=True,
            )
        )
        seen.add(main_id)
    for idx, iso in sorted(dates.items()):
        if idx >= len(nodes) or not iso:
            continue
        parsed = _node(nodes[idx])
        if parsed is None:
            continue
        pano_id, third, lat, lon = parsed
        if pano_id in seen or lat is None:
            continue
        seen.add(pano_id)
        out.append(
            HistoricalPano(pano_id=pano_id, capture_date=iso, lat=lat, lon=lon, third_party=third)
        )
    return SearchResult(code, tuple(out), len(nodes))


def parse_search_response(text: str) -> list[HistoricalPano]:
    """Every dated pano in one response (the current one first); see ``parse_search``."""
    return list(parse_search(text).panos)


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
            return resp.status, await resp.text()
        if resp.status == 503:
            retry_after = resp.headers.get("Retry-After")
            if retry_after:
                try:
                    await asyncio.sleep(min(float(retry_after), 30.0))
                except ValueError:
                    pass
        resp.raise_for_status()
        return resp.status, await resp.text()


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

# One row per query made (the resume ledger; a key present here is done).
QUERY_COLUMNS = [
    "edge_id",
    "sample_idx",
    "http_status",
    "response_code",
    "panos_total",
    "panos_dated",
    "panos_third_party",
    "anchor_in_history",
    "fetched_at",
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
    "harvested_at",
]

# Sits beside the blocked family (75/76/81/84) but outside HOST_EXIT_CODES on
# purpose: the scheduler never runs this program and it takes no host lock, so
# nothing classifies the code as a night-level host verdict. 83 (crawl
# incomplete, resume) keeps its repo-wide meaning.
HISTORY_BLOCKED_EXIT_CODE = 86

LATCH_FILENAME = "blocked.json"
DEFAULT_COOLDOWN_HOURS = 24.0
DEFAULT_RATE_PER_MINUTE = 60  # 1 request/s mean
DEFAULT_JITTER = 0.6  # coefficient of variation, as the Mapillary pacer (#292)
DEFAULT_BREAKER_LIMIT = 8


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


def _complete_members_end(data: bytes) -> int:
    """Byte offset just past the last COMPLETE gzip member in ``data``."""
    good_end = 0
    pos = 0
    while pos < len(data):
        d = zlib.decompressobj(16 + zlib.MAX_WBITS)
        try:
            d.decompress(data[pos:])
        except zlib.error:
            break
        if not d.eof:
            break  # the member runs off the end of the file
        pos = len(data) - len(d.unused_data)
        good_end = pos
    return good_end


def repair_gz_tail(path: str) -> int:
    """
    Cut a gzip file back to the end of its last COMPLETE member.

    A crash mid-flush leaves the last member short. Appending after it would
    bury every later member behind damage no sequential reader can cross, so
    each run repairs its three files before it appends. Returns the bytes
    dropped (0 when the file is whole or absent). The rows in the dropped
    member were never recorded as done -- ``read_done_keys`` reads complete
    members only, and the ledger is written last -- so they are re-queried.
    """
    if not os.path.exists(path):
        return 0
    with open(path, "rb") as f:
        data = f.read()
    good_end = _complete_members_end(data)
    dropped = len(data) - good_end
    if dropped:
        with open(path, "r+b") as f:
            f.truncate(good_end)
        logger.warning("Dropped %d damaged trailing bytes from %s", dropped, path)
    return dropped


def read_done_keys(queries_path: str) -> set[tuple[str, int]]:
    """
    Keys already answered, read back from the queries ledger.

    Only COMPLETE gzip members count: a member a crash cut short is ignored
    whole (and ``repair_gz_tail`` drops it before the next append), so what
    this returns and what a plain reader sees after the repair agree. A line
    without every QUERY_COLUMNS field is dropped too.
    """
    done: set[tuple[str, int]] = set()
    if not os.path.exists(queries_path):
        return done
    with open(queries_path, "rb") as f:
        data = f.read()
    text = gzip.decompress(data[: _complete_members_end(data)]).decode("utf-8")
    for row in csv.DictReader(io.StringIO(text, newline="")):
        if any(row.get(c) in (None, "") for c in ("edge_id", "sample_idx")):
            continue
        if any(row.get(c) is None for c in QUERY_COLUMNS):
            continue
        try:
            done.add((row["edge_id"], int(row["sample_idx"])))
        except ValueError:
            continue
    return done


def _latch_path(out_dir: str) -> str:
    return os.path.join(out_dir, LATCH_FILENAME)


def write_latch(
    out_dir: str,
    *,
    reason: str,
    http_status: int | None,
    cooldown_hours: float,
    now: datetime | None = None,
) -> str:
    """Record a refusal and when a restart may be attempted."""
    now = now or datetime.now(UTC)
    path = _latch_path(out_dir)
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


def latch_active(out_dir: str, now: datetime | None = None) -> dict[str, Any] | None:
    """The latch record if its cool-down has not expired, else None."""
    path = _latch_path(out_dir)
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
    now = now or datetime.now(UTC)
    return record if until > now else None


def clear_latch(out_dir: str) -> bool:
    path = _latch_path(out_dir)
    if os.path.exists(path):
        os.remove(path)
        return True
    return False


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
    chunk_size: int = 50,
    request_timeout: float = 30,
    cooldown_hours: float = DEFAULT_COOLDOWN_HOURS,
    retry_passes: int = 1,
    fetch_tries: int = 4,
    fetch_backoff_factor: float = 1.0,
    now_func=None,
) -> dict[str, Any]:
    """
    Query the endpoint once per work-list row of ``pass_no`` and stream the
    answers to three gzip files under ``out_dir`` (see ``output_paths``).

    ONE request in flight, paced by ``AsyncRateLimiter`` at ``rate_per_minute``
    with coefficient of variation ``jitter``; the pace, not sockets, governs.
    Stops on the FIRST 429/403 or on ``breaker_limit`` consecutive transport
    failures, writes the ``blocked.json`` latch and raises HarvestBlockedError.
    Stops cleanly at ``max_requests`` (no latch) with HarvestIncompleteError,
    which is also raised when rows still fail after ``retry_passes``. The cap
    is checked before each row, so it is a SOFT ceiling: the row in flight may
    still retry, at most ``fetch_tries - 1`` requests past it.

    Every request's verbatim body goes to the raw file first -- the one copy
    that survives a parser that no longer matches the endpoint.

    Returns the counters when every row of the pass is answered.
    """
    now_func = now_func or (lambda: datetime.now(UTC))
    rows = worklist.loc[worklist["pass"] == pass_no].reset_index(drop=True)
    paths = output_paths(out_dir, stem, pass_no)
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    for path in paths.values():
        repair_gz_tail(path)
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
    breaker = _CircuitBreaker(limit=breaker_limit)
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

    @backoff.on_exception(
        backoff.expo,
        (asyncio.TimeoutError, aiohttp.ClientError),
        max_tries=fetch_tries,
        max_time=90,
        factor=fetch_backoff_factor,
    )
    async def paced_fetch(session, request_body):
        # Every attempt, retries included, waits for its own pacer slot and is
        # counted: a retry is a request the endpoint sees (issue #198).
        nonlocal api_requests
        await limiter.acquire()
        api_requests += 1
        return await _fetch_search(session, request_body, timeout)

    blocked: tuple[str, int | None] | None = None
    capped = False
    raw_buf: list[str] = []
    query_buf: list[dict] = []
    history_buf: list[dict] = []

    def flush() -> None:
        # Raw first, history second, the queries LEDGER last: a key counts as
        # done only once everything it stands for is on disk, so a crash
        # between two appends can leave a key unrecorded (re-queried, and its
        # history rows appended a second time -- importers dedupe on
        # (edge_id, sample_idx, pano_id)) but never recorded with holes.
        _append_gz(paths["raw"], "".join(raw_buf))
        raw_buf.clear()
        if history_buf:
            new_file = not os.path.exists(paths["history"])
            _append_gz(
                paths["history"], _csv_text(history_buf, SAMPLE_HISTORY_COLUMNS, header=new_file)
            )
            history_buf.clear()
        if query_buf:
            new_file = not os.path.exists(paths["queries"])
            _append_gz(paths["queries"], _csv_text(query_buf, QUERY_COLUMNS, header=new_file))
            query_buf.clear()

    headers = {"User-Agent": _USER_AGENT}
    try:
        async with aiohttp.ClientSession(headers=headers) as session:
            for sweep in range(1 + retry_passes):
                todo = [r for r in pending if (r.edge_id, int(r.sample_idx)) not in done]
                if not todo or blocked or capped:
                    break
                if sweep > 0:
                    logger.info("Retry pass %d/%d: %d failed rows", sweep, retry_passes, len(todo))
                for r in todo:
                    if max_requests is not None and api_requests >= max_requests:
                        capped = True
                        break
                    key = (r.edge_id, int(r.sample_idx))
                    request_body = build_search_body(float(r.query_lat), float(r.query_lon))
                    fetched_at = now_func().isoformat()
                    try:
                        status, body = await paced_fetch(session, request_body)
                    except (TimeoutError, aiohttp.ClientError) as e:
                        logger.debug("transport failure at %s: %s", key, e)
                        breaker.record(ok=False)
                        if breaker.tripped:
                            blocked = (
                                f"{breaker.consecutive} consecutive transport failures",
                                None,
                            )
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
                    breaker.record(ok=True)
                    result = parse_search(body)
                    found = result.panos
                    anchor = (
                        r.anchor_pano_id
                        if isinstance(r.anchor_pano_id, str) and r.anchor_pano_id
                        else None
                    )
                    anchor_in_history = (
                        None if anchor is None else any(p.pano_id == anchor for p in found)
                    )
                    query_buf.append(
                        {
                            "edge_id": key[0],
                            "sample_idx": key[1],
                            "http_status": status,
                            "response_code": result.response_code,
                            "panos_total": result.panos_total,
                            "panos_dated": len(found),
                            "panos_third_party": sum(1 for p in found if p.third_party),
                            "anchor_in_history": anchor_in_history,
                            "fetched_at": fetched_at,
                        }
                    )
                    for p in found:
                        history_buf.append(
                            {
                                "city_id": r.city_id,
                                "walk_csv_filename": r.walk_csv_filename,
                                "pass": pass_no,
                                "edge_id": key[0],
                                "sample_idx": key[1],
                                "query_lat": float(r.query_lat),
                                "query_lon": float(r.query_lon),
                                "anchor_pano_id": anchor,
                                "anchor_capture_date": r.anchor_capture_date
                                if isinstance(r.anchor_capture_date, str)
                                else None,
                                "pano_id": p.pano_id,
                                "capture_date": p.capture_date,
                                "pano_lat": p.lat,
                                "pano_lon": p.lon,
                                "third_party": p.third_party,
                                "is_current": p.is_current,
                                "harvested_at": fetched_at,
                            }
                        )
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
            out_dir,
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
