"""
Checkpoint plumbing shared by the census providers.

A census provider's crawl is long enough that losing it to an interruption costs
real money against a per-IP budget: KartaView's radius sweep is hours (Singapore
~10.4 h), and Mapillary's tile census is tens of minutes against a 1,750/day
channel budget that a re-spend eats directly (issues #239, #256).

Both therefore checkpoint, and the pieces that do not depend on WHAT is being
crawled live here: where checkpoints go, how one directory is named, how a
rename into it is made durable, how a stored bbox is compared, and how a
finished or never-opened directory is removed. What each provider keeps INSIDE
its directory -- the commit record, the part files, the validation cascade that
decides whether to resume -- stays in its own module, because those are shaped
by the crawl.

It also holds the CENSUS CACHE (issue #290), which is the same directory
machinery pointed at a different question: a checkpoint keeps ONE crawl safe
from an interruption, and the cache lets every other consumer of that
(provider, city, bbox) observation reuse the finished result for zero requests.
See the section at the bottom of this file.

This module is also what keeps ``download_mapillary`` from importing
``download_kartaview``: no provider module may import from another's (CLAUDE.md),
and a shared home is the alternative to a copy. It depends only on the standard
library and :mod:`streetscape_metadata_tracker.paths`.
"""

import errno
import json
import logging
import os
import shutil
from datetime import UTC, datetime
from typing import Any

from .paths import get_project_root

logger = logging.getLogger(__name__)

CHECKPOINT_STATE_FILENAME = "state.json"

# How old a checkpoint may be before it is discarded rather than resumed.
#
# NOT tidiness -- this is the one way a checkpoint could produce a WRONG artifact
# rather than merely wasted work, which is the line the whole design is drawn
# against. The frozen grid geometry never changes, so bbox, ipp, radius and
# root_count all still match months later: a checkpoint left by a city that was
# interrupted and then sat out a long gap (a channel switched off after a per-IP
# block, `consecutive_failures` quarantining a city for a whole 90-day cycle)
# would resume and splice rows fetched last quarter into a snapshot dated today,
# published as one observation of one day. Seven days is comfortably longer than
# any legitimate multi-night sweep -- Singapore, the worst city in the catalog, is
# ~10.4 h -- and comfortably shorter than the 80-day `min_days_since_last_run`
# cadence, so it can only ever catch the stale case.
CHECKPOINT_MAX_AGE_S = 7 * 24 * 3600


CHECKPOINT_DIR_ENV = "STREETSCAPE_CHECKPOINT_DIR"


def checkpoint_dir() -> str:
    """
    Directory holding in-flight sweep checkpoints.

    The same three constraints as ``host_lock.lock_dir``, for the same host and
    for reasons that rhyme: **not** ``/tmp`` (the systemd unit sets
    ``PrivateTmp=true``, so a resumed sweep would never find the night's work),
    **not** the unresolved checkout path (``%h/streetscape-tracker`` is a
    symlink and ``get_project_root()`` uses ``abspath``, which does not resolve
    it, so two spellings of one directory would silently be two checkpoints --
    i.e. exactly the restart-from-zero this exists to prevent), and **not** under
    ``data/``, which ``sync_data_to_server.sh`` rsyncs to a public web server. A
    partial census is the one artifact that must never reach the publisher.

    The env override is realpath'd for the same reason the default is: an
    operator exporting the ``~`` spelling of the deployed path would otherwise
    derive a different directory and resume nothing.
    """
    override = os.environ.get(CHECKPOINT_DIR_ENV)
    if override:
        return os.path.realpath(override)
    return os.path.join(os.path.realpath(get_project_root()), "checkpoints")


