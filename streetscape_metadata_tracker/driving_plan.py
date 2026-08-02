"""
Snapshot Google's published Street View driving-plan feed (issue #176).

Google publishes a forward-looking Street View collection plan behind a single
mutable, unauthenticated URL that it overwrites in place. Plan revisions — a
window shifting, a county added or dropped, a row retiring (`publish` flipping
Yes -> No) — are invisible to anyone not snapshotting, and cannot be
reconstructed after the fact. This module fetches the feed, archives dated
immutable gzip snapshots with content-hash dedupe, and explodes the records
into the catalog (one row per district) so planned re-drives can later be
joined against the observed capture dates this project already measures.

Like the GSV history harvester (issue #2), the source is an undocumented asset
with no guarantee it keeps working; the archive is the hedge against its own
fragility. Treat the plan as advisory, never a contract — Google's own note
says listed cities "may include smaller cities and towns within driving
distance", so absence is not a guarantee of no driving.

Politeness: one unauthenticated request per day, enforced by the ingest gate
(a snapshot row for today short-circuits before any network I/O). Fetching
uses stdlib urllib — deliberately not the aiohttp/backoff machinery the
collectors need, since this is one request to one static URL once a day.

Storage: artifacts live OUTSIDE data/ (default: <repo>/archive/gsv_driving_plan/)
because data/ is rsynced to the public web server and its whitelist publishes
every *.json.gz — archiving there would republish Google's content.
"""

import gzip
import hashlib
import json
import logging
import os
import time
import urllib.request
from dataclasses import dataclass
from datetime import UTC, date, datetime

from . import db
from .download_gsv_history import _USER_AGENT

logger = logging.getLogger(__name__)

FEED_URL = "https://www.google.com/streetview/static/feed/driving/data.json"


def default_archive_dir() -> str:
    """Default snapshot archive: <repo root>/archive/gsv_driving_plan."""
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(repo_root, "archive", "gsv_driving_plan")


def generate_snapshot_filename(fetch_date: date) -> str:
    """
    Dated immutable artifact name for one feed snapshot.

    >>> from datetime import date
    >>> generate_snapshot_filename(date(2026, 8, 1))
    'gsv_driving_plan_2026-08-01.json.gz'
    """
    return f"gsv_driving_plan_{fetch_date.isoformat()}.json.gz"


def fetch_feed(url: str = FEED_URL, *, timeout_s: float = 60.0, retries: int = 3) -> bytes:
    """
    Fetch the raw feed bytes with a browser User-Agent, retrying transient
    failures with a short backoff. Raises the last error if all tries fail.
    """
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(request, timeout=timeout_s) as resp:
                return resp.read()
        except Exception as e:  # noqa: BLE001 — urllib raises a menagerie; all retryable here
            last_error = e
            if attempt < retries - 1:
                delay = 5.0 * (attempt + 1)
                logger.warning(
                    "Driving-plan fetch attempt %d/%d failed (%s); retrying in %.0fs",
                    attempt + 1, retries, e, delay,
                )
                time.sleep(delay)
    raise last_error


def parse_feed(raw: bytes) -> list[dict]:
    """Decode the feed; it must be a JSON array of objects."""
    data = json.loads(raw)
    if not isinstance(data, list) or not all(isinstance(r, dict) for r in data):
        raise ValueError("Driving-plan feed is not a JSON array of objects")
    return data


