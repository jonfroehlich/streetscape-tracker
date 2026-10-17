"""
Historical Street View capture-date harvester (issues #2, #442).

A normal GSV run answers only "what's the nearest pano to point X, and when was
it taken?" — one *current* date per point. Google's own time-travel feature,
though, exposes the FULL capture history at a location: every official
Street View drive-through and its date. That history is not in any documented
Google API (free or paid). It is returned by an **unpublished endpoint**
(`GeoPhotoService.SingleImageSearch`, the backend the Maps JS
`StreetViewService.getPanorama().time[]` array reads from), which we query
directly here — no API key, no browser.

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
  * Each search returns a neighbourhood of panoramas; only those carrying a
    capture DATE are kept — in this endpoint a present date is the signal of
    official Google imagery (user photospheres come back undated). This is the
    endpoint-native analogue of the `© Google` filter used elsewhere.
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
import re
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
    The endpoint refused us -- a 429/403, or a run of transport failures that
    tripped the circuit breaker. The outputs written so far stay on disk and
    the ``blocked.json`` latch records when a restart may be attempted.
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


async def _fetch_search(
    session: aiohttp.ClientSession, url: str, timeout: aiohttp.ClientTimeout
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
    async with session.get(url, timeout=timeout) as resp:
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
    "panos_total",
    "panos_dated",
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
    async def paced_fetch(session, url):
        # Every attempt, retries included, waits for its own pacer slot and is
        # counted: a retry is a request the endpoint sees (issue #198).
        nonlocal api_requests
        await limiter.acquire()
        api_requests += 1
        return await _fetch_search(session, url, timeout)

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
                    url = build_search_url(float(r.query_lat), float(r.query_lon))
                    fetched_at = now_func().isoformat()
                    try:
                        status, body = await paced_fetch(session, url)
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
                                "url": url,
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
                    found = parse_search_response(body)
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
                            "panos_total": _count_panos(body),
                            "panos_dated": len(found),
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


def _count_panos(body: str) -> int | None:
    """Every pano node the response carries, dated or not (None if unparseable)."""
    m = _CALLBACK_RE.search(body.strip())
    if not m:
        return None
    try:
        data = json.loads(m.group(1))
        return len(data[1][5][0][3][0])
    except (ValueError, IndexError, KeyError, TypeError):
        return None