def checkpoint_path_for(
    city_id: str,
    bbox: tuple[float, float, float, float],
    channel: str,
    variant: str | None = None,
) -> str:
    """
    Checkpoint directory for one (city, grid geometry, channel).

    DATE-FREE by construction, which is the contract: a sweep is meant to span
    nights and a run is dated on the day it COMPLETES, so a date in this path
    would make every night start from zero.

    The CHANNEL is not optional, and it is what keeps the two channel PAIRS
    apart: ('mapillary', 'mapillary_streets') and ('kartaview',
    'kartaview_streets'). A road walk sweeps the same frozen bbox with the same
    geometry the grid run uses, so every geometric validation a loader makes
    would pass and the two channels would resume each other's crawls -- into
    different ledgers, and for Mapillary under different credentials. Each
    provider's commit record also stores the channel it was written under and
    its loader compares it, so the path is the half that keeps the directories
    apart and the state file is the half that refuses if they meet anyway.

    The bbox is folded in rather than trusted to the city_id because the frozen
    grid can be re-registered (``scripts/resize_city.py``, ``cap_oversized_grids.py``):
    a checkpoint keyed on the slug alone would survive a resize and resume onto a
    lattice it does not describe. ``load_checkpoint`` also compares the stored
    bbox, so this is the cheap half of a belt-and-braces pair -- but it is the
    half that keeps the stale directory from lingering under the live name.

    THE VARIANT IS THE SAME ARGUMENT ONE LEVEL DOWN, and a walk needs it. The
    channel separates a walk from a grid run, but it does NOT separate two walks
    of one city from each other: ``--network-type drive`` and
    ``--network-type all_public`` are separate series with separate artifacts
    (``generate_streetwalk_filename`` carries the network token for exactly this
    reason), and they meter into the SAME street channel over the SAME frozen
    bbox. So every check a loader makes would pass and the second walk would
    re-finalize the first's crawl for zero requests, writing the first's
    ``api_requests_total`` into the second's ``street_walks`` row. The census
    would be identical -- both walks read the same tiles -- which is what makes
    it silent. Each provider's commit record stores the variant too, so a future
    caller that derives the path without one is refused rather than mispriced.

    Args:
        city_id: canonical catalog slug.
        bbox: the frozen grid's (min_lon, min_lat, max_lon, max_lat).
        channel: the collecting channel's name -- 'mapillary' or 'kartaview' for
            a grid run, 'mapillary_streets' or 'kartaview_streets' for a walk.
        variant: what distinguishes two crawls of one city within one channel,
            or None when the channel is the whole key. Today that is a walk's
            ``--network-type``; a grid run has exactly one crawl per channel and
            passes nothing, which keeps its path byte-identical to #239's.
    """
    geometry = "_".join(f"{coord:.6f}" for coord in bbox)
    leaf = f"{city_id}_{geometry}" if variant is None else f"{city_id}_{geometry}_{variant}"
    return os.path.join(checkpoint_dir(), channel, leaf)


def _fsync_dir(path: str) -> None:
    """
    Make a rename into ``path`` durable.

    ``os.replace`` is atomic but not durable: the directory entry it rewrites
    lives in the containing directory, so without this the part-then-state
    ordering survives a process crash and not a power loss. Best effort -- some
    filesystems refuse ``O_RDONLY`` fsync on a directory, and a checkpoint must
    never be what fails a sweep.
    """
    try:
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError as e:  # pragma: no cover - platform-dependent
        logger.debug(f"Could not fsync the checkpoint directory {path}: {e}")


def _state_path(path: str) -> str:
    return os.path.join(path, CHECKPOINT_STATE_FILENAME)


def _bbox_matches(stored: Any, bbox: tuple[float, float, float, float]) -> bool:
    """
    Is a stored bbox the same lattice frame as this one?

    Compared numerically at 1e-9 deg (~0.1 mm) rather than exactly, for the
    reason the golden-fixture comparison uses the same figure: the frozen bbox
    comes from a geodesic solve over libm, whose last ULP is not portable. A
    tolerance that tight cannot hide a real reframing.
    """
    if not isinstance(stored, list | tuple) or len(stored) != 4:
        return False
    return all(abs(float(a) - float(b)) <= 1e-9 for a, b in zip(stored, bbox, strict=True))


