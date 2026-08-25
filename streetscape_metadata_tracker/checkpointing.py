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

This module is also what keeps ``download_mapillary`` from importing
``download_kartaview``: no provider module may import from another's (CLAUDE.md),
and a shared home is the alternative to a copy. It depends only on the standard
library and :mod:`streetscape_metadata_tracker.paths`.
"""

import logging
import os
import shutil
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


# THE PATH IS THE CALLER'S, AND IT MUST BE DATE-FREE. The whole point is a sweep
# that spans nights, and a run is dated on the day it COMPLETES -- so a
# date-bearing path (the `.downloading` and `.harvesting` convention, where a
# collection finishes in one night) would make every night start from zero. It
# must also be a realpath: on makelab2 the unit's WorkingDirectory is a symlink,
# and two spellings of one directory would silently be two checkpoints, i.e.
# exactly the restart-from-zero this exists to prevent (see host_lock.lock_dir,
# which carries the same reasoning for the same host). Finally it belongs OUTSIDE
# `data/` -- a partial census is the one artifact that must never reach the
# publisher -- which is what the gitignored `checkpoints/` sibling is for.
#
# THE PATH KEY IS (CITY, GRID GEOMETRY, CHANNEL), AND THE CHANNEL IS NOT
# OPTIONAL. Both census providers have a grid channel and a street channel, and
# a road walk sweeps THE SAME frozen `grid_bbox` the grid run does -- that is
# why the fetch is shared at all. So a walk and a grid run of one city agree on
# every geometric parameter a checkpoint records: each provider's validation
# would pass and the two channels would happily resume each other's crawls. The
# census would be the same either way, but the channels meter into SEPARATE
# `api_usage` ledgers, so one would inherit the other's `api_requests_total`,
# and for Mapillary they hold different credentials besides.
#
# THE CALLER OWNS THE DIRECTORY AND MUST `discard_checkpoint` IT once its dated
# artifacts are durable. The fetch functions deliberately do NOT delete the
# checkpoint on a clean crawl, and that is the whole of what makes "a crash in
# the caller's tail" recoverable: the census is returned as a DataFrame and the
# caller writes the CSV, the stats, the run row, the JSON and the diff after this
# returns. Deleting here -- which is where the delete first lived -- would mean
# the checkpoint is already gone by the time any of that can fail, so the one
# interruption of the four named above that lands OUTSIDE this function would be
# the one not covered. Discarding is one line at the end of a caller that just
# finished writing its artifact, exactly as `download_gsv` unlinks `.downloading`
# after its CSV lands. A caller that forgets is bounded rather than broken:
# CHECKPOINT_MAX_AGE_S caps how long a complete checkpoint can be re-finalized,
# each provider's loader says so at WARNING, and the tell is `api_requests == 0`.


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


def checkpoint_path_for(city_id: str, bbox: tuple[float, float, float, float], channel: str) -> str:
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

    Args:
        city_id: canonical catalog slug.
        bbox: the frozen grid's (min_lon, min_lat, max_lon, max_lat).
        channel: the collecting channel's name -- 'mapillary' or 'kartaview' for
            a grid run, 'mapillary_streets' or 'kartaview_streets' for a walk.
    """
    geometry = "_".join(f"{coord:.6f}" for coord in bbox)
    return os.path.join(checkpoint_dir(), channel, f"{city_id}_{geometry}")


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
