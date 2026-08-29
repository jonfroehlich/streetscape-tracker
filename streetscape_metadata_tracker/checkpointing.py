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
Everything about the cache that is not shaped by a provider's parts -- the
marker, promotion, the loader skeleton, what a reuser inherits, how a hit is
reconciled with the consumer's own checkpoint -- lives here ONCE, and each
provider plugs in only its validator and its completeness rule. That is the
CLAUDE.md census-seam rule applied to the cache: a contract enforced in one
provider's copy is invisible in a review of the other's, and the first version
of this feature had the two copies disagree about what "the same crawl" meant.
See the section at the bottom of this file.

This module is also what keeps ``download_mapillary`` from importing
``download_kartaview``: no provider module may import from another's (CLAUDE.md),
and a shared home is the alternative to a copy. It depends on the standard
library, :mod:`streetscape_metadata_tracker.paths` and the provider-neutral
:func:`download_common.grid_bbox`.
"""

import errno
import json
import logging
import os
import shutil
import time
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any, Protocol

from .download_common import grid_bbox
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


# ── The commit-record formats, one per census provider ──────────────────────
#
# Declared HERE rather than in the provider modules, because the census cache
# probe (below) has to be able to say whether an entry's commit record is one
# this build's loader would read, and the probe runs in callers -- the
# scheduler's estimate, a walk's --estimate -- that price a channel without
# importing the provider. Each provider module re-exports its own under the name
# its loader and commit use, so nothing else changes. Bumping one of these
# discards every in-flight checkpoint of that provider on the build that ships
# it, and makes every cached census of that provider a MISS at the probe rather
# than a hit the child then refuses (the mispricing the previous version of this
# feature had).
#
# Mapillary v1: `done_tiles` as [x, y, rows] triples, `created_at`.
MAPILLARY_CHECKPOINT_FORMAT_VERSION = 1
# KartaView v2 (issue #272): the commit record gained `created_at`, and the age
# cap moved from `updated_at` onto it. A v1 record cannot be read forward -- it
# has no first-commit stamp at all, and adopting `updated_at` in its place would
# be exactly the bug the bump exists to fix -- so an old checkpoint is discarded
# by the ordinary format-mismatch arm and its sweep restarts. That costs at most
# one in-flight city, once, on the build that ships this.
KARTAVIEW_CHECKPOINT_FORMAT_VERSION = 2

STORE_FORMAT_VERSIONS: dict[str, int] = {
    "kartaview": KARTAVIEW_CHECKPOINT_FORMAT_VERSION,
    "mapillary": MAPILLARY_CHECKPOINT_FORMAT_VERSION,
}

# The providers whose collection is a CENSUS -- one crawl of the whole frozen
# bbox -- and therefore the ones that checkpoint and share a cache. GSV samples
# per point and has neither. Derived from the format table rather than listed
# twice, so a provider cannot be added to one and forgotten in the other; this
# is THE membership test every caller uses (cli, the walk collector, the
# scheduler), never a literal.
CENSUS_PROVIDERS = frozenset(STORE_FORMAT_VERSIONS)


def _runtime_dir(env_var: str, leaf: str) -> str:
    """
    A runtime-state directory beside ``data/``: ``<project root>/<leaf>``.

    The same three constraints for every one of them (checkpoints, the census
    cache, and ``host_lock.lock_dir``), enforced once: **not** ``/tmp`` (the
    systemd unit sets ``PrivateTmp=true``, so a resumed sweep would never find
    the night's work), **not** the unresolved checkout path
    (``%h/streetscape-tracker`` is a symlink and ``get_project_root()`` uses
    ``abspath``, which does not resolve it, so two spellings of one directory
    would silently be two directories -- i.e. exactly the restart-from-zero this
    exists to prevent), and **not** under ``data/``, which
    ``sync_data_to_server.sh`` rsyncs to a public web server.

    The env override is realpath'd for the same reason the default is: an
    operator exporting the ``~`` spelling of the deployed path would otherwise
    derive a different directory and resume nothing.
    """
    override = os.environ.get(env_var)
    if override:
        return os.path.realpath(override)
    return os.path.join(os.path.realpath(get_project_root()), leaf)


def checkpoint_dir() -> str:
    """Directory holding in-flight sweep checkpoints. See :func:`_runtime_dir`."""
    return _runtime_dir(CHECKPOINT_DIR_ENV, "checkpoints")


def _geometry_leaf(city_id: str, bbox: tuple[float, float, float, float]) -> str:
    """
    ``<city_id>_<bbox at 6 dp>`` -- the half of a checkpoint or cache name that
    says WHICH lattice, shared by both so a promoted checkpoint is keyed on the
    identical spelling of its bbox that its consumers derive.
    """
    geometry = "_".join(f"{coord:.6f}" for coord in bbox)
    return f"{city_id}_{geometry}"


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
    leaf = _geometry_leaf(city_id, bbox)
    if variant is not None:
        leaf = f"{leaf}_{variant}"
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


def _write_json_durable(path: str, payload: Any) -> None:
    """
    Write ``payload`` to ``path`` so that it is either entirely there or not at
    all, and survives a power loss once this returns.

    Staged as ``<path>.tmp`` (fsync'd), renamed over ``path``, then the
    containing directory is fsync'd -- the sequence every commit record and the
    cache marker need, kept in ONE place so a copy cannot be "simplified" into
    dropping the fsync and quietly losing the crash-consistency argument for one
    store only. Raises on failure; callers decide what that costs.
    """
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    _fsync_dir(os.path.dirname(path))


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


def frozen_bbox(city: Any) -> tuple[float, float, float, float]:
    """
    The frozen grid bbox of a catalog city row -- THE derivation, for every
    caller that keys a checkpoint or a cache entry on it.

    The whole cache works only because the writer (the grid run) and every
    reader (the walk, the scheduler's probe, ``--estimate``) compute a
    byte-identical key; five call sites each spelling ``grid_bbox(center_lat,
    center_lon, grid_width_m, grid_height_m, step_m)`` by hand agree only by
    coincidence, and a cap or a rounding applied at one of them would make the
    grid run promote under one key and the walk probe another -- every reuse
    missing, nothing failing.

    Args:
        city: a :class:`db.CityRow` (or anything with its five geometry fields).
    """
    return grid_bbox(
        city.center_lat, city.center_lon, city.grid_width_m, city.grid_height_m, city.step_m
    )


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

# How long the pruner leaves a directory it cannot interpret -- an EXDEV staging
# copy (`<entry>.tmp`) or a marker-less directory -- before treating it as
# debris. Promotion is a single rename, so neither shape is ever a promotion in
# flight; the grace exists so that a staging copy being written across
# filesystems at the moment the tail runs is not deleted from under the copier.
_PRUNE_DEBRIS_GRACE_S = 24 * 3600


@dataclass(frozen=True)
class CensusCache:
    """
    How one consumer may use the shared census cache. Built at the call site
    (:func:`crawl_store_for`) and handed down every layer of a provider's fetch
    as ONE argument, so the three settings it bundles cannot be forwarded
    partially -- a layer that dropped one of them would silently fall back to a
    default, and the tests had to pin that for every layer when they travelled
    as separate keyword arguments.

    Attributes:
        path: the entry, from :func:`census_cache_path_for`.
        reuse: False refetches even when the cache holds a usable entry, and
            replaces it -- the ``--refetch-census`` escape hatch, for an
            operator who wants the observation taken NOW. It does not disable
            promotion: a refetch still leaves the fresher census for the
            consumers behind it.
        run_date: the snapshot date this consumer is writing. An entry whose
            crawl finished AFTER it is refused (without being deleted -- it is
            fine for a consumer dated later): a backdated ``--force --run-date``
            would otherwise publish rows observed after the snapshot's own
            ceiling, which ``plausible_capture_mask`` then drops as "cannot be
            true". None skips the check, for callers that have no date.
    """

    path: str
    reuse: bool = True
    run_date: date | None = None


def census_cache_dir() -> str:
    """
    Directory holding completed censuses available for reuse.

    See :func:`_runtime_dir` for the three constraints; the one that bites
    hardest here is ``PrivateTmp`` -- a cache in ``/tmp`` would hide the grid
    run's census from the walk that runs minutes later.
    """
    return _runtime_dir(CENSUS_CACHE_DIR_ENV, "census_cache")


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

    The bbox is folded in for the same reason a checkpoint's is (and by the same
    :func:`_geometry_leaf`, so the two can never spell one bbox two ways): a
    frozen grid can be re-registered, and an entry keyed on the slug alone would
    survive a resize and be reused onto a lattice it does not describe. Each
    provider's loader compares the stored bbox too, so this is the cheap half of
    a belt-and-braces pair.

    Args:
        provider: one of :data:`CENSUS_PROVIDERS` -- the PROVIDER, never a channel.
        city_id: canonical catalog slug.
        bbox: the frozen grid's (min_lon, min_lat, max_lon, max_lat).
    """
    return os.path.join(census_cache_dir(), provider, _geometry_leaf(city_id, bbox))


def crawl_store_for(
    provider: str,
    city: Any,
    channel: str,
    *,
    variant: str | None = None,
    reuse: bool = True,
    run_date: date | None = None,
) -> tuple[str | None, CensusCache | None]:
    """
    Where one collection checkpoints and which cache entry it shares: the
    ``(checkpoint_path, census_cache)`` pair every census consumer hands its
    provider's fetch, or ``(None, None)`` for a provider that has no census.

    The ONE place a consumer derives both, so the channel-keyed checkpoint and
    the geometry-keyed entry are built from the same :func:`frozen_bbox` and the
    same membership test -- the grid CLI, the road-walk collector and the
    scheduler's probe used to spell the provider test and the bbox each in their
    own words, and a channel missed in any one of them was silently priced at
    full cost or never reached the cache at all.

    Args:
        provider: the imagery provider ('gsv' gets ``(None, None)``).
        city: the catalog row -- see :func:`frozen_bbox`.
        channel: the collecting channel the checkpoint is keyed on
            ('mapillary', 'mapillary_streets', ...).
        variant: the walk's ``--network-type``; None for a grid run.
        reuse: ``not args.refetch_census``.
        run_date: the snapshot date being written; see :class:`CensusCache`.
    """
    if provider not in CENSUS_PROVIDERS:
        return None, None
    bbox = frozen_bbox(city)
    checkpoint_path = checkpoint_path_for(city.city_id, bbox, channel, variant)
    entry = CensusCache(census_cache_path_for(provider, city.city_id, bbox), reuse, run_date)
    return checkpoint_path, entry


def _marker_path(cache_path: str) -> str:
    return os.path.join(cache_path, CENSUS_CACHE_MARKER)


def census_cache_marker(
    provider: str,
    *,
    fetched_by: str | None,
    fetched_variant: str | None,
    crawl_started_at: str,
    api_requests_total: int,
    failed: list,
) -> dict:
    """
    The provenance record a promoted entry carries. THE ONE BUILDER -- both
    providers and every test that stamps an entry go through it, so a field
    added to the marker reaches every writer, and a reader that looks for it
    cannot be handed an entry from a writer that never learned the name.

    Args:
        provider: which provider's parts the entry holds. Also selects the
            store format the marker records, which is what lets the probe tell
            a hit from an entry the loader would refuse.
        fetched_by: the channel whose credential and ledger paid. RECORDED,
            never keyed -- see :func:`census_cache_path_for`.
        fetched_variant: that crawl's variant (a walk's network type), or None.
        crawl_started_at: when the crawl's FIRST commit landed, i.e. when the
            provider was first observed. Required: both providers promote only
            a crawl that committed, so there is always one, and the reuse
            window is aged from it.
        api_requests_total: what the whole crawl cost across every resume.
        failed: the tiles or cells that never answered, in the provider's own
            JSON-serializable spelling; a reuser inherits them.
    """
    if provider not in STORE_FORMAT_VERSIONS:
        raise ValueError(f"{provider!r} is not a census provider")
    if not crawl_started_at:
        raise ValueError("a promoted crawl always has a first commit to date it by")
    return {
        "format_version": CENSUS_CACHE_FORMAT_VERSION,
        "store_format_version": STORE_FORMAT_VERSIONS[provider],
        "provider": provider,
        "fetched_by": fetched_by,
        "fetched_variant": fetched_variant,
        "crawl_started_at": crawl_started_at,
        "completed_at": datetime.now(UTC).isoformat(),
        "api_requests_total": int(api_requests_total),
        "failed": list(failed),
    }


def promote_checkpoint_to_cache(checkpoint_path: str, cache_path: str, marker: dict) -> bool:
    """
    Move a COMPLETED checkpoint into the cache, stamped with its provenance.

    THE RENAME IS THE COMMIT POINT, AND IT IS THE ONLY STEP. The marker is
    written durably INSIDE the checkpoint directory first, and then one
    ``os.replace`` moves the whole directory under the cache name -- so at every
    instant an entry exists under that name it is complete and stamped, and a
    crash anywhere leaves either the checkpoint (with one extra file both
    providers' debris purges ignore) or the finished entry. Nothing can observe
    a renamed-but-unstamped directory, which is why neither the probe nor the
    pruner has to reason about one, and why a False return can promise that the
    checkpoint is still where it was.

    The first version of this wrote the marker AFTER the rename. That left a
    window -- an ``ENOSPC`` on the marker, a subprocess-timeout SIGKILL, which
    for KartaView lands at the END of a sweep, exactly here -- in which the
    directory had moved, every loader would delete it on sight, and this
    returned False claiming the checkpoint was still there. A caller's tail that
    then crashed lost a paid-for crawl from both places.

    BEST EFFORT, ALWAYS. A city must never fail over its own optimization: any
    ``OSError`` warns and returns False, the stray marker is removed from the
    checkpoint, and the caller then keeps its checkpoint and discards it exactly
    as it did before this existed.

    Args:
        checkpoint_path: the completed checkpoint directory. It is MOVED, so the
            caller must not use it afterwards -- a False return means it is
            still there.
        cache_path: from :func:`census_cache_path_for`.
        marker: from :func:`census_cache_marker`.

    Returns:
        True when the entry is in place and its marker is durable.
    """
    try:
        _write_json_durable(_marker_path(checkpoint_path), marker)
        os.makedirs(os.path.dirname(cache_path), exist_ok=True)
        # A stale entry for this (provider, city, bbox) is replaced wholesale
        # rather than merged: it describes an earlier observation, and half of
        # each would be a census of no single moment.
        shutil.rmtree(cache_path, ignore_errors=True)
        try:
            os.replace(checkpoint_path, cache_path)
        except OSError as e:
            if getattr(e, "errno", None) != errno.EXDEV:
                raise
            # checkpoints/ and census_cache/ are siblings by default, but either
            # can be pointed elsewhere by its env override -- and a rename
            # across filesystems is EXDEV, not a permission problem. Copy into a
            # staging name beside the entry, then rename THAT into place, so the
            # entry still appears in one step; the pruner treats a `.tmp` that
            # outlives its grace as the debris of a copy that died.
            staging = f"{cache_path}.tmp"
            shutil.rmtree(staging, ignore_errors=True)
            shutil.copytree(checkpoint_path, staging)
            os.replace(staging, cache_path)
            shutil.rmtree(checkpoint_path, ignore_errors=True)
        _fsync_dir(os.path.dirname(cache_path))
    except OSError as e:
        logger.warning(
            f"Could not promote the census at {checkpoint_path} into the cache at "
            f"{cache_path}; the next consumer will refetch it: {e}"
        )
        # Leave the checkpoint exactly as it was: a marker inside it is harmless
        # to a resume but would be a lie to anyone reading the directory.
        try:
            os.remove(_marker_path(checkpoint_path))
        except OSError:
            pass
        return False
    return True


def _demote_cache_to_checkpoint(cache_path: str, checkpoint_path: str) -> bool:
    """
    The inverse move: hand a cache entry back to the crawl that paid for it, so
    that crawl continues through its own checkpoint. See
    :func:`reconcile_cache_hit`. Best effort; False leaves the entry in place.
    """
    try:
        os.makedirs(os.path.dirname(checkpoint_path), exist_ok=True)
        os.replace(cache_path, checkpoint_path)
        try:
            os.remove(_marker_path(checkpoint_path))
        except FileNotFoundError:
            pass
        _fsync_dir(os.path.dirname(checkpoint_path))
    except OSError as e:
        logger.warning(
            f"Could not hand the cached census at {cache_path} back to its crawl at "
            f"{checkpoint_path}: {e}; reusing it as it stands"
        )
        return False
    return True


def _checkpoint_started_at(checkpoint_path: str | None) -> str | None:
    """
    When the crawl checkpointed at ``checkpoint_path`` first committed, or
    None when there is no committed crawl there. Both providers' commit records
    carry ``created_at`` at the top level, which is all this reads.
    """
    if checkpoint_path is None:
        return None
    try:
        with open(_state_path(checkpoint_path), encoding="utf-8") as f:
            return json.load(f).get("created_at") or None
    except (OSError, ValueError):
        return None


def same_crawl(marker: dict, channel: str | None, variant: str | None) -> bool:
    """
    Is the consumer identified by (channel, variant) the crawl that paid for
    this entry? Compared as the PAIR, because two walks of one city agree on the
    channel and differ only in variant -- and pricing an ``all_public`` walk as
    the re-finalize of the ``drive`` crawl is exactly the mis-accounting
    checkpoint variants exist to prevent.
    """
    return (marker.get("fetched_by"), marker.get("fetched_variant")) == (channel, variant)


def reconcile_cache_hit(
    marker: dict,
    *,
    cache_path: str,
    checkpoint_path: str | None,
    channel: str | None,
    variant: str | None,
) -> bool:
    """
    A provider's loader accepted the entry; should this consumer REUSE it, or
    crawl through its own checkpoint instead? Called under the host lock.

    Two things a bare "hit means reuse" got wrong, both about the consumer's OWN
    checkpoint at ``checkpoint_path``:

    1. A checkpoint may already exist there. If its crawl started AFTER the
       cached observation, it is the newer observation -- an interrupted
       ``--refetch-census`` sweep, nine thousand requests in -- and the operator
       asked for it, so it is resumed rather than abandoned for the stale entry.
       If it is OLDER (last night's walk, host-blocked at tile 300 before the
       grid run completed and promoted), the entry supersedes it and it is
       discarded here; otherwise nothing would ever touch it, because a hit
       returns before the checkpoint is opened and no pruner walks
       ``checkpoints/``.

    2. The entry may be THIS crawl's own -- the same (channel, variant) that
       paid for it, coming back because its tail died between promotion and the
       catalog row. That is #239/#256's re-finalize, and a resume from a
       COMPLETE checkpoint re-probes the tiles or cells that failed, because a
       refusal is time-varying. Reusing the entry as-is would inherit those
       holes for zero requests and silently drop the re-probe. So an own entry
       with failed work is handed BACK to its checkpoint path, and the ordinary
       resume path -- which already knows how to finish a complete checkpoint --
       re-probes and re-promotes it. An own entry with nothing failed is
       reused: that is the same result the resume would produce for no
       requests.

    Returns True to reuse the entry, False to proceed with the crawl's own
    checkpoint (which then exists at ``checkpoint_path`` in both False cases).
    """
    if checkpoint_path is None:
        return True
    started = _checkpoint_started_at(checkpoint_path)
    if started is not None:
        newer = False
        try:
            newer = datetime.fromisoformat(started) > datetime.fromisoformat(
                marker["crawl_started_at"]
            )
        except (KeyError, TypeError, ValueError):
            pass
        if newer:
            logger.info(
                f"Resuming the crawl checkpointed at {checkpoint_path} (started {started}) "
                f"rather than reusing the cached census fetched by "
                f"{marker.get('fetched_by')} (started {marker.get('crawl_started_at')}): "
                f"it is the newer observation"
            )
            return False
        logger.info(
            f"Discarding the checkpoint at {checkpoint_path} (started {started}): the cached "
            f"census fetched by {marker.get('fetched_by')} supersedes it"
        )
        discard_checkpoint(checkpoint_path)
    elif os.path.isdir(checkpoint_path):
        # A directory nothing ever committed to -- a crawl that died before
        # its first commit -- is debris the resume path would also sweep.
        discard_checkpoint(checkpoint_path)
    if same_crawl(marker, channel, variant) and marker.get("failed"):
        logger.warning(
            f"The cached census at {cache_path} is this crawl's own ({channel!r}, "
            f"{variant!r}) and recorded {len(marker['failed'])} failed tile(s)/cell(s): "
            f"handing it back to {checkpoint_path} so the resume re-probes them rather "
            f"than inheriting the holes"
        )
        if _demote_cache_to_checkpoint(cache_path, checkpoint_path):
            return False
    return True


def _read_census_cache_marker(
    cache_path: str, *, max_age_s: float, store_format_version: int | None = None
) -> tuple[dict | None, str | None]:
    """
    ``(marker, None)`` when this entry is usable, ``(None, reason)`` otherwise.

    Split from :func:`load_census_cache_marker` because :func:`prune_census_cache`
    needs the same verdict without the "delete exactly this one" framing, and a
    second copy of the rules is how the loader and the pruner would come to
    disagree about what "expired" means.

    ``(None, None)`` -- no entry at all -- is the ordinary first-consumer case
    and is not worth a line of log.

    Args:
        store_format_version: when given, the commit-record format this build's
            loader for that provider reads; a marker recording another is
            refused HERE, at the probe, rather than priced as a hit the child
            then refuses and refetches at full cost with no budget gate left.
    """
    marker_path = _marker_path(cache_path)
    if not os.path.exists(marker_path):
        if not os.path.isdir(cache_path):
            return None, None
        # Promotion is a single rename of an already-stamped directory, so this
        # is never a promotion in flight: it is a hand-copied directory, or an
        # entry from before the marker travelled with it. Nothing says who paid
        # or when the provider was observed, so it cannot be reused.
        return None, "it has no marker, so its provenance is unknown"
    try:
        with open(marker_path, encoding="utf-8") as f:
            marker = json.load(f)
        if marker["format_version"] != CENSUS_CACHE_FORMAT_VERSION:
            return None, (
                f"it is marker format v{marker['format_version']}, this build writes "
                f"v{CENSUS_CACHE_FORMAT_VERSION}"
            )
        if (
            store_format_version is not None
            and marker.get("store_format_version") != store_format_version
        ):
            return None, (
                f"its commit record is format v{marker.get('store_format_version')}, this "
                f"build reads v{store_format_version}"
            )
        # AGED FROM crawl_started_at -- when the provider was first observed --
        # not from completed_at. The window bounds how far apart two halves of
        # one dated observation may be, and a multi-night crawl's last commit
        # says nothing about how old its oldest rows are.
        age_s = (
            datetime.now(UTC) - datetime.fromisoformat(marker["crawl_started_at"])
        ).total_seconds()
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
    cache_path: str,
    *,
    max_age_s: float = CENSUS_REUSE_MAX_AGE_S,
    run_date: date | None = None,
) -> dict | None:
    """
    The provenance record for a usable cache entry, or None.

    NEVER RAISES, and an entry that is unusable BY ANYONE is DELETED rather
    than left in place -- both the same contract each provider's checkpoint
    loader keeps. Deleting is what keeps a stale entry from sitting under the
    live name until the pruner happens to run: the next consumer refetches and
    promotes a fresh one over the space this just freed.

    An entry that is unusable only for THIS consumer is refused and left alone.
    Today that is the ``run_date`` rule: a crawl that finished after the
    snapshot date being written cannot go into that snapshot, but is exactly
    right for the consumer dated tomorrow.

    FOR CONSUMERS ONLY. Every caller of this is a provider fetch holding its
    host lock, so no second process on this machine is mid-read of the entry
    being removed. A planning caller -- ``--estimate``, a budget pre-flight, the
    scheduler's per-channel estimate -- must use :func:`census_cache_probe`
    instead, which never deletes.

    Args:
        cache_path: from :func:`census_cache_path_for`.
        max_age_s: the reuse window; see :data:`CENSUS_REUSE_MAX_AGE_S`.
        run_date: see :class:`CensusCache`.
    """
    marker, reason = _read_census_cache_marker(cache_path, max_age_s=max_age_s)
    if reason is not None:
        logger.warning(f"Discarding the cached census at {cache_path}: {reason}")
        discard_checkpoint(cache_path)
        return None
    if marker is not None and run_date is not None:
        try:
            observed_until = datetime.fromisoformat(marker["completed_at"]).date()
        except (KeyError, TypeError, ValueError):
            observed_until = None
        if observed_until is not None and observed_until > run_date:
            logger.warning(
                f"Not reusing the cached census at {cache_path} for a snapshot dated "
                f"{run_date}: its crawl finished {observed_until}, after that date, so its "
                f"rows cannot be part of that observation; refetching. The entry stays for "
                f"consumers dated on or after {observed_until}."
            )
            return None
    return marker


class CacheEntryUnusableHere(Exception):
    """
    Raised by a provider's validator for a mismatch that belongs to the CALLER
    rather than to the entry -- KartaView's page size, an explicit radius. The
    entry is left in place for the consumers it does fit; deleting a shared
    Singapore sweep because one caller asked for ``--ipp 200`` would cost every
    other consumer ten hours.
    """


class _Validator(Protocol):
    def __call__(self, state: dict) -> tuple[Any, str | None]: ...


class _Completeness(Protocol):
    def __call__(self, handle: Any, state: dict, marker: dict) -> str | None: ...


def load_cached_store(
    cache_path: str,
    *,
    label: str,
    run_date: date | None,
    validate: _Validator,
    is_complete: _Completeness,
) -> tuple[Any, dict] | None:
    """
    A COMPLETE census another consumer already paid for, or None -- the loader
    skeleton both providers share, with only their two provider-shaped checks
    plugged in.

    Never raises and DELETES an entry it refuses on the entry's own account: an
    entry that does not describe this lattice will never describe it, and
    leaving it under the live name would make every later consumer pay the same
    read to reach the same verdict. A refusal that is this caller's alone
    (:class:`CacheEntryUnusableHere`) leaves it.

    Three things are checked, in this order:

    1. the marker's own reuse window and the caller's ``run_date``
       (:func:`load_census_cache_marker`);
    2. ``validate(state)`` -- the same geometric/footer cascade the provider's
       resume makes, returning ``(handle, None)`` or ``(None, reason)``; a cache
       entry is a moved checkpoint, so a re-registered grid or a truncated part
       must be caught identically;
    3. ``is_complete(handle, state, marker)`` -- the one check a resume
       deliberately does NOT make and the whole difference between the two. A
       partial entry reused as a census would publish the unfetched tiles' or
       unvisited cells' points as genuine no-imagery, absence never observed, in
       an immutable dated snapshot.

    Args:
        label: for the log line -- 'Mapillary census', 'KartaView sweep'.
        run_date: see :class:`CensusCache`.
    """

    def discard(reason: str) -> None:
        logger.warning(f"Ignoring the cached {label} at {cache_path}: {reason}")
        discard_checkpoint(cache_path)

    marker = load_census_cache_marker(cache_path, run_date=run_date)
    if marker is None:
        return None  # no entry, one the marker check removed, or one not for this date
    try:
        with open(_state_path(cache_path), encoding="utf-8") as f:
            state = json.load(f)
        handle, reason = validate(state)
        if reason is not None:
            discard(reason)
            return None
        reason = is_complete(handle, state, marker)
        if reason is not None:
            discard(reason)
            return None
    except CacheEntryUnusableHere as e:
        logger.info(
            f"Not reusing the cached {label} at {cache_path} for this caller: {e}; the "
            f"entry stays for the consumers it does fit"
        )
        return None
    except Exception as e:
        # Broad on purpose, the loaders' never-raise posture: an unreadable
        # cache entry -- a missing commit record included -- must cost a
        # re-fetch, never a city.
        discard(f"{type(e).__name__}: {e}")
        return None
    return handle, marker


def reused_census_provenance(
    marker: dict, *, channel: str | None, variant: str | None
) -> dict[str, Any]:
    """
    The accounting and provenance half of a reuse result -- the keys every
    provider's ``_reuse_cached_*`` merges into its dict, so the two cannot
    price a reuse differently.

    THE TWO REQUEST COUNTERS SPLIT DIFFERENTLY HERE, and it is the one rule in
    this feature that is easy to get backwards:

    * ``api_requests`` is 0 unconditionally. This process issued none, and the
      daily ledger is additive and keyed by (date, provider) -- charging it
      anything would bill another channel's spend against this one's budget
      gate, which is exactly the per-IP figure #241/#267/#286 reason about.
    * ``api_requests_total`` is the crawl's cost ONLY when the reuser is
      :func:`same_crawl` -- the (channel, variant) that paid it -- because that
      is not a cross-channel reuse at all: it is a caller coming back to write
      the row for a crawl it paid for, and the row must still say what the
      collection cost. A DIFFERENT channel records 0, and the provenance
      columns are what explain the zero.

    ``checkpoint_path`` is None: the entry is shared, so it is nobody's to
    discard. ``census_fetched_at`` is when the provider was FIRST observed,
    which is what a reused census's rows are stamped with.
    """
    return {
        "api_requests": 0,
        "api_requests_total": (
            int(marker.get("api_requests_total") or 0)
            if same_crawl(marker, channel, variant)
            else 0
        ),
        "checkpoint_path": None,
        "census_fetched_by": marker.get("fetched_by"),
        "census_fetched_at": marker.get("crawl_started_at"),
        "census_reused": True,
    }


def observation_timestamp(fetched: dict[str, Any], started_at: str) -> str:
    """
    What every row of a census is stamped with as ``query_timestamp``: WHEN THE
    PROVIDER WAS OBSERVED, not when this process started, and only when the
    census was REUSED (issue #290).

    Every row of a reused census was fetched by an earlier collection, possibly
    on an earlier night, so stamping it with this process's clock would record
    an observation that never happened -- and ``json_summarizer`` reports the
    run's start/end from exactly this column. A freshly fetched census keeps
    ``started_at``, which holds #256's byte-identity contract between an
    interrupted census and an uninterrupted one.

    ``calculate_run_stats`` still ages imagery against ``run_date`` rather than
    this, so a reused census can skew a pano age by at most the reuse window
    (7 days against an 80-day cadence).

    One function rather than the same conditional in three tails, because a
    grid run and its paired walk must stamp one census with one instant.
    """
    if fetched.get("census_reused") and fetched.get("census_fetched_at"):
        return str(fetched["census_fetched_at"])
    return started_at


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

    It does read the marker's ``store_format_version`` against this build's
    (:data:`STORE_FORMAT_VERSIONS`), so an entry the provider's loader would
    refuse on format is a MISS here rather than a hit priced at 0 that the child
    then refetches at full cost with no budget gate left -- the scheduler passes
    no in-child request cap. What remains is a STRONG HINT rather than a
    guarantee (a torn part, a re-registered grid): the consumer's own loader
    still validates and refetches, and that asymmetry is the safe one -- an
    over-optimistic probe costs a request budget that turns out to be
    unnecessary, never a wrong artifact. A scheduler caller narrows
    ``max_age_s`` by the length of its batch for the same reason, so an entry
    that would expire mid-night is not priced as free.

    IT ALSO DELETES NOTHING, unlike :func:`load_census_cache_marker`: a planning
    caller holds no host lock, and the consumer that actually reads an entry
    does the deleting under the lock. Expired entries are swept by
    :func:`prune_census_cache` in the scheduler's tail.

    Returns the marker (so a caller can name who paid and when), or None.
    """
    marker, _reason = _read_census_cache_marker(
        census_cache_path_for(provider, city_id, bbox),
        max_age_s=max_age_s,
        store_format_version=STORE_FORMAT_VERSIONS.get(provider),
    )
    return marker


def prune_census_cache(max_age_s: float = CENSUS_REUSE_MAX_AGE_S) -> int:
    """
    Delete cache entries past the reuse window, and the debris of failed moves.

    The cache is bounded by this and by nothing else: an entry is written for
    every census the night fetches and is never overwritten until that city is
    collected again, which for a real catalog is ~80 days away. One night's
    ~20 cities of Detroit-scale parquet is hundreds of megabytes, so a cache
    nobody swept would grow without limit on a host whose disk is shared.

    SAFE WITHOUT A LOCK, because promotion is a single rename of an
    already-stamped directory: there is no instant at which an entry exists
    under its name without its marker, so nothing here can delete a census a
    concurrent collection has just moved and not yet stamped (the race the
    first version of this feature had, since its marker was a second step). The
    two shapes that are not entries -- an EXDEV staging copy (``<entry>.tmp``)
    and a marker-less directory -- are removed only once they have sat for
    :data:`_PRUNE_DEBRIS_GRACE_S`, so a cross-filesystem copy in progress at the
    moment the tail runs is left to finish.

    Best effort in every direction -- a missing cache directory is zero, an
    unreadable entry is skipped rather than raised on -- because this runs in
    the scheduler's tail beside the backup and the publish, where #167's rule is
    that no housekeeping step may cost the night's visibility.

    Returns the number of directories removed.
    """
    root = census_cache_dir()
    removed = 0
    try:
        providers = sorted(os.listdir(root))
    except OSError:
        return 0  # nothing has ever been cached on this host
    now = time.time()
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
            if entry.endswith(".tmp") or not os.path.exists(_marker_path(path)):
                try:
                    idle_s = now - os.path.getmtime(path)
                except OSError:
                    continue
                if idle_s <= _PRUNE_DEBRIS_GRACE_S:
                    continue
                reason = "it is not a cache entry and nothing has touched it for a day"
            else:
                _marker, reason = _read_census_cache_marker(path, max_age_s=max_age_s)
                if reason is None:
                    continue
            logger.info(f"Pruning the cached census at {path}: {reason}")
            discard_checkpoint(path)
            removed += 1
    return removed