def discard_checkpoint(path: str) -> None:
    """
    Remove a finished checkpoint. THE CALLER'S JOB, once its artifact is durable.

    A provider's fetch deliberately does not call this on a clean crawl. It
    returns the census as a DataFrame and the caller then writes the dated
    CSV, the stats, the ``runs`` row, the JSON and the diff -- so a delete
    issued before returning would be the one thing guaranteeing that a crash in
    that tail costs the whole sweep again, which is one of the four
    interruptions #239 exists to cover. Call this last, after the artifact
    lands, the way ``download_gsv`` unlinks its ``.downloading`` sibling.

    A caller that forgets is bounded rather than broken: the age cap limits how
    long a complete checkpoint can be re-finalized, each provider's loader says
    so at WARNING, and the tell is ``api_requests == 0`` on a run that collected
    a whole city.

    Best effort: a checkpoint that cannot be removed must never fail a run that
    has already succeeded. The stale directory it leaves is bounded by
    :data:`CHECKPOINT_MAX_AGE_S`.

    Args:
        path: the checkpoint directory, as echoed back in ``checkpoint_path``.
    """
    try:
        shutil.rmtree(path)
    except FileNotFoundError:
        pass  # already gone; discarding twice is not an error
    except OSError as e:
        logger.warning(f"Could not remove the finished checkpoint at {path}: {e}")


def _remove_empty_checkpoint_dir(path: str | None) -> None:
    """
    Drop a checkpoint directory nothing was ever written into.

    The directory is created BEFORE the first request, deliberately, so that an
    unwritable path fails in one second rather than ten hours in. But a sweep
    can then die before the radius is settled -- a rejected credential, a host
    block during calibration, a bbox where no rung answers anywhere (Horace) --
    and never open a checkpoint at all, leaving an empty directory behind on
    every attempt. ``os.rmdir`` refuses a non-empty directory, which is exactly
    the test wanted: a real checkpoint is never touched.
    """
    if path is None:
        return
    try:
        os.rmdir(path)
    except OSError:
        pass


# ── The census cache: fetch once per (provider, bbox), reuse across channels ──
#
# A checkpoint protects ONE crawl from an interruption. The cache is the other
# half of the same observation: for a given city on a given night, several
# consumers want the IDENTICAL census and each was paying for its own copy.
# Mapillary's grid run and its road walk read the same z14 tiles over the same
# frozen bbox (the ledger's two channels showed byte-identical daily totals);
# a second walk at `--network-type all_public` is a third copy; and KartaView's
# grid run and its road walk (#258) would repeat the pattern against a sweep
# that is ~10 h for Singapore.
#
# So a COMPLETED checkpoint is PROMOTED here rather than deleted, and the next
# consumer of that (provider, city, bbox) reads it for zero requests.
#
# THE KEY IS GEOMETRY AND THE MARKER IS THE RECORD. A checkpoint path carries
# the channel and the variant because two crawls must never resume each other's
# spend; a cache entry deliberately carries NEITHER, because the census content
# depends on (provider, frozen bbox, when it was fetched) and on nothing else.
# Who paid, under which channel and variant, is written INTO the entry
# (CENSUS_CACHE_MARKER) so the catalog can say so -- never keyed, or the reuse
# this exists for could not happen.
#
# A cache entry is a checkpoint directory that was moved, so each provider's
# loader validates it with the same geometric/footer cascade it validates a
# resume with, plus one check a resume does not make: COMPLETENESS. A resume is
# allowed to be partial by definition; a cache entry that is missing tiles or
# root cells would publish a hole.

CENSUS_CACHE_DIR_ENV = "STREETSCAPE_CENSUS_CACHE_DIR"

CENSUS_CACHE_MARKER = "census_cache.json"

# Bumped when the marker's shape changes. An entry written by another version
# is deleted rather than read, exactly as a format-mismatched checkpoint is.
CENSUS_CACHE_FORMAT_VERSION = 1