def parse_feed_date(value) -> str | None:
    """
    Defensively parse a feed timestamp to 'YYYY-MM-DD', or None.

    The feed carries clean ISO timestamps but ~58 records have dirty values
    ('13/1/19', truncated month names). Strict ISO only — no day/month-
    ambiguous heuristics; callers keep the raw string beside the parsed date,
    so a None loses nothing. Never raises.

    >>> parse_feed_date("2026-02-02T08:00:00.000Z")
    '2026-02-02'
    >>> parse_feed_date("13/1/19") is None
    True
    >>> parse_feed_date(None) is None
    True
    >>> parse_feed_date("") is None
    True
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        return None


def explode_records(records: list[dict], snapshot_id: int) -> list[tuple]:
    """
    Explode feed records into driving_plan_entries tuples, one per district.

    `districts` is a comma-joined string; the split is uniform for all
    countries (non-US districts are free-form, but the raw feed is fully
    preserved in the artifact, so nothing is lost if a comma-split is wrong
    there). An empty/missing `districts` yields a single row with a NULL
    district. No record is ever dropped — dirty dates become NULL parsed
    dates beside the intact raw string.
    """
    entries = []
    for rec in records:
        start_raw = rec.get("datestart")
        end_raw = rec.get("dateend")
        common = (
            snapshot_id,
            rec.get("country"),
            rec.get("code"),
            rec.get("svspc"),
            rec.get("region"),
        )
        tail = (
            rec.get("publish"),
            start_raw,
            parse_feed_date(start_raw),
            end_raw,
            parse_feed_date(end_raw),
        )
        districts = [d.strip() for d in (rec.get("districts") or "").split(",") if d.strip()]
        if not districts:
            entries.append(common + (None,) + tail)
        else:
            for district in districts:
                entries.append(common + (district,) + tail)
    return entries


def write_snapshot_artifact(raw: bytes, archive_dir: str, fetch_date: date) -> str:
    """
    Gzip the raw feed bytes to the dated artifact, atomically (tmp + rename).
    mtime=0 keeps the gzip bytes deterministic for a given input. Returns the
    bare filename.
    """
    os.makedirs(archive_dir, exist_ok=True)
    filename = generate_snapshot_filename(fetch_date)
    path = os.path.join(archive_dir, filename)
    tmp_path = path + ".tmp"
    with open(tmp_path, "wb") as f:
        f.write(gzip.compress(raw, mtime=0))
    os.replace(tmp_path, path)
    return filename


@dataclass(frozen=True)
class IngestResult:
    """Outcome of one ingest pass, for logging and the CLI summary."""

    snapshot_id: int | None
    fetch_date: str
    skipped: bool
    changed: bool
    record_count: int
    entry_count: int


def ingest(
    conn,
    *,
    archive_dir: str,
    fetch_date: date | None = None,
    raw: bytes | None = None,
    force: bool = False,
    url: str = FEED_URL,
    timeout_s: float = 60.0,
) -> IngestResult:
    """
    Fetch (or accept injected bytes for backfill/tests), archive, and catalog
    one snapshot of the driving-plan feed. The single code path behind both
    the nightly scheduler hook and the manual fetch-driving-plan subcommand.

    A snapshot row is written on EVERY ingest; the artifact and the exploded
    entries only when the sha256 differs from the previous snapshot (the feed
    moves rarely — this avoids ~365 near-identical files a year). `publish`
    is never filtered: the Yes -> No transition is the campaign-closed signal.

    Politeness gate: if a snapshot row already exists for fetch_date and not
    `force`, returns skipped=True without touching the network.
    """
    fetch_date = fetch_date or datetime.now(UTC).date()
    existing = db.get_driving_plan_snapshot(conn, fetch_date)
    if existing is not None and not force:
        return IngestResult(
            snapshot_id=existing["snapshot_id"],
            fetch_date=fetch_date.isoformat(),
            skipped=True,
            changed=bool(existing["changed"]),
            record_count=existing["record_count"],
            entry_count=0,
        )

    if raw is None:
        raw = fetch_feed(url, timeout_s=timeout_s)
    sha = hashlib.sha256(raw).hexdigest()
    records = parse_feed(raw)

    # Compare against the snapshot BEFORE this date (not any-hash-ever), so an
    # A -> B -> A flip-flop is recorded as a change both times, and a forced
    # same-day re-ingest never compares against its own prior row.
    prev = db.get_latest_driving_plan_snapshot(conn, before_date=fetch_date.isoformat())
    changed = prev is None or prev["sha256"] != sha

    artifact_filename = write_snapshot_artifact(raw, archive_dir, fetch_date) if changed else None
    snapshot_id = db.register_driving_plan_snapshot(
        conn,
        fetch_date=fetch_date,
        sha256=sha,
        record_count=len(records),
        changed=changed,
        artifact_filename=artifact_filename,
        source_url=url,
    )
    # Always replace (with [] when unchanged): a forced same-day re-ingest that
    # flips changed 1 -> 0 must not leave the earlier ingest's rows behind.
    entries = explode_records(records, snapshot_id) if changed else []
    entry_count = db.replace_driving_plan_entries(conn, snapshot_id, entries)

    return IngestResult(
        snapshot_id=snapshot_id,
        fetch_date=fetch_date.isoformat(),
        skipped=False,
        changed=changed,
        record_count=len(records),
        entry_count=entry_count,
    )