# How old a cached census may be before a consumer refetches instead.
#
# The SAME number as CHECKPOINT_MAX_AGE_S, and deliberately the same constant
# rather than a second one that happens to agree: both answer "how far apart may
# two halves of one dated observation be?", and the justification is identical --
# comfortably longer than any pairing gap the scheduler produces (a budget
# deferral moves a channel by a night or two, not a week), and comfortably
# shorter than the 80-day `min_days_since_last_run` cadence, so it can only ever
# catch the stale case. Splitting them would let the two drift for no reason.
CENSUS_REUSE_MAX_AGE_S = CHECKPOINT_MAX_AGE_S


def census_cache_dir() -> str:
    """
    Directory holding completed censuses available for reuse.

    The same three constraints as :func:`checkpoint_dir`, for the same reasons:
    not ``/tmp`` (``PrivateTmp=true`` in the unit would hide the grid run's
    census from the walk that runs minutes later), not the unresolved checkout
    path (two spellings of one directory would be two caches and every reuse
    would miss), and NOT under ``data/``, which is rsynced to a public web
    server -- a raw provider census is not ours to republish.
    """
    override = os.environ.get(CENSUS_CACHE_DIR_ENV)
    if override:
        return os.path.realpath(override)
    return os.path.join(os.path.realpath(get_project_root()), "census_cache")


def census_cache_path_for(
    provider: str,
    city_id: str,
    bbox: tuple[float, float, float, float],
) -> str:
    """
    Cache entry for one (provider, city, frozen grid geometry).

    NO CHANNEL, NO VARIANT AND NO DATE, which is the whole difference from
    :func:`checkpoint_path_for` and the reason both exist. A checkpoint is one
    crawl's private workspace and must never be shared; a cache entry is a
    finished OBSERVATION, and every consumer that would have asked the provider
    the same question over the same lattice is entitled to it. The channel and
    variant that paid are recorded in the entry's marker instead, so a ``0`` in
    ``runs.api_requests`` is explicable rather than mysterious.

    The provider IS part of the key: two providers' censuses share a bbox and
    nothing else, and their part files are not even the same schema.

    The bbox is folded in for the same reason a checkpoint's is: a frozen grid
    can be re-registered (``scripts/resize_city.py``, ``cap_oversized_grids.py``),
    and an entry keyed on the slug alone would survive a resize and be reused
    onto a lattice it does not describe. Each provider's loader compares the
    stored bbox too, so this is the cheap half of a belt-and-braces pair.

    Args:
        provider: 'mapillary' or 'kartaview' -- the PROVIDER, never a channel.
        city_id: canonical catalog slug.
        bbox: the frozen grid's (min_lon, min_lat, max_lon, max_lat).
    """
    geometry = "_".join(f"{coord:.6f}" for coord in bbox)
    return os.path.join(census_cache_dir(), provider, f"{city_id}_{geometry}")


def _marker_path(cache_path: str) -> str:
    return os.path.join(cache_path, CENSUS_CACHE_MARKER)


def promote_checkpoint_to_cache(checkpoint_path: str, cache_path: str, marker: dict) -> bool:
    """
    Move a COMPLETED checkpoint into the cache and stamp it with its provenance.

    THE MARKER IS WRITTEN AFTER THE RENAME, AND THAT ORDER IS THE COMMIT POINT.
    A crash between the two leaves a marker-less directory, which every loader
    deletes on sight -- so the failure mode is a re-fetch, never a half-promoted
    entry read as a complete census. Written the other way round, a crash would
    leave a marker describing a directory that does not hold what it claims.

    BEST EFFORT, ALWAYS. A city must never fail over its own optimization: any
    ``OSError`` warns and returns False, and the caller then keeps its
    checkpoint and discards it exactly as it did before this existed.

    Args:
        checkpoint_path: the completed checkpoint directory. It is MOVED, so the
            caller must not use it afterwards -- a False return means it is
            still there.
        cache_path: from :func:`census_cache_path_for`.
        marker: the provenance record. See CENSUS_CACHE_MARKER.

    Returns:
        True when the entry is in place and its marker is durable.
    """
    try:
        # A stale entry for this (provider, city, bbox) is replaced wholesale
        # rather than merged: it describes an earlier observation, and half of
        # each would be a census of no single moment.
        shutil.rmtree(cache_path, ignore_errors=True)
        os.makedirs(os.path.dirname(cache_path), exist_ok=True)
        try:
            os.replace(checkpoint_path, cache_path)
        except OSError as e:
            if getattr(e, "errno", None) != errno.EXDEV:
                raise
            # checkpoints/ and census_cache/ are siblings by default, but either
            # can be pointed elsewhere by its env override -- and a rename
            # across filesystems is EXDEV, not a permission problem. Copy, then
            # remove, which is os.replace's semantics at a higher price.
            shutil.copytree(checkpoint_path, cache_path)
            shutil.rmtree(checkpoint_path, ignore_errors=True)
        _fsync_dir(os.path.dirname(cache_path))
        tmp = f"{_marker_path(cache_path)}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(marker, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, _marker_path(cache_path))
        _fsync_dir(cache_path)
    except OSError as e:
        logger.warning(
            f"Could not promote the census at {checkpoint_path} into the cache at "
            f"{cache_path}; the next consumer will refetch it: {e}"
        )
        return False
    return True


def _read_census_cache_marker(
    cache_path: str, *, max_age_s: float
) -> tuple[dict | None, str | None]:
    """
    ``(marker, None)`` when this entry is usable, ``(None, reason)`` otherwise.

    Split from :func:`load_census_cache_marker` because :func:`prune_census_cache`
    needs the same verdict without the "delete exactly this one" framing, and a
    second copy of the rules is how the loader and the pruner would come to
    disagree about what "expired" means.

    ``(None, None)`` -- no entry at all -- is the ordinary first-consumer case
    and is not worth a line of log.
    """
    marker_path = _marker_path(cache_path)
    if not os.path.exists(marker_path):
        if not os.path.isdir(cache_path):
            return None, None
        # A directory with no marker is a promotion that crashed between the
        # rename and the stamp. It may hold a complete census, but nothing says
        # who paid or when the provider was observed, so it cannot be reused.
        return None, "it has no marker, so its provenance is unknown"
    try:
        with open(marker_path, encoding="utf-8") as f:
            marker = json.load(f)
        if marker["format_version"] != CENSUS_CACHE_FORMAT_VERSION:
            return None, (
                f"it is marker format v{marker['format_version']}, this build writes "
                f"v{CENSUS_CACHE_FORMAT_VERSION}"
            )
        # AGED FROM crawl_started_at -- when the provider was first observed --
        # not from completed_at. The window bounds how far apart two halves of
        # one dated observation may be, and a multi-night crawl's last commit
        # says nothing about how old its oldest rows are. completed_at is the
        # fallback only for a crawl that never checkpointed and so has no first
        # commit to point at.
        stamp = marker.get("crawl_started_at") or marker.get("completed_at")
        age_s = (datetime.now(UTC) - datetime.fromisoformat(stamp)).total_seconds()
        if age_s > max_age_s:
            return None, (
                f"the census it holds was fetched {age_s / 86400:.1f} days ago, past the "
                f"{max_age_s / 86400:.0f}-day reuse window; its rows would be spliced into "
                f"a snapshot dated today"
            )
    except Exception as e:
        # Broad on purpose, the loaders' never-raise posture: an unreadable
        # cache entry must cost a re-fetch, never a city.
        return None, f"{type(e).__name__}: {e}"
    return marker, None


def load_census_cache_marker(
    cache_path: str, *, max_age_s: float = CENSUS_REUSE_MAX_AGE_S
) -> dict | None:
    """
    The provenance record for a usable cache entry, or None.

    NEVER RAISES, and an unusable entry is DELETED rather than left in place --
    both the same contract each provider's checkpoint loader keeps. Deleting is
    what keeps a stale entry from sitting under the live name until the pruner
    happens to run: the next consumer refetches and promotes a fresh one over
    the space this just freed.

    FOR CONSUMERS ONLY, and that restriction is what makes the deletion safe.
    Every caller of this is a provider fetch holding its host lock, so no second
    process on this machine can be mid-``promote_checkpoint_to_cache`` into the
    entry being removed. A planning caller -- ``--estimate``, a budget
    pre-flight, the scheduler's per-channel estimate -- must use
    :func:`census_cache_probe` instead, which holds no lock and therefore never
    deletes.

    Args:
        cache_path: from :func:`census_cache_path_for`.
        max_age_s: the reuse window; see :data:`CENSUS_REUSE_MAX_AGE_S`.
    """
    marker, reason = _read_census_cache_marker(cache_path, max_age_s=max_age_s)
    if reason is not None:
        logger.warning(f"Discarding the cached census at {cache_path}: {reason}")
        discard_checkpoint(cache_path)
    return marker


def census_cache_probe(
    provider: str,
    city_id: str,
    bbox: tuple[float, float, float, float],
    *,
    max_age_s: float = CENSUS_REUSE_MAX_AGE_S,
) -> dict | None:
    """
    Is a reusable census on hand for this (provider, city, bbox)? Marker only.

    Reads no part file and validates no geometry, because its callers are the
    ones who must not pay to find out: ``--estimate``, the road walk's
    ``--daily-budget`` pre-flight, and the scheduler's per-channel estimate,
    which prices a channel for EVERY due city on every night. A probe that hit
    the parquet footers would turn a cheap planning pass into a disk sweep.

    So a hit here is a STRONG HINT, not a guarantee: the consumer's own loader
    still validates the entry and still refetches if it does not match. That
    asymmetry is the safe one -- an over-optimistic probe costs a request budget
    that turns out to be unnecessary, never a wrong artifact.

    IT ALSO DELETES NOTHING, unlike :func:`load_census_cache_marker`. A planning
    caller holds no host lock and runs concurrently with the children that are
    promoting into these entries -- the scheduler prices a channel from a lane
    worker while a sibling child is mid-collection -- and
    ``promote_checkpoint_to_cache`` is deliberately a rename followed by a
    separate marker write. A probe that removed what it refused could therefore
    delete a census a child had just renamed into place and not yet stamped,
    for no gain: the entry it saw was unusable to IT, and the consumer that
    actually reads one does the deleting under the lock. Expired entries are
    swept by :func:`prune_census_cache` in the scheduler's tail, after the city
    loop has returned.

    Returns the marker (so a caller can name who paid and when), or None.
    """
    marker, _reason = _read_census_cache_marker(
        census_cache_path_for(provider, city_id, bbox), max_age_s=max_age_s
    )
    return marker


def prune_census_cache(max_age_s: float = CENSUS_REUSE_MAX_AGE_S) -> int:
    """
    Delete cache entries past the reuse window, or with no usable marker.

    The cache is bounded by this and by nothing else: an entry is written for
    every census the night fetches and is never overwritten until that city is
    collected again, which for a real catalog is ~80 days away. One night's
    ~20 cities of Detroit-scale parquet is hundreds of megabytes, so a cache
    nobody swept would grow without limit on a host whose disk is shared.

    Best effort in every direction -- a missing cache directory is zero, an
    unreadable entry is skipped rather than raised on -- because this runs in
    the scheduler's tail beside the backup and the publish, where #167's rule is
    that no housekeeping step may cost the night's visibility.

    Returns the number of entries removed.
    """
    root = census_cache_dir()
    removed = 0
    try:
        providers = sorted(os.listdir(root))
    except OSError:
        return 0  # nothing has ever been cached on this host
    for provider in providers:
        provider_dir = os.path.join(root, provider)
        try:
            entries = sorted(os.listdir(provider_dir))
        except OSError as e:
            logger.warning(f"Could not read the census cache at {provider_dir}: {e}")
            continue
        for entry in entries:
            path = os.path.join(provider_dir, entry)
            if not os.path.isdir(path):
                continue
            _marker, reason = _read_census_cache_marker(path, max_age_s=max_age_s)
            if reason is None:
                continue
            logger.info(f"Pruning the cached census at {path}: {reason}")
            discard_checkpoint(path)
            removed += 1
    return removed
