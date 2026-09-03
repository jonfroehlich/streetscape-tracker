"""
Staggered collection scheduler for Streetscape Tracker.

Designed to run as a daily systemd timer (oneshot): each invocation of
`run-due` collects the cities that are due today, within a configurable
daily API-request budget, then regenerates the aggregate JSON and
(optionally) publishes to the web server. All state lives in the SQLite
catalog, so the process is crash-safe and a missed day self-heals (due
selection is ordered stalest-first).

Usage (--config accepted on either side of the subcommand):
    python -m streetscape_metadata_tracker.scheduler [--config PATH] status
    python -m streetscape_metadata_tracker.scheduler [--config PATH] assign
    python -m streetscape_metadata_tracker.scheduler [--config PATH] run-due [--dry-run] [--limit N] [--provider CHANNEL]
    python -m streetscape_metadata_tracker.scheduler [--config PATH] regenerate-aggregate [--publish]
    python -m streetscape_metadata_tracker.scheduler [--config PATH] reconcile-walks [--date D] [--dry-run]
    python -m streetscape_metadata_tracker.scheduler [--config PATH] fetch-driving-plan [--force] [--from-file P --date D]
    python -m streetscape_metadata_tracker.scheduler [--config PATH] backup-status
    python -m streetscape_metadata_tracker.scheduler [--config PATH] restore-backup PATH [--to DEST]

Config: TOML (see config/scheduler.toml). Requires Python 3.11+ (tomllib).
"""

import argparse
import concurrent.futures
import contextlib
import gzip
import json
import logging
import logging.handlers
import os
import signal
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import tomllib
import traceback
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, NamedTuple

from tabulate import tabulate

from . import catalog_backup, cgroup_memory, db, driving_plan
from .alerting import AlertConfig, send_alert, should_alert
from .checkpointing import (
    CENSUS_PROVIDERS,
    CENSUS_REUSE_MAX_AGE_S,
    CHECKPOINT_MAX_AGE_S,
    census_cache_probe,
    checkpoint_path_for,
    frozen_bbox,
    prune_census_cache,
    sweep_progress,
)
from .city_registration import (
    MAX_GRID_DIM_M,
    CityResolutionError,
    resolve_or_register_city,
)
from .download_common import (
    HOST_BY_BUSY_EXIT_CODE,
    HOST_BY_EXIT_CODE,
    HOST_KARTAVIEW,
    HOST_LABELS,
    HOST_MAPILLARY_TILES,
    HOST_OVERPASS,
    SWEEP_INCOMPLETE_EXIT_CODE,
    coerce_jitter,
    redact_credentials,
)
from .download_kartaview import (
    DEFAULT_BACKPRESSURE_RETRIES,
    DEFAULT_CALIBRATION_PROBES,
    DEFAULT_SWEEP_REQUESTS_PER_MINUTE,
    RADIUS_LADDER_M,
    estimate_sweep_requests,
)
from .download_mapillary import (
    DEFAULT_TILE_JITTER,
    DEFAULT_TILE_REQUESTS_PER_MINUTE,
    estimate_tile_count,
)
from .json_summarizer import (
    generate_aggregate_v2,
    generate_driving_plan_summary,
    generate_streetwalk_manifest,
    regenerate_run_json,
)
from .naming import (
    DEFAULT_NETWORK_TYPE,
    KNOWN_PROVIDERS,
    STREETWALK_NETWORK_TOKENS,
    generate_streetwalk_filename,
    streetwalk_coverage_filename,
)
from .walk_diff import compute_and_record_walk_diff

# Isolated street-coverage collection channels (issue #99). These ARE scheduled
# channels — each cycles the catalog exactly like a grid provider, with its own
# schedule_state cadence, failure counting and api_usage ledger — they just run
# a different subprocess (the road-walk collector) against a different
# credential. The map is channel -> imagery provider, which is what lands in
# street_walks and the published artifacts.
#
# This one entry is what most of the wiring below reads: `_street_collect_cmd`
# takes the child's `--provider` from it, `channel_census_cache_marker` derives
# which census a channel would reuse from it, and `is_street_channel` is it. A
# channel added here without the decisions in CHANNEL_HOSTS and
# CHANNEL_DEFAULT_MEMBERSHIP is a red test, not a channel that runs half-wired
# (both are asserted as set EQUALITY against KNOWN_PROVIDERS | STREET_CHANNELS).
STREET_CHANNELS = {
    "gsv_streets": "gsv",
    "kartaview_streets": "kartaview",
    "mapillary_streets": "mapillary",
}


def is_street_channel(name: str) -> bool:
    """True for a road-walk collection channel (vs. a grid run provider)."""
    return name in STREET_CHANNELS


# Which per-IP third-party hosts each channel depends on (issue #208). When one
# of them refuses us, every channel listed against it would fail identically
# for the rest of the night, so the loop stops trying them.
#
# 'gsv' is empty on purpose: Google meters the Street View Static API per
# project, not per IP, so a GSV failure is never a whole-host condition.
# 'gsv_streets' needs Overpass because a road walk starts by fetching the
# city's street network, and 1134 of 1144 enabled cities have no cached
# GraphML — a first walk always goes to the network.
CHANNEL_HOSTS: dict[str, tuple[str, ...]] = {
    "gsv": (),
    "gsv_streets": (HOST_OVERPASS,),
    "mapillary": (HOST_MAPILLARY_TILES,),
    "mapillary_streets": (HOST_OVERPASS, HOST_MAPILLARY_TILES),
    # kartaview shares its host with nothing, which is why it was the channel
    # that moved the effective concurrency ceiling from 3-of-4 to 4-of-5.
    # test_every_scheduled_channel_declares_its_per_ip_hosts asserts set
    # EQUALITY against KNOWN_PROVIDERS, so a token landing without an entry
    # here is a red test rather than a channel that silently fails open.
    "kartaview": (HOST_KARTAVIEW,),
    # The walk needs BOTH: it starts at the city's OSM street network and then
    # sweeps kartaview.org for the census it joins against. Overpass is not
    # hypothetical here even though a re-walk reads a cached GraphML — a FIRST
    # walk of a city always goes to the network, the same reason gsv_streets
    # declares it.
    #
    # Adding it does NOT lower the effective concurrency ceiling, and the
    # arithmetic is worth stating because the figure is a property of this
    # graph rather than a constant: the largest host-disjoint set is gsv (no
    # host) + ONE of the three Overpass channels + mapillary (tiles) +
    # kartaview (KV) = 4. So 4 of 6 now, where it was 4 of 5.
    "kartaview_streets": (HOST_OVERPASS, HOST_KARTAVIEW),
}

# What a NULL `schedule_state.member` means for each channel (issue #248), i.e.
# whether a channel's nightly queue is the whole enabled catalog or only the
# cities an operator has opted in with `enroll-city`.
#
# Read it as CHANNEL_DEFAULT_MEMBERSHIP[provider] — NEVER `.get(p, True)`, and
# never as a set of opt-in names tested with `in`. Both of those classify a
# NEWLY ADDED provider as "every enabled city, immediately", which is precisely
# how #225 phase 3b created the bug this table fixes: putting a token in
# naming.KNOWN_PROVIDERS was enough to make a channel configurable, and four
# fail-open arms then did the wrong thing silently. A missing entry must be a
# KeyError, and test_every_scheduled_channel_declares_its_default_membership
# asserts set EQUALITY so the token cannot land without a decision.
#
# Not a config key either: load_scheduler_config reads provider keys with
# p.get(...), so a typo'd key is swallowed — and membership is catalog data
# (like cities.enabled), not per-host policy, which also dodges the standing
# footgun that production reads config/scheduler.makelab1.toml while the repo
# default is config/scheduler.toml.
#
# kartaview is False because one pass over all 1,144 enabled cities prices at
# ~186,000 requests ≈ 186 h (docs/experiments/kartaview-sweep-cost.md), and
# stalest-first ordering knows nothing about cost.
#
# kartaview_streets is False for the same arithmetic and then some: a road walk
# reads the SAME sweep the grid run reads, so a whole-catalog pass prices at
# another ~186,000 requests — and it buys nothing the grid run has not already
# paid for whenever the two are paired. Opting a city in is therefore a
# statement that its STREET coverage is wanted, which is a different question
# from whether its grid coverage is, and is why this is a second enrollment
# rather than a mirror of [providers.kartaview]'s.
CHANNEL_DEFAULT_MEMBERSHIP: dict[str, bool] = {
    "gsv": True,
    "gsv_streets": True,
    "mapillary": True,
    "mapillary_streets": True,
    "kartaview": False,
    "kartaview_streets": False,
}


def is_opt_in_channel(name: str) -> bool:
    """True when this channel collects only cities explicitly enrolled in it."""
    return not CHANNEL_DEFAULT_MEMBERSHIP[name]


# Whether a channel accepts a REQUEST CAP that pauses and checkpoints instead of
# failing (issues #273, #274). Not "does it checkpoint at all": the two are
# different properties and only this one makes a capped launch safe.
#
# mapillary and mapillary_streets checkpoint their tile census (#256), and are
# still False here, because download_mapillary takes only
# max_requests_per_minute -- a PACING knob. It has no request cap, no
# stop_reason and no SweepIncompleteError, so a Mapillary census resumes after
# an interruption it did not choose and has no way to stop itself at a number.
# Handing one a cap would do nothing; handing it a budget it cannot honour is
# the overrun this table exists to bound.
#
# Read it as CHANNEL_RESUMABLE[provider], NEVER `.get(p, ...)`, for exactly the
# reason CHANNEL_DEFAULT_MEMBERSHIP spells out above: either default is a wrong
# answer for a channel nobody has thought about. False would silently keep a
# resumable channel on the permanent-skip arm; True would hand a cap to a child
# that ignores it and let the budget gate believe it is bounded. A missing
# entry must be a KeyError, and
# test_every_scheduled_channel_declares_whether_it_is_resumable asserts set
# EQUALITY so a new token cannot land without a decision. That test earned its
# keep immediately: #299 added kartaview_streets while this was in review, and
# the set check is what turned an unconsidered channel into a red build instead
# of a sixth channel quietly inheriting whichever default was written here.
#
# BOTH KartaView channels are True, because the walk reads the same census by
# the same radius sweep and so inherits the same defect exactly -- its
# --daily-budget is a GATE priced from a geometric floor, not a ceiling on what
# the sweep then spends. Marking the walk True is only honest because
# _street_collect_cmd actually forwards the cap; a True here with nothing
# reading it downstream is the fail-open this table was written against.
CHANNEL_RESUMABLE: dict[str, bool] = {
    "gsv": False,
    "gsv_streets": False,
    "kartaview": True,
    "kartaview_streets": True,
    "mapillary": False,
    "mapillary_streets": False,
}


def is_resumable_channel(name: str) -> bool:
    """True when a request cap pauses this channel's work rather than failing it."""
    return CHANNEL_RESUMABLE[name]


# Channels that KNOWN_PROVIDERS makes configurable but that the scheduler cannot
# yet run correctly. load_scheduler_config drops such a block from `providers`
# and records the error in SchedulerConfig.unwired_channel_errors; the two
# channel-running commands (run-due, assess-city) refuse with USAGE_EXIT_CODE
# while the read-only subcommands — backup-status and restore-backup are the
# incident-time handles — keep working with the error in the log.
#
# #225 phase 3b put "kartaview" in naming.KNOWN_PROVIDERS so the CLI could
# collect a city by hand, and the config loader gates on that same tuple -- so
# [providers.kartaview] started PARSING while four arms here stayed fail-open,
# none of which raises: the channel would simply have run wrong, nightly.
#
# THREE OF THE FOUR ARE NOW WIRED (issue #238). They are still listed because
# what each one silently did is the reason its replacement looks as it does:
#   * city_timeout_seconds' allow-list returned the flat city_timeout_minutes
#     floor for an unlisted channel. At the sweep's 16 req/min that SIGKILLs
#     Singapore (~9,974 requests, ~10.4 h) at 180 minutes, and a killed child
#     records NO api_usage, so its whole spend vanishes from the daily ledger.
#     Now _kartaview_timeout_seconds.
#   * estimate_requests fell through to the GSV GRID formula -- thousands of
#     "requests" for a bbox the sweep covers in a handful of circles -- so the
#     budget guard was wrong in both directions. Now estimate_kartaview_requests.
#   * enabled_providers' rank.get(p, 99) ordered it by accident. Now an explicit
#     rank of 4, argued in that docstring.
#   * _run_one_city did not hand the child the channel's configured pace, so a
#     timeout derived from that rate could be measured against a rate the sweep
#     never used. Now sent as --kartaview-max-requests-per-minute.
#
# DUENESS WAS THE LAST BLOCKER AND IS NOW WIRED (#248): CHANNEL_DEFAULT_MEMBERSHIP
# makes kartaview an opt-in channel, so its nightly queue is the cities an
# operator enrolled with `enroll-city` rather than all 1,144, and _collect_due
# hoists an opt-in-only city so that queue is actually reached. The entry is
# gone; the dict stays, because the record/drop/don't-raise asymmetry above is
# the mechanism the NEXT unwired channel needs, and rebuilding it from scratch
# under time pressure is how a fail-open arm gets missed again.
UNWIRED_CHANNELS: dict[str, str] = {}


logger = logging.getLogger("streetscape_scheduler")

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = _PROJECT_ROOT / "config" / "scheduler.toml"


@dataclass
class ProviderConfig:
    """Per-channel scheduling settings ([providers.NAME] in the TOML)."""

    enabled: bool = True
    daily_request_budget: int = 250_000  # gsv: metadata requests; mapillary: tiles
    # Street channels only: client-side pacing for the isolated streets key
    # (0/None → fall back to [download].max_requests_per_minute) and the
    # on-street sample spacing the road walk collects at.
    max_requests_per_minute: int | None = None
    # Mapillary channels only: the exponential share of each tile-request gap
    # (issue #292) — also the resulting coefficient of variation, and 1 minus it
    # is the gap's floor as a fraction of the mean. None leaves the collector's
    # own default in force; 0 restores an exact cadence. Validated at load by
    # `coerce_jitter`, so an out-of-range config field never reaches a child.
    jitter: float | None = None
    spacing_m: int = 15
    # Which OSM network the road walk covers. 'drive' (motorized public roads)
    # is the scheduled default; 'all_public' additionally walks alleys,
    # footways, park paths, cycleways and steps, which is a substantially
    # larger network — see _BROAD_NETWORK_MULTIPLIER. Each type is its own walk
    # series (own artifacts, own catalog rows, own cadence), so changing this
    # starts a new series rather than continuing the existing one.
    network_type: str = "drive"


@dataclass
class ResourceGuardConfig:
    """[resource_guard] — back off when the shared host is already busy.

    makelab1 is shared with other Makeability Lab services. The systemd unit
    already caps our absolute CPU/RAM, but those caps are static. This guard
    lets a nightly run *react* to co-tenant load: when the box is under real
    pressure it reduces its own concurrency (the child's ``--connection-limit``)
    instead of piling on. All checks read Linux ``/proc`` and are best-effort —
    on any other platform, or if the reads fail, the guard is a silent no-op.
    """

    enabled: bool = True
    # Throttle to min_connection_limit when MemAvailable drops below this.
    min_available_memory_gb: float = 8.0
    # Throttle when the 5-minute load average exceeds this * CPU count.
    max_load_per_core: float = 0.9
    # Floor for the scaled-down connection limit (never throttle below this).
    min_connection_limit: int = 5


@dataclass
class DrivingPlanConfig:
    """[driving_plan] — nightly snapshot of Google's published Street View
    driving-plan feed (issue #176).

    NOT a [providers.*] channel on purpose: it has no API key, no request
    budget, no per-city cadence — one unauthenticated request per night for
    the whole worldwide feed. RAW artifacts live OUTSIDE data/ so the publish
    rsync never sees them (its whitelist would republish Google's feed
    verbatim); the derived join in data/driving_plan.json.gz IS published on
    purpose. See driving_plan.py for the mirror-vs-analysis distinction.
    """

    enabled: bool = True
    archive_dir: str = str(_PROJECT_ROOT / "archive" / "gsv_driving_plan")
    url: str = driving_plan.FEED_URL
    timeout_s: float = 60.0


@dataclass
class SchedulerConfig:
    # [schedule]
    cycle_days: int = 90
    grace_days: int = 7
    daily_request_budget: int = 10_000_000  # legacy gsv budget ([providers] overrides)
    max_cities_per_day: int = 20
    # Slots reserved out of max_cities_per_day for cities due ONLY on an opt-in
    # channel (issue #282). None means "derive from the cap" — see
    # _opt_in_reservation, which is the single place that resolution happens.
    # It belongs beside max_cities_per_day because a night's cap and its split
    # are only meaningful read together.
    opt_in_cities_per_day: int | None = None
    max_consecutive_failures: int = 5
    city_timeout_minutes: int = 180
    # Wall-clock ceiling on the CITY LOOP, leaving the tail (aggregate,
    # manifest, catalog backup, publish) room to run inside whatever budget the
    # supervisor allows. The unit is Type=oneshot under TimeoutStartSec, so a
    # batch that overruns is killed mid-loop and publishes NOTHING — every city
    # it already collected stays invisible on the public site until someone runs
    # regenerate-aggregate by hand (issue #167; on 2026-07-29 a batch died at
    # exactly 12 h having collected most of the night). Keep this comfortably
    # below the unit's TimeoutStartSec so this deadline, not systemd, ends a
    # long night.
    max_batch_hours: float = 10.0
    # How many of ONE city's channels may be in flight at once (issue #240).
    # 1 is the historical behaviour — channels back-to-back on the main thread,
    # byte-equivalent to the pre-#240 loop — and is deliberately the default so
    # this is a config-only lever: rolling the concurrency back is an edit to a
    # TOML and a restart, not a deploy. Above 1 the city's channels run in
    # host-disjoint lanes, which compresses a night's WALL CLOCK only; no
    # channel goes faster, because each keeps its own limiter and its own daily
    # budget. Channels that share a per-IP third party never overlap whatever
    # this says (the launch pass defers them), so with today's five channels the
    # effective ceiling is 4: mapillary_streets shares Overpass with gsv_streets
    # and the tile CDN with mapillary, so it always runs after both, while
    # kartaview shares its host with nothing and can always take a lane.
    max_concurrent_channels: int = 1
    # [download]
    batch_size: int = 100
    connection_limit: int = 50
    request_timeout_s: float = 30.0
    # Pause between cities. 5 s, not the historical 60 s (issue #306): every
    # mechanism the original "spread load" rationale named now exists properly
    # — a per-channel `max_requests_per_minute` limiter inside each child, the
    # cross-process lock that serializes the three per-IP metered hosts (#208),
    # jittered Mapillary tile gaps (#292), and osmnx's server-advertised
    # Overpass slot wait — while the sleep itself was 62 s of a small city's
    # 83 s slot. Kept non-zero rather than deleted: it is the only thing
    # standing between one city's writeback and the next city's first write,
    # and a settle time is cheap where a race is not.
    sleep_between_cities_s: int = 5
    # Client-side gsv pacing; 80% of the API's default 30k/min quota. Scale
    # with the project's granted quota. 0 disables.
    max_requests_per_minute: int = 24_000
    # [paths]
    data_dir: str = str(_PROJECT_ROOT / "data")
    db_path: str = ""
    log_dir: str = str(_PROJECT_ROOT / "logs")
    # Dated catalog backups (issue #145). Its own directory rather than log_dir:
    # that tree is size-rotated, and a backup that ages out with the logs is not
    # a backup. Must sit inside the path CSE IT snapshots — under the project
    # root it does, and the publish rsync only ever walks data_dir, so nothing
    # here can leak to the public web server.
    backup_dir: str = str(_PROJECT_ROOT / "backups")
    # [publish]
    publish_enabled: bool = False
    publish_script: str = str(_PROJECT_ROOT / "sync_data_to_server.sh")
    # Publish by local rsync to an NFS-mounted docroot instead of over SSH.
    # The publish script has always supported this, but only by reading
    # STREETSCAPE_PUBLISH_LOCAL from the environment — which the nightly unit
    # sets and an operator shell does not, so a hand-run `regenerate-aggregate
    # --publish` on makelab2 took the SSH path, failed with rsync code 12, and
    # emailed a publish-FAILED alert that looked like an outage (issue #215).
    # Declaring it in config makes publishing a property of the host's
    # configuration rather than of whoever's shell happened to invoke it.
    publish_local: bool = False
    # Public base URL of the site, used only to print operator-facing links
    # (e.g. assess-city's city page). Empty means print relative URLs. A missing
    # trailing slash is added in __post_init__ rather than required of the
    # operator: link building is bare concatenation, so "…/streetscape-tracker"
    # would silently emit "…/streetscape-trackercity.html".
    site_url: str = ""
    # [providers.*] — when None (no section in the TOML), falls back to
    # gsv-only with the legacy [schedule].daily_request_budget
    providers: dict[str, ProviderConfig] | None = None
    # [providers.*] blocks the naming contract knows but the scheduler cannot
    # run yet (UNWIRED_CHANNELS). Such a block is DROPPED from `providers` at
    # load so nothing downstream can price, budget or launch it, and its error
    # message collects here instead of raising: a load-time ValueError took
    # down EVERY subcommand — backup-status and restore-backup are the
    # incident-time handles — over a config block that only run-due and
    # assess-city could ever act on. Those two refuse (USAGE_EXIT_CODE) while
    # this list is non-empty; everything else proceeds with the error logged.
    unwired_channel_errors: list[str] = field(default_factory=list)
    # [alerts] — operator email on unhealthy runs (off by default)
    alerts: AlertConfig = field(default_factory=AlertConfig)
    # [resource_guard] — load/RAM-aware concurrency backoff on shared hosts
    resource_guard: ResourceGuardConfig = field(default_factory=ResourceGuardConfig)
    # [driving_plan] — nightly driving-plan feed snapshot (issue #176)
    driving_plan: DrivingPlanConfig = field(default_factory=DrivingPlanConfig)

    def __post_init__(self):
        if not self.db_path:
            self.db_path = db.get_default_db_path(self.data_dir)
        if self.site_url and not self.site_url.endswith("/"):
            self.site_url += "/"
        if self.providers is None:
            self.providers = {"gsv": ProviderConfig(daily_request_budget=self.daily_request_budget)}

    def enabled_providers(self) -> list[str]:
        """Enabled channel names in a stable canonical order, most expensive first.

        The rule is **most expensive first, EXCEPT where truncation is cheapest
        to absorb** — kartaview is the exception and ranks last (#238).

        The mechanism is the deadline CLAMP. ``remaining_s`` is read fresh at
        every launch (one ``time.monotonic()`` per LAUNCHED channel, plus one
        per resumable channel that is priced and then skipped, since its request
        cap is sized against the clamped timeout — see #273) and
        ``city_timeout_seconds`` clamps the derived timeout down to it, floored
        at ``_MIN_CLAMPED_TIMEOUT_S`` — so a channel launched later sees less of
        the batch deadline, and an expensive one launched late can be truncated
        to the floor and SIGKILLed part-way. Expensive channels therefore start
        while the most of it remains. That holds only while no single channel is
        long enough to consume the deadline by itself; one that IS starves
        everything behind it, and for that channel the question stops being which
        is most expensive and becomes which can best absorb being truncated. A
        multi-hour KartaView sweep is that channel, and it absorbs truncation
        most cheaply because #239 checkpoints it — cheaply, not freely: no
        channel keeps its ledger row through a SIGKILL, since every
        ``api_usage`` write happens in the child after the download returns.

        Order also decides which channels have FINISHED when a wind-down stops
        the city (a SIGTERM is a submit gate, #206, but ``KillMode`` defaults to
        control-group, so a ``systemctl stop`` takes the in-flight children with
        it — see ``_log_stop_declined``), and which claim a lane first when a
        city has more channels than lanes. Above one lane it is the ATTEMPT
        order rather than the launch order: host affinity can defer a
        higher-ranked channel and let a lower-ranked one take the free slot. It
        does not keep a per-IP host to one talker — affinity does that, under
        any ordering.

        ``test_the_deadline_is_a_submit_gate_and_every_lane_child_gets_its_own_remaining_s``
        pins the clamp mechanism, as a decreasing sequence in submit order.

        FOUR superseded rationales for this ordering, and what each got wrong,
        are recorded in docs/scheduler.md. Read them before writing a fifth:
        every one was reasoned from prose adjacent to this docstring instead of
        from the code it describes, which was ~200 lines away the whole time.
        """
        # kartaview_streets ranks immediately AFTER kartaview, and that adjacency
        # is a COST decision rather than tidiness. The two read one observation:
        # whichever runs first pays the sweep and promotes it into the shared
        # census cache (#290), and the second then prices at 0 through
        # `_channel_estimate`. Ordering the walk before the grid run would work
        # equally well arithmetically — the saving is symmetric — but it would
        # put the multi-hour sweep behind a channel that can be deferred by host
        # affinity, so the grid run keeps the earlier slot and the walk inherits
        # a paid-for census. Separating them (anything ranked between) is the
        # only ordering that is actually wrong here, because the truncation
        # argument above applies to BOTH and a night that reaches one but not
        # the other pays full price on the next.
        rank = {
            "gsv": 0,
            "gsv_streets": 1,
            "mapillary": 2,
            "mapillary_streets": 3,
            "kartaview": 4,
            "kartaview_streets": 5,
        }
        return sorted(
            (p for p, pc in self.providers.items() if pc.enabled),
            key=lambda p: (rank.get(p, 99), p),
        )


# The share of a night's city cap reserved for opt-in-only cities when
# [schedule].opt_in_cities_per_day is unset. A quarter, so the derived value at
# prod's cap of 20 is 5 — comfortably above the two-city seed set (so this
# changes no night that runs today) and comfortably below the cap (so a widened
# enrolled set cannot starve the default-membership channels). It is a divisor
# rather than a constant because the thing being split is the cap, and a
# constant would silently become the whole cap if someone lowered it.
_OPT_IN_SLOT_SHARE = 4


def _opt_in_reservation(cfg: SchedulerConfig, max_cities: int) -> int:
    """
    How many opt-in-only cities may lead tonight's slate (issue #282).

    The single place ``[schedule].opt_in_cities_per_day``'s None is resolved, so
    a config that sets it and a config that does not cannot disagree about what
    the reservation means. 0 is a legitimate value: it switches the promotion
    off entirely without un-enrolling anybody.

    BOTH PATHS SCALE WITH THE RUN'S CAP, and the explicit one has to because
    ``max_cities`` is the ``--limit`` override rather than the standing
    ``max_cities_per_day``. A bare ``min(configured, max_cities)`` saturates:
    with the ``opt_in_cities_per_day = 5`` the shipped config comments show an
    operator uncommenting, ``run-due --limit 4`` would clamp the reservation to
    4 and hand the WHOLE night to opt-in-only cities -- gsv, gsv_streets,
    mapillary and mapillary_streets collecting nothing, which is the starvation
    this key exists to prevent, reached through the flag meant to narrow a run.
    So an explicit value is scaled by the same ratio the cap moved, and only
    then clamped. At ``--limit == max_cities_per_day`` the scaling is the
    identity, so a nightly run is unaffected.

    The final clamp to ``max_cities`` stays, and stays at the cap rather than
    below it: a reservation EQUAL to the cap is the unbounded hoist spelled
    differently, but it is a thing an operator can mean, and the starvation
    WARNING in ``cmd_run_due`` is kept precisely as the backstop that names it.
    Foreclosing it here would turn that warning into dead code and take away a
    deliberate choice; `run-due --provider kartaview` is the better way to ask
    for the same night anyway.

    One reachable case the WARNING's comment used to deny: a DERIVED value can
    equal the cap at ``max_cities == 1``, where ``max(1, 1 // 4)`` is 1. That is
    `run-due --limit 1`, a degenerate one-city run, and the warning firing there
    is correct rather than a false alarm — but "unreachable by arithmetic on a
    derived value" was wrong, so it no longer says that.
    """
    configured = cfg.opt_in_cities_per_day
    if configured is None:
        reservation = max(1, max_cities // _OPT_IN_SLOT_SHARE)
    elif max_cities >= cfg.max_cities_per_day or cfg.max_cities_per_day <= 0:
        reservation = max(0, configured)
    else:
        # Round down, so narrowing a run never rounds the reservation UP into a
        # larger share of it than the standing config asks for.
        reservation = max(0, configured * max_cities // cfg.max_cities_per_day)
    return max(0, min(reservation, max_cities))


def _lane_count(sched: dict, config_path) -> int:
    """Read ``[schedule].max_concurrent_channels``, falling back to 1 (issue #240).

    Warn-and-fall-back rather than raise, following the ``network_type`` field
    below: a nonsense value here is one key of one section, and aborting the load
    over it would take down every subcommand — including backup-status and
    restore-backup, the incident-time handles — the way an unwired-channel
    ValueError once did. Falling back to 1 is also the safe direction: it is the
    sequential behaviour every guard in this file was written against.

    ``isinstance(v, bool)`` is excluded explicitly because TOML booleans are
    Python ints, so ``max_concurrent_channels = true`` would otherwise load as
    one lane and read as if it had been honoured.
    """
    value = sched.get("max_concurrent_channels", 1)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        logger.warning(
            f"[schedule] max_concurrent_channels={value!r} in {config_path} is not a "
            f"positive integer; using 1 (one channel at a time)"
        )
        return 1
    return value


def load_scheduler_config(path: str | None = None) -> SchedulerConfig:
    """Load scheduler config from TOML; missing file yields defaults."""
    config_path = Path(path) if path else DEFAULT_CONFIG_PATH
    if not config_path.exists():
        logger.warning(f"Config {config_path} not found; using defaults")
        return SchedulerConfig()

    with open(config_path, "rb") as f:
        raw = tomllib.load(f)

    sched = raw.get("schedule", {})
    dl = raw.get("download", {})
    paths = raw.get("paths", {})
    pub = raw.get("publish", {})
    al = raw.get("alerts", {})
    rg = raw.get("resource_guard", {})
    dp = raw.get("driving_plan", {})

    providers = None
    unwired_channel_errors: list[str] = []
    if "providers" in raw:
        providers = {}
        for name, p in raw["providers"].items():
            # Street-coverage channels (issue #99) are scheduled like grid
            # providers but are not themselves imagery providers, so they are
            # validated against STREET_CHANNELS rather than KNOWN_PROVIDERS.
            if name not in KNOWN_PROVIDERS and not is_street_channel(name):
                logger.warning(
                    f"Ignoring unknown provider [providers.{name}] "
                    f"(known: {', '.join(KNOWN_PROVIDERS)}, "
                    f"{', '.join(sorted(STREET_CHANNELS))})"
                )
                continue
            # A channel the naming contract knows but the scheduler cannot yet
            # run. RECORDED and dropped, not warned-and-forgotten like the
            # branch above: an unknown name is a typo and dropping it silently
            # is the kind thing to do, whereas this one is spelled correctly,
            # would be accepted by every check after this point, and would then
            # collect wrongly every night. Dropping it from `providers` is what
            # makes it impossible to run by accident; recording the message is
            # what lets run-due/assess-city REFUSE rather than quietly run a
            # night around a channel the config asks for. Not raised, because a
            # load-time ValueError took down every subcommand — including
            # backup-status and restore-backup, the incident-time handles —
            # over a block only the channel-running commands could act on.
            if name in UNWIRED_CHANNELS:
                message = (
                    f"[providers.{name}] in {config_path} is not a runnable scheduler "
                    f"channel yet: {UNWIRED_CHANNELS[name]}. Remove the block."
                )
                logger.error(message)
                unwired_channel_errors.append(message)
                continue
            # An unknown network_type reaches `collect --network-type` as an
            # argparse choices violation, i.e. exit 2 on EVERY street run of
            # EVERY due city, night after night, with nothing in the scheduler's
            # own output naming the config as the cause. Catch it here, where
            # the message can point at the offending key, and fall back to the
            # default rather than aborting the whole nightly run over one field.
            network_type = p.get("network_type", DEFAULT_NETWORK_TYPE)
            if network_type not in STREETWALK_NETWORK_TOKENS:
                logger.warning(
                    f"[providers.{name}] network_type={network_type!r} is not a known "
                    f"OSM network type (known: {', '.join(sorted(STREETWALK_NETWORK_TOKENS))}); "
                    f"using {DEFAULT_NETWORK_TYPE!r}"
                )
                network_type = DEFAULT_NETWORK_TYPE
            # And the same guard for jitter, for the same reason and with the
            # same cost: out of [0, 1) or non-numeric, it reaches
            # `--mapillary-jitter` as an argparse type error, i.e. exit 2 on
            # EVERY Mapillary run of EVERY due city. Exit 2 is not one of the
            # amnestied families (see _run_city_channels), so five such nights
            # spend the city's whole `consecutive_failures` budget and drop it
            # out of `get_due_cities` — where only a success can put it back,
            # and no success is reachable while the config still says this.
            # Falls back to None ("use the collector's own default"), never 0,
            # which would silently restore the metronome (issue #292).
            raw_jitter = p.get("jitter")
            jitter = coerce_jitter(raw_jitter)
            if raw_jitter is not None and jitter is None:
                logger.warning(
                    f"[providers.{name}] jitter={raw_jitter!r} is not a fraction in "
                    f"[0, 1); ignoring it and leaving the collector's own default "
                    f"in force (0 would mean an exact, metronomic cadence)"
                )
            providers[name] = ProviderConfig(
                enabled=p.get("enabled", True),
                daily_request_budget=p.get("daily_request_budget", 250_000),
                max_requests_per_minute=p.get("max_requests_per_minute"),
                jitter=jitter,
                spacing_m=p.get("spacing_m", 15),
                network_type=network_type,
            )

    return SchedulerConfig(
        cycle_days=sched.get("cycle_days", 90),
        grace_days=sched.get("grace_days", 7),
        daily_request_budget=sched.get("daily_request_budget", 10_000_000),
        max_cities_per_day=sched.get("max_cities_per_day", 20),
        opt_in_cities_per_day=sched.get("opt_in_cities_per_day"),
        max_consecutive_failures=sched.get("max_consecutive_failures", 5),
        city_timeout_minutes=sched.get("city_timeout_minutes", 180),
        max_batch_hours=sched.get("max_batch_hours", 10.0),
        max_concurrent_channels=_lane_count(sched, config_path),
        batch_size=dl.get("batch_size", 100),
        connection_limit=dl.get("connection_limit", 50),
        request_timeout_s=dl.get("request_timeout_s", 30.0),
        # Clamped at 0 rather than trusted: a negative here reaches time.sleep()
        # inside the city loop, where ValueError is caught by the loop's broad
        # `except Exception` and ends the whole night at _STOP_REASON_ERROR after
        # one city -- a batch lost to a typo, reported as "City loop aborted by
        # an unexpected error", i.e. pointing at the loop instead of the config.
        sleep_between_cities_s=max(0, dl.get("sleep_between_cities_s", 5)),
        max_requests_per_minute=dl.get("max_requests_per_minute", 24_000),
        data_dir=paths.get("data_dir", str(_PROJECT_ROOT / "data")),
        db_path=paths.get("db_path", ""),
        log_dir=paths.get("log_dir", str(_PROJECT_ROOT / "logs")),
        backup_dir=paths.get("backup_dir", str(_PROJECT_ROOT / "backups")),
        publish_enabled=pub.get("enabled", False),
        publish_script=pub.get("publish_script", str(_PROJECT_ROOT / "sync_data_to_server.sh")),
        publish_local=pub.get("local", False),
        site_url=pub.get("site_url", ""),
        providers=providers,
        unwired_channel_errors=unwired_channel_errors,
        alerts=AlertConfig(
            enabled=al.get("enabled", False),
            recipient=al.get("recipient", ""),
            transport=al.get("transport", "mail"),
            command=al.get("command", ""),
            failure_threshold=al.get("failure_threshold", 1),
            subject_prefix=al.get("subject_prefix", "[streetscape-tracker]"),
            smtp_host=al.get("smtp_host", ""),
            smtp_port=al.get("smtp_port", 25),
            smtp_from=al.get("smtp_from", ""),
            smtp_starttls=al.get("smtp_starttls", False),
            smtp_user=al.get("smtp_user", ""),
            smtp_password=al.get("smtp_password", ""),
        ),
        resource_guard=ResourceGuardConfig(
            enabled=rg.get("enabled", True),
            min_available_memory_gb=rg.get("min_available_memory_gb", 8.0),
            max_load_per_core=rg.get("max_load_per_core", 0.9),
            min_connection_limit=rg.get("min_connection_limit", 5),
        ),
        driving_plan=DrivingPlanConfig(
            enabled=dp.get("enabled", True),
            archive_dir=dp.get("archive_dir", str(_PROJECT_ROOT / "archive" / "gsv_driving_plan")),
            url=dp.get("url", driving_plan.FEED_URL),
            timeout_s=dp.get("timeout_s", 60.0),
        ),
    )


@dataclass
class SystemPressure:
    """A point-in-time read of host pressure (from Linux /proc)."""

    load5: float  # 5-minute load average
    ncpu: int  # logical CPUs
    mem_available_gb: float  # MemAvailable, in GiB


def read_system_pressure() -> "SystemPressure | None":
    """Best-effort read of 5-min load and available memory from ``/proc``.

    Returns None when the data can't be read (non-Linux host, missing /proc,
    or a malformed line), which callers treat as "no info" → no throttling.
    """
    try:
        with open("/proc/loadavg") as f:
            load5 = float(f.read().split()[1])
        mem_available_kb = None
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    mem_available_kb = int(line.split()[1])
                    break
        if mem_available_kb is None:
            return None
        return SystemPressure(
            load5=load5,
            ncpu=os.cpu_count() or 1,
            mem_available_gb=mem_available_kb / 1024 / 1024,
        )
    except (OSError, ValueError, IndexError):
        return None


def plan_connection_limit(
    base_limit: int,
    pressure: "SystemPressure | None",
    cfg: ResourceGuardConfig,
) -> tuple[int, str | None]:
    """Choose an effective ``--connection-limit`` given current host pressure.

    Pure/deterministic (pressure is passed in, not read here) so it is unit
    testable without touching ``/proc``. Returns ``(limit, reason)`` where
    ``reason`` is None when the base limit is left unchanged. The result is
    always clamped to ``[floor, base_limit]`` — the guard only ever *lowers*
    concurrency, never raises it.
    """
    if not cfg.enabled or pressure is None:
        return base_limit, None

    floor = min(cfg.min_connection_limit, base_limit)
    limit = base_limit
    reasons = []

    # Each condition contributes a reason only if it actually LOWERS the limit,
    # so the caller never logs a no-op throttle (e.g. base already at the floor).
    if pressure.mem_available_gb < cfg.min_available_memory_gb and floor < limit:
        limit = floor
        reasons.append(
            f"low memory ({pressure.mem_available_gb:.1f}G available "
            f"< {cfg.min_available_memory_gb:.0f}G)"
        )

    load_ceiling = cfg.max_load_per_core * pressure.ncpu
    if load_ceiling > 0 and pressure.load5 > load_ceiling:
        # Scale down in proportion to how far load exceeds the ceiling.
        scaled = max(floor, int(base_limit * load_ceiling / pressure.load5))
        if scaled < limit:
            limit = scaled
            reasons.append(f"high load ({pressure.load5:.1f} > {load_ceiling:.0f})")

    return limit, ("; ".join(reasons) if reasons else None)


# Street-km of drivable network per km² of a city's grid area, used only when
# nothing better is known. Calibrated on Seattle (3,709 street-km over a 497.9
# km² grid). Grid areas include water and rural fringe, so this OVER-estimates
# most cities — deliberately, so the budget guard errs toward deferring a city
# rather than blowing through the daily ceiling mid-run.
_STREET_KM_PER_KM2 = 7.45


# Multiplier on the drive-calibrated street-density constant for broader OSM
# networks, which add footways, paths, cycleways, steps, tracks and every
# service road (alleys, driveways, parking aisles). Measured on Corvallis at
# 15 m: drive 2,591 edges / 25,555 samples vs all_public 30,643 edges / 83,928
# samples — a 3.3x sample ratio (the edge ratio is far higher, 11.8x, because
# broad-network edges are much shorter). Rounded UP from that, because like
# _STREET_KM_PER_KM2 this must over-estimate so the budget guard defers a city
# rather than overrunning the channel.
#
# Only ever applied to the last-resort area proxy. The frozen-network path above
# already errs high for broad networks on its own (its 4.2 samples-per-edge
# ratio is drive-calibrated, and broad networks run ~2.7), and once a city has
# one walk of that type the exact prior-walk count takes over entirely.
_BROAD_NETWORK_MULTIPLIER = 4.0


def estimate_street_samples(
    conn, city: db.CityRow, spacing_m: int, network_type: str = "drive"
) -> int:
    """
    Estimated on-street sample points for a road walk, WITHOUT touching OSM.

    The collector's own ``--estimate`` is exact but fetches (and on a first walk
    downloads) the street network, which is far too expensive to do for every
    due city while merely planning a night's work. Precedence, most to least
    trustworthy:

    1. This city's last road walk OF THE SAME NETWORK TYPE — exact sample count,
       rescaled if the configured spacing changed.
    2. Its frozen OSM network of that type (#103) × the observed samples-per-
       edge ratio at that spacing.
    3. Grid area × a street-density constant, scaled up for broad networks.

    Every step filters on network_type. A 'drive' walk and an 'all_public' walk
    of one city are different amounts of work — Seattle's drive network is
    59,218 edges, its all_public network far more — so reusing one to plan the
    other would badly misbudget the night.

    Args:
        conn: open catalog connection.
        city: the city row (frozen grid geometry).
        spacing_m: along-edge sample spacing the walk will use.
        network_type: osmnx network type the walk will use.

    Returns:
        Estimated number of sample points (== GSV requests; Mapillary pays
        tiles instead — see estimate_requests).
    """
    spacing = max(1, int(spacing_m))

    prior = conn.execute(
        """SELECT sample_points, spacing_m FROM street_walks
           WHERE city_id = ? AND network_type = ?
             AND sample_points IS NOT NULL AND spacing_m > 0
           ORDER BY run_date DESC LIMIT 1""",
        (city.city_id, network_type),
    ).fetchone()
    if prior:
        # Samples scale inversely with spacing along a fixed network length.
        return max(1, int(prior["sample_points"] * (prior["spacing_m"] / spacing)))

    network = conn.execute(
        """SELECT edge_count FROM street_networks
           WHERE city_id = ? AND network_type = ?
           ORDER BY network_id DESC LIMIT 1""",
        (city.city_id, network_type),
    ).fetchone()
    if network and network["edge_count"]:
        # Seattle: 59,218 graph edges → 247k samples at 15 m ≈ 4.2 samples per
        # edge per 15 m of spacing. The ratio is a property of edge geometry
        # (mean edge length), not of the filter, so it carries across types.
        return max(1, int(network["edge_count"] * 4.2 * (15.0 / spacing)))

    area_km2 = (city.grid_width_m / 1000.0) * (city.grid_height_m / 1000.0)
    street_km = area_km2 * _STREET_KM_PER_KM2
    if network_type != "drive":
        street_km *= _BROAD_NETWORK_MULTIPLIER
    return max(1, int(street_km * 1000.0 / spacing))


def estimate_kartaview_requests(conn, city: db.CityRow) -> int:
    """
    Estimated API requests for one KartaView radius sweep of this city's bbox.

    The sweep tiles the frozen grid's bbox with squares, covers each with its
    circumscribed circle, and pages each circle to exhaustion, so cost tracks
    bbox AREA rather than imagery or sample spacing. Precedence, most to least
    trustworthy — the same shape as :func:`estimate_street_samples`:

    1. This city's last KartaView run's ``runs.api_requests``. For KartaView
       that column holds ``api_requests_total`` (see ``cli.py``), i.e. the
       sweep's cumulative spend across every process that resumed it -- the
       OBSERVED cost the cost study says to quote. It carries the four terms
       the lattice cannot see: the calibrated radius, the extra pages, the
       backpressure retries and the per-city calibration ladder. A paused sweep
       raises ``SweepIncompleteError`` and never reaches ``register_run``, so a
       row here always describes a COMPLETE sweep rather than a partial night.
       It therefore over-prices a city resuming from a checkpoint, which needs
       only its remainder -- the conservative direction: the budget guard defers
       such a city on a tight night instead of overspending, and the timeout is
       merely generous.
    2. The geometric lattice x ``_SWEEP_OVERHEAD_MULTIPLIER``.

    Tier 1 is the LARGER of the two, not the prior on its own, because the
    prior describes the bbox as it was *then*. This is the one channel whose
    cost tracks bbox AREA directly, and the frozen grid is mutable through two
    documented escape hatches (``scripts/resize_city.py --force``,
    ``cap_oversized_grids.py --include-collected``), so a grid re-registered
    LARGER would go on being priced at the old, smaller sweep -- which is the
    hazard ``city_timeout_seconds``' Anchorage comment names, reached from the
    other direction. Every other arm of :func:`estimate_requests` recomputes
    from today's geometry on every call; taking the larger is how this one
    keeps that property while still preferring the measured number whenever the
    bbox is unchanged or has shrunk.

    Tier 2 has a known blind spot, and it is a factor of four rather than a
    rounding error: ``estimate_sweep_requests`` must assume the default
    ``r=1000`` because the working radius is a property of the LOCATION,
    measured once per city by ``calibrate_radius`` and not predicted by
    density. A city that calibrates down to r=500 costs ~4x this estimate --
    Singapore, New York and Manila all do (Singapore: ~1,273 circles estimated
    against 9,974 requests actually spent). Nothing durable records that
    radius: the checkpoint pins it for the duration of one sweep and ``cli.py``
    discards the checkpoint once the run is cataloged. So a FIRST sweep of such
    a city is under-estimated by construction, which is survivable in exactly
    one direction -- #239's checkpoint means the resulting SIGKILL resumes
    tomorrow instead of discarding the night -- and tier 1 corrects it from the
    second run onward.

    Args:
        conn: open catalog connection, or None to force the geometric tier.
        city: the city row (frozen grid geometry).

    Returns:
        Estimated KartaView API requests for one full sweep of the bbox.
    """
    lattice = estimate_sweep_requests(
        city.center_lat, city.center_lon, city.grid_width_m, city.grid_height_m, city.step_m
    )
    geometry = max(1, int(lattice * _SWEEP_OVERHEAD_MULTIPLIER))
    if conn is None:
        return geometry

    prior = _prior_kartaview_spend(conn, city.city_id)
    if prior is None:
        return geometry
    # max(), never the prior alone -- see the precedence note above. The prior
    # wins on every city whose grid has not grown (it is larger than the
    # default-radius lattice on exactly the r=500 metros this tier exists for),
    # and a grown grid falls back to geometry instead of pricing a bbox that no
    # longer exists.
    return max(geometry, max(1, prior))


def _prior_kartaview_spend(conn, city_id: str) -> int | None:
    """This city's last COMPLETE sweep's observed cost, or None if it has none.

    One query behind two readers: :func:`estimate_kartaview_requests`' tier 1,
    and ``_enrolment_cost_note``, which has to say WHICH tier the number it is
    about to print came from — a tier-2 figure is the default-radius geometry
    and can be ~4x low on an r=500 metro. Sharing the query rather than
    repeating it keeps "does a prior exist" from drifting away from "what does
    the estimator do with it", which is the pair a reader has to trust
    together.

    ``api_requests > 0`` rather than NOT NULL, matching the estimator: a
    cataloged run that recorded no spend prices nothing.
    """
    row = conn.execute(
        """SELECT api_requests FROM runs
           WHERE city_id = ? AND provider = 'kartaview' AND api_requests > 0
           ORDER BY run_date DESC LIMIT 1""",
        (city_id,),
    ).fetchone()
    return None if row is None else int(row["api_requests"])


def estimate_requests(
    city: db.CityRow,
    provider: str = "gsv",
    conn=None,
    spacing_m: int = 15,
    network_type: str = "drive",
) -> int:
    """
    Estimated API requests for one collection.

    Grid runs: one metadata request per grid point (GSV), or the z14 tile count
    (Mapillary, bulk metadata). Street channels: one request per on-street
    sample point (gsv_streets), or the same tile count (mapillary_streets — a
    road walk reads the identical census, so its cost does not scale with
    sample spacing at all).

    KartaView: the radius-sweep lattice over the frozen bbox, carrying the
    study's measured overhead (see :func:`estimate_kartaview_requests`).

    ``conn`` is read by ``gsv_streets`` and ``kartaview``; without it each falls
    back to its geometry-only tier (the area proxy, and the default-radius
    lattice respectively).
    """
    if provider in ("mapillary", "mapillary_streets"):
        # Both Mapillary channels read the identical z14 census.
        return estimate_tile_count(
            city.center_lat, city.center_lon, city.grid_width_m, city.grid_height_m, city.step_m
        )
    if provider == "gsv_streets":
        if conn is None:
            area_km2 = (city.grid_width_m / 1000.0) * (city.grid_height_m / 1000.0)
            street_km = area_km2 * _STREET_KM_PER_KM2
            if network_type != "drive":
                street_km *= _BROAD_NETWORK_MULTIPLIER
            return max(1, int(street_km * 1000.0 / max(1, spacing_m)))
        return estimate_street_samples(conn, city, spacing_m, network_type)
    if provider in ("kartaview", "kartaview_streets"):
        # A paginated radius sweep priced by bbox area, NOT the grid formula
        # below: the lattice covers a median catalog city in ~12 circles where
        # the grid formula would read tens of thousands of points (#238).
        #
        # Both KartaView channels read the IDENTICAL sweep, the way both
        # Mapillary channels read the identical tile census — the walk has no
        # per-sample endpoint, it joins the census locally. So the walk's cost
        # tracks bbox area and is independent of `--spacing`, and pricing it off
        # the sample count would have read 18,851 requests for a Krabi walk the
        # sweep covers in 64 circles.
        return estimate_kartaview_requests(conn, city)
    return (city.grid_width_m // city.step_m + 1) * (city.grid_height_m // city.step_m + 1)


# Wall-clock headroom over the paced-request estimate: covers retry passes,
# per-request overhead, and the diff+JSON pipeline tail after the download.
_TIMEOUT_HEADROOM = 1.5
# Fixed slack (seconds) for process startup, geocode reuse, compression, and
# the inter-pass retry sleeps that are not part of the paced request time.
_TIMEOUT_FIXED_SLACK_S = 600
# max_requests_per_minute is a client-side *ceiling*, not the achieved rate: the
# async engine undershoots it (connection limit, ~30 ms metadata latency, the
# resource guard lowering concurrency on a busy host). makelab2 sustained
# ~24.6k/min against a 48k cap across large runs on 2026-07 — roughly half. The
# timeout derivation must budget for the achieved rate, or a big city's download
# alone eats the whole floor and the child is SIGKILLed during the diff/JSON
# tail (leaving a valid run row with no JSON — see cmd_run_due reconciliation).
_ACHIEVED_RATE_FRACTION = 0.5
# The Mapillary equivalent, and much closer to 1 on purpose: the tile limiter is
# a hard client-side ceiling the fetch tracks closely (one token, one request —
# retries included since #198), where the GSV figure above is a project quota
# the async engine never approaches. The shortfall being budgeted for here is
# per-request latency and the occasional retry, not structural undershoot.
_TILE_ACHIEVED_RATE_FRACTION = 0.8
# The KartaView equivalent, and LOWER than the Mapillary one rather than higher.
# The intuition runs the other way from the tile fetch: that sweep is concurrent,
# so per-request latency hides behind other requests in flight and the limiter is
# what binds. The KartaView sweep is deliberately SERIAL (the next question
# depends on the last answer, and fanning out into a server that answers HTTP 400
# under load is #198's shape), so its wall-clock per request is
# max(pacing_interval, latency) with nothing to overlap. At the shipped 16/min
# the interval is only 3.75 s, and a `nearby-photos` page carries up to 2,000
# photo records — a far fatter response than a z14 tile — so latency can be the
# binding term, not the limiter. 0.5 budgets 7.5 s per request at 16/min.
#
# The error is deliberately asymmetric. Under-timing is the failure this whole
# derivation exists to prevent: a SIGKILLed child records NO api_usage, so its
# spend vanishes from the daily ledger, AND it counts a collection failure that
# only a success ever resets (five quarantine a city for a 90-day cycle).
# Over-timing costs nothing comparable — the batch deadline still clamps the
# value (see city_timeout_seconds), and since #239 an eventual kill means the
# sweep resumes from its checkpoint tomorrow rather than restarting at cell zero.
# That last clause is about the WORK and not the schedule: a kill still counts a
# failure, so resumption is bounded at five nights. See _kartaview_timeout_seconds
# — it is the reason this fraction is loose rather than tight.
_SWEEP_ACHIEVED_RATE_FRACTION = 0.5
# Observed KartaView sweep cost over the bare root-cell lattice, median across
# the 14-city study (`summary.observed_over_root_cells.p50` in
# docs/experiments/kartaview-sweep-cost_metrics.json). estimate_sweep_requests
# counts ONE page-1 per root cell and nothing else, so the overhead this carries
# is the extra pages, the backpressure retries and the per-city calibration
# ladder.
#
# NOT the 1.54x that the same study also reports: that is
# `summary.observed_over_floor`, measured against a DIFFERENT denominator — the
# study's floor counts cells *plus pages 2+*, where estimate_sweep_requests
# counts cells alone. Quoting 1.54x here would under-price the pages twice over.
#
# It is a MEDIAN, and the same summary puts the max at 13.66x — so half the
# catalog costs more than this prices, and the tail is not where you would
# guess. The 13.66x city is Horace, ND: a p65 city (55.9 km2, 35 root cells,
# 478 requests observed), i.e. this constant is ~7.6x low on a MID-catalog city,
# and the study is explicit that refusal cascades make SPARSE bboxes the
# expensive ones — inverting the feasibility study's expectation that cost per
# km2 is worst where imagery is richest.
#
# That tail reaches the two consumers differently, and only one of them absorbs
# it. The TIMEOUT does: it under-times only where the true overhead exceeds 5.4x
# the cells this estimator counts AND the honest wall-clock already exceeds the
# 180-minute floor, and no study city does both (Horace is 30 minutes of
# fetching; Singapore's 1.94x is nowhere near 5.4x). The radius blind spot in
# estimate_kartaview_requests is the one case that clears both bars, and it is
# documented there. The daily BUDGET guard does not absorb it: the guard is a
# pre-flight check, and kartaview is the only channel whose estimate is not
# exact (grid points and tile counts both are), so it is also the only one where
# a city can overspend what the ledger said it could afford.
#
# THE STOP FOR THAT IS NOW WIRED (#273), so this multiplier no longer has to be
# right for the spend to be bounded. _run_city_channels hands the child #239's
# `--kartaview-max-requests`, and exhausting it checkpoints the rest and exits
# 83 rather than overspending -- so the overrun this paragraph used to describe
# is a resumable pause. The cap is the SMALLER of `budget - used` and what the
# child's own timeout can pace (_sweep_requests_within_timeout), because the
# ledger term alone is often unreachable: a night's paced wall clock is finite,
# and on prod 16/min over a 10 h batch affords ~9,600 requests against a 10,000
# budget. The number travels as `request_cap`, spelled differently from
# `daily_budget` on purpose: the street channels' ceiling is the FULL budget
# because that collector subtracts today's spend itself (see
# _street_collect_cmd), and the grid CLI reads no ledger, so its number has to
# arrive already subtracted.
#
# The multiplier still matters in the other direction. It is what the two
# budget gates and the timeout are priced from, so a median used as a ceiling
# still UNDER-prices a mid-catalog city -- it just costs that city an extra
# night now instead of an unbounded overrun. #274 is what stops it costing the
# city everything: a sweep priced above the whole daily budget is launched with
# a cap rather than skipped forever.
_SWEEP_OVERHEAD_MULTIPLIER = 1.80

# Smallest remaining daily budget worth launching a resumable sweep with (#274).
#
# DERIVED, not chosen, because the binding reason is a specific failure rather
# than a taste for round numbers. calibrate_radius is asked the runaway guard
# before every probe, and a budget that runs out THERE raises a plain
# DownloadError -- "nothing was swept and nothing is checkpointed" -- not
# SweepIncompleteError. So it takes none of the exit-83 amnesty and counts a
# real consecutive_failure, five of which quarantine the city for a 90-day
# cycle. A cap below the ladder's worst case therefore buys nothing and costs a
# failure, on precisely the metros this whole change exists to collect.
#
# The ladder's bound is its own documented one: a rung costs either
# probes_per_rung answers or one probe's full retry budget (the rung is lost the
# moment a probe fails, hence the break), so 6 * (2 + 3) = 30 at the defaults.
# Read from the constants rather than pinned at 30, so retuning the ladder
# carries this with it. The extra retries + 1 is one root cell's full attempt on
# top: clearing calibration with nothing left to sweep is a legal pause, but it
# spends a night and a checkpoint-age day to record zero progress.
_MIN_SWEEP_LAUNCH_REQUESTS = len(RADIUS_LADDER_M) * (
    DEFAULT_CALIBRATION_PROBES + DEFAULT_BACKPRESSURE_RETRIES
) + (DEFAULT_BACKPRESSURE_RETRIES + 1)

# Floor for a deadline-clamped timeout. A city is only started while the batch
# deadline still has room, so the clamp should shorten a run — never hand a
# child a timeout too short to reach its first request.
_MIN_CLAMPED_TIMEOUT_S = 300
# Stop reason for an exception escaping the city loop. Distinct from the benign
# early exits (day cap, deadline, SIGTERM) because it must still fail the run:
# the batch publishes what it collected, but the night is not healthy.
_STOP_REASON_ERROR = "unexpected error in the city loop"
# Stop reason for an operator-requested wind-down. A named constant rather than a
# literal because there are now TWO assignment sites in _run_city_loop — between
# cities and after a city's channels (issue #206) — and a drifted spelling would
# be invisible. Unlike _STOP_REASON_ERROR this value is never *compared* against;
# it is benign, so the night still publishes and exits 0. Keep it a constant
# anyway: the pair of assignment sites is the reason, not the comparison.
_STOP_REASON_SIGTERM = "received SIGTERM"

# Exit status for a bad `run-due` argument (issue #214). This is `sysexits.h`'s
# EX_USAGE, and unlike HOST_BUSY_EXIT_CODES (79/80, deliberately past the end of
# that header so they carry no false analogy) the analogy here is a true one: it
# really is a command-line usage error.
#
# It has to be its own number rather than the obvious 2, because 2 is already
# taken twice over — argparse exits 2 on any parse error, and main() ends with a
# catch-all `return 2` for an unknown subcommand. A wrapper around the on-demand
# catch-up has to tell "you typed the channel wrong, fix it and retry" apart from
# both of those, and from the 0/1 the run-due path already owns.
USAGE_EXIT_CODE = 64


class _UsageError(ValueError):
    """
    A rejected ``run-due`` argument, carrying the operator-facing message.

    A dedicated type rather than a ``None`` return, because ``None`` already
    means the opposite thing one call away: ``_collect_due(providers=None)`` used
    to mean "every enabled channel". Handing an error sentinel to a function that
    reads the same value as "no filter" fails *open* — a rejected channel name
    would run the full night, GSV included. Raising cannot be mistaken that way.
    """


@contextlib.contextmanager
def _stop_on_sigterm():
    """Turn SIGTERM into a stop *request* instead of an abrupt death.

    systemd stops a unit with SIGTERM (and escalates to SIGKILL only after
    TimeoutStopSec). Under the default handler that lands wherever the loop
    happens to be, so the night's aggregate/manifest/publish tail never runs and
    everything already collected stays unpublished (issue #167). Here it just
    sets a flag, letting the batch wind down and still publish. The flag is
    checked at BOTH levels — between cities in ``_run_city_loop`` and between a
    city's channels in ``_run_city_channels``, which takes it as its required
    ``stop_requested`` argument. Only the outer check existed until issue #206,
    so a stop still launched every remaining channel of the in-flight city.

    Because the unit's default KillMode is control-group, the SIGTERM also
    reaches the running child, so the in-flight ``subprocess.run`` returns
    promptly rather than holding the loop for the rest of its timeout. That
    child is then NOT recorded as the city's failure — see ``_run_city_channels``
    — since it is one we killed on purpose.

    The unit must also set ``TimeoutStopSec`` (30min) for any of this to be
    reachable: systemd's 90-second default expires long before the tail can run.

    Yields an ``Event`` that is set once SIGTERM has been seen. Restores the
    previous handler on exit, and no-ops off the main thread (signal handlers
    can only be installed there) — the deadline check still bounds the batch.
    """
    requested = threading.Event()

    def handler(signum, frame):
        requested.set()

    try:
        previous = signal.signal(signal.SIGTERM, handler)
    except ValueError:
        yield requested
        return
    try:
        yield requested
    finally:
        signal.signal(signal.SIGTERM, previous)


def _mapillary_timeout_seconds(
    city: db.CityRow, provider: str, pc: ProviderConfig | None, floor: int
) -> int:
    """
    Derived timeout for a paced Mapillary tile census (issue #198).

    Both Mapillary channels read the SAME census over the city's frozen bbox —
    the road walk joins it onto sample points locally rather than issuing more
    requests — so spacing and network type do not enter, unlike the GSV street
    estimate. Cost is purely tile count, and wall-clock is that divided by the
    pacing rate.

    Uses ``_TILE_ACHIEVED_RATE_FRACTION`` rather than gsv's ``_ACHIEVED_RATE
    _FRACTION``: here the limiter IS the binding constraint (it is a hard
    ceiling the fetch tracks closely), where gsv's cap is a project quota the
    async engine never approaches. Never returns below the configured floor.
    """
    # `is None`, not falsy: 0 means "pacing disabled", not "use the default".
    configured = pc.max_requests_per_minute if pc else None
    rate = DEFAULT_TILE_REQUESTS_PER_MINUTE if configured is None else configured
    if rate <= 0:  # pacing disabled: nothing to derive from
        return floor
    tiles = estimate_requests(city, provider)  # the same z14 count the budget uses
    paced_seconds = tiles / (rate * _TILE_ACHIEVED_RATE_FRACTION) * 60.0
    return int(max(floor, paced_seconds * _TIMEOUT_HEADROOM + _TIMEOUT_FIXED_SLACK_S))


def _census_reuse_window_s(cfg: SchedulerConfig) -> float:
    """
    The reuse window a SCHEDULER probe prices against: the consumer's window
    less the length of the batch (issue #290).

    The probe runs at slate time and the child loads the entry up to
    ``max_batch_hours`` later, so an entry that would expire in between must
    not be priced as free: the child's loader would refuse it and fetch at full
    cost with the budget gate already passed and no in-child request cap.
    """
    return max(0.0, CENSUS_REUSE_MAX_AGE_S - cfg.max_batch_hours * 3600.0)


def channel_census_cache_marker(
    city: db.CityRow, channel: str, *, max_age_s: float = CENSUS_REUSE_MAX_AGE_S
) -> dict | None:
    """
    Is a reusable census on hand for this (channel, city)? Marker only (#290).

    A channel reads its PROVIDER's entry -- 'mapillary' and 'mapillary_streets'
    are different ledgers reading one observation -- and the provider is derived
    the one way the scheduler already knows (``STREET_CHANNELS``), gated on
    ``CENSUS_PROVIDERS``, rather than by a second table that would have to be
    edited beside the first when a channel is added. Answers None for every
    channel whose cost is not a shared census: gsv and gsv_streets query per
    point, so there is nothing to share.
    """
    provider = STREET_CHANNELS.get(channel, channel)
    if provider not in CENSUS_PROVIDERS:
        return None
    return census_cache_probe(provider, city.city_id, frozen_bbox(city), max_age_s=max_age_s)


def _channel_estimate(
    cfg: SchedulerConfig,
    city: db.CityRow,
    provider: str,
    conn=None,
    *,
    cached: bool | None = None,
) -> int:
    """Price one channel's request count — the ONE derivation, for every caller.

    ``estimate_requests`` needs a channel's ``spacing_m``/``network_type`` out of
    config, and how you reach for them is where two copies drift. They already
    had: the lane launch site indexed ``cfg.providers[provider]`` directly while
    ``_run_one_city``'s fallback tolerated a missing entry via
    ``ProviderConfig()``, so a channel enabled without its own ``[providers.*]``
    block priced fine on one path and raised ``KeyError`` on the other. Only the
    launch site runs in production, which is exactly why no test noticed.

    The tolerant lookup is the one kept, because ``city_timeout_seconds`` already
    makes it — the timeout and the estimate must not disagree about what a
    missing provider block means.

    A CENSUS ALREADY IN THE SHARED CACHE COSTS 0 (issue #290), and this is the
    seam that has to know it rather than ``estimate_requests``. The two gates
    this feeds are ``est > budget`` and ``used + est > budget``, so without it
    the cheapest channel of the night — a road walk whose census the grid run
    bought minutes earlier — is exactly the one a nearly-spent budget defers,
    and the pairing the cache exists to exploit never happens on the nights it
    matters. ``estimate_requests`` stays cache-blind on purpose: it is also the
    input to ``_mapillary_timeout_seconds``/``_kartaview_timeout_seconds``, and
    a 0 there would collapse a child's timeout onto the fixed floor.

    ``cached`` lets a caller that has already probed the marker (the dry run,
    which also names who paid) pass its answer rather than read it again; None
    probes here, against the batch-narrowed window.
    """
    if cached is None:
        cached = (
            channel_census_cache_marker(city, provider, max_age_s=_census_reuse_window_s(cfg))
            is not None
        )
    if cached:
        return 0
    pc = (cfg.providers or {}).get(provider) or ProviderConfig()
    return estimate_requests(
        city, provider, conn=conn, spacing_m=pc.spacing_m, network_type=pc.network_type
    )


def _kartaview_timeout_seconds(
    city: db.CityRow, pc: ProviderConfig | None, floor: int, conn
) -> int:
    """
    Derived timeout for a paced KartaView radius sweep (issue #238).

    The sweep is SERIAL and paced, so wall-clock is simply request count over
    the achieved rate -- but both terms are unlike the other channels'. The
    count comes from bbox area rather than grid points or tiles, and it spans
    four orders of magnitude across the catalog (median ~16 requests, p95 636,
    Singapore ~9,974); and the rate is 16/min, low enough that per-request
    latency competes with the pacing interval, which is why this uses
    ``_SWEEP_ACHIEVED_RATE_FRACTION`` rather than the tile census's figure.

    Without this arm the channel fell through ``city_timeout_seconds``'
    allow-list to the flat ``city_timeout_minutes`` floor, which SIGKILLs a
    metro sweep part-way through -- worse than a plain failure twice over,
    because a killed child records no ``api_usage`` (so its spend vanishes from
    the daily ledger) and it burns one of the five ``consecutive_failures`` that
    only a success resets.

    Note what this does NOT buy: a metro's honest timeout exceeds
    ``max_batch_hours`` outright, so the caller's deadline clamp is what bounds
    it in a real night. Never returns below the configured floor.

    "Being cut short just means resuming tomorrow" is TRUE OF THE WORK AND NOT
    OF THE SCHEDULE, and the difference decides how generous this has to be.
    #239's checkpoint means a killed sweep re-pays for nothing, but a SIGKILLed
    child has no exit code, so ``_run_city_channels`` cannot tell it from a
    child that made no progress and records a ``consecutive_failure`` --  and
    ``get_due_cities`` filters on that with only a success resetting it, so five
    such nights quarantine the city for a 90-day cycle. A *deliberate* pause
    (exit ``SWEEP_INCOMPLETE_EXIT_CODE``) is amnestied there and can repeat
    indefinitely; a KILL cannot. That asymmetry is why this derivation is
    deliberately loose -- ``_SWEEP_ACHIEVED_RATE_FRACTION`` (0.5) and
    ``_TIMEOUT_HEADROOM`` (1.5) compound to ~3x the honest paced wall-clock --
    and why a metro whose clamped timeout is genuinely too short to finish in
    five nights needed the dueness work in #248 rather than a bigger constant
    here. That work has landed: the five nights are ``consecutive`` only because
    ``_collect_due`` hoists a city due solely on an opt-in channel to the head
    of the slate, so #239's checkpointed progress accumulates instead of the
    city falling to the tail of the union and returning months later. The bound
    is still five, and it is still on the SCHEDULE rather than the work.
    """
    # `is None`, not falsy: 0 means "pacing disabled", not "use the default".
    configured = pc.max_requests_per_minute if pc else None
    rate = DEFAULT_SWEEP_REQUESTS_PER_MINUTE if configured is None else configured
    if rate <= 0:  # pacing disabled: nothing to derive from
        return floor
    # Not via _channel_estimate: that helper exists to unify callers who need
    # spacing_m/network_type out of config, and this channel reads neither --
    # same shape as _mapillary_timeout_seconds. The two agree on the NUMBER
    # because both land in estimate_requests' kartaview arm, which ignores those
    # two arguments; they are not sharing a call site.
    requests = estimate_requests(city, "kartaview", conn=conn)
    paced_seconds = requests / (rate * _SWEEP_ACHIEVED_RATE_FRACTION) * 60.0
    return int(max(floor, paced_seconds * _TIMEOUT_HEADROOM + _TIMEOUT_FIXED_SLACK_S))


def _sweep_requests_within_timeout(timeout_s: int, pc: ProviderConfig | None) -> int | None:
    """
    Requests a paced sweep can issue inside ``timeout_s``, or None when the
    channel's pace makes that unknowable.

    The INVERSE of :func:`_kartaview_timeout_seconds`, and it lives beside it
    because the two must be read from the same constants or they drift: that
    function turns a request count into a wall clock, this one turns a wall
    clock back into a request count. Both use the channel's configured rate
    (default ``DEFAULT_SWEEP_REQUESTS_PER_MINUTE``) discounted by
    ``_SWEEP_ACHIEVED_RATE_FRACTION``, and both hold ``_TIMEOUT_FIXED_SLACK_S``
    back for process startup, the checkpoint write and the tail.

    WHY THE CALLER NEEDS IT (#273/#274). ``_run_city_channels`` hands a
    resumable sweep ``budget - used`` as its request cap so an overrun becomes a
    deliberate pause (exit ``SWEEP_INCOMPLETE_EXIT_CODE``, amnestied, no
    ``consecutive_failure``) rather than a SIGKILL at the per-city timeout,
    which counts one. A cap the child cannot physically REACH inside its own
    timeout buys none of that: on prod (16/min, a 10 h batch, a 10,000-request
    budget) a whole night paces ~9,600 requests, so a fresh night's cap is
    unreachable by arithmetic and the arm actually taken stays the kill. Sizing
    the cap to what the clock affords is what makes the pause the arm a healthy
    child takes.

    ``_TIMEOUT_HEADROOM`` is deliberately NOT divided out here, and that is the
    one asymmetry between the two directions. It covers the request COUNT being
    under-estimated (extra pages, backpressure retries, the calibration ladder)
    — and a cap bounds requests actually issued, retries included, so applying
    it again would halve the cap for a hazard the cap already removes. What is
    left over is the margin: a child that achieves the assumed
    ``rate x _SWEEP_ACHIEVED_RATE_FRACTION`` hits the cap with the headroom
    still unspent. A child that runs SLOWER than that is still killed, and that
    is the arm this cannot remove — only a wall-clock stop inside the child can
    (the option this deliberately did not take, because the child has no clock
    budget flag).

    Returns None for a channel with pacing disabled (``rate <= 0``): with no
    pace there is no wall-clock arithmetic to do, and the honest answer is "do
    not bound the cap by the clock" rather than 0, which would skip every city.
    """
    # `is None`, not falsy: 0 means "pacing disabled", exactly as above.
    configured = pc.max_requests_per_minute if pc else None
    rate = DEFAULT_SWEEP_REQUESTS_PER_MINUTE if configured is None else configured
    if rate <= 0:
        return None
    paceable_s = max(0.0, timeout_s - _TIMEOUT_FIXED_SLACK_S)
    return int(paceable_s / 60.0 * rate * _SWEEP_ACHIEVED_RATE_FRACTION)


def city_timeout_seconds(
    cfg: SchedulerConfig,
    city: db.CityRow,
    provider: str,
    conn=None,
    remaining_s: float | None = None,
) -> int:
    """
    Per-city subprocess timeout, derived from the estimated request count and
    the *achieved* download rate rather than a single flat cap.

    A GSV run is paced under ``max_requests_per_minute``, so its wall-clock
    scales with grid size; a flat ``city_timeout_minutes`` SIGKILLs large cities
    mid-run (Austin/Houston/NYC …), and a killed child records no api_usage, so
    its already-spent requests vanish from the budget ledger. The estimate uses
    ``max_requests_per_minute * _ACHIEVED_RATE_FRACTION`` because the pacing cap
    is not actually achieved (see the constant). Every channel is now paced, so
    every channel scales — the Mapillary pair off tile count and their own
    per-IP rate (see _mapillary_timeout_seconds), and KartaView off its bbox's
    swept circle count and its own 16/min pace (see _kartaview_timeout_seconds).
    The derived value never drops below the configured floor, so small cities
    keep the flat timeout whatever their provider.

    ``remaining_s`` clamps the result to what is left of the batch deadline
    (issue #167). Without it, a city started just inside the deadline still runs
    its full derived timeout and pushes the batch past the supervisor's ceiling,
    which is the overrun the deadline exists to prevent.
    """
    floor = cfg.city_timeout_minutes * 60

    def clamp(value: int) -> int:
        if remaining_s is None:
            return value
        return int(max(_MIN_CLAMPED_TIMEOUT_S, min(value, remaining_s)))

    # gsv_streets scales exactly like gsv — a 247k-sample city (Seattle) needs
    # ~20 minutes of querying, and a flat floor would SIGKILL the biggest ones.
    #
    # Mapillary used to keep the flat floor on the grounds that a tile census is
    # "a handful of tiles in seconds". Client-side pacing (issue #198) ended
    # that: a run's wall-clock is now tile_count / rate, not a constant. On
    # today's catalog that mostly resolves to the floor — measured over the
    # 1,214 enabled cities (2026-08-16), the median is 12 tiles and the largest
    # frozen grid is 870 (Moscow), ~15 minutes at 60/min, well inside the
    # 180-minute floor. It was NOT always so: before the #166 grid caps,
    # Anchorage's 105x84 km grid was ~6,480 tiles (~108 min of deliberate
    # sleeping before the decode/assignment/CSV tail even starts) and today it
    # is 575. The derivation stays because a grid can be re-registered larger at
    # any time, and a SIGKILL costs the requests already spent AND counts a
    # failure — the guard must not depend on today's geometry staying capped.
    if provider not in (
        "gsv",
        "gsv_streets",
        "mapillary",
        "mapillary_streets",
        "kartaview",
        "kartaview_streets",
    ):
        return clamp(floor)
    pc = (cfg.providers or {}).get(provider)
    if provider in ("mapillary", "mapillary_streets"):
        return clamp(_mapillary_timeout_seconds(city, provider, pc, floor))
    if provider in ("kartaview", "kartaview_streets"):
        # Same sweep, same derivation. Deliberately NOT discounted for a cache
        # hit: `estimate_requests` stays cache-blind precisely so this timeout
        # does, because a walk that finds no reusable census must still be
        # given time to fetch one, and a 0 here would collapse it onto the
        # fixed floor — the exact failure #238's arm exists to prevent, arrived
        # at from the other direction.
        return clamp(_kartaview_timeout_seconds(city, pc, floor, conn))
    rate = (pc.max_requests_per_minute if pc else None) or cfg.max_requests_per_minute
    if rate <= 0:
        return clamp(floor)
    spacing = pc.spacing_m if pc else 15
    network_type = pc.network_type if pc else "drive"
    effective_rate = rate * _ACHIEVED_RATE_FRACTION
    estimated = estimate_requests(
        city, provider, conn=conn, spacing_m=spacing, network_type=network_type
    )
    paced_seconds = estimated / effective_rate * 60.0
    return clamp(int(max(floor, paced_seconds * _TIMEOUT_HEADROOM + _TIMEOUT_FIXED_SLACK_S)))


# ── The resumable-sweep launch decision, made ONCE for both callers ──────────
#
# The live launch site (_run_city_channels) and `run-due --dry-run` each had
# their own copy of the budget arithmetic, and they disagreed the moment #274
# landed: the dry run printed "OVER BUDGET (deferred)" whenever
# `est > budget_left`, so an operator's pre-flight said a metro was deferred
# while the live path launched it capped and resumable. The preview is the one
# place an operator checks BEFORE a night, so a preview that is wrong about the
# expensive channel is worse than none.
#
# The decision is therefore derived here and both callers read it — the same
# argument `_channel_estimate` was extracted for, one level up: this helper
# decides, the callers do the I/O (launch and account, or print). Adding a
# fourth skip means editing one function, and no caller can be left behind.
#
# The skip tokens double as the dry run's short label, so a skip cannot be
# rendered under a name the live path does not use.
_SWEEP_SKIP_SIBLING = "sibling sweep in flight"
_SWEEP_SKIP_AGE_WALL = "checkpoint at the age wall"
_SWEEP_SKIP_FLOOR = "under the calibration floor"

# How close to CHECKPOINT_MAX_AGE_S a checkpoint may get before the scheduler
# refuses to add another night to it: ONE night. The wall is measured from the
# checkpoint's first commit, so a sweep still short of the lattice when it is
# within a night of the wall is one resume away from having the whole crawl
# discarded (download_kartaview.load_checkpoint) and re-swept from root 0 -- and
# the re-sweep commits a FRESH created_at, so the cycle repeats weekly, forever.
# One night rather than zero because the check runs BEFORE the launch: at zero
# margin the refusal would arrive on the night the child has already thrown the
# spend away.
_CHECKPOINT_AGE_WALL_MARGIN_S = 86400


class SweepLaunchPlan(NamedTuple):
    """What tonight can do with one resumable channel of one city.

    ``skip`` is None for a launch and otherwise one of the ``_SWEEP_SKIP_*``
    tokens; ``message`` is a full clause for the log (the caller prefixes
    ``city [channel]:``) and ``label`` its short form for the dry run's table.
    ``request_cap`` and ``timeout_s`` are the two numbers the child receives,
    and they are decided together deliberately -- see
    :func:`_sweep_requests_within_timeout`.
    """

    timeout_s: int
    request_cap: int
    affordable: int | None
    skip: str | None
    message: str
    label: str


def _sweep_checkpoint_progress(cfg: SchedulerConfig, city: db.CityRow, channel: str) -> dict | None:
    """
    How far the sweep checkpointed for one (city, channel) has got, or None.

    THE one reader, because the variant is not optional decoration: a WALK's
    store is keyed by (channel, network type) and a grid run's by the channel
    alone, so omitting it reads a grid run's checkpoint and reports its progress
    against the walk's. Two call sites spelling that out independently is how
    the pause log and the launch gate would come to disagree about which crawl
    they are talking about.

    None for a channel that cannot pause at all, so a caller asking about a
    channel's sibling (see :func:`_sweep_launch_plan`) never has to test
    resumability first.

    Best-effort and total: every failure is None. One caller reports on a night
    that already succeeded in its own terms and the other is a launch gate, and
    neither may be the thing that breaks over a half-written state file.
    """
    if not is_resumable_channel(channel):
        return None
    try:
        variant = (
            ((cfg.providers or {}).get(channel) or ProviderConfig()).network_type
            if is_street_channel(channel)
            else None
        )
        return sweep_progress(
            checkpoint_path_for(city.city_id, frozen_bbox(city), channel, variant)
        )
    except Exception:
        return None


def _sweep_launch_plan(
    cfg: SchedulerConfig,
    city: db.CityRow,
    channel: str,
    conn=None,
    *,
    est: int,
    remaining: int,
    remaining_s: float | None,
    city_channels: Sequence[str],
) -> SweepLaunchPlan:
    """
    Launch, or skip and why, for one resumable channel of one city tonight.

    ``remaining`` is the channel's budget MINUS today's spend, ``remaining_s``
    what is left of the batch deadline (None for a preview or an operator run),
    and ``city_channels`` every channel this city is scheduled on tonight --
    needed for the sibling arm below, which must not defer behind a sweep
    nothing is going to run.

    THE CAP IS THE SMALLER OF TWO CEILINGS. The budget remainder alone is not
    reachable: a night's paced wall clock is finite, and on prod (16/min, a 10 h
    batch, a 10,000-request budget affording ~9,600) a fresh night's remainder
    is unreachable by arithmetic, so the arm a big city actually took was the
    SIGKILL at the timeout -- no exit code, a consecutive_failure, a city-cap
    slot spent and NO ledger write, because the child's add_api_usage calls both
    need it to return. Sizing the cap to what the clock affords makes the
    deliberate pause the arm a healthy child takes (#273).

    THE THREE SKIPS, in the order they are asked, which is the order of how much
    each one costs to get wrong:

    * ``_SWEEP_SKIP_SIBLING`` -- this is a road walk whose GRID sibling has an
      in-flight checkpoint over the same frozen bbox. ``kartaview_streets``
      ranks immediately after ``kartaview`` for the same city, and a PAUSED grid
      sweep leaves a checkpoint, not a cache entry -- so the walk prices at full
      and would launch a second, independently checkpointed sweep of one
      lattice, against the same per-IP host, for an observation the grid will
      put in the census cache the moment it completes (#290).
      ``reconcile_cache_hit`` then discards the walk's older checkpoint and
      everything it spent.
      Deferring costs a night and buys the pairing back: once the grid finishes,
      the walk prices at 0.
    * ``_SWEEP_SKIP_AGE_WALL`` -- the checkpoint is within a night of
      ``CHECKPOINT_MAX_AGE_S`` and tonight cannot finish it. See
      ``_CHECKPOINT_AGE_WALL_MARGIN_S``: resuming buys one more night of spend
      that the child then throws away. The caller records this as a FAILURE, on
      purpose and unlike every other skip here, because it is the only one that
      is not self-correcting -- a pause records nothing, so without a failure
      the five-night backstop, the nightly alert and the operator all stay
      unaware while ~60k requests a cycle are discarded.
    * ``_SWEEP_SKIP_FLOOR`` -- the cap cannot even clear radius calibration. A
      budget exhausted there raises a plain DownloadError, not
      SweepIncompleteError, so it takes no amnesty and burns a real
      consecutive_failure to accomplish nothing.

    ``est == 0`` means a census already in the shared cache, and it exempts a
    channel from the sibling and floor arms both. Nothing is being crawled: the
    ladder is never walked, so its cost is not a reason to skip (#274 review),
    and there is no second sweep of the lattice to avoid because there is no
    sweep at all.
    """
    timeout_s = city_timeout_seconds(cfg, city, channel, conn=conn, remaining_s=remaining_s)
    # `.get`, matching _channel_estimate and city_timeout_seconds: a channel
    # enabled without its own [providers.*] block must price, time and cap the
    # same way on every path, and this one also runs under the dry run, which
    # must never raise over a config shape the live path tolerates.
    affordable = _sweep_requests_within_timeout(timeout_s, (cfg.providers or {}).get(channel))
    request_cap = remaining if affordable is None else min(remaining, affordable)
    clock_note = (
        f"{remaining:,} left in the budget, "
        f"{'unpaced' if affordable is None else f'{affordable:,}'} "
        f"inside a {timeout_s // 60:,}-minute timeout"
    )

    def plan(skip: str | None, message: str, label: str) -> SweepLaunchPlan:
        return SweepLaunchPlan(timeout_s, request_cap, affordable, skip, message, label)

    if est > 0 and is_street_channel(channel):
        # The pairing is read from STREET_CHANNELS, the table that already maps
        # every walk to the provider whose census it reads -- deliberately NOT a
        # second table beside CHANNEL_RESUMABLE. channel_census_cache_marker
        # makes the same call for the same reason: a second copy would have to
        # be edited beside the first when a channel is added, and the one that
        # was forgotten fails open. Direct indexing, never `.get`, so an
        # unmapped street channel is a KeyError rather than a silent "no
        # sibling" (test_a_resumable_walk_names_its_grid_sibling pins the set).
        sibling = STREET_CHANNELS[channel]
        # Only when the sibling is actually on tonight's slate for this city.
        # A checkpoint left by a channel nothing is going to run tonight will
        # not complete tonight either, so deferring behind it would be a wait
        # with no end -- which is the very shape of failure the age wall arm
        # below exists to stop.
        if sibling in city_channels and _sweep_checkpoint_progress(cfg, city, sibling) is not None:
            return plan(
                _SWEEP_SKIP_SIBLING,
                f"deferring — {sibling} has an in-flight checkpoint over the same frozen "
                f"bbox, and sweeping it twice would spend ~{est:,} requests against the "
                f"same host for a census the cache will hand this walk for free once that "
                f"sweep completes (#290). Not a failure; it stays due.",
                f"deferred ({sibling} sweep in flight)",
            )

    progress = _sweep_checkpoint_progress(cfg, city, channel)
    age_s = None if progress is None else progress["age_s"]
    if age_s is not None and age_s >= CHECKPOINT_MAX_AGE_S - _CHECKPOINT_AGE_WALL_MARGIN_S:
        roots_done, root_count = progress["roots_done"], progress["root_count"]
        # What is LEFT, priced by the share of the lattice still unvisited.
        # Root cells are not uniform, and `est` is a floor rather than a budget
        # (a Yogyakarta sweep ran 3.0x its geometry estimate), so this is a
        # lower bound on the remaining cost -- which is the safe direction for
        # the question being asked: "could tonight plausibly finish it?" A
        # nonsense root_count cannot be projected from at all, so it prices the
        # whole sweep, and a checkpoint that has answered every root is already
        # finished and only needs its finalize.
        projected = (
            est if root_count <= 0 else int(est * max(0, root_count - roots_done) / root_count)
        )
        if projected > request_cap:
            return plan(
                _SWEEP_SKIP_AGE_WALL,
                f"refusing to resume — its checkpoint is {age_s / 86400:.1f} days old "
                f"(discarded past {CHECKPOINT_MAX_AGE_S / 86400:.0f}) at "
                f"{roots_done}/{root_count} root cells, and the ~{projected:,} requests "
                f"left will not fit tonight's {request_cap:,} ({clock_note}). Another night "
                f"would be thrown away with the checkpoint. RECORDED AS A FAILURE so this "
                f"is alerted rather than re-swept from zero every week: raise "
                f"[providers.{channel}].daily_request_budget, shrink the city's grid, or "
                f"delete the checkpoint to start the sweep over deliberately.",
                f"REFUSED (checkpoint {age_s / 86400:.1f} d old, {roots_done}/{root_count})",
            )

    if est > 0 and request_cap < _MIN_SWEEP_LAUNCH_REQUESTS:
        binding = (
            "requests left in today's budget"
            if request_cap == remaining
            else f"requests its {timeout_s // 60:,}-minute timeout affords"
        )
        return plan(
            _SWEEP_SKIP_FLOOR,
            f"{request_cap:,} {binding} is under the {_MIN_SWEEP_LAUNCH_REQUESTS} needed to "
            f"clear radius calibration; skipping (resumes tomorrow).",
            f"deferred ({request_cap:,} req under the calibration floor)",
        )

    if est > request_cap:
        return plan(
            None,
            f"~{est:,} estimated requests exceeds the {request_cap:,} this night affords "
            f"({clock_note}); launching capped — it will checkpoint and resume.",
            f"launch capped at {request_cap:,}; resumes",
        )
    return plan(None, "", "ok")


def setup_logging(cfg: SchedulerConfig, verbose: bool = False) -> None:
    os.makedirs(cfg.log_dir, exist_ok=True)
    handlers = [logging.StreamHandler(sys.stdout)]
    file_handler = logging.handlers.TimedRotatingFileHandler(
        os.path.join(cfg.log_dir, "streetscape_scheduler.log"), when="midnight", backupCount=30
    )
    handlers.append(file_handler)
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        handlers=handlers,
    )


def _emit(message: str) -> None:
    """``print`` that a vanished reader cannot turn into a failure.

    Used where a print sits on a path whose *remaining* work matters more than
    the message — cmd_regenerate, whose print precedes the publish that is the
    whole reason the operator ran it. A dead pipe there aborted the recovery
    before it recovered anything.

    Deliberately not applied to the interactive read-only subcommands (status,
    restore-backup, the dry runs): there the output IS the product, and a
    swallowed BrokenPipeError would hide that the reader is gone.
    """
    try:
        print(message)
    except BrokenPipeError:
        logger.debug("stdout closed; continuing")


# The batch email's window into the scheduler log, and it is sized against what
# now gets PASTED into that log rather than against the log's own narrative. A
# failed collection child contributes _CHILD_LOG_TAIL_LINES + 2 lines, and since
# issue #218 so does a failed publish — which, being the last thing a night does,
# is the block the tail is guaranteed to contain. At the 40-line default a night
# that failed to publish spent 27 of those 40 on the rsync tail and evicted the
# report of which cities failed and which host refused us: the fix eating the
# context it exists to be read beside. Sized for the realistic bad night (a
# failed publish plus two failed channels) plus room for the summary above them.
_BATCH_LOG_TAIL_LINES = 120


def _recent_log_tail(cfg: SchedulerConfig, n: int = 40) -> str:
    """
    Last n lines of the scheduler log, for pasting into an alert email.

    Scrubbed with redact_credentials as the last line of defense: anything
    that slipped an API key/token into a log line must not travel further
    in a cleartext email.
    """
    log_path = os.path.join(cfg.log_dir, "streetscape_scheduler.log")
    try:
        with open(log_path, encoding="utf-8", errors="replace") as f:
            return redact_credentials("".join(f.readlines()[-n:])) or "(log empty)"
    except OSError as e:
        return f"(could not read {log_path}: {e})"


# The publish is the last thing a night does, and it was the only unbounded
# subprocess.run in this file (issue #230). Every other long-running child here
# is already bounded — the collection child by a per-city timeout, the catalog
# backup by catalog_backup.BACKUP_TIMEOUT_S, the Overpass fetch by _deadline() —
# and rsync has exactly the property those bounds exist for: a half-open SSH
# connection, or a stalled NFS mount on the --local path, sits indefinitely
# rather than erroring.
#
# Sized from a measured distribution, not guessed. 16 nights of prod scheduler
# logs (2026-07-21..2026-08-20) put a healthy publish at p50 12.1 s, p95 24.3 s,
# max 25.5 s — and every one of those is an UPPER bound, since the interval they
# come from (`Publishing via` -> the next log line) also contains the alert's
# SMTP send. The rsync's tree walk over the 7,409 published files (7,416 rsync
# candidates) is 0.138-2.303 s of that, depending on NFS dentry-cache state.
# See docs/experiments/publish-duration.md, and re-measure from the
# `Published in N.N s` line rather than trusting these.
#
# 600 s is ~23x that max, and deliberately the same number as
# BACKUP_TIMEOUT_S so the tail's two bounded terms read alike. The ceiling is
# not free choice: during a `systemctl stop` wind-down the WHOLE tail has to fit
# inside the unit's TimeoutStopSec (30 min), and the other two large terms are
# BACKUP_TIMEOUT_S (600 s) plus the measured aggregate+manifest rebuild (435 s),
# so anything above ~765 s is back to being SIGKILLed with no explanation — the
# exact outcome this bound exists to replace, not an improvement on it.
# test_stop_timeout_covers_the_publish_tail_it_waits_for pins that sum.
PUBLISH_TIMEOUT_S = 600.0

# How long to wait for the publish child's process GROUP to actually die after
# SIGKILL before giving up and letting the tail continue. subprocess.run's own
# post-kill wait() is unbounded, which is the one thing about it this file must
# not reproduce: the whole point of PUBLISH_TIMEOUT_S is that the tail stops
# waiting on a wedged rsync, and inheriting an unbounded reap would hand that
# wait straight back. A child blocked in an uninterruptible NFS RPC does not die
# on SIGKILL until the mount answers — as it would not for systemd's own SIGKILL
# — so the grace is short and expiring it is reported, not retried.
_PUBLISH_REAP_GRACE_S = 30.0


def _kill_publish_group(proc: subprocess.Popen) -> None:
    """SIGKILL the publish child's whole process group, then reap it, bounded.

    The group, not the process: see ``_run_publish_child``. Falls back to killing
    the leader alone if the group is already gone (the ordinary race — the child
    exited between the timeout firing and this call).
    """
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except OSError:
        # Already reaped, or a platform that refused the group kill. Either way
        # the leader is the only thing left we can name.
        proc.kill()
    try:
        proc.wait(timeout=_PUBLISH_REAP_GRACE_S)
    except subprocess.TimeoutExpired:
        logger.error(
            f"publish child (pid {proc.pid}) did not die within "
            f"{_PUBLISH_REAP_GRACE_S:.0f} s of SIGKILL — it is stuck in the kernel, "
            f"which on the --local path means an uninterruptible NFS RPC. Leaving it "
            f"and continuing; the tail must not inherit that wait."
        )


def _run_publish_child(cmd: list[str], fh, timeout_s: float) -> subprocess.CompletedProcess:
    """Run the publish script bounded, killing the whole process group on timeout.

    ``Popen`` rather than ``subprocess.run``, and the difference is the entire
    point of issue #230's bound. ``cmd`` is ``["bash", sync_data_to_server.sh]``
    and that script runs rsync as an ordinary child with echoes after it — no
    implicit ``exec`` — so ``run``'s timeout path (``Popen.kill()`` ->
    ``os.kill(self.pid)``) reaches only the SHELL. The rsync that is actually
    wedged on the half-open SSH connection or the stalled NFS mount would be
    reparented and keep going: still holding the transport, still appending to
    the per-day publish log after ``_tail_lines`` has read it, and still live
    when the next publish starts — which is not hypothetical, since ``_publish``
    APPENDS to a per-day log, so a manual ``regenerate-aggregate --publish``
    after a timed-out nightly one would put a second rsync into the same docroot
    beside the wedged one.

    ``start_new_session=True`` makes the child a session and process-group
    leader, so ``os.killpg`` reaches the shell and the rsync together. Two
    consequences of that, both deliberate:

    - The child leaves the terminal's foreground process group, so a Ctrl-C in an
      operator shell no longer reaches it. That would trade one orphan for
      another, so anything unwinding past the wait — ``KeyboardInterrupt``
      included — kills the group on the way out.
    - It does NOT leave the cgroup, which is what ``systemctl stop``'s default
      ``KillMode=control-group`` signals, so #206's wind-down still reaches this
      child exactly as it did before.
    """
    proc = subprocess.Popen(
        cmd,
        cwd=str(_PROJECT_ROOT),
        stdout=fh,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    try:
        returncode = proc.wait(timeout=timeout_s)
    except BaseException:
        # Both the timeout and the Ctrl-C case: nothing else is going to kill
        # this group now, so do it before the exception leaves this frame.
        _kill_publish_group(proc)
        raise
    return subprocess.CompletedProcess(cmd, returncode)


def _publish(cfg: SchedulerConfig, context: str, alert_on_failure: bool = True) -> int:
    """
    Run the publish script (rsync data/ to the web server), returning its exit code.

    ``[publish].local`` becomes an explicit ``--local`` argument rather than
    being left to STREETSCAPE_PUBLISH_LOCAL in the environment: the nightly
    systemd unit exports that variable and an operator shell does not, so any
    hand-run publish on a local-docroot host took the SSH path and emailed a
    false publish-FAILED alert (issue #215). Passing it explicitly makes the
    two paths identical.

    The child's output goes to a per-day log rather than being inherited, for the
    same reason ``_run_collection_subprocess`` redirects its children — and here
    for a second, sharper one. Python ignores SIGPIPE only for *itself*;
    ``subprocess`` restores it to SIG_DFL in children. So on a ``run-due ... |
    tail -40`` whose reader has gone away, an inherited fd 1 means
    sync_data_to_server.sh (``set -euo pipefail``) takes SIGPIPE on its first
    echo, before any rsync, and dies with returncode -13. That is the 2026-08-17
    incident surviving every other guard in this file: the tail would run, reach
    here, and still publish nothing.

    ``alert_on_failure`` is off when the caller reports failures itself. The
    batch tail does, so that one email names the failed publish alongside the
    failed index or backup it usually accompanies (a full data_dir breaks both),
    instead of sending a partial email and returning early. Either way the
    script's own output is copied into THIS log before the alert decision, since
    that is the only route by which the rsync error text reaches the batch email
    — which quotes ``_recent_log_tail`` and nothing else (issue #218).

    The rsync is bounded by ``PUBLISH_TIMEOUT_S`` (issue #230). A timeout is
    reported as an ordinary publish failure — logged, alerted, nonzero — never
    raised, because #167's rule is that the tail reports rather than propagates.
    The kill reaches the whole process GROUP rather than the shell alone, which
    is not a detail — see ``_run_publish_child``, where the rsync we are actually
    trying to stop is a grandchild.

    One case the bound still does not cover, stated rather than implied: a child
    the KERNEL will not kill. Over SSH the child is in interruptible sleep and
    dies at once; on prod's ``--local`` path one blocked in an uninterruptible
    NFS RPC defers SIGKILL until the mount answers, and systemd's own SIGKILL is
    deferred identically, so no userspace bound can end that process. What this
    file can do is refuse to WAIT on it, which is what ``_PUBLISH_REAP_GRACE_S``
    buys, and say so: the reap logs its own expiry, and the failure line reports
    the timeout and the ACTUAL elapsed separately, so a gap between them is
    visible rather than silent.
    """
    cmd = ["bash", cfg.publish_script]
    if cfg.publish_local:
        cmd.append("--local")
    logger.info(f"Publishing via {' '.join(cmd[1:])}")
    os.makedirs(cfg.log_dir, exist_ok=True)
    log_path = Path(cfg.log_dir) / f"publish_{date.today().isoformat()}.log"
    # Time the rsync. It is the publish tail's largest component (7,409 published
    # files / 30.75 GB, measured 2026-08-20 — 7,416 is rsync's candidate count,
    # which is a different number) and was its only UNMEASURED one:
    # everything else in the tail is either bounded in code
    # (catalog_backup.BACKUP_TIMEOUT_S) or already visible in the log's
    # timestamps. The tail is exactly what the unit's TimeoutStopSec has to cover
    # when `systemctl stop` winds a night down, so this line is what any future
    # re-sizing of that number — or of PUBLISH_TIMEOUT_S — has to be argued from
    # (issues #206, #230). Monotonic rather than wall clock: an NTP step must not
    # be able to report a negative publish.
    started = time.monotonic()
    try:
        # Append, not truncate: a night can publish more than once (a manual
        # regenerate-aggregate after the batch), and the earlier attempt is
        # exactly what an operator is trying to read.
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write(f"\n===== {datetime.now(UTC).isoformat()} =====\n")
            fh.write(redact_credentials(" ".join(cmd)) + "\n\n")
            fh.flush()
            result = _run_publish_child(cmd, fh, PUBLISH_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        elapsed = time.monotonic() - started
        # The bound and the actual elapsed are reported as two numbers, not one:
        # subprocess.run's post-kill wait() is unbounded, so `timed out at 600 s
        # after 742.3 s` is the signature of a SIGKILL the kernel deferred (see
        # the docstring), and there is no other place that shows.
        detail = f"(timed out at {PUBLISH_TIMEOUT_S:.0f} s) after {elapsed:.1f} s"
        # The alert carries BOTH numbers too, not just the bound: it is the first
        # thing an operator reads, and the gap between them is the whole signal
        # (see the docstring). Leaving the elapsed to the log tail lower in the
        # same email made the headline the one number that says least.
        reason = f"timed out at {PUBLISH_TIMEOUT_S:.0f} s and was killed after {elapsed:.1f} s"
        # The same 1 the OSError branch below already returns. _finish_batch has
        # only an int to act on, so a bespoke status here would put a second
        # meaning on a number no caller can interpret; the reason travels in the
        # log line, which is what both alert paths quote.
        rc = 1
    except OSError as e:
        # The log itself is unwritable (full or read-only disk) — which is also
        # a good reason for the publish to be about to fail. Report it as the
        # publish failing rather than taking down the tail.
        logger.error(f"Could not open {log_path} for the publish script: {e}")
        return 1
    else:
        elapsed = time.monotonic() - started
        if result.returncode == 0:
            logger.info(f"Published in {elapsed:.1f} s")
            return 0
        # Elapsed on the failure line too: a publish that failed in 2 s (bad
        # path, auth) is a different incident from one that failed at 25 minutes
        # (a stalled NFS transfer), and the message alone could not tell them
        # apart.
        detail = f"(exit {result.returncode}) after {elapsed:.1f} s"
        reason = f"exited {result.returncode} after {elapsed:.1f} s"
        rc = result.returncode

    # Copy the script's own output into the SCHEDULER log, exactly as
    # _run_collection_subprocess does for a failed child — not only into the
    # alert below. The nightly path passes alert_on_failure=False so the batch
    # tail can send one combined email, and that email pastes _recent_log_tail
    # and nothing else. While this tail lived only in the alert, the one path
    # that runs every night reported "publish script FAILED (exit N); see
    # logs/publish_*.log" and left the rsync error in a file on a host nobody
    # was reading (issue #218).
    tail = _tail_lines(log_path, _CHILD_LOG_TAIL_LINES)
    message = f"Publish script failed {detail}; output in {log_path}"
    if tail:
        message += f"\n--- last {_CHILD_LOG_TAIL_LINES} lines of {log_path.name} ---\n{tail}"
    logger.error(message)
    if alert_on_failure:
        # Still pasted explicitly here rather than left to _recent_log_tail's
        # 40-line window: this path's whole job is to be the complete report,
        # and the overlap is the same one a failed collection child already
        # produces in the threshold email.
        send_alert(
            cfg.alerts,
            f"publish script FAILED on {socket.gethostname()}",
            f"{context}\n\nPublish step {reason}.\n\n"
            f"--- last {_CHILD_LOG_TAIL_LINES} lines of {log_path.name} ---\n{tail}\n\n"
            f"Recent log:\n{_recent_log_tail(cfg)}",
        )
    return rc


def cmd_notify_failure(cfg: SchedulerConfig) -> int:
    """
    Email that the scheduled run failed. Intended for a systemd
    ``OnFailure=`` hook, which fires when the unit exits nonzero (a crash, or
    — since run-due returns nonzero on any failed city — a failed collection
    the in-run threshold alert may have already covered). Best-effort.
    """
    host = socket.gethostname()
    body = (
        "streetscape-tracker's scheduled run exited nonzero (systemd OnFailure).\n\n"
        "Recent log:\n" + _recent_log_tail(cfg, 60)
    )
    sent = send_alert(cfg.alerts, f"scheduled run FAILED on {host}", body)
    # 0 when we alerted or alerting is intentionally off; 1 only if a send was
    # attempted and failed, so the notify unit's own status is meaningful.
    return 0 if sent or not cfg.alerts.enabled else 1


# Cap the failure list in `status` so one systemically bad night (every city
# failing the same way) doesn't bury the budget summary underneath it.
_STATUS_MAX_FAILURES = 40


def cmd_status(cfg: SchedulerConfig) -> int:
    """Print a per-(city, provider) schedule table plus today's budgets."""
    conn = db.connect(cfg.db_path)
    today = datetime.now(UTC).date()
    providers = cfg.enabled_providers()

    # `s.member AS channel_member`, aliased rather than bare, because `c.enabled`
    # is selected beside it and this row is read by key: a column name colliding
    # across the join is resolved silently and wrongly by sqlite3.Row (the same
    # hazard that decided the column's name — see db._SCHEMA).
    rows = conn.execute(
        """SELECT c.city_id, c.enabled, s.provider, s.day_of_cycle,
                  s.last_success_at, s.consecutive_failures, s.last_error,
                  s.member AS channel_member,
                  (SELECT MAX(run_date) FROM runs r
                   WHERE r.city_id = c.city_id
                     AND r.provider = COALESCE(s.provider, 'gsv')) AS last_run
           FROM cities c LEFT JOIN schedule_state s ON s.city_id = c.city_id
           ORDER BY s.last_success_at ASC NULLS FIRST, c.city_id,
                    s.provider"""
    ).fetchall()

    due_pairs = set()
    due_counts = {}
    for provider in providers:
        due = db.get_due_cities(
            conn,
            today=today,
            cycle_days=cfg.cycle_days,
            grace_days=cfg.grace_days,
            max_consecutive_failures=cfg.max_consecutive_failures,
            default_membership=CHANNEL_DEFAULT_MEMBERSHIP[provider],
            provider=provider,
        )
        due_counts[provider] = len(due)
        due_pairs.update((c.city_id, provider) for c in due)

    def _enabled_cell(row) -> str:
        """The `enabled` column, which is two gates once a channel is opt-in.

        `c.enabled` alone would print `yes` for a non-member whose DUE stays
        permanently blank — which reads as "the scheduler is broken" rather
        than "this city is not enrolled in this channel" (issue #248).
        """
        if not row["enabled"]:
            return "no"
        provider = row["provider"]
        if provider is None:
            return "yes"
        member = row["channel_member"]
        effective = CHANNEL_DEFAULT_MEMBERSHIP[provider] if member is None else bool(member)
        return "yes" if effective else "not enrolled"

    table = [
        [
            r["city_id"],
            r["provider"] or "—",
            _enabled_cell(r),
            r["day_of_cycle"],
            r["last_run"] or "—",
            (r["last_success_at"] or "—")[:10],
            r["consecutive_failures"] or 0,
            "DUE" if (r["city_id"], r["provider"] or "gsv") in due_pairs else "",
        ]
        for r in rows
        if r["provider"] is None or r["provider"] in providers
    ]
    print(
        tabulate(
            table,
            headers=[
                "city",
                "provider",
                "enabled",
                "cycle day",
                "last run",
                "last success",
                "failures",
                "",
            ],
            tablefmt="simple",
        )
    )

    # Failing (city, channel) pairs with their recorded cause. The main table is
    # ~1200 rows, so a last_error column there would be noise on every healthy
    # row; what an operator actually wants after a bad night is just this list
    # (issue #169).
    failing = [
        [r["city_id"], r["provider"], r["consecutive_failures"], (r["last_error"] or "—")[:90]]
        for r in rows
        if (r["consecutive_failures"] or 0) > 0 and (r["provider"] in providers)
    ]
    if failing:
        failing.sort(key=lambda row: (-row[2], row[0]))
        print(f"\n{len(failing)} failing (city, channel) pairs:")
        print(
            tabulate(
                failing[:_STATUS_MAX_FAILURES],
                headers=["city", "provider", "failures", "last error"],
                tablefmt="simple",
            )
        )
        if len(failing) > _STATUS_MAX_FAILURES:
            print(f"... and {len(failing) - _STATUS_MAX_FAILURES} more.")

    n_cities = conn.execute("SELECT COUNT(*) FROM cities").fetchone()[0]
    due_str = ", ".join(f"{due_counts[p]} {p}" for p in providers)
    print(f"\n{n_cities} cities; due today ({today}): {due_str}.")
    # Per-channel enrolment counts, for the opt-in channels only. Without this
    # an operator who has just landed the config block sees `assign_schedule`
    # create a row per enabled city and concludes from a table of blank DUEs
    # that the flip did not take (issue #248, risk 3).
    _print_membership_footer(conn, providers)
    for provider in providers:
        used = db.get_api_usage(conn, today, provider)
        budget = cfg.providers[provider].daily_request_budget
        print(f"{provider} budget today: {used:,} / {budget:,} requests used.")

    if cfg.driving_plan.enabled:
        latest = db.get_latest_driving_plan_snapshot(conn)
        if latest is None:
            print("driving plan: never fetched.")
        else:
            last_changed = conn.execute(
                """SELECT fetch_date FROM driving_plan_snapshots WHERE changed = 1
                   ORDER BY fetch_date DESC LIMIT 1"""
            ).fetchone()
            state = "changed" if latest["changed"] else "unchanged"
            print(
                f"driving plan: last fetch {latest['fetch_date']} ({state}, "
                f"{latest['record_count']:,} records; last change "
                f"{last_changed['fetch_date'] if last_changed else '—'})."
            )
    return 0


def _print_membership_footer(conn, providers) -> None:
    """Print one enrolment line per configured opt-in channel (issue #248).

    Nothing is printed when every configured channel defaults to member, which
    is every production config today — so `status` and `assign` output is
    byte-identical until an opt-in channel is enabled.
    """
    n_enabled = conn.execute("SELECT COUNT(*) FROM cities WHERE enabled = 1").fetchone()[0]
    for provider in providers:
        if not is_opt_in_channel(provider):
            continue
        n_member = db.count_channel_members(conn, provider, CHANNEL_DEFAULT_MEMBERSHIP[provider])
        print(
            f"{provider}: {n_member:,} of {n_enabled:,} enabled cities opted in "
            f"(opt-in channel; enrol with `enroll-city CITY --channel {provider}`)."
        )


def cmd_assign(cfg: SchedulerConfig) -> int:
    """(Re)compute the day-of-cycle stagger assignment for all cities.

    Assignment is not enrolment: it creates a schedule_state row for every
    enabled (city, channel) pair, including an opt-in channel's, and leaves
    `member` alone. On an opt-in channel that means ~1,144 new rows that
    collect nothing, which is why the summary below names the enrolled count.
    """
    conn = db.connect(cfg.db_path)
    providers = tuple(cfg.enabled_providers())
    n = db.assign_schedule(conn, cfg.cycle_days, providers=providers)
    print(
        f"Assigned day_of_cycle for {n} enabled cities x "
        f"{len(providers)} provider(s) over a {cfg.cycle_days}-day cycle "
        f"(~{n / max(cfg.cycle_days, 1):.1f} cities/day)."
    )
    _print_membership_footer(conn, providers)
    return 0


def _enrolment_cost_note(conn, cfg: SchedulerConfig, city: db.CityRow, channel: str) -> list[str]:
    """One or two lines pricing a sweep of this city, for the enrol preview.

    Turns "I typed a slug" into "I saw the number", which is the point of
    printing anything at all here: risk 1 of issue #248 is that a hoisted
    opt-in city can consume essentially a whole night, and the mitigation is
    keeping the enrolled set to cities whose estimate is well under one — a
    decision the operator can only make if the number is in front of them.

    A second line names the estimator's own error bar when this city has no
    prior sweep to read, and that caveat is the reason the number is worth
    printing rather than a hedge on it. ``estimate_kartaview_requests``' tier 2
    must assume the default ``r=1000``, because the working radius is a
    property of the LOCATION and is measured once per city by
    ``calibrate_radius``; a city that calibrates down to ``r=500`` costs ~4x
    this figure (Singapore, New York and Manila all do). No city has a
    cataloged KartaView run today, so tier 2 is what EVERY enrolment prints —
    and nothing downstream absorbs that miss, because the daily budget guard is
    a pre-flight check against this same estimate and the child is handed no
    request cap at all (issue #273). The operator reading this line is
    currently the last gate.

    Empty for a channel with no sweep estimator; ``kartaview`` is the only
    opt-in channel today.
    """
    if channel != "kartaview":
        return []
    requests = estimate_kartaview_requests(conn, city)
    pc = cfg.providers.get(channel)
    configured = pc.max_requests_per_minute if pc else None
    rate = DEFAULT_SWEEP_REQUESTS_PER_MINUTE if configured is None else configured
    line = (
        f"  bbox {city.grid_width_m / 1000:.1f} x {city.grid_height_m / 1000:.1f} km "
        f"-> ~{requests:,} requests at {_SWEEP_OVERHEAD_MULTIPLIER:.2f}x overhead"
    )
    if rate > 0:
        minutes = requests / rate
        paced = f"{minutes:.0f} min" if minutes < 90 else f"{minutes / 60:.1f} h"
        line += f" (~{paced} paced at {rate}/min)"
    lines = [line]
    # `is None` rather than falsy: a previous sweep is the observed tier
    # whatever it cost, and 0 requests is not a thing a cataloged run records.
    if _prior_kartaview_spend(conn, city.city_id) is None:
        lines.append(
            "  NOTE  no prior sweep for this city, so that is the GEOMETRY estimate at the "
            "default r=1000. A city that calibrates to r=500 costs ~4x it, and nothing "
            "caps the child's spend (#273) — treat it as a floor."
        )
    return lines


def _bulk_candidates(conn, channel: str, target: bool | None) -> list:
    """The enabled cities a bulk enrolment would actually CHANGE, priced.

    Returns ``[(estimate, CityRow), ...]``, cheapest first. Three properties are
    deliberate:

    * **Already-correct cities are excluded, not re-written.** The selection is
      what changes, so `--limit 200` means 200 new members rather than 200 rows
      touched of which some number were already members — which is the
      difference between a tranche and a no-op an operator cannot see.
    * **"Already correct" is the EFFECTIVE membership, not the stored column**,
      and that distinction is the whole of the `--remove` direction. On an
      opt-in channel almost every row is NULL (`assign_schedule` creates them
      unset) and NULL means "not a member" — so comparing the raw column would
      make `None == False` false, select all ~1,214 enabled cities for
      `--all --remove`, and stamp an explicit `0` across a catalog of which two
      cities were ever members. That is not merely a large no-op: it
      permanently destroys the NULL-vs-explicit-0 distinction `cmd_enroll_city`
      keeps on purpose (an explicit 0 survives a future flip of the channel
      default; a NULL flips with it), and `--all --remove` is the natural way
      to undo a tranche. `--all` and `--clear` were correct against the raw
      column only by coincidence — `--remove` is the one case where the stored
      value and the effective one disagree.
    * **The price is the geometry FLOOR** (`estimate_requests`), so the printed
      total is a lower bound and is labelled as one. For KartaView it is the
      swept-circle lattice times a MEDIAN overhead whose study max was 13.66x,
      and it under-prices any city that calibrates to r=500 by ~4x. A tranche
      is sized on it; a budget is not.
    """
    default_member = CHANNEL_DEFAULT_MEMBERSHIP[channel]
    rows = []
    for city in db.get_all_cities(conn, enabled_only=True):
        stored = db.get_channel_membership(conn, city.city_id, channel)
        # `--clear` (target None) is the one direction that really does ask
        # about the stored column: it restores NULL, so a row already NULL is
        # unchanged while an explicit 0 or 1 is not, whatever they mean.
        effective = (
            stored if target is None else (default_member if stored is None else bool(stored))
        )
        if effective == target:
            continue
        rows.append((estimate_requests(city, channel, conn=conn), city))
    # city_id breaks ties, so a tranche is reproducible: the same command twice
    # against an unchanged catalog selects the same cities in the same order.
    rows.sort(key=lambda ec: (ec[0], ec[1].city_id))
    return rows


def _cmd_enroll_bulk(
    conn,
    cfg: SchedulerConfig,
    *,
    channel: str,
    target: bool | None,
    limit: int | None,
    execute: bool,
    n_enabled: int,
) -> int:
    """`enroll-city --all`: enrol (or un-enrol) many cities in one reproducible step.

    Dry-run by DEFAULT, following the `scripts/` convention rather than the
    rest of this command, because the blast radius is the whole catalog and
    `--all` is one keystroke from `--all --remove`. `--execute` writes.
    """
    candidates = _bulk_candidates(conn, channel, target)
    if not candidates:
        print(f"{channel}: nothing to change — every enabled city already matches.")
        return 0

    selected = candidates if limit is None else candidates[:limit]
    total = sum(e for e, _ in selected)
    verb = "enrol" if target else ("un-enrol" if target is False else "clear")

    print(f"{'WOULD ' if not execute else ''}{verb.upper()} {len(selected):,} cities on {channel}")
    for est, city in selected[:10]:
        print(f"  {est:>9,} req  {city.city_id}")
    if len(selected) > 10:
        print(f"  ... and {len(selected) - 10:,} more")
    # Floor, and said so every time it is printed: the whole point of the
    # tranche is that this number is the one being tested against reality.
    print(f"  estimated {total:,} requests for the tranche (a FLOOR, not a budget)")
    if limit is not None and len(candidates) > len(selected):
        print(f"  {len(candidates) - len(selected):,} further cities would still be unchanged")

    if not execute:
        print("  DRY RUN — nothing written. Re-run with --execute to apply.")
        return 0

    # ONE transaction for the tranche, which is what "one reproducible step" in
    # the docstring above has to mean to be worth saying. A loop over the
    # single-city writer commits per city, so an interrupted `--execute` leaves
    # an enrolment nobody can size from the catalog afterwards -- while this
    # command has already printed a count and a nights-to-work-through estimate
    # for a set that was never fully written.
    db.set_channel_membership_bulk(
        conn, [c.city_id for _est, c in selected], channel, target, cycle_days=cfg.cycle_days
    )
    n_member = db.count_channel_members(conn, channel, CHANNEL_DEFAULT_MEMBERSHIP[channel])
    print(f"  {channel}: {n_member:,} of {n_enabled:,} enabled cities opted in.")
    # The reservation, not the enrolled count, is what paces the widening — an
    # operator who reads "800 enrolled" and expects 800 collected tomorrow has
    # the wrong model of the night (issue #282).
    reserved = _opt_in_reservation(cfg, cfg.max_cities_per_day)
    if target and reserved:
        print(
            f"  NOTE  at [schedule].opt_in_cities_per_day={reserved} this set takes "
            f"~{-(-n_member // reserved):,} nights to work through."
        )
    _print_unwired_note(cfg, channel)
    return 0


def cmd_enroll_city(
    cfg: SchedulerConfig,
    city_query: str | None,
    *,
    channel: str,
    remove: bool = False,
    clear: bool = False,
    list_only: bool = False,
    all_cities: bool = False,
    limit: int | None = None,
    execute: bool = False,
) -> int:
    """Opt one city into (or out of) an opt-in channel's nightly queue (issue #248).

    There is no existing handle for this. ``cities.enabled`` is flipped with
    hand-written SQL (deploy/README.md, scripts/register_frame.py), and that
    norm is tolerable for a NOT NULL DEFAULT 1 column on a row that always
    exists. None of it holds for ``schedule_state.member``, where there are
    four ways to type a plausible command and get a silent no-op:

    * ``day_of_cycle`` is NOT NULL with no default, so a bare INSERT fails and
      the operator has to hand-write the ON CONFLICT upsert *and* reproduce
      ``compute_day_of_cycle``'s sha256 stagger;
    * ``UPDATE schedule_state SET member = 1 WHERE ... provider = 'kartaview'``
      matches zero rows and exits 0 whenever ``assign`` has not yet run with
      that channel enabled — which it has not, because the config block does
      not exist yet;
    * a typo'd slug is the same silent zero-row success;
    * NULL/0/1 is three-valued and its meaning lives in a code-side table
      (CHANNEL_DEFAULT_MEMBERSHIP) invisible from a `sqlite3` prompt.

    ``--remove`` writes an explicit 0 and ``--clear`` restores NULL. Under an
    opt-in default the two are indistinguishable TODAY — both mean non-member —
    so the honest reason to keep both is the future one: an explicit 0 persists
    as an exclusion if a channel's default membership ever flips to True (the
    plausible end-state of "widen after"), while NULL flips with it. Collapsing
    them would put that distinction back out of reach of everything but
    hand-SQL.

    This deliberately does NOT refuse on ``cfg.unwired_channel_errors``:
    enrolment has to work BEFORE the config block exists or the rollout order
    is impossible. It notes the situation instead.

    ``--list`` is read-only and therefore scoped differently from the rest:
    it accepts any known channel, including a default-membership one, because
    "who is in this channel's queue" is a true and answerable question there
    ("every enabled city") with no hazard behind refusing it. It refuses to
    run beside ``--remove``/``--clear``, which argparse's mutually exclusive
    group does not cover — those flags would otherwise be accepted, ignored,
    and exit 0, which is the silent no-op this whole command exists to stop.
    """
    try:
        # Channel validation first, before the catalog is even opened, so an
        # operator typo costs nothing.
        if channel not in CHANNEL_DEFAULT_MEMBERSHIP:
            raise _UsageError(
                f"--channel {channel}: unknown channel. Known: "
                f"{', '.join(sorted(CHANNEL_DEFAULT_MEMBERSHIP))}"
            )
        if list_only and (remove or clear):
            # argparse's mutually exclusive group covers --remove/--clear but
            # not --list, and `list_only` short-circuits the whole write path
            # below — so without this, `--list --remove` lists and exits 0
            # having changed nothing. A silent no-op is the exact failure this
            # command exists to prevent; it cannot ship one of its own.
            raise _UsageError("--list cannot be combined with --remove or --clear")
        # The opt-in guard is scoped to the WRITE path. Listing a channel's
        # members is read-only and answers correctly for any channel — under a
        # default-membership channel it is "every enabled city", which is a
        # true and occasionally useful answer, and refusing it would be a
        # refusal with no hazard behind it.
        if not list_only and not is_opt_in_channel(channel):
            # Per-city exclusion for a default-membership channel already has a
            # handle: cities.enabled. Shipping a second, less visible way to
            # disable a city on gsv is how two operators end up disagreeing
            # about why a city stopped collecting.
            raise _UsageError(
                f"--channel {channel}: every enabled city is already a member of this "
                f"channel. Membership is only settable on an opt-in channel "
                f"({', '.join(sorted(c for c in CHANNEL_DEFAULT_MEMBERSHIP if is_opt_in_channel(c)))}); "
                f"to take one city out of {channel}, set cities.enabled = 0."
            )
        if remove and clear:
            raise _UsageError("--remove and --clear are mutually exclusive")
        if all_cities and list_only:
            raise _UsageError("--all cannot be combined with --list")
        if all_cities and city_query:
            # Accepting both would make it ambiguous which one won, and the two
            # readings differ by the whole catalog.
            raise _UsageError("--all takes no CITY argument")
        if limit is not None and not all_cities:
            raise _UsageError("--limit only applies to --all")
        if limit is not None and limit < 1:
            raise _UsageError(f"--limit {limit}: must be >= 1")
        if execute and not all_cities:
            # Single-city enrolment has always written immediately; adding a
            # confirmation step only to --all keeps that true rather than
            # silently changing what an existing command does.
            raise _UsageError("--execute only applies to --all (a single city writes immediately)")
        if not list_only and not all_cities and not city_query:
            raise _UsageError("CITY is required unless --list or --all is given")
    except _UsageError as e:
        logger.error(str(e))
        return USAGE_EXIT_CODE

    conn = db.connect(cfg.db_path)
    n_enabled = conn.execute("SELECT COUNT(*) FROM cities WHERE enabled = 1").fetchone()[0]

    if list_only:
        rows = conn.execute(
            """SELECT c.city_id, c.display_name, s.member, s.last_success_at
               FROM cities c
               LEFT JOIN schedule_state s
                 ON s.city_id = c.city_id AND s.provider = ?
               WHERE c.enabled = 1 AND COALESCE(s.member, ?) = 1
               ORDER BY c.city_id""",
            (channel, 1 if CHANNEL_DEFAULT_MEMBERSHIP[channel] else 0),
        ).fetchall()
        for r in rows:
            print(
                f"{r['city_id']}  ({r['display_name']}; last success {r['last_success_at'] or 'never'})"
            )
        print(f"{channel}: {len(rows):,} of {n_enabled:,} enabled cities opted in.")
        _print_unwired_note(cfg, channel)
        return 0

    if all_cities:
        return _cmd_enroll_bulk(
            conn,
            cfg,
            channel=channel,
            target=None if clear else (False if remove else True),
            limit=limit,
            execute=execute,
            n_enabled=n_enabled,
        )

    city = db.resolve_city(conn, city_query)
    if city is None:
        logger.error(
            f"{city_query!r}: no such city in the catalog. Membership is per "
            f"(city, channel) and a typo'd slug would otherwise be a silent "
            f"zero-row success."
        )
        return USAGE_EXIT_CODE
    if not city.enabled:
        # get_due_cities still requires cities.enabled = 1, so enrolling a
        # disabled city IS the silent no-op this command exists to prevent.
        logger.error(
            f"{city.city_id}: cities.enabled = 0, so it can never be due on any "
            f"channel. Enable the city first; enrolling it now would be a no-op."
        )
        return USAGE_EXIT_CODE

    before = db.get_channel_membership(conn, city.city_id, channel)
    target = None if clear else (False if remove else True)
    db.set_channel_membership(conn, city.city_id, channel, target, cycle_days=cfg.cycle_days)

    def _describe(value: int | None) -> str:
        if value is None:
            default = CHANNEL_DEFAULT_MEMBERSHIP[channel]
            return f"unset ({'member' if default else 'not a member'} by channel default)"
        return "MEMBER" if value else "not a member (explicit)"

    after = db.get_channel_membership(conn, city.city_id, channel)
    row = conn.execute(
        "SELECT day_of_cycle, last_success_at FROM schedule_state WHERE city_id = ? AND provider = ?",
        (city.city_id, channel),
    ).fetchone()
    print(f"{city.city_id} [{channel}]: {_describe(before)} -> {_describe(after)}")
    last_success = row["last_success_at"] if row else None
    print(
        f"  day_of_cycle {row['day_of_cycle'] if row else '—'} of {cfg.cycle_days}; "
        f"last success: {last_success or 'never, so it is due on the next run'}."
    )
    for line in _enrolment_cost_note(conn, cfg, city, channel):
        print(line)
    n_member = db.count_channel_members(conn, channel, CHANNEL_DEFAULT_MEMBERSHIP[channel])
    print(f"  {channel}: {n_member:,} of {n_enabled:,} enabled cities opted in.")
    _print_unwired_note(cfg, channel)
    return 0


def _print_unwired_note(cfg: SchedulerConfig, channel: str) -> None:
    """Say so when the enrolled channel cannot actually run yet.

    Enrolment intentionally precedes the config block (see cmd_enroll_city), so
    "nothing collected overnight" is the EXPECTED outcome at this point in the
    rollout and has to be stated rather than discovered.
    """
    if channel in UNWIRED_CHANNELS:
        print(f"  NOTE  {channel} is not a runnable scheduler channel yet, so nothing collects it.")
    elif channel not in cfg.enabled_providers():
        print(
            f"  NOTE  [providers.{channel}] is not enabled in this config, "
            f"so nothing collects it yet."
        )


def _regenerate_published_json(conn, cfg: SchedulerConfig) -> tuple[str, bool]:
    """
    Rebuild every catalog-derived published artifact.

    Returns ``(operator summary, complete)``. No collection, no API calls, no
    publish.

    Shared by ``regenerate-aggregate`` and ``assess-city`` so the two cannot
    drift into publishing different sets of files — the failure mode being an
    artifact whose companion index still describes the previous state.

    ``complete`` is False when ANY of the three failed. Swallowing a failure and
    returning success would leave a scripted `regenerate-aggregate` unable to
    tell a full rebuild from a partial one, so the outcome is returned rather
    than only logged — and each caller decides what it means for ITS exit code.

    All three go through _tail_artifact, matching what _finish_batch does with
    the same three functions. An earlier version guarded only the driving-plan
    join and let the other two propagate, on the reasoning that a caller must
    not publish an aggregate it failed to rebuild. That is the wrong half of the
    trade, and it is the bug this whole path exists to fix: the aggregate's own
    progress bar is where the 2026-08-17 BrokenPipeError landed, so an
    unguarded rebuild here means a dead stdout takes down `regenerate-aggregate`
    — the command prescribed as the RECOVERY from a stale index — and
    `assess-city` with it, in both cases before their publish. Continuing is
    safe because every artifact is written via _write_json_gz_atomic, so a
    failed rebuild leaves the PREVIOUS good file in place: the publish ships a
    stale-but-valid index, never a truncated one, and the caller still learns
    what happened from ``complete`` and from the summary line.
    """
    logger.info("Regenerating aggregate cities.json.gz")
    agg, agg_err = _tail_artifact("aggregate index", generate_aggregate_v2, conn, cfg.data_dir)
    manifest, man_err = _tail_artifact(
        "streetwalk manifest", generate_streetwalk_manifest, conn, cfg.data_dir
    )
    plan, plan_err = _tail_artifact(
        "driving-plan summary", generate_driving_plan_summary, conn, cfg.data_dir
    )

    def _count(result, key: str, noun: str, filename: str) -> str:
        """One artifact's clause of the summary — its count, or that it is stale."""
        if result is None:
            return f"{filename} NOT regenerated (see log)"
        value = result[key]
        return f"{filename} ({value if isinstance(value, int) else len(value)} {noun})"

    summary = f"Regenerated in {cfg.data_dir}: " + "; ".join(
        (
            _count(agg, "cities_count", "cities", "cities.json.gz"),
            _count(manifest, "walks", "walks", "streetwalks.json.gz"),
            _count(plan, "records", "plan records", "driving_plan.json.gz"),
        )
    )
    return summary, not (agg_err or man_err or plan_err)


def cmd_regenerate(cfg: SchedulerConfig, publish: bool = False) -> int:
    """
    Rebuild the aggregate ``cities.json.gz`` from the catalog without collecting
    anything, then optionally publish. Useful after a code change to the
    aggregate schema, a manual/back-filled run, or to refresh stale published
    data — a clean one-liner instead of an inline Python snippet.

    Exits nonzero if any artifact was left un-regenerated, while still
    publishing the ones that were: the whole job of this command is to rebuild
    the published JSON, so "it rebuilt two of three" must not read as success to
    a wrapper. (``assess-city`` scores the same partial rebuild differently — it
    is not what that command was asked to do.)

    This is also the command _tail_artifact's docstring prescribes as the
    recovery from a stale index, so it has to survive the conditions that make
    an index go stale. Its rebuilds are guarded in _regenerate_published_json,
    and its prints go through _emit, because an operator runs this as
    ``... --publish | tail -20`` or over an ssh session that drops — and the
    print sat BEFORE the publish, so a dead stdout aborted the recovery before
    it recovered anything.
    """
    conn = db.connect(cfg.db_path)
    summary, complete = _regenerate_published_json(conn, cfg)
    _emit(summary)

    if publish:
        # An explicit --publish overrides [publish].enabled: the operator is
        # asking for it directly on the command line. Publish even when a
        # rebuild failed: the others are fresh, the failed one left its previous
        # good file in place (_write_json_gz_atomic), and shipping a stale-but-
        # valid index beats shipping nothing.
        if _publish(cfg, "regenerate-aggregate (manual)") != 0:
            return 1
        _emit("Published to the web server.")
    return 0 if complete else 1


def cmd_fetch_driving_plan(
    cfg: SchedulerConfig,
    *,
    force: bool = False,
    from_file: str | None = None,
    target_date: date | None = None,
) -> int:
    """
    Snapshot the GSV driving-plan feed once, outside the nightly cycle
    (issue #176). Same driving_plan.ingest() code path as run-due's hook.

    ``--from-file`` ingests already-saved raw feed JSON instead of fetching —
    the backfill handle for snapshots saved by hand before this existed (pair
    it with ``--date`` for the date the bytes were actually fetched).
    """
    conn = db.connect(cfg.db_path)
    raw = None
    if from_file is not None:
        opener = gzip.open if from_file.endswith(".gz") else open
        with opener(from_file, "rb") as f:
            raw = f.read()
    try:
        result = driving_plan.ingest(
            conn,
            archive_dir=cfg.driving_plan.archive_dir,
            fetch_date=target_date,
            raw=raw,
            force=force,
            url=cfg.driving_plan.url,
            timeout_s=cfg.driving_plan.timeout_s,
        )
    except Exception as e:
        logger.exception("Driving-plan fetch failed")
        print(f"Driving-plan fetch failed: {e}")
        return 1
    if result.skipped:
        print(
            f"Snapshot for {result.fetch_date} already exists "
            f"({result.record_count:,} records); use --force to re-fetch."
        )
    elif result.changed:
        print(
            f"Snapshot {result.fetch_date}: feed CHANGED — archived "
            f"{driving_plan.generate_snapshot_filename(date.fromisoformat(result.fetch_date))} "
            f"({result.record_count:,} records, {result.entry_count:,} entries)."
        )
    else:
        print(
            f"Snapshot {result.fetch_date}: feed unchanged since the previous "
            f"snapshot ({result.record_count:,} records); no artifact written."
        )
    return 0


def _fmt_bytes(n: float) -> str:
    """Human-readable size for the operator-facing report."""
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:,.1f} {unit}"
        n /= 1024.0
    raise AssertionError("unreachable: the GB branch always returns")


def cmd_backup_status(cfg: SchedulerConfig, *, alert: bool = False) -> int:
    """
    Report catalog-backup health and inventory the un-republishable assets.

    The operator handle for issue #145. Two halves, for two different questions:

    1. *Are the catalog backups working?* Read from ``backup_status.json``, which
       is written on failure as well as success — a backup that fails silently
       every night is exactly how ``/projects/makeabilitylab`` sat unbacked-up
       for months.
    2. *What exists in only one place?* ``archive/gsv_driving_plan`` and
       ``data/osm_cache`` are published nowhere (the driving-plan archive
       deliberately so — #176 keeps it outside ``data/`` so the rsync can't
       republish Google's feed), so lab-storage backups are their only copy.
       Printing counts and bytes here is what lets us hand CSE IT concrete paths
       and sizes to confirm against their configuration.

    Exit status is nonzero when the newest backup is missing, **older than
    ``STALE_AFTER_HOURS``**, or the last recorded attempt failed, so it doubles
    as a check a cron/monitor can run. The age gate is not redundant with the
    outcome: "the last thing we tried worked" stays true forever once the
    scheduler stops running at all — a masked timer, a disabled unit, a
    ``ConditionHost`` that no longer matches after a host cutover — and since
    the newest copy is deliberately never pruned, that state otherwise presents
    as one ancient file plus an ``ok`` status. Which is #145 again.

    ``alert=True`` (issue #193) additionally emails the report through the
    ``[alerts]`` transport when the verdict is unhealthy, which is what makes
    this a *monitor* rather than a command someone has to remember to type.
    Deliberately not wired to the ``OnFailure=`` notify unit: that unit is not
    installed on makelab2 and mails the scheduler log tail, whereas the useful
    body here is the report itself. The exit status is unchanged either way, so
    a systemd unit still goes red and an external monitor still sees nonzero.
    """
    st = catalog_backup.backup_status(cfg.backup_dir)
    out: list[str] = []

    out.append(f"Catalog backups: {st.backup_dir}")
    if not st.exists:
        out.append("  MISSING — the backup directory does not exist yet.")
    elif st.file_count == 0:
        out.append("  EMPTY — no dated backups present.")
    else:
        out.append(
            f"  {st.file_count} dated copies, {_fmt_bytes(st.total_bytes)} total, "
            f"retention {catalog_backup.KEEP_DAYS} days"
        )
        out.append(
            f"  newest: {os.path.basename(st.newest_path)} "
            f"({st.age_hours:,.1f} h old, {_fmt_bytes(os.path.getsize(st.newest_path))})"
        )
        if st.stale:
            out.append(
                f"  STALE — the newest backup is {st.age_hours:,.1f} h old "
                f"(limit {st.max_age_hours:,.0f} h). Is run-due still running on this host?"
            )

    last = st.last_attempt
    if last is None:
        out.append("  last attempt: UNKNOWN (no backup_status.json)")
    else:
        verdict = "ok" if last.get("ok") else f"FAILED — {last.get('error')}"
        out.append(f"  last attempt: {last.get('last_attempt_at')} — {verdict}")
        out.append(f"  source: {last.get('source_db')} on {last.get('source_host')}")
        counts = last.get("row_counts") or {}
        if counts:
            out.append("  rows: " + ", ".join(f"{t}={n:,}" for t, n in sorted(counts.items())))

    out.append("")
    out.append("Assets that exist ONLY on lab storage (published nowhere):")
    inventory = catalog_backup.inventory_single_copy(
        {
            "driving-plan archive": cfg.driving_plan.archive_dir,
            "frozen OSM networks": os.path.join(cfg.data_dir, "osm_cache"),
        }
    )
    for asset in inventory:
        if not asset.exists:
            out.append(f"  {asset.label:22s} {asset.path} — absent")
            continue
        out.append(
            f"  {asset.label:22s} {asset.file_count:,} files, "
            f"{_fmt_bytes(asset.total_bytes)}, newest {asset.newest_mtime}"
        )
        out.append(f"  {'':22s} {asset.path}")

    report = "\n".join(out)
    print(report)

    healthy = st.exists and st.file_count > 0 and not st.stale and bool(last and last.get("ok"))
    if alert and not healthy:
        # Name the reason in the subject: the whole point of the out-of-band
        # check is the case where nobody is reading anything but the subject
        # line, and "stale" vs "the last attempt failed" call for different
        # responses (is the scheduler running at all? vs. why did the copy fail?).
        if not st.exists or st.file_count == 0:
            why = "NO BACKUPS"
        elif st.stale:
            why = f"STALE ({st.age_hours:,.0f} h old)"
        else:
            why = "last attempt FAILED"
        send_alert(
            cfg.alerts,
            f"catalog backup unhealthy on {socket.gethostname()} — {why}",
            "The out-of-band catalog-backup check (issue #193) found an unhealthy "
            "state.\n\n" + report,
        )
    return 0 if healthy else 1


def cmd_restore_backup(cfg: SchedulerConfig, backup_path: str, dest: str | None) -> int:
    """
    Restore a dated backup onto ``dest`` (default: the configured catalog).

    A subcommand rather than a documented ``python -c``, for the same reason the
    restore is drilled in the tests: the moment you need it is the worst moment
    to be composing one. It inherits ``restore_backup``'s refusals — an existing
    catalog, or orphaned ``-wal``/``-shm`` sidecars beside the destination — and
    reports them as an operator error rather than a traceback.
    """
    target = dest or cfg.db_path or os.path.join(cfg.data_dir, "streetscape_tracker.db")
    try:
        catalog_backup.restore_backup(backup_path, target)
    except (FileNotFoundError, FileExistsError, RuntimeError, sqlite3.Error) as e:
        print(f"Restore refused: {e}")
        return 1
    print(f"Restored {backup_path} -> {target}")
    print("Check it before pointing the scheduler at it:")
    print(f"  sqlite3 {target} 'PRAGMA integrity_check; PRAGMA user_version;'")
    return 0


def cmd_reconcile_walks(
    cfg: SchedulerConfig, target_date: date | None = None, dry_run: bool = False
) -> int:
    """
    Catalog road walks that finished but were never registered.

    ``run-due`` now reconciles these inline, so this is the operator handle for
    orphans it couldn't catch: a walk collected by the manual CLI, one left by a
    nightly run that predates the inline path, or one whose scheduler process
    itself died. Checks each enabled city's expected artifact names for the date
    and salvages any whose coverage file is on disk with no catalog row —
    stat-per-candidate rather than a glob, since data/ holds thousands of files.
    """
    conn = db.connect(cfg.db_path)
    today = target_date or datetime.now(UTC).date()
    channels = [p for p in cfg.enabled_providers() if is_street_channel(p)]
    if not channels:
        print("No street channels enabled; nothing to reconcile.")
        return 0

    cities = db.get_all_cities(conn, enabled_only=True)
    reconciled = 0
    for channel in channels:
        provider = STREET_CHANNELS[channel]
        pc = (cfg.providers or {}).get(channel) or ProviderConfig()
        for city in cities:
            existing = conn.execute(
                "SELECT 1 FROM street_walks WHERE city_id = ? AND provider = ? "
                "AND network_type = ? AND run_date = ?",
                (city.city_id, provider, pc.network_type, today.isoformat()),
            ).fetchone()
            if existing:
                continue
            if dry_run:
                stem = generate_streetwalk_filename(
                    city.city_id,
                    city.grid_width_m,
                    city.grid_height_m,
                    city.step_m,
                    pc.spacing_m,
                    today,
                    provider=provider,
                    network_type=pc.network_type,
                )
                coverage_name = streetwalk_coverage_filename(stem + ".csv.gz")
                if (Path(cfg.data_dir) / coverage_name).exists():
                    print(f"  would reconcile {city.city_id} [{channel}] from {coverage_name}")
                    reconciled += 1
                continue
            # Manifest is rebuilt once at the end, not per salvaged walk.
            if _reconcile_orphaned_walk(conn, cfg, city, channel, today, regenerate_manifest=False):
                # Clear the failure that the orphaned walk recorded. Without
                # this the salvage is cosmetic: the row exists but the channel
                # still has no last_success_at, so the city stays due and gets
                # re-crawled anyway — which is the entire cost this avoids.
                # (run-due's inline path already does this via record_attempt.)
                db.record_attempt(conn, city.city_id, success=True, provider=channel)
                reconciled += 1

    if dry_run:
        print(f"DRY RUN — {reconciled} orphaned walk(s) for {today}.")
        return 0

    if reconciled:
        manifest = generate_streetwalk_manifest(conn, cfg.data_dir)
        print(
            f"Reconciled {reconciled} orphaned walk(s) for {today}; "
            f"streetwalks.json.gz now lists {len(manifest['walks'])} walks. "
            f"Run regenerate-aggregate --publish to publish."
        )
    else:
        print(f"No orphaned walks found for {today}.")
    return 0


# ---------------------------------------------------------------------------
# assess-city: same-day answer for a new city (issue #215)
# ---------------------------------------------------------------------------

# The channels assess-city runs, in the order enabled_providers() ranks them.
#
# The two road walks are the point: a deployment decision turns on street
# coverage, not grid coverage, and the two disagree badly. Highland Heights
# reads 55.6% of grid points but 92.8% of street-km; Covington 8.2% vs 50.8% on
# Mapillary. Grid points land on river, rail, parkland and rooftops, so a grid
# percentage understates what a Sidewalk deployment would actually get.
#
# The Mapillary GRID run is here for a different reason: it is the same z14 tile
# census the Mapillary walk already pays for, and it is what makes the answer
# linkable the same day. generate_aggregate_v2 skips a city with no `runs` row,
# so without it the new city is absent from cities.json.gz — streets.html would
# show the walk under a raw slug with nothing to click, because city.html is
# addressed by run-CSV filename.
#
# The GSV GRID run is deliberately absent: it is the expensive half (one request
# per grid point) and it needs no help arriving. A newly registered city is
# enabled with last_success_at NULL, which puts it at the head of the next
# night's stalest-first queue.
ASSESS_CHANNELS = ("gsv_streets", "mapillary", "mapillary_streets")

# Below this share of the search rectangle lying inside the city/county
# boundary, warn before spending. Calibrated on the Northern Kentucky round
# (2026-07-30), where four county rectangles scored 0.49-0.69 and the remainder
# was largely Cincinnati — whose dense recent GSV would have flattered every
# number quoted to the partner.
_LOW_IN_BOUNDARY_FRAC = 0.70


def _host_names(hosts) -> str:
    """Human-readable, stably ordered host list for a log line or alert subject."""
    return ", ".join(sorted(HOST_LABELS.get(h, h) for h in hosts))


def _boundary_preflight(city: db.CityRow) -> tuple[float | None, float | None]:
    """
    (in_boundary_frac, boundary_coverage_frac) for a city's frozen rectangle.

    Advisory ONLY, and that shapes the implementation: this is one unlocked,
    per-IP Nominatim call (geoutils rate-limits it to ~1/s) whose entire job is
    to print two numbers, so every failure — timeout, service unavailable, a
    Point instead of a polygon, a schema surprise — degrades to (None, None)
    rather than costing a collection. Same posture as the Overpass /status
    probe: a probe must never be able to fail the work it speaks for.
    """
    try:
        from .boundary_audit import frozen_rect_bounds, rect_in_boundary_frac
        from .boundary_audit import rect_polygon_coverage as _coverage
        from .geoutils import geocode_boundary_raw

        raw = geocode_boundary_raw(city.city_name, city.state_name, city.country_name)
        geometry = (raw or {}).get("geojson")
        rect = frozen_rect_bounds(
            city.center_lat, city.center_lon, city.grid_width_m, city.grid_height_m
        )
        return rect_in_boundary_frac(geometry, rect), _coverage(geometry, rect)
    except Exception as e:
        logger.warning(f"Boundary pre-flight for {city.city_id} unavailable: {e}")
        return None, None


def _assess_preflight_report(
    cfg: SchedulerConfig, conn, city: db.CityRow, today: date, channels: list[str]
) -> str:
    """
    The operator-facing report printed before anything is spent: what geometry
    we froze, whether it is actually about this place, and what each channel
    will cost against today's remaining budget.
    """
    area_km2 = (city.grid_width_m / 1000.0) * (city.grid_height_m / 1000.0)
    lines = [
        f"{city.display_name} — {city.city_id}",
        f"  grid    {city.grid_width_m:,} x {city.grid_height_m:,} m "
        f"({area_km2:,.0f} km²), step {city.step_m} m, "
        f"center {city.center_lat:.5f},{city.center_lon:.5f}",
    ]
    if city.grid_width_m >= MAX_GRID_DIM_M or city.grid_height_m >= MAX_GRID_DIM_M:
        lines.append(f"          (at the {MAX_GRID_DIM_M // 1000} km per-side cap)")

    in_frac, coverage = _boundary_preflight(city)
    if in_frac is None:
        lines.append("  boundary  no polygon from Nominatim — in-boundary fraction unknown")
    else:
        cov = "unknown" if coverage is None else f"{coverage:.0%}"
        lines.append(
            f"  boundary  {in_frac:.0%} of the sampled rectangle is inside the "
            f"boundary; the rectangle covers {cov} of the boundary"
        )
        if in_frac < _LOW_IN_BOUNDARY_FRAC:
            # One element, not one per output line: the sentences here get edited
            # (and asserted on by substring), and hand-balanced fragments make a
            # reword mean re-breaking every line.
            lines.append(
                f"  ⚠ Only {in_frac:.0%} of what we are about to sample is actually "
                f"{city.display_name}.\n"
                "    Imagery outside the boundary still lands in the numbers. In the "
                "Northern Kentucky\n"
                "    round (2026-07-30) the out-of-county remainder was largely "
                "Cincinnati, whose dense\n"
                "    recent GSV would have flattered every figure quoted to the "
                "partner. Consider a\n"
                "    compact city grid instead: --width/--height TOGETHER WITH "
                "--lat/--lng (size alone\n"
                "    centers on the OSM bbox midpoint, not downtown)."
            )

    lines.append("  cost")
    mapillary_tiles = 0
    # (rate, jitter) pairs, not rates alone: since #292 the rate is a MEAN under
    # jittered gaps, and printing it bare reads exactly like the metronome it
    # replaced — which is the difference this pre-flight exists to make visible.
    tile_paces: set[tuple[int, float]] = set()
    for channel in channels:
        pc = cfg.providers[channel]
        est = estimate_requests(
            city, channel, conn=conn, spacing_m=pc.spacing_m, network_type=pc.network_type
        )
        used = db.get_api_usage(conn, today, channel)
        # The same two guards _run_city_channels applies, named apart because the
        # operator's next move differs. Over the WHOLE budget can never succeed —
        # tomorrow's fresh budget is the same size — so it needs a config change
        # or a smaller grid, and calling it "deferred" (run-due --dry-run's
        # wording for the other case) would promise a later run that never comes.
        if est > pc.daily_request_budget:
            fits = "  ← EXCEEDS THE ENTIRE DAILY BUDGET; raise it or shrink the grid"
        elif est > pc.daily_request_budget - used:
            fits = "  ← OVER REMAINING BUDGET, deferred to a later run"
        else:
            fits = ""
        lines.append(
            f"    {channel:<18} ~{est:>9,} requests   "
            f"({used:,} of {pc.daily_request_budget:,} spent today){fits}"
        )
        # Derived from CHANNEL_HOSTS rather than a hardcoded channel list, and
        # the pacing figure is taken from the channels actually in play: a config
        # can enable mapillary_streets with no [providers.mapillary] section at
        # all, and reaching for that sibling would KeyError in the pre-flight.
        if HOST_MAPILLARY_TILES in CHANNEL_HOSTS.get(channel, ()):
            mapillary_tiles += est
            tile_paces.add(
                (
                    pc.max_requests_per_minute or DEFAULT_TILE_REQUESTS_PER_MINUTE,
                    # None means the child keeps its own default, which is jittered.
                    DEFAULT_TILE_JITTER if pc.jitter is None else pc.jitter,
                )
            )
    if mapillary_tiles:
        # Every rate in play, not just the last channel's: the two Mapillary
        # channels hold independent [providers.*] blocks and run back-to-back, so
        # one figure beside a summed total would misreport a config that paces
        # them differently.
        rates = " and ".join(
            f"{r}/min (mean, gaps at CV {j:.2f})" if j > 0 else f"{r}/min (exact cadence)"
            for r, j in sorted(tile_paces)
        )
        lines.append(
            f"    Mapillary total {mapillary_tiles:,} tile requests from THIS HOST's IP "
            f"(the block is per-IP, not per-token — issues #198/#205), paced at {rates}."
        )
    return "\n".join(lines)


def _assess_answer_report(cfg: SchedulerConfig, conn, city: db.CityRow) -> str:
    """
    The partner-facing numbers, read back from the catalog rather than parsed
    out of a child's stdout (the children print for humans; the catalog is the
    contract).

    Leads with street coverage and labels grid coverage as the area measure it
    is, because quoting grid coverage to a partner is the specific mistake this
    command exists to stop.
    """
    lines = [f"ANSWER — {city.display_name} ({city.city_id})", "  Street coverage (drive network)"]
    walked = False
    for provider in ("gsv", "mapillary"):
        walk = db.get_latest_street_walk(conn, city.city_id, provider, DEFAULT_NETWORK_TYPE)
        if walk is None:
            lines.append(f"    {provider:<10} not walked")
            continue
        walked = True
        length = walk["length_km"]
        pct = walk["coverage_pct_by_length"]
        covered = walk["length_km_covered"]
        detail = (
            f"    {provider:<10} {pct:.1f}% of street-km"
            if pct is not None
            else f"    {provider:<10} coverage not recorded"
        )
        if length is not None and covered is not None:
            detail += f"  ({covered:,.1f} of {length:,.1f} km)"
        # Any-imagery is Mapillary-only information: GSV emits no FLAT_ONLY, so
        # its any-value equals its 360° value by construction, and printing it
        # as a second figure would invent a distinction. NULL means "not
        # measured" (a pre-v8 walk), never a copy — so only a real, differing
        # value is worth a partner's attention.
        any_pct = walk["coverage_pct_by_length_any"]
        if provider == "mapillary" and any_pct is not None and pct is not None and any_pct > pct:
            detail += f"; {any_pct:.1f}% including flat imagery"
        age = walk["median_covered_age_years"]
        if age is not None:
            detail += f"; median covered imagery {age:.1f} y old"
        lines.append(f"{detail}  [{walk['run_date']}]")
    if not walked:
        lines.append("    (no road walk completed — the numbers above are all this run produced)")

    mapillary_run = db.get_latest_run(conn, city.city_id, "mapillary")
    if mapillary_run is not None:
        rate = mapillary_run.coverage_rate_pct
        lines.append(
            f"  Grid coverage (mapillary) {rate:.1f}% of grid points — an AREA measure "
            f"over water, parks and rooftops too. NOT the deployment number."
            if rate is not None
            else "  Grid coverage (mapillary) not recorded."
        )

    # city.html is addressed by run-CSV filename and city.js derives the provider
    # from that filename's token, so the link needs SOME grid run — which is why
    # the Mapillary one is in ASSESS_CHANNELS. Fall back to the GSV run rather
    # than only asking about Mapillary: an already-tracked city has one, and the
    # Mapillary channel is routinely absent here (switched off after a per-IP
    # block, narrowed away by --provider, over budget, or skipped by the host
    # breaker). Claiming "no city page" while a perfectly good one exists sent
    # the operator away without the link they ran this command for.
    link_run = mapillary_run or db.get_latest_run(conn, city.city_id, "gsv")
    if link_run is not None:
        # site_url defaults to "", which leaves a relative link — usable as-is on
        # a host with no configured public base.
        link = f"city.html?file={link_run.csv_filename}&network={DEFAULT_NETWORK_TYPE}"
        lines.append(f"  City page ({link_run.provider} run)  {cfg.site_url}{link}")
    else:
        lines.append(
            "  No grid run on any provider yet, so there is no city page to link "
            "(generate_aggregate_v2 skips a city with no runs row). The GSV grid run "
            "lands on the next nightly batch — that channel is due and leads the queue."
        )
    return "\n".join(lines)


def _select_assess_channels(cfg: SchedulerConfig, requested: list[str] | None) -> list[str]:
    """
    Resolve ``assess-city --provider`` into a channel list.

    Reuses ``_select_providers`` for the unknown/disabled/empty checks so both
    commands refuse the same things the same way, then restricts the result to
    ASSESS_CHANNELS. A grid GSV request is refused with a pointer rather than
    honoured: it is the expensive half and ``run-due`` is where a grid run
    belongs, with the batch deadline and city cap around it.

    Raises ``_UsageError`` — never returns an empty list. A config with no
    assess channel enabled is an error for the same reason #214's disabled
    channel is: the run would publish, exit 0, and have collected nothing.
    """
    if requested is None:
        channels = [p for p in cfg.enabled_providers() if p in ASSESS_CHANNELS]
        if not channels:
            raise _UsageError(
                f"none of {', '.join(ASSESS_CHANNELS)} is enabled in this config "
                f"(enabled: {', '.join(cfg.enabled_providers()) or '(none)'}), so "
                f"assess-city has nothing to collect."
            )
        return channels
    selected = _select_providers(cfg, requested)
    rejected = [p for p in selected if p not in ASSESS_CHANNELS]
    if rejected:
        raise _UsageError(
            f"--provider {', '.join(rejected)}: assess-city collects only "
            f"{', '.join(ASSESS_CHANNELS)}. The GSV grid run is the expensive half "
            f"and belongs to the nightly cycle — use `run-due --provider gsv` if you "
            f"really want it now."
        )
    return selected


def cmd_assess_city(
    cfg: SchedulerConfig,
    query: str,
    *,
    lat: float | None = None,
    lng: float | None = None,
    width: float | None = None,
    height: float | None = None,
    step: float = 20,
    requested_providers: list[str] | None = None,
    estimate_only: bool = False,
    assume_yes: bool = False,
    publish: bool = True,
    today: date | None = None,
) -> int:
    """
    Register a city if new, walk its streets on both providers, publish, and
    print the numbers a deployment inquiry actually turns on (issue #215).

    This is the supported same-day path, and routing it through the scheduler is
    the whole point: it inherits the daily budget ledgers, the per-IP host lock
    and its exit-code contract, #205's fail-fast, orphan salvage, and the
    publish tail. A bespoke script with none of those is what got this host
    banned by both Mapillary and Overpass in one night.

    It deliberately does NOT collect the GSV grid — see ASSESS_CHANNELS — and it
    deliberately does not record a channel failure; see ``_run_city_channels``.

    ``today`` is injectable so tests can pin a date; production callers omit it.
    """
    # Validate BEFORE opening the catalog or geocoding, so a typo costs nothing.
    if cfg.unwired_channel_errors:
        # Same refusal as cmd_run_due's, for the same reason: this command
        # launches channels, so it must not run a collection around a config
        # block the loader had to drop. The read-only subcommands proceed.
        for message in cfg.unwired_channel_errors:
            logger.error(message)
        return USAGE_EXIT_CODE
    try:
        if (lat is None) != (lng is None):
            raise _UsageError("--lat and --lng must be given together")
        if (width is None) != (height is None):
            raise _UsageError("--width and --height must be given together")
        if width is not None and lat is None:
            # cli.py tolerates this and silently centers the grid on the OSM
            # bounding-box midpoint, which for an irregular or river-bounded
            # place is not downtown — the grid ends up the right SIZE in the
            # wrong PLACE, frozen forever. Refusing is the cheap fix.
            raise _UsageError(
                "--width/--height without --lat/--lng would freeze the grid on the "
                "OSM bounding-box midpoint rather than the city center. Pass "
                "--lat/--lng too (or omit both and let the boundary derive them)."
            )
        channels = _select_assess_channels(cfg, requested_providers)
    except _UsageError as e:
        logger.error(str(e))
        return USAGE_EXIT_CODE

    conn = db.connect(cfg.db_path)
    if today is None:
        today = datetime.now(UTC).date()

    try:
        city, newly_registered = resolve_or_register_city(
            conn, query=query, lat=lat, lng=lng, width=width, height=height, step=step
        )
    except CityResolutionError as e:
        logger.error(str(e))
        return 1
    print(
        f"{'Registered' if newly_registered else 'Already registered'}: "
        f"{city.city_id} (geometry is frozen from here on)"
    )
    print(_assess_preflight_report(cfg, conn, city, today, channels))

    if estimate_only:
        print(
            "\n--estimate: the city is registered (a catalog-only write) and no "
            "provider request was issued. Re-run without --estimate to collect."
        )
        return 0

    if not assume_yes:
        if not sys.stdin.isatty():
            logger.error(
                "Refusing to collect without confirmation on a non-interactive stdin. "
                "Re-run with --yes (or --estimate to see the costs only)."
            )
            return USAGE_EXIT_CODE
        answer = input(f"\nCollect {', '.join(channels)} for {city.city_id}? [y/N] ")
        if answer.strip().lower() not in ("y", "yes"):
            print("Aborted; nothing collected.")
            return 0

    blocked_hosts: set[str] = set()
    busy_hosts: Counter[str] = Counter()
    # Unreachable for this command as it stands — assess-city's channel
    # vocabulary has no KartaView in it, and the sibling deferral is a property
    # of a resumable street channel — but owned and scored here anyway, for the
    # reason the docstring below gives about a skip an operator cannot see: a
    # channel silently not collected is exactly what makes an inquiry answer
    # wrong, and #274's counter must not be the one the shared path drops.
    deferred_channels: Counter[str] = Counter()
    attempted, succeeded, skipped_budget = _run_city_channels(
        cfg,
        conn,
        city,
        today,
        channels,
        blocked_hosts=blocked_hosts,
        busy_hosts=busy_hosts,
        deferred_channels=deferred_channels,
        # No batch deadline: an operator run has nothing queued behind it, and
        # each child still carries its own derived per-city timeout.
        batch_deadline=None,
        # No stop signal either: this runs in an operator's foreground shell,
        # not under a supervisor that stops units, and a foreground command is
        # interrupted with Ctrl-C (SIGINT) rather than SIGTERM. There is also no
        # batch behind this city to wind down — the tail below already runs on a
        # partial failure. Passed explicitly because the parameter has no
        # default: see _run_city_channels' docstring (issue #206).
        stop_requested=None,
        # A manual probe must not be able to quarantine a city.
        record_failures=False,
    )

    # The tail runs even on a partial failure (#167): everything collected is
    # already in the catalog but invisible on the public site until the aggregate
    # is rebuilt, and a partial answer is still an answer. A failed driving-plan
    # rebuild is deliberately NOT scored here, unlike in cmd_regenerate: it is
    # unrelated to this city's numbers, and failing an answered inquiry over it
    # would be the tail wagging the dog.
    regen_summary, _regen_complete = _regenerate_published_json(conn, cfg)
    # _emit, not print: this line sits in front of the publish below, so a
    # reader that has gone away must not cost this command its rsync.
    _emit(regen_summary)
    publish_wanted = publish and cfg.publish_enabled
    published = False
    if publish_wanted:
        if succeeded == 0:
            # Mirrors _finish_batch's `succeeded > 0` gate: an rsync shipping no
            # new artifact is noise, and a failed one would email an alert for it.
            logger.warning("Nothing collected successfully; not publishing.")
        elif _publish(cfg, f"assess-city {city.city_id} (manual)") != 0:
            logger.error("Publish failed; the numbers below are cataloged but not public.")
        else:
            published = True

    print()
    print(_assess_answer_report(cfg, conn, city))
    if publish and not cfg.publish_enabled:
        # Said HERE, beside the link, because the link is built from the CATALOG
        # rather than from what is live: with publishing off it reads exactly
        # like an answer while pointing at stale or absent data, and the only
        # other signal is the ABSENCE of "; published" from the summary line —
        # which nobody reads as "not published". The realistic case is not a dev
        # laptop but prod with publishing switched off during a block or a
        # maintenance window, where the operator emails a partner a dead link.
        #
        # Deliberately a notice and not a --publish override: [publish].enabled
        # is the host's own declaration, and moving publishing OUT of ambient
        # state and INTO config is what the rest of issue #215 does. An override
        # belongs to `regenerate-aggregate --publish`, whose job genuinely is
        # "push the catalog to the site right now" — this is a collection
        # command whose publish is a consequence, not the point.
        print(
            "  NOTE  [publish].enabled is false in this config, so nothing was rsynced. "
            "The link above describes the catalog, not what is live. Publish with "
            "`regenerate-aggregate --publish` when you mean to."
        )
    blocked_note = f", {_host_names(blocked_hosts)} refused this host" if blocked_hosts else ""
    busy_note = f", {_host_names(busy_hosts)} busy locally" if busy_hosts else ""
    summary = (
        f"{succeeded}/{attempted} channel(s) collected"
        + (f", {skipped_budget} skipped on budget" if skipped_budget else "")
        + (
            f", {sum(deferred_channels.values())} deferred behind a sibling sweep"
            if deferred_channels
            else ""
        )
        + blocked_note
        + busy_note
        + ("; published" if published else "")
    )
    print(f"  {summary}")
    # What is due after this run, stated per channel rather than as a blanket
    # "due on every channel" — which was the opposite of true for the channels
    # this command just collected. A recorded success stamps last_success_at, and
    # get_due_cities reads only that, so every collected channel is now the least
    # stale thing in the catalog. That IS the intent (it stops tonight's batch
    # re-spending the crawl), but it has the same cost `run-due --provider`
    # carries, and #214 warns about it out loud rather than leaving it to be
    # discovered.
    if newly_registered:
        print(
            "  The GSV grid run is not part of this command: it has no schedule_state "
            "row yet, so it is due and leads the next nightly batch's queue."
        )
    if succeeded:
        print(
            f"  The {succeeded} channel(s) collected here recorded a success, which starts "
            f"their own {cfg.cycle_days}-day clocks — so they are NOT due tonight, and this "
            f"city's channels no longer share one run date until their cadences re-converge "
            f"(the paired-snapshot cost `run-due --provider` also carries, issue #214)."
        )
    # Exit 0 only for a run that produced a COMPLETE answer on every channel that
    # was asked for, and got it published if publishing was on the table at all.
    # "Public" is conditional deliberately: `--no-publish` and a config with
    # `[publish].enabled = false` are both declarations that this invocation does
    # not publish, so neither is a failure — while a publish that was attempted
    # and FAILED is (that is the `published or not publish_wanted` below, and the
    # printed NOTE above is what keeps the two silent cases from passing
    # unnoticed). Three of these differ from how a nightly run scores itself, and
    # deliberately:
    #
    #   - `attempted == 0` is a failure, not a no-op. Every channel was skipped,
    #     so the operator has nothing to reply to the partner with.
    #   - a budget skip counts against it, and so does a walk deferred behind a
    #     sibling sweep (issue #274). On a nightly run either is a normal
    #     deferral — the city rolls to tomorrow inside a 90-day cycle, and
    #     _finish_batch scores only `attempted - succeeded`. Here the skipped
    #     channel IS the job, and today is the deadline.
    #   - a refused or busy host is never clean, which _finish_batch does agree
    #     with (it alerts unconditionally on either).
    collected_everything = attempted > 0 and succeeded == attempted
    nothing_deferred = skipped_budget == 0 and not deferred_channels
    hosts_were_fine = not blocked_hosts and not busy_hosts
    complete = collected_everything and nothing_deferred and hosts_were_fine
    if complete and (published or not publish_wanted):
        return 0
    return 1


def _street_collect_cmd(
    cfg: SchedulerConfig,
    city: db.CityRow,
    today: date,
    channel: str,
    conn_limit: int,
    daily_budget: int,
    request_cap: int | None = None,
) -> list[str]:
    """Argv for a road-walk collection of one (city, street channel).

    No ``--min-days-since-last-run`` equivalent is needed (or exists): the
    scheduler owns cadence through ``schedule_state``, and the collector's only
    skip is its immutable same-run-date guard — which is exactly the behaviour
    wanted if a night is re-run.

    ``daily_budget`` is the channel's FULL daily ceiling, not what's left of it:
    the collector reads the same ``api_usage`` ledger and subtracts today's
    spend itself, so passing the remainder would charge that spend twice and
    abort cities that actually fit. Its guard is then an exact re-check of the
    caller's, using the true sample count rather than the planning estimate.
    """
    pc = (cfg.providers or {}).get(channel) or ProviderConfig()
    cmd = [
        sys.executable,
        "-m",
        "streetscape_street_analyzer.collect",
        "--provider",
        STREET_CHANNELS[channel],
        "--run-date",
        today.isoformat(),
        "--data-dir",
        cfg.data_dir,
        "--db-path",
        cfg.db_path,
        "--spacing",
        str(pc.spacing_m),
        # Passed explicitly rather than relying on the collector's own default:
        # each network type is a separate walk series, so the channel's
        # configured type must be the one that lands in the artifact name and
        # the catalog row.
        "--network-type",
        pc.network_type,
        "--connection-limit",
        str(conn_limit),
        "--timeout",
        str(cfg.request_timeout_s),
        # Hard stop before the isolated street ledger overruns today's ceiling.
        "--daily-budget",
        str(max(0, daily_budget)),
        "--log-level",
        "INFO",
    ]
    if channel == "gsv_streets":
        # `is not None`, not `or`: 0 is documented as "disable pacing", and a
        # falsy test silently promoted it to the 24k/48k GSV project figure.
        rate = pc.max_requests_per_minute
        cmd += [
            "--max-requests-per-minute",
            str(cfg.max_requests_per_minute if rate is None else rate),
        ]
    elif channel == "mapillary_streets":
        # Paces the tile CDN, which limits per IP rather than per token — so
        # this channel and the grid one can ban each other (issue #198). Unset
        # leaves the collector's own conservative default in force; the GSV
        # fallback above would be nonsensically large here.
        if pc.max_requests_per_minute is not None:
            cmd += [
                "--mapillary-max-requests-per-minute",
                str(pc.max_requests_per_minute),
            ]
        # Same contract for the jitter (issue #292): unset means the collector's
        # own default, which is itself jittered.
        if pc.jitter is not None:
            cmd += ["--mapillary-jitter", str(pc.jitter)]
    elif channel == "kartaview_streets":
        # The child MUST be told the pace this channel's timeout was derived
        # from. _kartaview_timeout_seconds divides the sweep estimate by the
        # configured rate, so a child left on the collector's own default would
        # be measured against a rate it never used — one of the four fail-open
        # arms #238 closed for the grid channel, and it would land here intact
        # if this arm were left out. Unset means the collector's default, and
        # then the timeout derivation reads the same default.
        if pc.max_requests_per_minute is not None:
            cmd += [
                "--kartaview-max-requests-per-minute",
                str(pc.max_requests_per_minute),
            ]
        # And the night's REMAINING budget as a hard stop (#273). --daily-budget
        # above only GATES: the collector prices it from estimate_sweep_requests,
        # a geometric FLOOR (measured overhead 1.80x; Yogyakarta ran 3.0x), so a
        # gate that passes does not bound what the sweep then spends against a
        # host that meters by IP. This is the enforceable half, and it is the
        # same defect the grid channel had -- the walk reads the same census by
        # the same sweep, so it inherits it exactly.
        #
        # Both flags, not one: they are a gate and a stop, with opposite
        # subtraction conventions. --daily-budget is the FULL ceiling because
        # this collector subtracts today's spend itself; the cap arrives already
        # subtracted, because nothing in the child can compute it.
        if request_cap is not None:
            cmd += ["--kartaview-max-requests", str(request_cap)]
    # '--' so a display name can never be parsed as a flag
    cmd += ["--", city.display_name]
    return cmd


# How much of a failed child's output to copy into the scheduler log. Enough for
# a Python traceback; the whole thing is always on disk in the per-attempt log.
_CHILD_LOG_TAIL_LINES = 25


@dataclass(frozen=True)
class CollectionOutcome:
    """Whether one collection subprocess worked, and if not, why.

    Deliberately truthy/falsy like the plain bool it replaced, so callers (and
    the test fakes that still return bare bools) read unchanged. The point is
    ``reason``: it is what finally reaches ``schedule_state.last_error``, which
    until now recorded only "subprocess failed on <date>" for every failure in
    the catalog — so a bad night had to be re-derived from scratch the next
    morning, if the daily-rotated logs still had it (issue #169).
    """

    ok: bool
    reason: str | None = None
    # The child's exit status, when there was one. None on a timeout, because a
    # SIGKILLed child has no exit code to report. Carried so the loop can map
    # HOST_EXIT_CODES back to "which per-IP host is unavailable" — the child's
    # error message never crosses the process boundary (issue #208).
    exit_code: int | None = None

    def __bool__(self) -> bool:
        return self.ok


def _child_log_path(cfg: SchedulerConfig, city: db.CityRow, provider: str, today: date) -> Path:
    """Per-attempt log for one (city, channel) collection subprocess."""
    return Path(cfg.log_dir) / f"collect_{city.city_id}_{provider}_{today.isoformat()}.log"


def _tail_lines(path: Path, n: int) -> str:
    """Last n lines of a file, credential-scrubbed; empty string if unreadable."""
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return redact_credentials("".join(fh.readlines()[-n:]))
    except OSError:
        return ""


def _run_collection_subprocess(
    cfg: SchedulerConfig,
    cmd: list[str],
    timeout_s: int,
    city: db.CityRow,
    provider: str,
    today: date,
) -> CollectionOutcome:
    """
    Run one collection subprocess with its output captured to a per-attempt log.

    The children (streetscape_tracker.py, streetscape_street_analyzer.collect)
    configure logging with a bare ``basicConfig``, so everything they emit —
    including the traceback that explains a failure — goes to stderr. Inheriting
    the parent's stderr sent that to the systemd journal, which is not readable
    by the service account: every ``collection failed`` line in the scheduler log
    had its actual cause discarded. Redirecting to a file per attempt keeps the
    full output greppable in logs/ and streams it to disk rather than buffering a
    multi-hour run in this process's memory (``capture_output=True`` would, inside
    a cgroup whose memory is capped — see deploy/systemd/; deliberately not
    quoting the cap here, since it has moved twice and the argument does not
    depend on its value).

    The tail of a failed run is also copied into the scheduler log so it reaches
    the [alerts] email, which sends only that log's tail.
    """
    os.makedirs(cfg.log_dir, exist_ok=True)
    log_path = _child_log_path(cfg, city, provider, today)
    try:
        # Append, not truncate: a re-run of the same city/channel/day should add
        # to the record rather than destroy the failure being diagnosed.
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write(f"\n===== {datetime.now(UTC).isoformat()} =====\n")
            fh.write(redact_credentials(" ".join(cmd)) + "\n\n")
            fh.flush()
            result = subprocess.run(
                cmd,
                timeout=timeout_s,
                cwd=str(_PROJECT_ROOT),
                stdout=fh,
                stderr=subprocess.STDOUT,
            )
        if result.returncode == 0:
            return CollectionOutcome(True)
        exit_code = result.returncode
        # Name the host-level conditions, and which of the two they are: the
        # scheduler reacts very differently to "refused by the third party" and
        # "another local process is already talking to it" (issue #208).
        blocked = HOST_BY_EXIT_CODE.get(exit_code)
        busy = HOST_BY_BUSY_EXIT_CODE.get(exit_code)
        if blocked:
            why = f"exited {exit_code} ({HOST_LABELS[blocked]} unavailable to this host)"
        elif busy:
            why = f"exited {exit_code} ({HOST_LABELS[busy]} busy with another local process)"
        else:
            why = f"exited {exit_code}"
    except subprocess.TimeoutExpired:
        exit_code = None
        why = f"timed out after {timeout_s // 60} minutes"

    tail = _tail_lines(log_path, _CHILD_LOG_TAIL_LINES)
    message = f"{city.city_id} [{provider}]: {why}; full output in {log_path}"
    if tail:
        message += f"\n--- last {_CHILD_LOG_TAIL_LINES} lines of {log_path.name} ---\n{tail}"
    logger.error(message)
    # The log name, not the full path: the catalog outlives any given checkout
    # location, and `logs/<name>` is what an operator actually greps for.
    return CollectionOutcome(False, f"{why} (see {log_path.name})", exit_code=exit_code)


def _run_one_city(
    cfg: SchedulerConfig,
    city: db.CityRow,
    today: date,
    provider: str = "gsv",
    connection_limit: int | None = None,
    daily_budget: int = 0,
    conn=None,
    remaining_s: float | None = None,
    *,
    timeout_s: int | None = None,
    estimated_requests: int | None = None,
    request_cap: int | None = None,
) -> CollectionOutcome:
    """Collect one (city, channel) in a subprocess.

    Grid providers run ``streetscape_tracker.py``; street channels (issue #99)
    run the road-walk collector instead. Both are metered, timed out and
    failure-counted the same way by the caller.

    ``connection_limit`` overrides ``cfg.connection_limit`` for this run (the
    resource guard lowers it when the shared host is under pressure); None uses
    the configured default. ``daily_budget`` is the street channel's full daily
    ceiling — see _street_collect_cmd on why it is not the remainder.
    ``remaining_s`` is what is left of the batch deadline, which caps this
    child's timeout (issue #167).

    ``request_cap`` is the OPPOSITE convention to ``daily_budget`` and the two
    must never be conflated (issue #273). ``daily_budget`` is a ceiling the
    child subtracts today's spend from ITSELF, because the street collector
    reads the same ``api_usage`` ledger; passing it a remainder would charge
    that spend twice. The grid CLI reads no ledger, so ``request_cap`` is the
    remainder ALREADY SUBTRACTED — ``budget - used`` — and only the caller can
    compute it, because a lane worker is handed ``conn=None`` by design (see
    above) and has no catalog to query. It is passed only for channels
    ``CHANNEL_RESUMABLE`` marks, where exhausting it checkpoints and exits
    ``SWEEP_INCOMPLETE_EXIT_CODE`` instead of failing. ``None`` — the default —
    omits the flag entirely and sweeps to completion.

    ``timeout_s`` and ``estimated_requests`` let the CALLER precompute the two
    values this function would otherwise derive here, and exist so this body can
    run off the main thread (issue #240). Both derivations need either the
    catalog connection or the clock: ``db.connect`` opens with
    ``check_same_thread=True``, and every deadline read has to happen at the one
    place that knows what the whole batch is doing. Given both, this function
    touches neither, and a lane worker is safe. ``None`` — the default — keeps
    today's derivation for every direct caller and test, so nothing outside
    ``_run_city_channels`` has to know these exist.
    """
    conn_limit = cfg.connection_limit if connection_limit is None else connection_limit

    if is_street_channel(provider):
        cmd = _street_collect_cmd(cfg, city, today, provider, conn_limit, daily_budget, request_cap)
        estimated = (
            _channel_estimate(cfg, city, provider, conn)
            if estimated_requests is None
            else estimated_requests
        )
        logger.info(
            f"Collecting streets for {city.city_id} [{provider}] "
            f"(~{estimated:,} requests estimated)"
        )
        logger.debug(f"Command: {' '.join(cmd)}")
        child_timeout_s = (
            city_timeout_seconds(cfg, city, provider, conn=conn, remaining_s=remaining_s)
            if timeout_s is None
            else timeout_s
        )
        return _run_collection_subprocess(cfg, cmd, child_timeout_s, city, provider, today)

    cmd = [
        sys.executable,
        str(_PROJECT_ROOT / "streetscape_tracker.py"),
        "--provider",
        provider,
        "--run-date",
        today.isoformat(),
        "--download-dir",
        cfg.data_dir,
        "--db-path",
        cfg.db_path,
        "--batch-size",
        str(cfg.batch_size),
        "--connection-limit",
        str(conn_limit),
        "--max-requests-per-minute",
        str(cfg.max_requests_per_minute),
        "--timeout",
        str(cfg.request_timeout_s),
        # The scheduler already decided this city is due (cycle − grace),
        # so disable the CLI's own skip window. Otherwise any config with
        # cycle_days − grace_days ≤ the CLI default (80) makes every run
        # "succeed" as a skip — stamping last_success_at while never
        # collecting anything, forever, with green logs.
        "--min-days-since-last-run",
        "0",
        "--no-visual",
        "--no-publish-json",
        "--log-level",
        "INFO",
    ]
    if provider == "mapillary":
        # The --max-requests-per-minute above is a GSV number ([download], 48k
        # on prod) and the CLI applies it only to the GSV path. Mapillary needs
        # its own, far smaller cap against a per-IP limit on the tile CDN
        # (issue #198); omitting the flag would leave the CLI's own
        # conservative default in force, which is also correct.
        pc = (cfg.providers or {}).get(provider) or ProviderConfig()
        if pc.max_requests_per_minute is not None:
            cmd += ["--mapillary-max-requests-per-minute", str(pc.max_requests_per_minute)]
        # And its jitter (issue #292), under the same unset-means-default rule.
        if pc.jitter is not None:
            cmd += ["--mapillary-jitter", str(pc.jitter)]
    if provider == "kartaview":
        # Same reason as Mapillary's flag above, plus one specific to this
        # channel: the timeout is DERIVED from the configured rate (#238), so a
        # child left on its own default would be timed against a number it never
        # honoured. Omitting the flag when unset is still correct -- the CLI
        # default is the same conservative 16/min this derivation assumes.
        rate = ((cfg.providers or {}).get(provider) or ProviderConfig()).max_requests_per_minute
        if rate is not None:
            cmd += ["--kartaview-max-requests-per-minute", str(rate)]
        # The night's REMAINING budget as a hard stop (issue #273). Until this
        # landed the guard was a pre-flight check against an estimate and
        # nothing bounded the child, on the one channel whose estimate is not
        # exact: _SWEEP_OVERHEAD_MULTIPLIER is a MEDIAN (1.80x) whose study max
        # is 13.66x, on a p65 city, so a mid-catalog sweep could spend ~7.6x
        # what the ledger said it could afford against a host that meters us by
        # IP. Exhausting this checkpoints the rest and exits 83 instead, which
        # _run_city_channels amnesties -- so the overrun became a pause.
        #
        # Same unset-means-omit rule as the rate above, and the value is never
        # < 1: the CLI's _positive_int refuses 0 at parse time (it used to be
        # accepted, spend the whole calibration ladder and checkpoint nothing),
        # and the caller's floor is what keeps this side of that.
        if request_cap is not None:
            cmd += ["--kartaview-max-requests", str(request_cap)]
    # '--' so a display name can never be parsed as a flag
    cmd += ["--", city.display_name]
    # `conn` on both fallbacks, matching the street arm above (#238). It was
    # omitted here while `estimate_requests` read it only for gsv_streets; the
    # KartaView tier that prefers a previous sweep's OBSERVED cost makes it
    # load-bearing, and without it the child is timed from default-radius
    # geometry that under-prices an r=500 metro roughly fourfold.
    estimated = (
        _channel_estimate(cfg, city, provider, conn)
        if estimated_requests is None
        else estimated_requests
    )
    logger.info(f"Collecting {city.city_id} [{provider}] (~{estimated:,} requests estimated)")
    logger.debug(f"Command: {' '.join(cmd)}")
    child_timeout_s = (
        city_timeout_seconds(cfg, city, provider, conn=conn, remaining_s=remaining_s)
        if timeout_s is None
        else timeout_s
    )
    return _run_collection_subprocess(cfg, cmd, child_timeout_s, city, provider, today)


def _reconcile_orphaned_run(
    conn, cfg: SchedulerConfig, city: db.CityRow, provider: str, today: date
) -> bool:
    """
    Salvage a run whose subprocess reported failure but which actually cataloged
    a valid run row for today.

    The download is the expensive, budgeted part of the pipeline; once it
    finishes, ``register_run`` commits the row *before* the diff and per-run
    JSON. A subprocess killed in that tail (e.g. a large city SIGKILLed right at
    the timeout boundary) leaves a complete, valid run whose only defect is a
    missing JSON — which the aggregate then skips. Discarding it as a failure
    would re-spend the whole download next cycle and strand the row.

    Returns True if a valid run row for (city, provider, today) exists and was
    reconciled (JSON rebuilt if it was missing). Returns False when there is no
    such row — a genuine failure the caller should record normally.
    """
    row = conn.execute(
        "SELECT run_id, json_filename FROM runs "
        "WHERE city_id = ? AND provider = ? AND run_date = ?",
        (city.city_id, provider, today.isoformat()),
    ).fetchone()
    if row is None:
        return False

    if not row["json_filename"]:
        # Tail was interrupted before the JSON was written; rebuild it from the
        # cataloged CSV. If the CSV is somehow gone, treat it as a real failure.
        if regenerate_run_json(conn, row["run_id"], cfg.data_dir) is None:
            logger.error(
                f"{city.city_id} [{provider}]: run row exists but JSON could not "
                f"be rebuilt (CSV missing); recording failure"
            )
            return False

    logger.info(
        f"{city.city_id} [{provider}]: subprocess reported failure but run "
        f"{row['run_id']} is cataloged for {today}; reconciled as success"
    )
    return True


def _count_streetwalk_samples(csv_path: Path) -> int | None:
    """
    Number of sampled locations in a road-walk snapshot: its data rows.

    The walk writes exactly one row per on-street sample point, so the row count
    recovers ``sample_points`` — which the artifact itself does not carry and
    which ``estimate_street_samples`` prefers over every other precedence step
    when budgeting a later walk of the same city. Counted line-by-line rather
    than via pandas: the caller may be reconciling a multi-hundred-MB snapshot
    inside the scheduler's memory-capped cgroup, and only the count is wanted.

    Returns None if the snapshot is missing or unreadable.
    """
    try:
        with gzip.open(csv_path, "rt", encoding="utf-8") as fh:
            return max(sum(1 for _ in fh) - 1, 0)  # minus the header
    except (OSError, EOFError, UnicodeDecodeError) as e:
        logger.warning(f"Could not count samples in {csv_path.name}: {e}")
        return None


def _reconcile_orphaned_walk(
    conn,
    cfg: SchedulerConfig,
    city: db.CityRow,
    channel: str,
    today: date,
    *,
    regenerate_manifest: bool = True,
) -> bool:
    """
    Salvage a road walk whose subprocess reported failure but which actually
    finished and left its artifacts on disk.

    The street analogue of ``_reconcile_orphaned_run``, and needed for a
    stronger reason. A grid run commits ``register_run`` *before* its diff/JSON
    tail, so only the tail can be lost; a road walk catalogs nothing until the
    single ``register_street_walk`` call at the very end of ``collect.py``, so
    ANY failure after the crawl — a DB write racing a schema migration, an OOM
    kill, a timeout landing in the tail — discards a complete, fully-paid-for
    crawl. Berlin lost 611k requests exactly that way on 2026-07-28.

    The coverage GeoJSON is self-describing (it carries the same per-edge
    totals that were about to be cataloged), so the row is rebuilt from the
    artifact rather than recomputed.

    Returns True if a walk was cataloged from artifacts on disk; False when
    there is nothing to salvage — a genuine failure the caller records normally.
    """
    pc = (cfg.providers or {}).get(channel) or ProviderConfig()
    provider = STREET_CHANNELS[channel]

    # Rebuild the names the collector would have written, never by hand: the
    # provider and network tokens are what keep same-night walks apart, and a
    # hand-built name is how the streetwalk artifacts got silently collided
    # before (see scripts/repair_streetwalk_names.py).
    stem = generate_streetwalk_filename(
        city.city_id,
        city.grid_width_m,
        city.grid_height_m,
        city.step_m,
        pc.spacing_m,
        today,
        provider=provider,
        network_type=pc.network_type,
    )
    csv_name = stem + ".csv.gz"
    coverage_name = streetwalk_coverage_filename(csv_name)
    coverage_path = Path(cfg.data_dir) / coverage_name
    if not coverage_path.exists():
        return False

    try:
        with gzip.open(coverage_path, "rt", encoding="utf-8") as fh:
            geojson = json.load(fh)
        meta = geojson["properties"]["metadata"]
        totals = meta["totals"]
        edges_total = totals["edges"]
    except (OSError, EOFError, ValueError, KeyError, TypeError) as e:
        # A truncated or malformed artifact says nothing about coverage and
        # must not become a catalog row; treat it as the real failure it is.
        logger.error(
            f"{city.city_id} [{channel}]: coverage artifact {coverage_name} exists "
            f"but could not be read ({e}); recording failure"
        )
        return False

    sample_points = _count_streetwalk_samples(Path(cfg.data_dir) / csv_name)
    # .get()-guarded like the other stats: an artifact written before issue
    # #101 carries no such key, and its absence must never fail the salvage.
    breakdown = meta.get("coverage_by_highway")
    walk_id = db.register_street_walk(
        conn,
        city_id=city.city_id,
        run_date=today,
        csv_filename=csv_name,
        provider=provider,
        coverage_filename=coverage_name,
        # From the channel's config, NOT the artifact: walks written before the
        # network-type series existed carry no network_type key at all, and the
        # configured type is what this channel actually asked the collector for.
        network_type=pc.network_type,
        spacing_m=meta.get("spacing_m", pc.spacing_m),
        match_dist_m=meta.get("match_dist_m"),
        sample_points=sample_points,
        edges_total=edges_total,
        edges_fully_covered=totals.get("edges_fully_covered"),
        mean_edge_coverage=totals.get("mean_edge_coverage"),
        coverage_pct_by_length=totals.get("coverage_pct_by_length"),
        coverage_pct_by_length_any=totals.get("coverage_pct_by_length_any"),
        coverage_by_highway=json.dumps(breakdown) if breakdown else None,
        # Absolute street length (v12). All four are .get()-guarded for the same
        # reason the breakdown above is: the salvage path reads whatever artifact
        # happens to be on disk, including ones written before these keys existed.
        length_km=totals.get("length_km"),
        length_km_covered=totals.get("length_km_covered"),
        length_km_covered_any=totals.get("length_km_covered_any"),
        median_covered_age_years=totals.get("median_covered_age_years"),
        # GSV issues one metadata request per sample point, so the row count is
        # the request count. Mapillary's cost is a z14 tile census independent
        # of the sample count, which the artifacts don't record — leave it NULL
        # ("not measured") rather than writing a number that isn't its cost.
        api_requests=sample_points if provider == "gsv" else None,
        started_at=None,
        finished_at=datetime.fromtimestamp(coverage_path.stat().st_mtime, UTC).isoformat(),
    )
    # The salvage path is precisely where the collect-side diff was lost (the
    # crash landed in the tail), and the previous walk's artifact is on disk —
    # so compute it here too (issue #101). Failure-guarded: a diff bug must
    # never turn a successful salvage into a recorded failure, which would
    # re-crawl the city at full cost and defeat the salvage's purpose.
    try:
        compute_and_record_walk_diff(
            conn,
            data_dir=cfg.data_dir,
            city_id=city.city_id,
            walk_id=walk_id,
            run_date=today,
            provider=provider,
            network_type=pc.network_type,
            spacing_m=meta.get("spacing_m", pc.spacing_m),
            match_dist_m=meta.get("match_dist_m"),
            fc_new=geojson,
        )
    except Exception:
        logger.exception(f"{city.city_id} [{channel}]: walk diff failed during salvage; continuing")
    if regenerate_manifest:
        # Without this the salvaged walk publishes but stays invisible: the city
        # page finds streetwalk artifacts only through the sidecar manifest.
        generate_streetwalk_manifest(conn, cfg.data_dir)

    logger.info(
        f"{city.city_id} [{channel}]: subprocess reported failure but the walk "
        f"finished ({edges_total:,} edges, {totals.get('coverage_pct_by_length')}% "
        f"by length) and was cataloged from {coverage_name}; reconciled as success"
    )
    return True


def _collect_due(
    conn,
    cfg: SchedulerConfig,
    today: date,
    providers: list[str],
    *,
    max_opt_in: int,
    max_cities: int,
):
    """
    Due work for today: an ordered city list (stalest-first, gsv's order
    leading since it's the expensive series) and, per city, which of
    ``providers`` are due. Providers pair on the same cycle day by design, so
    most cities are due for all providers at once; they only diverge after
    per-provider failures or when a provider was enabled later.

    ``providers`` is **required** — the caller states the channel set, which for
    a nightly run is ``cfg.enabled_providers()`` and for ``run-due --provider``
    is a subset of it (issue #214). It used to default to None-means-everything,
    which made "no channels named" and "every channel" the same value and left a
    fail-open path one refactor away; see ``_UsageError``.

    Filtering here is the whole mechanism: ``_run_city_loop`` works from
    ``providers_for_city``, so a channel absent from this mapping is never
    priced, never budgeted and never launched.

    Returns ``(ordered, providers_for_city, hoisted)``; ``hoisted`` is the
    number of cities the opt-in reorder below moved, logged by ``cmd_run_due``.

    ``max_opt_in`` is the reservation from issue #282 — how many opt-in-only
    cities may be promoted to the head of the slate. It is **keyword-only with
    no default**, for the same reason ``providers`` is required and
    ``_run_city_loop``'s ``max_cities`` is: a permissive default here is an
    unbounded hoist one refactor away, and unbounded is the exact failure #282
    exists to remove. Callers resolve it through ``_opt_in_reservation``.
    """
    due_by_provider = {
        provider: db.get_due_cities(
            conn,
            today=today,
            cycle_days=cfg.cycle_days,
            grace_days=cfg.grace_days,
            max_consecutive_failures=cfg.max_consecutive_failures,
            default_membership=CHANNEL_DEFAULT_MEMBERSHIP[provider],
            provider=provider,
        )
        for provider in providers
    }
    ordered, seen = [], set()
    providers_for_city = {}
    for provider, due in due_by_provider.items():
        for city in due:
            if city.city_id not in seen:
                seen.add(city.city_id)
                ordered.append(city)
            providers_for_city.setdefault(city.city_id, []).append(provider)

    # Membership scopes a channel; without this it is still never REACHED
    # (issue #248). The union above is ordered by first appearance, so the
    # first channel in `providers` — normally gsv, rank 0 — dictates city
    # order and later channels only append cities gsv did not already surface.
    # _run_city_loop then stops at max_cities_per_day (prod: 20) out of ~949.
    #
    # For a city due on both gsv and an opt-in channel that is already right:
    # it sits in gsv's stalest-first list and both channels run the same night.
    # It breaks for a city whose sweep did not finish, because its gsv run
    # SUCCEEDED — so gsv's clock advanced and gsv will not surface it again for
    # ~83 days. The city falls to the tail of the union and is truncated by the
    # city cap, which is what makes docs/scheduler.md's "leads tomorrow's
    # stalest-first queue" true of the channel's own list and false of the
    # union. That is also what converts scheduler.py's and docs/census.md's
    # "five nights" from a figure of speech into a mechanism: the five
    # consecutive_failures a SIGKILL can burn are survivable only while the
    # five nights are CONSECUTIVE, so #239's checkpointed progress accumulates.
    #
    # WHICH unfinished sweep, precisely, because the two arms behave very
    # differently and BOTH are live since #273:
    #
    #   * A deliberate pause (SWEEP_INCOMPLETE_EXIT_CODE, amnestied in
    #     _run_city_channels, consuming no slot). This is the arm a HEALTHY
    #     sweep takes: _run_city_channels caps every sweep at the smaller of the
    #     night's remaining budget and what the child's own timeout can pace
    #     (_sweep_requests_within_timeout), so a sweep that would overrun stops
    #     itself deliberately instead of being killed. Both terms are needed --
    #     on prod 16/min over a 10 h batch paces ~9,600 requests against a
    #     10,000 budget, so a cap set to a fresh night's remainder is
    #     unreachable by arithmetic and the kill below stays the arm actually
    #     taken. A pause records no consecutive_failure, so the five-night bound
    #     below does not bind it at all -- what bounds it instead is
    #     CHECKPOINT_MAX_AGE_S, seven days from the checkpoint's FIRST commit,
    #     after which its rows would be spliced into a snapshot dated today and
    #     it is discarded.
    #   * A SIGKILL at the per-city timeout. Still reachable, and what it now
    #     catches is a child running SLOWER than the assumed rate x
    #     _SWEEP_ACHIEVED_RATE_FRACTION -- the one overrun a request cap cannot
    #     bound, since only a clock inside the child could. The checkpoint on
    #     disk survives, so tomorrow resumes — but the kill has no exit code, so
    #     it counts a consecutive_failure, and `attempted` was incremented, so
    #     it DID consume a city-cap slot. The hoist is what makes tomorrow's
    #     retry the FIRST slot rather than one truncated away, and the
    #     five-night bound is this arm's, not the pause's.
    #     Since #282 bounded the hoist, "the first slot" is no longer automatic
    #     and is bought deliberately instead: a live checkpoint takes a reserved
    #     slot ahead of a city that has never been swept. See the reservation
    #     below -- without that preference a killed city sorts alphabetically
    #     among the never-run block and the five nights stop being consecutive.
    #
    # `all`, not `any`, and the choice is the blast radius. A city due on gsv
    # too needs no hoist (see above), and there is no pairing argument either:
    # below the city cap it is truncated on both channels together, which pairs
    # fine. An `any` key would hoist it anyway, displacing the stalest gsv-only
    # city from a capped night every time a member city comes due. `all`
    # rescues exactly the stranded case — due ONLY on opt-in channels — and
    # leaves gsv's ordering strictly untouched.
    #
    # Reordering the CITY LIST, never the union loop: providers_for_city is
    # passed straight to _run_city_channels, where `pending = list(providers)`
    # IS the launch order, so hoisting by iterating opt-in channels first would
    # silently promote them to rank 0 and overturn the enabled_providers
    # ordering that a 40-line docstring and four superseded rationales in
    # docs/scheduler.md exist to protect.
    opt_in = {p for p in providers if is_opt_in_channel(p)}
    hoisted = 0
    if opt_in:
        # BOUNDED since #282. The promotion is now a RESERVATION -- at most
        # `max_opt_in` cities move -- and the bound is what makes the mechanism
        # survive a wide enrolled set. Unbounded, the hoist's success case and
        # its starvation case are the same case at different N: "due only on
        # the opt-in channel" is the NORMAL steady state for an enrolled city,
        # because gsv succeeds nightly and advances its clock while the opt-in
        # channel's stays put. So at a seed set of two it rescues a stranded
        # city, and at a few hundred it takes the whole city cap and every
        # default-membership channel collects nothing.
        #
        # The cities beyond the bound keep their union position rather than
        # being dropped: they are still due, still counted in `due`, and simply
        # wait for a later night's reservation. That is the intended shape of a
        # widening -- N cities per night, indefinitely -- not a truncation.
        # WHICH cities the reservation spends its slots on, which a bound makes
        # a real question for the first time. Unbounded, every opt-in-only city
        # led the slate and the order among them did not matter.
        #
        # Bounded and filled in union order it does, and it breaks the one
        # invariant the hoist exists to provide. `get_due_cities` orders
        # `last_success_at ASC NULLS FIRST, city_id ASC`, and a city SIGKILLed
        # mid-sweep still has NULL there -- it never succeeded -- so it sorts
        # ALPHABETICALLY among every never-run enrolled city, which during a
        # widening is the whole enrolled set. Enrol 200 at a reservation of 5
        # and a killed city sorting late is not reached for ~40 nights, far past
        # CHECKPOINT_MAX_AGE_S (7 days): its checkpoint is discarded,
        # _SWEEP_SKIP_AGE_WALL records a real consecutive_failure, and the
        # partial sweep is re-paid every cycle forever. The five failures stop
        # being CONSECUTIVE, which is the property the whole amnesty design
        # rests on and the reason docs/scheduler.md says the hoist buys it.
        #
        # So a live checkpoint takes a reserved slot first. That is the exact
        # population the invariant is about -- both unfinished-sweep arms leave
        # one, the deliberate pause and the SIGKILL -- and it is the only signal
        # that distinguishes "this city has already been paid for and the
        # payment expires" from "this city has never been touched".
        # `consecutive_failures` would catch only the SIGKILL arm, and a
        # healthy multi-night pause records none.
        #
        # Probed only when the reservation actually has to choose. At today's
        # enrolled set the whole slate fits and this costs no filesystem reads
        # at all; the cost arrives with the widening, alongside the problem.
        opt_in_only = [
            i
            for i, c in enumerate(ordered)
            if all(p in opt_in for p in providers_for_city[c.city_id])
        ]
        if len(opt_in_only) <= max_opt_in:
            chosen = set(opt_in_only)
        else:

            def _has_live_checkpoint(city) -> bool:
                # Through _sweep_checkpoint_progress, THE reader, because a
                # walk's store is keyed by (channel, network type) and a grid
                # run's by the channel alone -- a second spelling here would
                # ask about a different crawl than the launch gate does.
                return any(
                    _sweep_checkpoint_progress(cfg, city, p) is not None
                    for p in providers_for_city[city.city_id]
                )

            # Stable, so within each group the union's stalest-first order is
            # untouched and the choice is only ever "resumers before starters".
            chosen = set(
                sorted(opt_in_only, key=lambda i: 0 if _has_live_checkpoint(ordered[i]) else 1)[
                    :max_opt_in
                ]
            )
        keys = [0 if i in chosen else 1 for i in range(len(ordered))]
        promoted = len(chosen)
        # Cities that actually MOVED, which is what the word means and what
        # scripts/night_length_analyze.py reads off the opening line.
        #
        # The old test -- both key values present -- was written when a slate
        # was all-0 or all-1, and the reservation broke it: `run-due --provider
        # kartaview` makes every due city opt-in-only, so with a bound the
        # first `max_opt_in` take key 0 and the rest key 1, the keys are
        # ALREADY sorted, the stable sort moves nothing, and the night would
        # report `hoisted=10` having reordered nothing at all.
        #
        # A stable sort on a boolean key moves a city exactly when some key-1
        # city precedes it, so the count is the key-0 cities after the first
        # key-1 one. That is the identity permutation for an all-0 slate, an
        # all-1 slate AND a bounded all-opt-in slate, without special-casing
        # any of the three.
        first_kept = next((i for i, k in enumerate(keys) if k == 1), len(keys))
        hoisted = sum(1 for k in keys[first_kept:] if k == 0)
        # Stable sort on a boolean key: with no opt-in channel configured every
        # key is 1 and this is the identity permutation, which is what makes
        # PR A's inertness provable by construction rather than argued. (The
        # `if opt_in` guard is belt-and-braces on the same claim.)
        ordered = [c for _, c in sorted(zip(keys, ordered, strict=True), key=lambda kc: kc[0])]
        # The backlog is the number an operator widening a channel actually
        # needs, and it is invisible from `hoisted` alone -- a reservation that
        # is working looks identical whether 3 cities are waiting or 800.
        #
        # "Waiting" is measured against the CITY CAP, not against the
        # reservation, and the difference is not cosmetic. An unpromoted city
        # keeps its union position rather than being dropped, so it is deferred
        # only if it falls outside `max_cities` -- and on the catch-up a
        # widening actually uses (`run-due --provider kartaview --limit 40`
        # with 40 due cities) every one of them collects tonight. Counted off
        # the reservation this logged "10 ... take tonight's reserved slots; 30
        # wait for a later night" and then collected all 40, which is the one
        # number the operator is reading.
        opt_in_only = [
            i
            for i, c in enumerate(ordered)
            if all(p in opt_in for p in providers_for_city[c.city_id])
        ]
        reached = sum(1 for i in opt_in_only if i < max_cities)
        waiting = len(opt_in_only) - reached
        if waiting:
            logger.info(
                f"{reached} of {len(opt_in_only)} opt-in-only cities are inside tonight's "
                f"{max_cities}-city cap ([schedule].opt_in_cities_per_day={max_opt_in} "
                f"reserved, {promoted} promoted); {waiting} wait for a later night"
            )
    return ordered, providers_for_city, hoisted


def _backup_catalog_nightly(cfg: SchedulerConfig, conn, today: date) -> str | None:
    """
    Write the night's dated catalog backup (issue #145). Returns an error string
    for the batch summary, or None.

    Called BEFORE the city loop, and again in the tail. The pre-flight copy is
    the one that matters for durability: ``_finish_batch`` runs after any
    *loop-level* failure (errored loop, batch deadline, SIGTERM — see #167) but
    NOT after a SIGKILL, which is the documented OOM/timeout mode on the
    Mapillary post-decode path (#157). A tail-only backup is therefore missing
    on exactly the nights something went badly wrong. Running here first means
    the night has a verified copy before anything can kill the process, and it
    also covers zero-due nights, where the tail's work is skipped.

    Unlike the driving-plan hook next door, a failure here is NOT advisory: see
    _finish_batch, which reports it as an unhealthy night. A backup nobody
    notices failing is how #145 happened.
    """
    try:
        result = catalog_backup.write_backup(conn, cfg.backup_dir, today, source_db=cfg.db_path)
    except Exception as e:  # defensive — write_backup already swallows its own
        logger.exception("Catalog backup raised")
        return f"catalog backup failed: {e}"
    return None if result.ok else f"catalog backup failed: {result.error}"


def _fetch_driving_plan_nightly(cfg: SchedulerConfig, conn, today: date) -> str | None:
    """
    Snapshot the driving-plan feed (issue #176). Returns an error string for
    the batch summary, or None. Never raises: a broken Google feed must not
    cost a night of collection (the issue #167 lesson), and the feed is an
    undocumented asset with no uptime contract.

    "Never raises" is not "never reported", though — the returned error reaches
    _finish_batch, which alerts and exits nonzero on it. Google overwrites this
    feed in place, so a night we do not snapshot is a revision nobody can ever
    recover; that deserves louder handling than the plan *summary* rebuild
    beside it, which can be regenerated from the catalog at any time.
    """
    if not cfg.driving_plan.enabled:
        return None
    try:
        result = driving_plan.ingest(
            conn,
            archive_dir=cfg.driving_plan.archive_dir,
            fetch_date=today,
            url=cfg.driving_plan.url,
            timeout_s=cfg.driving_plan.timeout_s,
        )
    except Exception as e:
        logger.exception("Driving-plan snapshot failed")
        return f"driving-plan fetch failed: {e}"
    if result.skipped:
        outcome = "already snapshotted today"
    elif result.changed:
        outcome = f"CHANGED — archived, {result.entry_count:,} entries"
    else:
        outcome = "unchanged"
    logger.info(
        f"Driving-plan snapshot {result.fetch_date}: {outcome} ({result.record_count:,} records)"
    )
    return None


def _select_providers(cfg: SchedulerConfig, requested: list[str]) -> list[str]:
    """
    Resolve ``run-due --provider`` into a channel list (issue #214).

    Accepts both the repeated form (``--provider a --provider b``) and a comma
    list (``--provider a,b``). The result is filtered out of
    ``cfg.enabled_providers()`` rather than taken in CLI order, so the canonical
    gsv-first ranking survives whatever order an operator types.

    Raises ``_UsageError`` — never returns an empty list — when the flag names no
    channel, or names one that is unknown OR configured ``enabled = false``. That
    is an error rather than a silent no-op: on a host where Mapillary is switched
    off, accepting ``--provider mapillary`` would run a zero-due night, and its
    full publish tail, while looking like it collected something.
    """
    enabled = cfg.enabled_providers()
    names = [n.strip() for value in requested for n in value.split(",") if n.strip()]
    if not names:
        # Reachable: argparse takes any string, so `--provider ""` and
        # `--provider ,` both land here. Falling through with an empty list would
        # be the zero-due night this function exists to refuse.
        raise _UsageError(f"--provider given with no channel name (got {requested!r})")
    unusable = [n for n in names if n not in enabled]
    if unusable:
        raise _UsageError(
            f"--provider {', '.join(unusable)}: not an enabled channel. "
            f"Enabled in this config: {', '.join(enabled) or '(none)'}"
        )
    return [p for p in enabled if p in set(names)]


def cmd_run_due(
    cfg: SchedulerConfig,
    dry_run: bool = False,
    limit: int | None = None,
    today: date | None = None,
    requested_providers: list[str] | None = None,
) -> int:
    """
    Collect all cities due today, within per-provider budgets, publish.

    ``today`` is injectable so tests can pin a date (a wall-clock read here
    can cross UTC midnight mid-test and flake); production callers omit it.

    ``requested_providers`` restricts the night to a subset of the enabled
    channels, and an explicit ``limit`` overrides ``max_cities_per_day``.
    Together they are the supported way to run an on-demand catch-up for one
    provider (issue #214) — the point being that it inherits the daily budget
    ledger, stalest-first ordering, per-channel cadence and failure counting,
    the host lock, fail-fast, the night-level breaker, alerting, orphan salvage
    and the publish tail. The bespoke detached script that had none of those is
    what got this host per-IP banned on 2026-08-14.

    A filtered run is NOT the nightly run with fewer channels: it advances only
    the named channels' ``schedule_state``, so the cities it touches stop sharing
    a run date with their other channels. That is the feature (catching one
    channel up means moving its clock alone), not a defect — see the warning
    logged below.
    """
    # Validate BEFORE opening the catalog, so an operator typo costs nothing.
    # Returning rather than propagating is deliberate: main()'s run-due branch
    # emails an alert on an exception, and a typo is not a nightly crash.
    if cfg.unwired_channel_errors:
        # The load already dropped the block, so nothing below could launch
        # it — but a night that silently ran AROUND a channel the config asks
        # for would read as a success while collecting nothing on it, the same
        # shape as the unknown-channel refusal below. Only the channel-running
        # commands refuse; backup-status and friends proceed with the error in
        # the log.
        for message in cfg.unwired_channel_errors:
            logger.error(message)
        return USAGE_EXIT_CODE
    try:
        providers = (
            _select_providers(cfg, requested_providers)
            if requested_providers is not None
            else cfg.enabled_providers()
        )
        if limit is not None and limit < 1:
            # Left unchecked, `--limit 0`/`-1` makes `processed >= max_cities`
            # true on the first iteration: zero cities collected, no publish,
            # exit 0. That is the same "a night that did nothing reads as a
            # success" failure the channel check refuses, reached through the
            # sibling flag.
            raise _UsageError(f"--limit must be at least 1 (got {limit})")
    except _UsageError as e:
        logger.error(str(e))
        return USAGE_EXIT_CODE

    conn = db.connect(cfg.db_path)
    if today is None:
        today = datetime.now(UTC).date()
    batch_started = time.monotonic()
    batch_deadline = batch_started + cfg.max_batch_hours * 3600.0

    # Ensure new cities (and newly enabled providers) have stagger assignments.
    # Deliberately over the FULL enabled set, not the filtered one: a
    # Mapillary-only catch-up must not leave new cities unregistered on the
    # channels it isn't running tonight.
    db.assign_schedule(conn, cfg.cycle_days, providers=tuple(cfg.enabled_providers()))

    # An explicit --limit IS the cap for this run. Without this the config's
    # max_cities_per_day silently wins, and `--limit 40` quietly does 20 — which
    # would leave a Mapillary catch-up at the nightly cap's ~61 nights per pass
    # rather than the ~5 the daily budget allows.
    #
    # There is deliberately NO `due = due[:limit]` here, and that omission is the
    # whole fix rather than a tidy-up. The loop's cap counts cities it actually
    # *processed*, and a candidate can be skipped without processing (budget
    # guard, host breaker, busy lock), so pre-truncating the candidate list to N
    # lets the loop run out of list below N — `--limit 40` silently doing 30 and
    # reporting a clean night, which is this flag's own bug one layer down.
    max_cities = limit if limit is not None else cfg.max_cities_per_day
    # Resolved BEFORE the slate is built, because the reservation is an input to
    # the ordering rather than a filter applied after it — and against
    # `max_cities`, not `cfg.max_cities_per_day`, so `--limit` scales the split
    # with the cap it overrides instead of leaving a 20-city reservation on a
    # 5-city night.
    max_opt_in = _opt_in_reservation(cfg, max_cities)
    due, providers_for_city, hoisted = _collect_due(
        conn, cfg, today, providers, max_opt_in=max_opt_in, max_cities=max_cities
    )
    day_cap = min(len(due), max_cities)

    budget_str = ", ".join(f"{cfg.providers[p].daily_request_budget:,} {p}" for p in providers)
    filter_note = f" [--provider {','.join(providers)}]" if requested_providers is not None else ""
    logger.info(
        f"{len(due)} cities due on {today}{filter_note}; "
        f"processing up to {day_cap} within daily budgets of "
        f"{budget_str} requests"
        # The lane count is logged rather than left to an operator's memory of
        # when the knob was flipped: the night-length measurement (issue #240)
        # compares elapsed hours ACROSS that flip, so which setting a night ran
        # under has to be recoverable from the night's own record. Named exactly
        # like the config key so a log line and a TOML line are greppable
        # together — scripts/night_length_analyze.py reads this.
        f"; max_concurrent_channels={cfg.max_concurrent_channels}"
        # Same reason as the lane count: an opt-in channel's hoist reorders the
        # night's slate (issue #248), and which cities a capped night reached
        # has to be recoverable from the night's own record. Omitted entirely
        # when no opt-in channel is configured, so nightly log lines are
        # byte-identical to today's.
        + (f"; hoisted={hoisted} opt-in-only cities" if hoisted else "")
    )
    starved = [p for p in providers if not is_opt_in_channel(p)]
    if starved and hoisted and hoisted >= max_cities:
        # KEPT as a backstop after #282 bounded the hoist, not left behind by
        # it. Two ways to reach it, and neither is the wide enrolled set the
        # pre-#282 version fired on: an operator set
        # [schedule].opt_in_cities_per_day equal to max_cities_per_day and
        # re-created the unbounded hoist by hand, or `--limit 1` made a derived
        # `max(1, 1 // 4)` equal the cap. Both are legal, the first is a bad
        # configuration and the second is a degenerate one-city run; the night
        # it starves every default-membership channel is the night to say so.
        #
        # `starved` gates the whole warning, because on `run-due --provider
        # kartaview` every requested channel is opt-in and the list is EMPTY --
        # which used to render "so  will collect nothing tonight" about no
        # channel at all. There is nothing to starve on such a run: the
        # operator asked for exactly the channels that are running.
        logger.warning(
            f"{hoisted} opt-in-only cities fill the city cap ({max_cities}), so "
            f"{', '.join(starved)} will collect "
            f"nothing tonight. Lower [schedule].opt_in_cities_per_day (the reserved "
            f"share), narrow the enrolled set (`enroll-city --remove`), or raise "
            f"[schedule].max_cities_per_day."
        )
    if requested_providers is not None and set(providers) != set(cfg.enabled_providers()):
        # Say out loud what a filtered run costs, because nothing downstream
        # will. `get_due_cities` derives dueness from `schedule_state
        # .last_success_at` alone and never reads `day_of_cycle`, so the
        # same-run-date pairing of a city's channels is a *consequence* of their
        # clocks being in lockstep, not a constraint the scheduler maintains.
        # Advancing one channel's clock alone therefore un-pairs every city this
        # run collects, permanently, until their cadences happen to re-converge.
        # That is exactly what a catch-up is for — but an operator reaching for
        # this flag to "just run the one channel tonight" should know it is not a
        # narrower version of the nightly run.
        logger.warning(
            f"--provider advances only {', '.join(providers)}: each city collected "
            f"tonight will stop sharing a run date with its other channels "
            f"({', '.join(p for p in cfg.enabled_providers() if p not in set(providers))}) "
            f"until their cadences re-converge. Intended for a catch-up; not a "
            f"narrower nightly run."
        )

    if dry_run:
        budget_left = {
            p: cfg.providers[p].daily_request_budget - db.get_api_usage(conn, today, p)
            for p in providers
        }
        left_str = ", ".join(f"{budget_left[p]:,} {p}" for p in providers)
        print(f"DRY RUN — would process (budget remaining {left_str}):")
        for city in due[:day_cap]:
            for provider in providers_for_city[city.city_id]:
                # Via _channel_estimate, not estimate_requests: the dry run has
                # to price what the night will ACTUALLY spend, and a census
                # already in the shared cache is free (issue #290). Reading the
                # raw estimate here would show an over-budget deferral for a
                # channel the real run launches for nothing.
                marker = channel_census_cache_marker(
                    city, provider, max_age_s=_census_reuse_window_s(cfg)
                )
                est = _channel_estimate(cfg, city, provider, conn, cached=marker is not None)
                if is_resumable_channel(provider):
                    # THE SAME DECISION THE LIVE PATH MAKES, from the same
                    # helper (issue #274). `est > budget_left` is the wrong
                    # question for a channel that pauses and resumes: it printed
                    # "OVER BUDGET (deferred)" for precisely the metros
                    # _run_city_channels launches capped, and a preview that is
                    # wrong about the expensive channel is worse than no preview.
                    #
                    # `remaining_s=None` is the ONE thing this cannot share: the
                    # batch deadline is a clock reading inside a running night,
                    # and a preview has no night. So the cap shown here is the
                    # UNCLAMPED one — an upper bound, matched by a night whose
                    # deadline is still far off and larger than what a city
                    # launched late actually gets.
                    plan = _sweep_launch_plan(
                        cfg,
                        city,
                        provider,
                        conn,
                        est=est,
                        remaining=budget_left[provider],
                        remaining_s=None,
                        city_channels=providers_for_city[city.city_id],
                    )
                    fits = plan.label
                    # What the night would actually spend on it: a capped launch
                    # stops at the cap, and a skip spends nothing at all.
                    spend = 0 if plan.skip else min(est, plan.request_cap)
                else:
                    fits = "ok" if est <= budget_left[provider] else "OVER BUDGET (deferred)"
                    spend = est if est <= budget_left[provider] else 0
                if marker is not None:
                    payer = marker.get("fetched_by") or "an earlier collection"
                    # Still worth saying when a cached channel is nonetheless
                    # being stood down (a walk waiting on its grid sibling), so
                    # this reads as an addition to the verdict, not a replacement.
                    fits = (
                        f"ok (cached census from {payer})"
                        if fits == "ok"
                        else f"{fits} (cached census from {payer})"
                    )
                print(f"  {city.city_id:60s} {provider:16s} ~{est:>9,} req  {fits}")
                budget_left[provider] -= spend
        if cfg.driving_plan.enabled:
            print("Would also snapshot the GSV driving-plan feed (issue #176).")
        print(f"Would also back up the catalog to {cfg.backup_dir} (issue #145).")
        return 0

    # Back up the catalog BEFORE the city loop, so the night has a verified
    # copy even if the process is SIGKILLed mid-loop (issue #145; the tail
    # can't be reached in that case). Repeated in the tail to capture the
    # night's registered runs.
    backup_error = _backup_catalog_nightly(cfg, conn, today)

    # Snapshot the driving-plan feed BEFORE the city loop: upstream of the
    # batch deadline and any mid-loop kill, and on zero-due nights too. Cheap
    # (one request), and a failure never *stops* the night — though it does
    # make it report unhealthy, since a missed snapshot is unrecoverable.
    plan_error = _fetch_driving_plan_nightly(cfg, conn, today)

    # The tail below (aggregate, manifest, backup, publish) is what makes a
    # night visible, and it only runs if the loop returns. Everything that can
    # end the loop early therefore lives inside _run_city_loop, which always
    # returns counters rather than propagating (issue #167).
    with _stop_on_sigterm() as sigterm_seen:
        (
            processed,
            succeeded,
            attempted,
            skipped_budget,
            stop_reason,
            blocked_hosts,
            busy_hosts,
            deferred_channels,
        ) = _run_city_loop(
            cfg,
            conn,
            today,
            due,
            providers_for_city,
            batch_deadline,
            sigterm_seen,
            max_cities=max_cities,
        )

    if stop_reason:
        logger.info(f"Stopped early: {stop_reason}")

    blocked_note = (
        "; ".join(sorted(HOST_LABELS.get(h, h) for h in blocked_hosts)) + " unavailable"
        if blocked_hosts
        else ""
    )
    busy_note = (
        f"{sum(busy_hosts.values())} channel(s) skipped, "
        + _host_names(busy_hosts)
        + " busy locally"
        if busy_hosts
        else ""
    )
    # Elapsed is in the summary because the Mapillary per-IP block may have a
    # sustained-load component and nothing else records time-under-load: peak rate
    # is a config value and `api_usage` is a daily total, so "we spent too much"
    # and "we spent too long" are indistinguishable after the fact. The summary is
    # what the [alerts] email carries, so this is where an operator can read it
    # off. See the falsifier in docs/provider-access.md's Mapillary budget section.
    elapsed_h = (time.monotonic() - batch_started) / 3600.0
    summary = (
        f"run-due {today}{filter_note}: {succeeded}/{attempted} runs succeeded across "
        f"{processed} cities in {elapsed_h:.2f} h"
        + (f"; {skipped_budget} deferred for budget" if skipped_budget else "")
        # Named apart from the budget deferral because the operator's next move
        # differs: nothing is over budget and nothing failed -- the walk is
        # waiting for its grid sibling's sweep to land in the census cache, and
        # collects for 0 requests once it does (issues #274, #290).
        + (
            f"; {sum(deferred_channels.values())} walk(s) deferred behind a paused sibling "
            f"sweep ({', '.join(sorted(deferred_channels))})"
            if deferred_channels
            else ""
        )
        + (f"; {blocked_note}" if blocked_note else "")
        + (f"; {busy_note}" if busy_note else "")
        + (f"; stopped early ({stop_reason})" if stop_reason else "")
        + (f"; {plan_error}" if plan_error else "")
    )
    logger.info("Done: " + summary)
    return _finish_batch(
        cfg,
        conn,
        summary,
        succeeded,
        attempted,
        today,
        errored=stop_reason == _STOP_REASON_ERROR,
        backup_error=backup_error,
        plan_error=plan_error,
        blocked_hosts=blocked_hosts,
        busy_hosts=busy_hosts,
    )


def _log_channel_error(city_id: str, provider: str, exc: BaseException) -> None:
    """Write one channel's unhandled exception to the log, come what may.

    EVERY exception gets a line, not only the first one ``_run_city_channels``
    keeps to re-raise. At more than one lane two channels can fail for unrelated
    reasons in the same completion pass, and only one of them can be the
    exception that propagates; the ``[alerts]`` email carries just this log's
    tail, so a cause not written here is unrecoverable — the same
    "``collection failed`` with no cause" hole the per-attempt child logs were
    added to close.

    Suppressed rather than allowed to raise because this sits ON the path that
    salvages the other channels' outcomes, and a dead output pipe raises
    ``BrokenPipeError`` out of logging itself — which is one of the ways
    classification fails on this thread in the first place. Losing a log line is
    bad; losing a paid-for channel's ``record_attempt`` because the log line
    failed is worse. ``logger.exception`` reads the live exception state, so the
    traceback is the caller's even though the call is here.
    """
    with contextlib.suppress(Exception):
        logger.exception(f"{city_id} [{provider}]: channel raised {exc!r}")


def _sweep_progress_note(progress: dict | None) -> str:
    """
    " 3/8 root cells." for a paused sweep; empty when nothing readable is on
    disk. Takes the dict :func:`_sweep_checkpoint_progress` returns, because the
    caller needs it for the age-wall warning too and reading the state file
    twice is how two reports of one pause come to disagree.

    A pause is amnestied and consumes no city-cap slot, so without this a
    multi-night sweep is invisible: the log says "resumes on the next run" every
    night whether or not any root cell was answered, and the two failure modes
    it hides look identical from outside.

    Deliberately NO age-wall warning in here any more. It used to be appended as
    the substring "WARNING:" inside a record logged at INFO — invisible to every
    level filter, every handler that splits by severity and every operator
    grepping for warnings, on the one line that says a sweep is about to have
    its whole spend thrown away. It is its own ``logger.warning`` record now;
    see :func:`_warn_if_checkpoint_near_the_age_wall`.
    """
    if progress is None:
        return ""
    return f" {progress['roots_done']}/{progress['root_count']} root cells."


def _warn_if_checkpoint_near_the_age_wall(
    city_id: str, provider: str, progress: dict | None
) -> None:
    """
    A WARNING record — its own, not a substring of one — when a paused sweep's
    checkpoint is within a night of ``CHECKPOINT_MAX_AGE_S``.

    ``CHECKPOINT_MAX_AGE_S`` is measured from the checkpoint's FIRST commit, so
    a city that cannot finish inside it has its checkpoint discarded mid-crawl
    and re-sweeps from root 0 -- weekly and forever, because the re-sweep
    commits a fresh ``created_at`` and every individual night then reads as
    ordinary progress.

    This fires the night BEFORE the discard, from the pause branch. It is the
    early notice, not the guard: ``_sweep_launch_plan`` refuses the next night's
    launch outright and records a failure, which is what reaches the alert path
    (issue #274). Both exist because they answer different questions — this one
    says a sweep is falling behind while there is still a night to act in.
    """
    age_s = None if progress is None else progress["age_s"]
    if age_s is None or age_s < CHECKPOINT_MAX_AGE_S - _CHECKPOINT_AGE_WALL_MARGIN_S:
        return
    logger.warning(
        f"{city_id} [{provider}]: this checkpoint is {age_s / 86400:.1f} days old and is "
        f"discarded past {CHECKPOINT_MAX_AGE_S / 86400:.0f}, which throws the whole sweep "
        f"away — it is not finishing at this nightly budget, and the next run will refuse "
        f"to resume it (and record a failure) rather than spend another night on it."
    )


def _log_stop_declined(city_id: str, declined: list[str]) -> None:
    """Name the channels a wind-down is choosing not to start (issue #206).

    Called from exactly one place — after ``_run_city_channels`` has drained —
    because the stop exit an operator actually hits is not the one that reads
    like the main path. The unit's ``KillMode`` defaults to control-group, so a
    ``systemctl stop`` reaches the in-flight children too: they die first, and
    the city therefore leaves via the killed-child branch rather than by
    noticing the flag at a submit gate. While this message lived only at the
    top of the channel loop, a real stop named no declined channel at all — and
    with Mapillary enabled those are exactly the ones that would otherwise have
    fired into a live per-IP tile block (#205), i.e. usually the thing the
    operator typing ``stop`` was trying to prevent. Both exits now converge on
    the set of channels still un-launched, which is why there is one call site
    and no wording to keep in step.

    Silent when there is nothing left to decline (a stop landing on a city's
    last channel), so the log can never claim a wind-down skipped work that did
    not exist.
    """
    if not declined:
        return
    logger.info(
        f"{city_id}: stop requested — not starting {', '.join(declined)}. "
        f"Not counted as failures; these channels keep their cadence and lead "
        f"the next batch's queue."
    )


def _run_city_channels(
    cfg: SchedulerConfig,
    conn,
    city: db.CityRow,
    today: date,
    providers: list[str],
    *,
    blocked_hosts: set[str],
    busy_hosts: Counter[str],
    deferred_channels: Counter[str],
    batch_deadline: float | None,
    stop_requested: threading.Event | None,
    record_failures: bool = True,
) -> tuple[int, int, int]:
    """
    Collect one city on each of ``providers`` with every guard the nightly batch
    applies: the per-IP host breaker, both daily-budget checks, the resource
    guard, the stop signal, orphan salvage, and cadence bookkeeping.

    ``cfg.max_concurrent_channels`` (issue #240) says how many of this city's
    channels may be in flight at once. At the default 1 they run back-to-back on
    THIS thread, exactly as they always have. Above 1 they run in lanes — but
    only ever host-disjoint ones: a channel whose per-IP third party is already
    being talked to by an in-flight sibling is deferred, not launched, so each of
    those hosts still sees at most one talker from this process. That is what
    makes the concurrency invisible provider-side; the only thing it changes is
    the night's wall clock (see docs/provider-access.md).

    The city is the join point either way: this returns only when every channel
    of it has finished or been skipped, so all of a city's snapshots still carry
    one run date and stay comparable.

    Every decision lives on this thread — pricing, the budget ledger reads, the
    breaker, and all of the classification below. A lane worker runs
    ``_run_one_city`` and nothing else, because ``db.connect`` opens the catalog
    with ``check_same_thread=True``; the two values the child body would
    otherwise derive from ``conn`` or the clock are precomputed here and passed
    in (``timeout_s``, ``estimated_requests``).

    Split out of ``_run_city_loop`` so the on-demand single-city path
    (``assess-city``, issue #215) inherits all of it rather than reimplementing
    a simplified — i.e. eventually divergent — copy. ``_run_city_loop`` keeps
    what is genuinely about a *batch*: the city cap, the deadline, and the
    inter-city sleep.

    ``blocked_hosts`` and ``busy_hosts`` are owned by the caller and mutated in
    place, because the breaker's scope is the whole run, not one city: a host
    that refused us cannot answer differently for the next city, so its channels
    stay skipped once seen. A *busy* host is only counted — that condition ends
    when the other local process does.

    ``deferred_channels`` counts, per channel, the walks this city stood down
    because their GRID sibling's sweep of the same lattice is still in flight
    (issue #274; see ``_sweep_launch_plan``). Same ownership and the same reason
    as ``busy_hosts``: it is neither a failure nor a budget skip — nothing is
    wrong and nothing was spent — but a night that quietly collected no road
    walk has to say so somewhere, and the two existing counters would each be a
    lie about why.

    ``batch_deadline`` (a ``time.monotonic()`` value) clamps each child's
    timeout so no collection outlives the window reserved for the publish tail;
    None means no deadline, which is right for a single-city operator run. It has
    no default deliberately — the same reason ``_collect_due``'s ``providers``
    doesn't (issue #214): a caller that silently inherited "no deadline" would
    lose the guard that keeps a night from being SIGKILLed before it publishes.
    Each child is priced against it once, at ITS OWN launch, which stays correct
    with several in flight: every one of them genuinely does have until the
    shared deadline.

    ``stop_requested`` is the wind-down flag from ``_stop_on_sigterm``, and has
    no default for exactly the same reason: a caller that silently inherited
    "nothing can stop this" would look correct until someone typed
    ``systemctl stop``, which is the one moment it matters (issue #206). ``None``
    means no supervisor can ask this run to stop — right for an operator's
    foreground command, wrong for a batch. It is named for the *contract* rather
    than the mechanism so ``None`` reads as "nothing can ask us to stop" rather
    than "we don't know whether SIGTERM was seen". It gates SUBMISSION: work
    already in flight is allowed to finish and is credited, because it has
    already been paid for.

    ``record_failures=False`` records a success but never a failure. Manual runs
    use it because ``get_due_cities`` filters on ``consecutive_failures <
    max_consecutive_failures`` and nothing resets that counter except a success —
    so letting an operator's ad-hoc probe increment it would let a few of them
    quarantine a city for a whole cycle. A recorded *success* is still wanted:
    it says this channel genuinely has today's data, which is what stops the
    next nightly batch from re-spending the same crawl hours later.

    Returns ``(attempted, succeeded, skipped_budget)``. ``attempted == 0`` means
    every channel was skipped, which is what tells the batch loop this city
    should not count against the city cap or earn an inter-city sleep.
    """
    attempted = succeeded = skipped_budget = 0
    lanes = max(1, cfg.max_concurrent_channels)
    # `connection_limit` is a HOST budget, so it is divided across lanes rather
    # than handed to each child whole. The resource guard reads host-wide
    # pressure (MemAvailable, load5) and only ever LOWERS its answer — but it is
    # consulted once per child, from a sample taken before that child's siblings
    # have ramped, so at N lanes each of them reads a quiet box and each takes
    # the full limit. The guard structurally cannot see the load it is about to
    # permit.
    #
    # What that costs is not uniform, because only three channels carry this
    # number at all: the gsv grid (download_gsv's TCPConnector), the gsv road
    # walk (the same engine) and the Mapillary road walk. The Mapillary GRID
    # never receives it — cli.py's branch omits the argument, so
    # fetch_city_images_async's own default of 5 applies. Combined with affinity
    # (mapillary/mapillary_streets share the tile CDN, gsv_streets/
    # mapillary_streets share Overpass), the only overlapping pair that points
    # two full-size connectors at ONE third party is gsv + gsv_streets, both of
    # which are Google: prod's 50 becomes 100 sockets on the endpoints whose
    # per-project metering is already the second gate on raising this knob.
    #
    # Exactly `cfg.connection_limit` at one lane, so the shipped default path is
    # unchanged. This is the CONSERVATIVE direction and not the free one: a city
    # with a single enabled channel gets the divided share too, though it has no
    # sibling to share with. Raising `[download].connection_limit` alongside the
    # knob is a deliberate decision with a measurement behind it; multiplying it
    # silently is not.
    lane_connection_limit = max(1, cfg.connection_limit // lanes)
    # Channels not yet launched, in canonical (most-expensive-first) order. A
    # channel LEAVES this list the moment it is launched or finally skipped, so
    # whatever remains at the end is exactly the set nothing was ever asked of —
    # which is what a wind-down has to name.
    pending: list[str] = list(providers)
    in_flight: dict[concurrent.futures.Future, str] = {}
    worker_error: BaseException | None = None
    stopped = False

    # None at the default, and that is the point: at one lane the channel body
    # runs INLINE on this thread, not on a size-1 pool. It keeps the default
    # path byte-equivalent to the pre-#240 loop, and it keeps `conn` usable by
    # anything that substitutes _run_one_city (the catalog handle is
    # check_same_thread=True, so a pool would break test fakes and any future
    # caller that reaches for it).
    #
    # Per CITY rather than hoisted to the batch, deliberately. A 20-city night
    # therefore builds and joins 20 pools, which is microseconds against
    # subprocess-bound work — and what it buys is that `shutdown(wait=True)`
    # below makes "no child outlives its city" structural rather than a property
    # of the loop being written correctly. Everything this function hands back or
    # mutates is reasoned per city: `blocked_hosts` and `busy_hosts` are read by
    # the NEXT city's submit gates, and the streetwalks.json.gz writer ordering
    # assumes at most one street child alive at a time. A pool shared across
    # cities would let a straggler from city A be alive during city B, and
    # nothing in either contract would notice.
    pool = (
        concurrent.futures.ThreadPoolExecutor(max_workers=lanes, thread_name_prefix="channel")
        if lanes > 1
        else None
    )

    def _start(provider: str, **kwargs) -> concurrent.futures.Future:
        """Launch one channel; returns an already-finished Future in the inline case.

        ``conn=None`` always, never conditionally on the mode: a call shape that
        differed between one lane and several would leave the precomputed path
        exercised only in the mode production does not run yet.
        """
        if pool is not None:
            return pool.submit(_run_one_city, cfg, city, today, provider, conn=None, **kwargs)
        future: concurrent.futures.Future = concurrent.futures.Future()
        try:
            future.set_result(_run_one_city(cfg, city, today, provider, conn=None, **kwargs))
        # BaseException, not Exception, because that is what ThreadPoolExecutor's
        # own work item catches — the two paths must classify identically, and
        # the caller re-raises it either way.
        except BaseException as exc:
            future.set_exception(exc)
        return future

    try:
        while pending or in_flight:
            # ── launch pass ─────────────────────────────────────────────────
            # The ONLY place work starts, and it is always this thread. Nothing
            # below reads a result; a channel is either launched, deferred (left
            # in `pending`), or finally skipped (removed from it).
            if not stopped and worker_error is None:
                hosts_in_flight: set[str] = set()
                for running in in_flight.values():
                    hosts_in_flight.update(CHANNEL_HOSTS.get(running, ()))

                for provider in list(pending):
                    if len(in_flight) >= lanes:
                        break

                    # A stop was requested (systemd's SIGTERM). This is a submit
                    # gate, not a kill: every other guard here is a property of
                    # one CHANNEL — this host refused us, this channel's budget
                    # is spent — so a later channel can still answer differently.
                    # A stop is a property of the PROCESS, so none of them can,
                    # and nothing further is launched for this city. It sits
                    # FIRST for the blocked-host guard's own stated reason:
                    # there is no point pricing work we already know we will not
                    # do. What is already in flight is left to finish and is
                    # credited — it has been paid for either way. The channels
                    # nothing was asked of are named once, after the drain; see
                    # _log_stop_declined.
                    if stop_requested is not None and stop_requested.is_set():
                        stopped = True
                        break

                    # A sibling of this city is already talking to a per-IP third
                    # party this channel also needs. DEFER — the only gate here
                    # that leaves a channel in `pending` — and reconsider it when
                    # that sibling completes. Silent: nothing was decided about
                    # this channel, it simply has to wait its turn, and it is the
                    # affinity rule (not the submit ordering, and not the
                    # child-side per-host flock) that keeps those hosts to one
                    # talker from this process. Deliberately ahead of the gates
                    # below, so a deferred channel is priced and breaker-checked
                    # against the state that exists when it actually launches.
                    if hosts_in_flight.intersection(CHANNEL_HOSTS.get(provider, ())):
                        continue

                    pending.remove(provider)

                    # Same contract as the completion pass: pricing a channel
                    # touches the catalog (estimate_requests, get_api_usage,
                    # city_timeout_seconds) and the log, so it can raise on this
                    # thread while siblings are in flight. Stash, stop
                    # launching, and let the drain below credit what was already
                    # paid for, rather than propagating past a `finally` that
                    # would only wait for those children and then discard them.
                    try:
                        # A host this channel needs already refused us during this
                        # run. A final skip (not a deferral): the other channels of
                        # this and later cities are still worth running. Deliberately
                        # BEFORE the budget checks — there is no point pricing work
                        # we already know we will not do.
                        unavailable = blocked_hosts.intersection(CHANNEL_HOSTS.get(provider, ()))
                        if unavailable:
                            logger.info(
                                f"{city.city_id} [{provider}]: skipping — "
                                f"{_host_names(unavailable)} "
                                f"already refused this host."
                            )
                            continue

                        budget = cfg.providers[provider].daily_request_budget
                        est = _channel_estimate(cfg, city, provider, conn)
                        # Read BEFORE the est > budget arm, not between the two arms
                        # as it used to be, because the resumable branch below needs
                        # the remainder to decide anything at all. It is a read on
                        # the one thread that owns the catalog either way.
                        used = db.get_api_usage(conn, today, provider)
                        remaining = budget - used

                        # Derived here, ahead of the gates, for the resumable channels
                        # ONLY -- their request cap is sized against the timeout their
                        # child will actually be given, deadline clamp included, so the
                        # two numbers have to be decided together (#273). Everything
                        # else keeps deriving it at the launch site below, where a
                        # channel the gates skip pays for nothing it will not use.
                        # Whichever site runs, it runs exactly once per channel and on
                        # this thread: `city_timeout_seconds` needs the catalog handle,
                        # and the deadline has to be read by the thread that knows what
                        # the whole batch is doing.
                        timeout_s: int | None = None
                        request_cap: int | None = None

                        if is_resumable_channel(provider):
                            # NEITHER gate below applies to a channel that can stop
                            # itself and continue tomorrow (#274). Both of them exist
                            # because every other channel is all-or-nothing: a partial
                            # GSV grid, a partial tile census and a partial road walk
                            # are not runs, so there is no way to spend half a budget
                            # usefully and refusing to start is honest.
                            #
                            # A sweep is not all-or-nothing. Since #239 it spends what
                            # tonight affords, checkpoints the unvisited roots and
                            # exits 83; nothing is finalized or published until the
                            # lattice is complete, so the immutable dated-snapshot
                            # contract is untouched -- the run is simply dated the day
                            # it completes. The cities `est > budget` skipped forever
                            # are therefore precisely the ones the checkpoint was
                            # built for: Singapore ~9,974 requests, New York ~12,355,
                            # both permanently skipped against any sane budget while
                            # being loudly logged every single night.
                            #
                            # `est` is deliberately not consulted here. It prices the
                            # WHOLE sweep even for a city resuming from a checkpoint
                            # -- estimate_kartaview_requests says so itself, because
                            # its observed tier reads a `runs` row and a paused sweep
                            # never reaches register_run -- so gating on it is exactly
                            # the over-pricing this branch exists to stop.
                            #
                            # THE CAP, THE TIMEOUT AND THE THREE SKIPS ARE ONE
                            # DECISION, and it is made in _sweep_launch_plan rather
                            # than here -- shared with `run-due --dry-run`, which used
                            # to print "OVER BUDGET (deferred)" for exactly the metros
                            # this path launches capped. The preview is the one thing
                            # an operator reads before a night, so the two must not be
                            # able to disagree. Why the cap is the smaller of the
                            # budget remainder and what the clock affords, and why
                            # each skip answers as it does, is stated once, there.
                            #
                            # What stays HERE is the accounting, because only this
                            # path has any: a skip is a counter or a recorded failure,
                            # and the dry run neither collects nor records.
                            plan = _sweep_launch_plan(
                                cfg,
                                city,
                                provider,
                                conn,
                                est=est,
                                remaining=remaining,
                                remaining_s=(
                                    None
                                    if batch_deadline is None
                                    else batch_deadline - time.monotonic()
                                ),
                                city_channels=providers,
                            )
                            timeout_s, request_cap = plan.timeout_s, plan.request_cap
                            if plan.skip == _SWEEP_SKIP_AGE_WALL:
                                # THE ONE SKIP HERE THAT IS RECORDED AS A FAILURE, and
                                # it has to be. Everything else on this path is
                                # self-correcting: a budget skip retries tomorrow, a
                                # pause is amnestied and resumes. A checkpoint at the
                                # age wall corrects nothing -- the child would discard
                                # it, re-commit a FRESH created_at on the same path and
                                # start from root 0, weekly and forever, with no
                                # failure counted, no `unhealthy` component set and no
                                # email. `attempted` is what feeds
                                # `failures = attempted - succeeded` in _finish_batch,
                                # so incrementing it here is what buys both the nightly
                                # alert and the five-night backstop that eventually
                                # quarantines the city instead of burning ~60k requests
                                # a cycle on a sweep that cannot finish.
                                logger.warning(f"{city.city_id} [{provider}]: {plan.message}")
                                attempted += 1
                                if record_failures:
                                    db.record_attempt(
                                        conn,
                                        city.city_id,
                                        success=False,
                                        error=(
                                            f"checkpoint at the {CHECKPOINT_MAX_AGE_S / 86400:.0f}"
                                            f"-day age wall and unfinishable at tonight's cap"
                                        ),
                                        provider=provider,
                                    )
                                continue
                            if plan.skip == _SWEEP_SKIP_SIBLING:
                                # Neither a failure nor a budget deferral: nothing is
                                # wrong and the budget is not the reason. Counted apart
                                # so a night that quietly collected no walk says so.
                                logger.info(f"{city.city_id} [{provider}]: {plan.message}")
                                deferred_channels[provider] += 1
                                continue
                            if plan.skip is not None:
                                # The calibration floor. A budget decision, so it takes
                                # the budget counter and no failure -- see the plan.
                                logger.info(f"{city.city_id} [{provider}]: {plan.message}")
                                skipped_budget += 1
                                continue
                            if plan.message:
                                logger.info(f"{city.city_id} [{provider}]: {plan.message}")
                        elif est > budget:
                            # This city can NEVER fit the daily budget — skipping (not
                            # ending the city) so it can't starve every smaller city
                            # behind it in the stalest-first queue. Needs a manual run
                            # or a config change; surfaced loudly so it doesn't rot
                            # silently.
                            logger.warning(
                                f"{city.city_id} [{provider}]: ~{est:,} estimated requests "
                                f"exceeds the entire daily budget ({budget:,}). "
                                f"Skipping — run manually with streetscape_tracker.py --force, "
                                f"raise daily_request_budget, or set enabled=0."
                            )
                            skipped_budget += 1
                            continue
                        elif used + est > budget:
                            # Doesn't fit in what's LEFT today — try the next (smaller)
                            # city rather than ending the day; this one rolls to tomorrow
                            # when the budget is fresh.
                            logger.info(
                                f"{city.city_id} [{provider}] (~{est:,} req) doesn't fit "
                                f"remaining budget ({remaining:,} left); skipping."
                            )
                            skipped_budget += 1
                            continue

                        conn_limit, throttle_reason = plan_connection_limit(
                            lane_connection_limit, read_system_pressure(), cfg.resource_guard
                        )
                        if throttle_reason:
                            logger.info(
                                f"Resource guard: {throttle_reason}; connection limit "
                                f"{lane_connection_limit} → {conn_limit} for "
                                f"{city.city_id} [{provider}]"
                            )
                        if timeout_s is None:
                            # Exactly one clock read per LAUNCHED channel, here, so the
                            # deadline is priced by the thread that knows what the whole
                            # batch is doing. Never let a child run past it; the point of
                            # the deadline is to reserve time for the publish tail. A
                            # resumable channel already took this branch above, where its
                            # cap needed the answer.
                            remaining_s = (
                                None
                                if batch_deadline is None
                                else batch_deadline - time.monotonic()
                            )
                            timeout_s = city_timeout_seconds(
                                cfg, city, provider, conn=conn, remaining_s=remaining_s
                            )
                        future = _start(
                            provider,
                            connection_limit=conn_limit,
                            # The channel's FULL ceiling, not `budget - used`: the
                            # street collector subtracts today's spend from this
                            # itself, so passing the remainder would count it twice.
                            daily_budget=budget,
                            # Derived above for a resumable channel, beside the cap
                            # that is sized against it, and handed down rather than
                            # re-derived: the cap is only meaningful against the
                            # timeout the child actually gets, deadline clamp and all.
                            timeout_s=timeout_s,
                            # The gate above already priced this channel; handing the
                            # number down deletes the duplicate computation (and with
                            # it the only other reason the child body would want the
                            # catalog handle).
                            estimated_requests=est,
                            # The budget remainder ALREADY SUBTRACTED, floored at what
                            # the timeout above can pace -- the opposite convention to
                            # daily_budget, deliberately spelled differently so the two
                            # cannot drift into each other (#273). None for every
                            # non-resumable channel. Computed on this thread because it
                            # has to be: a lane worker gets conn=None, so `used` is
                            # only knowable here, which is also the thread whose
                            # serialized read-then-write keeps the guard honest.
                            request_cap=request_cap,
                        )
                        in_flight[future] = provider
                        hosts_in_flight.update(CHANNEL_HOSTS.get(provider, ()))
                    except BaseException as exc:
                        _log_channel_error(city.city_id, provider, exc)
                        if worker_error is None:
                            worker_error = exc
                        break

            # No-livelock invariant: an empty `in_flight` here means the pass
            # above ran with an empty `hosts_in_flight`, so nothing could have
            # deferred — every pending channel was launched, finally skipped, or
            # the city was stopped. `wait()` is therefore never called on an
            # empty set, and a city can never spin holding channels it will not
            # start.
            if not in_flight:
                break

            # ── completion pass ─────────────────────────────────────────────
            # ALL classification lives here, on this thread, and it drains before
            # the next launch pass. That ordering is a correctness invariant, not
            # a convenience: `streetwalks.json.gz` has three writers through
            # json_summarizer._write_json_gz_atomic's fixed `path + ".tmp"` — the
            # street child's own end-of-walk rebuild, the orphan-walk salvage
            # below, and the batch tail. Host affinity keeps at most one street
            # child alive, and draining classification (salvage included) before
            # launching again keeps a salvage rebuild from overlapping the next
            # street child's tail write.
            done, _still_running = concurrent.futures.wait(
                in_flight, return_when=concurrent.futures.FIRST_COMPLETED
            )
            # Canonical order, so a night's log and its bookkeeping don't depend
            # on which lane happened to finish first within one wait().
            for future in sorted(done, key=lambda f: providers.index(in_flight[f])):
                provider = in_flight.pop(future)

                # One channel's outcome, classified in full or not at all.
                # The `try` covers BOTH the worker's own exception and
                # everything this thread then does with the result, because the
                # two lose the same thing: a channel that already spent its
                # crawl, whose outcome would otherwise never reach
                # `record_attempt` or `blocked_hosts`. Before #240 that loss was
                # impossible — one channel was in flight and it was classified
                # before the next started — so with lanes the main thread needs
                # the same drain-and-credit the worker path already had.
                #
                # Stash and keep draining: the siblings' work is already paid
                # for, so it still gets salvaged and recorded. The launch pass
                # is gated on `worker_error`, so nothing new starts and the
                # `while` empties `in_flight` classifying what is left.
                # Re-raised once the city is quiet, which is what turns it into
                # _STOP_REASON_ERROR upstream — an unhealthy night that still
                # publishes what it collected (issue #167).
                try:
                    exc = future.exception()
                    if exc is not None:
                        # Re-raised rather than handled here so a worker's
                        # failure and this thread's own take the identical
                        # logging and stashing path below.
                        raise exc
                    ok = future.result()

                    # getattr, not attribute access: tests (and any future caller)
                    # may hand back a plain bool, which CollectionOutcome is
                    # deliberately compatible with.
                    reason = getattr(ok, "reason", None)
                    exit_code = getattr(ok, "exit_code", None)

                    busy_host = HOST_BY_BUSY_EXIT_CODE.get(exit_code)
                    if busy_host is not None:
                        # Another process on this machine holds that host's lock.
                        # Transient — it ends when that process does — so this skips
                        # ONE channel of ONE city and does not trip the breaker.
                        # Like a blocked skip it records no `record_attempt`: the
                        # city didn't fail, we simply never asked it. _finish_batch
                        # still alerts, so it cannot pass as a clean night.
                        busy_hosts[busy_host] += 1
                        logger.warning(
                            f"{city.city_id} [{provider}]: {HOST_LABELS[busy_host]} is busy with "
                            f"another process on this machine — skipping this channel. Not counted "
                            f"as a failure for this city, and not a run-wide skip: the lock frees "
                            f"when that process finishes. ({reason})"
                        )
                        continue

                    blocked_host = HOST_BY_EXIT_CODE.get(exit_code)
                    if blocked_host is not None:
                        # A whole-host condition, so this is NOT the city's failure
                        # and must not be recorded as one: get_due_cities filters on
                        # `consecutive_failures < max_consecutive_failures` and
                        # nothing in the codebase resets that counter except a
                        # success, so a few blocked nights would quietly quarantine
                        # a city for an entire cycle. It stays due and leads
                        # tomorrow's stalest-first queue instead. _finish_batch
                        # alerts on blocked_hosts, so this is loud, not silent.
                        #
                        # A sibling that needs the same host cannot already be in
                        # flight — affinity forbids it — so it is still in `pending`
                        # and hits the breaker at its own submit, exactly as it would
                        # have with the channels run back-to-back.
                        #
                        # Skipping `_reconcile_orphaned_run`/`_reconcile_orphaned_walk`
                        # is safe rather than incidental: every host-unavailable exit
                        # happens before the child writes anything. Overpass is a road
                        # walk's first step, and the tile census is a Mapillary run's
                        # first step, so there is never a paid-for artifact to salvage
                        # here. Keep that true if either fetch ever moves later.
                        blocked_hosts.add(blocked_host)
                        logger.error(
                            f"{city.city_id} [{provider}]: {HOST_LABELS[blocked_host]} is "
                            f"unavailable to this host — skipping its remaining channels. "
                            f"Not counted as a failure for this city. ({reason})"
                        )
                        continue

                    if exit_code == SWEEP_INCOMPLETE_EXIT_CODE:
                        # A sweep that stopped with roots unvisited and CHECKPOINTED
                        # them (issue #239). Progress, not breakage — the cli calls it
                        # exactly that, and gives it its own exit code so a wrapper
                        # cannot escalate a legitimately capped night of a multi-night
                        # sweep. So it takes the same amnesty as the two host branches
                        # above, for the same reason: get_due_cities filters on
                        # `consecutive_failures < max_consecutive_failures` and NOTHING
                        # but a success resets it, so charging a pause would quarantine
                        # a city for a whole 90-day cycle after five of them — and a
                        # metro sweep needs more nights than that by construction
                        # (Singapore is ~10.4 h of pacing against a 10 h
                        # max_batch_hours). The city stays due and leads tomorrow's
                        # stalest-first queue, which is what resuming requires.
                        #
                        # The spend is NOT lost with it: the child ledgers its own
                        # per-process requests before exiting 83, so unlike a SIGKILL
                        # this path costs the budget ledger nothing.
                        #
                        # NOTE what this does not cover: a sweep SIGKILLed by the
                        # timeout has no exit code at all and still counts a failure,
                        # because nothing here can tell a kill that checkpointed
                        # progress from one that made none. That is the standing limit
                        # on "a kill just resumes tomorrow" — see
                        # _kartaview_timeout_seconds.
                        #
                        # One read of the state file, two records out of it: the
                        # progress line at INFO, and — only when the checkpoint is
                        # running out of days — a WARNING of its own. The warning used
                        # to be a substring inside this INFO line, which is invisible
                        # to every severity filter there is.
                        paused_progress = _sweep_checkpoint_progress(cfg, city, provider)
                        logger.info(
                            f"{city.city_id} [{provider}]: sweep paused with its progress "
                            f"checkpointed — not counted as a failure for this city; it "
                            f"stays due and resumes on the next run."
                            f"{_sweep_progress_note(paused_progress)} ({reason})"
                        )
                        _warn_if_checkpoint_near_the_age_wall(
                            city.city_id, provider, paused_progress
                        )
                        continue

                    ok = bool(ok)
                    # A subprocess can report failure yet still have done the expensive,
                    # budgeted part of its job; salvage that rather than re-spending the
                    # whole crawl next cycle. The two channel kinds fail differently: a
                    # grid run commits its `runs` row before the diff/JSON tail, while a
                    # road walk catalogs nothing until the very end and so is salvaged
                    # from the artifacts it left on disk.
                    if not ok:
                        if is_street_channel(provider):
                            ok = _reconcile_orphaned_walk(conn, cfg, city, provider, today)
                        else:
                            ok = _reconcile_orphaned_run(conn, cfg, city, provider, today)

                    # This child died of the SIGTERM that is stopping US. The unit's
                    # default KillMode is control-group, so a `systemctl stop` reaches
                    # the whole cgroup — the 2026-08-13 log shows the in-flight child as
                    # "exited -15", which is in neither HOST_BY_EXIT_CODE nor
                    # HOST_BY_BUSY_EXIT_CODE and therefore reads as an ordinary
                    # collection failure. Charging it to the city would be wrong twice
                    # over, and both are the argument the blocked- and busy-host
                    # branches above already make: it burns one of five
                    # `consecutive_failures` that ONLY a success ever resets, and it
                    # makes attempted > succeeded, so every deliberate stop would email
                    # a failure alert and end the unit red (issue #206). With lanes the
                    # cgroup kills every in-flight child at once, so this branch is now
                    # reached once per lane rather than once per stop — the amnesty is
                    # applied per result, which is why it lives here and not at the exit.
                    #
                    # Deliberately AFTER salvage, so anything the child actually finished
                    # and left on disk is still cataloged — and BEFORE `attempted += 1`, so
                    # a channel we killed is not counted as attempted at all, matching how
                    # the two host branches `continue` before that same line.
                    #
                    # A child that failed for its own reasons AND was still running when the
                    # stop arrived is credited to the stop. That is the safe direction and
                    # the same call the busy-lock branch makes.
                    if not ok and stop_requested is not None and stop_requested.is_set():
                        logger.warning(
                            f"{city.city_id} [{provider}]: child was killed by the stop "
                            f"signal ({reason}) — not counted as a failure for this city."
                        )
                        # No `stopped = True` here: this branch is reachable only
                        # when the event is already set, and nothing ever clears
                        # it, so the submit gate sets the flag itself on the very
                        # next pass. One writer, so a later edit cannot change the
                        # gate's meaning in one place and not the other.
                        continue

                    attempted += 1
                    if ok:
                        succeeded += 1
                        db.record_attempt(conn, city.city_id, success=True, provider=provider)
                    else:
                        if record_failures:
                            db.record_attempt(
                                conn,
                                city.city_id,
                                success=False,
                                error=reason or f"subprocess failed on {today}",
                                provider=provider,
                            )
                        logger.error(f"{city.city_id} [{provider}]: collection failed")
                except BaseException as exc:
                    _log_channel_error(city.city_id, provider, exc)
                    if worker_error is None:
                        worker_error = exc
                    continue
    finally:
        if pool is not None:
            pool.shutdown(wait=True)

    if worker_error is not None:
        raise worker_error

    # Whatever is still pending was never asked of any provider: the wind-down
    # declined it. Silent when there is nothing left, and deliberately the ONLY
    # call site — see _log_stop_declined on why the wording lives in one place.
    _log_stop_declined(city.city_id, pending)

    return attempted, succeeded, skipped_budget


def _run_city_loop(
    cfg: SchedulerConfig,
    conn,
    today: date,
    due: list,
    providers_for_city: dict[str, list[str]],
    batch_deadline: float,
    sigterm_seen: threading.Event,
    max_cities: int,
) -> tuple[int, int, int, int, str | None, set[str], Counter[str], Counter[str]]:
    """Collect due cities until the city cap, the batch deadline, or SIGTERM.

    ``sigterm_seen`` is both checked here (between cities) and forwarded to
    ``_run_city_channels`` as ``stop_requested`` (between a city's channels), so
    a stop cannot launch the rest of the in-flight city's work — issue #206.

    ``max_cities`` is ``cfg.max_cities_per_day`` on a nightly run and an explicit
    ``--limit`` on an on-demand catch-up (issue #214). Required rather than
    defaulting to the config value, for the same reason ``_collect_due``'s
    ``providers`` is: the caller resolves the policy, and a default here would be
    dead code that also reads as a second opinion on what the cap is.

    Returns ``(processed, succeeded, attempted, skipped_budget, stop_reason,
    blocked_hosts, busy_hosts, deferred_channels)``; ``stop_reason`` is None when the whole due
    list was worked through. Split out of ``cmd_run_due`` so every way of ending
    the night still reaches the publish tail — an unexpected exception here is
    logged and converted into a stop reason rather than discarding a night's
    collected data (issue #167).

    ``blocked_hosts`` holds the per-IP hosts that refused us mid-night (issue
    #208). Once a host is in there its channels are skipped for the remainder of
    the run: the condition is a property of this machine, not of a city, so
    asking again with the next city cannot produce a different answer.

    ``busy_hosts`` counts channels skipped because another process on this box
    held the host lock. Deliberately NOT a breaker: that condition ends when the
    other process does, so escalating it would let a two-minute manual run cost
    the batch every Mapillary city of the night. It is still reported, because a
    city that quietly did not collect is exactly the shape of failure #145
    exists to make impossible.

    ``deferred_channels`` counts road walks stood down behind their grid
    sibling's in-flight sweep (issue #274). Reported for exactly the reason
    ``busy_hosts`` is, and counted apart from both a failure and a budget skip
    because it is neither.
    """
    processed = succeeded = attempted = skipped_budget = 0
    stop_reason: str | None = None
    blocked_hosts: set[str] = set()
    busy_hosts: Counter[str] = Counter()
    deferred_channels: Counter[str] = Counter()
    try:
        for city in due:
            if processed >= max_cities:
                # Named with the number because the cap has two sources now: the
                # config's nightly max_cities_per_day, or an explicit --limit
                # overriding it for one on-demand run (issue #214).
                stop_reason = f"city cap reached ({max_cities})"
                break
            if sigterm_seen.is_set():
                stop_reason = _STOP_REASON_SIGTERM
                break
            remaining_s = batch_deadline - time.monotonic()
            if remaining_s <= _MIN_CLAMPED_TIMEOUT_S:
                stop_reason = (
                    f"batch deadline reached ({cfg.max_batch_hours:g} h); "
                    f"{len(due) - processed:,} due cities not attempted"
                )
                break

            city_attempted, city_succeeded, city_skipped = _run_city_channels(
                cfg,
                conn,
                city,
                today,
                providers_for_city[city.city_id],
                blocked_hosts=blocked_hosts,
                busy_hosts=busy_hosts,
                deferred_channels=deferred_channels,
                batch_deadline=batch_deadline,
                stop_requested=sigterm_seen,
            )
            attempted += city_attempted
            succeeded += city_succeeded
            skipped_budget += city_skipped

            if city_attempted:
                processed += 1

            # Re-check HERE, not only at the top of the next iteration. Two
            # things sit in between, and a stop has to survive both (issue #206):
            # the inter-city sleep below, which would spend its whole interval
            # out of a stop window whose entire purpose is the publish tail
            # (a full minute of it before #306 cut the sleep to 5 s, but the
            # check is not sized to the constant and must survive it moving
            # back) — and worse,
            # PEP 475 makes time.sleep RESUME after the handler runs rather than
            # returning early, so the flag is set and ignored for the whole
            # interval; and, on the LAST due city, nothing at all, so the `for`
            # would simply end and the night would summarize as complete while
            # that city's remaining channels went uncollected.
            if sigterm_seen.is_set():
                stop_reason = _STOP_REASON_SIGTERM
                break

            # Pause only when another city is actually going to be started.
            # `due` is the full candidate list (cmd_run_due deliberately does
            # not pre-truncate it to --limit), so gating on `len(due)` alone
            # would add a trailing sleep_between_cities_s to every capped run
            # — including `--limit 1`, the one-city smoke test.
            if city_attempted and processed < min(len(due), max_cities):
                time.sleep(cfg.sleep_between_cities_s)
    except Exception:
        # One city's unexpected error must not cost the night its publish:
        # everything collected so far is already committed to the catalog but
        # stays invisible on the public site until the aggregate is rebuilt.
        # The caller still treats this as an unhealthy night (nonzero exit +
        # alert) — swallowing it here buys the publish, not silence.
        logger.exception("City loop aborted by an unexpected error")
        stop_reason = _STOP_REASON_ERROR

    return (
        processed,
        succeeded,
        attempted,
        skipped_budget,
        stop_reason,
        blocked_hosts,
        busy_hosts,
        deferred_channels,
    )


def _tail_artifact(label: str, fn, conn, data_dir: str) -> tuple[Any, str | None]:
    """
    Rebuild one published index, turning a crash into a reported error rather
    than a lost tail (issue #167).

    The tail — indexes, catalog backup, publish — is what makes a night visible,
    and #167's lesson is that it must survive every way the work before it can
    fail. That was applied to the city loop but not to the tail's own first
    statements: on 2026-08-17 a `run-due ... | tail -40` whose pipe reader had
    gone away collected 10/10 cities and published none of them, because a
    BrokenPipeError out of the aggregate's progress bar (see the disable=None
    comment in json_summarizer.generate_aggregate_v2) skipped the manifest, the
    driving-plan summary, the tail backup AND the publish.

    Continuing past a failure is safe because all three indexes are written via
    json_summarizer._write_json_gz_atomic, so a failed rebuild leaves the
    PREVIOUS good file in place — the publish that follows ships a stale-but-
    valid index, never a truncated one. That is the cheaper loss by a wide
    margin: a stale index costs a day and one `regenerate-aggregate`, while an
    unpublished night costs every artifact the night collected plus its backup.

    Returns ``(result, None)`` on success, or ``(None, error)`` — a one-line
    error for the caller to report. The error is REPORTED, not swallowed: an
    index that fails forever is exactly the kind of thing #145 was about, so the
    night alerts and exits nonzero. The result is returned because
    ``cmd_regenerate`` prints counts out of it.
    """
    try:
        return fn(conn, data_dir), None
    except Exception as e:
        logger.exception(f"{label} failed; continuing with the rest of the tail")
        return None, f"{label} failed: {type(e).__name__}: {e}"


def _finish_batch(
    cfg: SchedulerConfig,
    conn,
    summary: str,
    succeeded: int,
    attempted: int,
    today: date,
    errored: bool = False,
    backup_error: str | None = None,
    plan_error: str | None = None,
    blocked_hosts: set[str] | None = None,
    busy_hosts: Counter[str] | None = None,
) -> int:
    """Rebuild the published indexes, back up the catalog, publish, alert.

    Kept separate from the city loop so it runs no matter how the night ended
    (issue #167). ``errored`` marks a night whose loop raised: it still
    publishes what was collected, but the batch reports failure and alerts, so
    a bug in the loop can't hide behind a green exit code.

    ``backup_error`` carries the pre-flight backup's outcome (issue #145) so a
    failure that happened before the loop is still reported here, where the
    alert is sent. ``today`` is passed in rather than read from the clock so a
    long night stamps the backup with the date the batch belongs to.

    ``plan_error`` carries the driving-plan FETCH's outcome, for the same
    reason and with more urgency than the plan *summary* rebuild below. Google
    overwrites that feed in place, so a night we fail to snapshot it is
    permanently unrecoverable — while the summary is regenerable from the
    catalog whenever we like. Before this it was folded into ``summary`` and
    nothing else, so a week of blocked fetches was a week of clean green nights
    and seven snapshots gone for good.

    The index rebuilds go through ``_tail_artifact``, so a crash in one of them
    is reported (alert + nonzero exit) without costing the catalog backup and
    the publish that follow — the same #167 posture the city loop already had.

    ``blocked_hosts`` names the per-IP hosts that refused us, and ``busy_hosts``
    counts channels skipped because another local process held a host lock
    (issue #208). Both alert unconditionally and exit nonzero, for the same
    reason a failed backup does: neither records a per-city failure, so without
    this a night that collected nothing from Mapillary — because it was refused,
    or because a manual run was holding the lock — would report a clean, silent
    success. They stay separate in the subject line because the operator's next
    move differs: a block is waited out, a busy lock is somebody's stray process.
    """
    # Every index rebuild goes through _tail_artifact, which reports a failure
    # instead of propagating it — see there for why a lost tail is the worse
    # outcome. Each is wrapped SEPARATELY: they are independent files with
    # independent readers (cities.json.gz feeds the overview map,
    # streetwalks.json.gz feeds streets.html), so a broken aggregate must not
    # also cost the manifest.
    tail_errors: list[str] = []

    def rebuild(label: str, fn) -> None:
        _result, error = _tail_artifact(label, fn, conn, cfg.data_dir)
        if error:
            tail_errors.append(error)

    # Regenerate the aggregate once for the whole batch
    if succeeded > 0:
        logger.info("Regenerating aggregate cities.json.gz")
        rebuild("aggregate index", generate_aggregate_v2)
        rebuild("streetwalk manifest", generate_streetwalk_manifest)

    # Deliberately NOT gated on succeeded > 0, unlike the two above. Those
    # describe the runs the night collected, so with nothing collected there is
    # nothing to rebuild. The driving-plan summary describes Google's feed,
    # which _fetch_driving_plan_nightly refreshed BEFORE the city loop and
    # which changes on its own schedule — roughly weekly, independent of
    # whether any city was due. Gating it would leave the published plan stale
    # on exactly the quiet nights, which are most of them.
    #
    # Failure-guarded for the same reason _fetch_driving_plan_nightly is, and
    # more urgently: this runs on EVERY night, ahead of the tail backup, the
    # publish and the operator alert. Because it is ungated it is the only tail
    # work a quiet night does at all, and it touches up to ~1,200 per-run JSONs
    # on disk — so an OSError here would have taken down the backup and the
    # publish of a night that was otherwise completely healthy. That is #167's
    # failure mode, and this artifact is the least important thing in the tail:
    # a stale plan page costs a day, an unpublished night costs the runs.
    #
    # "Least important" argued only against taking the tail down, never against
    # being reported — so it goes through _tail_artifact like the two above and
    # now alerts and exits nonzero. Before that it logged and continued, which
    # meant a permanently broken plan page could rot indefinitely with every
    # night still reporting a clean success.
    logger.info("Regenerating driving_plan.json.gz")
    rebuild("driving-plan summary", generate_driving_plan_summary)

    # Sweep expired shared censuses (issue #290). Bounded by nothing else: an
    # entry is written for every census the night fetched and is not overwritten
    # until that city comes round again, ~80 days later, so a cache nobody
    # pruned would grow without limit on a host whose disk is shared with
    # Project Sidewalk. In the tail with the other best-effort steps, and
    # never allowed to cost the publish -- prune_census_cache swallows its own
    # filesystem errors for exactly that reason.
    pruned = prune_census_cache()
    if pruned:
        logger.info(f"Pruned {pruned} expired cached census(es)")
        summary += f"; pruned {pruned} cached census(es)"

    # How close the night came to the systemd unit's memory cap (issue #305).
    #
    # Read HERE, not beside the elapsed-time figure in the summary above, because
    # the aggregate rebuild a few lines up is the tail's heaviest step and on a
    # big-census night it, not the city loop, sets the peak (issue #157). That
    # ordering is pinned by a test, because it is the whole reason the call sits
    # in this function rather than next to the figure it is quoted with.
    #
    # KNOWN BLIND SPOT, named rather than argued away: memory.peak is monotonic
    # and this runs BEFORE the tail catalog backup and the publish rsync, so
    # neither can ever appear in the number. That is structural -- `summary` has
    # to be complete before _publish receives it -- and both are believed small
    # on a pool where ZFS ARC rather than the cgroup absorbs the file IO. Neither
    # has been measured, and a PR about not assuming things about this cgroup
    # should not assume that one.
    #
    # Logged as well as appended, and that is not redundancy: the "Done: ..."
    # line is emitted by cmd_run_due BEFORE this function runs, so an append to
    # `summary` reaches the [alerts] email and the publish log but never the
    # scheduler log on a healthy night. `grep 'cgroup peak'` over a week of logs
    # is the measurement #305 asks for before max_concurrent_channels is raised.
    memory_note = cgroup_memory.describe_cgroup_memory()
    if memory_note:
        logger.info(memory_note)
        summary += f"; {memory_note}"

    # Back up again now that the night's runs, diffs and walks are registered:
    # the pre-flight copy (see _backup_catalog_nightly) guarantees a copy
    # EXISTS, this one makes the retained copy reflect what the night actually
    # collected. Same dated filename, atomically replaced.
    tail_backup = catalog_backup.write_backup(conn, cfg.backup_dir, today, source_db=cfg.db_path)
    if not tail_backup.ok:
        backup_error = f"catalog backup failed: {tail_backup.error}"

    # Publish BEFORE the alert, and report its failure through the alert rather
    # than returning here. The two failures share causes — a vanished,
    # unwritable or full data_dir breaks generate_aggregate_v2 AND makes
    # sync_data_to_server.sh exit 1 — so returning early sent the operator a
    # bare "publish script FAILED" and dropped the tail_errors, the backup
    # failure and the host conditions that explain it. One complete email.
    publish_error: str | None = None
    if cfg.publish_enabled and succeeded > 0:
        rc = _publish(cfg, summary, alert_on_failure=False)
        if rc != 0:
            # Deliberately NOT "(exit {rc})": _publish returns 1 for a timeout
            # and for an unwritable log, neither of which is a status the rsync
            # produced, so naming it as one sends the operator looking up an
            # rsync exit code that never existed. What actually happened is in
            # the scheduler-log tail quoted further down this same email —
            # which is why _publish logs the script's output there (issue #218).
            publish_error = (
                f"publish step FAILED (status {rc}); see logs/publish_*.log and the log tail below"
            )

    # Operator email when the batch finished unhealthy (threshold-controlled so
    # an occasional single flaky city doesn't page every night). No-op unless
    # [alerts] enabled.
    #
    # A failed backup alerts unconditionally, threshold or not. Backups that
    # fail silently are the entire reason #145 existed: /projects/makeabilitylab
    # went unbacked-up for months because nothing was watching. This is the one
    # thing in the tail that must never degrade quietly.
    blocked_hosts = blocked_hosts or set()
    blocked_note = (
        "This host's IP was refused by "
        + _host_names(blocked_hosts)
        + ". Those channels were skipped for the rest of the night and NO city was "
        "marked failed, so they stay due and lead tomorrow's queue. If this repeats, "
        "check whether another process on this machine is collecting concurrently "
        "(issue #208)."
        if blocked_hosts
        else ""
    )

    busy_hosts = busy_hosts or Counter()
    busy_note = (
        f"{sum(busy_hosts.values())} channel(s) were skipped because another process on this "
        "machine held the lock for "
        + _host_names(busy_hosts)
        + ". Those hosts meter by IP, so the two processes together would have presented double "
        "the paced rate — which is how this machine earned its bans. NO city was marked failed; "
        "they stay due and lead tomorrow's queue. Find the other process (its pid is in "
        "locks/*.lock.owner and in the child log tail above) and check whether it should have "
        "been running at all (issue #208)."
        if busy_hosts
        else ""
    )

    failures = attempted - succeeded
    unhealthy = (
        errored,
        backup_error,
        plan_error,
        tail_errors,
        publish_error,
        blocked_hosts,
        busy_hosts,
    )
    if any(unhealthy) or should_alert(failures, cfg.alerts.failure_threshold):
        host = socket.gethostname()
        # Several can be true at once, and the subject line is often all that
        # gets read on a phone at 03:00 — so say each rather than letting one
        # mask the others.
        parts = []
        if backup_error:
            parts.append("CATALOG BACKUP FAILED")
        if publish_error:
            parts.append("PUBLISH FAILED")
        if errored:
            # Without its own part, a night that BOTH crashed in the loop and
            # failed an index reported only the index — the crash, the more
            # serious of the two, appeared nowhere in the subject.
            parts.append("LOOP CRASHED")
        if tail_errors:
            parts.append(f"{len(tail_errors)} published index(es) FAILED")
        if plan_error:
            parts.append("DRIVING-PLAN FETCH FAILED")
        if blocked_hosts:
            parts.append(f"{len(blocked_hosts)} host(s) UNAVAILABLE")
        if busy_hosts:
            parts.append(f"{sum(busy_hosts.values())} channel(s) SKIPPED (host busy)")
        # The failure count is the subject on an ordinary bad night, and noise
        # ("0 failed collection(s)") when something above already carries it.
        if failures or not any(unhealthy):
            parts.append(f"{failures} failed collection(s)")
        subject = f"{' + '.join(parts)} on {host}"
        body = (
            summary
            + (f"\n\n{backup_error}" if backup_error else "")
            + (f"\n\n{publish_error}" if publish_error else "")
            # Named individually: which index broke decides the operator's next
            # move (a `regenerate-aggregate --publish` fixes a bad rebuild, but a
            # failing driving-plan summary means Google's feed changed shape).
            + ("\n\n" + "\n".join(tail_errors) if tail_errors else "")
            + (f"\n\n{blocked_note}" if blocked_note else "")
            + (f"\n\n{busy_note}" if busy_note else "")
        )
        send_alert(
            cfg.alerts,
            subject,
            f"{body}\n\nRecent log:\n{_recent_log_tail(cfg, _BATCH_LOG_TAIL_LINES)}",
        )

    # A backup failure, a failed index rebuild, a failed publish, a missed
    # driving-plan snapshot, a blocked host or a locally-busy one makes the
    # night unhealthy even when every attempted city landed — publishing was
    # still attempted above (the #167 posture: never withhold what was
    # collected), but the unit should go red so systemd and [alerts] both show
    # it. For the two host conditions this is the ONLY signal, since neither
    # records a per-city failure by design.
    if succeeded == attempted and not any(unhealthy):
        return 0
    return 1


def _add_global_flags(p: argparse.ArgumentParser) -> None:
    """Add --config/--verbose to a parser.

    Applied to BOTH the top-level parser and every subparser so the flags are
    accepted on either side of the subcommand (``--config X run-due`` and
    ``run-due --config X`` both work — systemd/docs historically wrote it after).
    ``SUPPRESS`` defaults mean an unused copy never clobbers the value parsed at
    the other position.
    """
    p.add_argument(
        "--config",
        default=argparse.SUPPRESS,
        help=f"Path to scheduler TOML (default: {DEFAULT_CONFIG_PATH})",
    )
    p.add_argument("--verbose", action="store_true", default=argparse.SUPPRESS)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m streetscape_metadata_tracker.scheduler",
        description="Staggered GSV collection scheduler",
    )
    _add_global_flags(parser)
    sub = parser.add_subparsers(dest="command", required=True)

    _add_global_flags(sub.add_parser("status", help="Show per-city schedule and budget status"))
    _add_global_flags(sub.add_parser("assign", help="(Re)compute stagger assignments"))
    p_enroll = sub.add_parser(
        "enroll-city",
        help="Opt one city into (or out of) an opt-in channel's nightly queue (issue #248)",
    )
    _add_global_flags(p_enroll)
    p_enroll.add_argument(
        "city", nargs="?", default=None, help='City query or slug, e.g. "Krabi, Thailand"'
    )
    p_enroll.add_argument(
        "--channel",
        required=True,
        metavar="CHANNEL",
        help="The opt-in channel to enrol in. Only channels whose default membership "
        "is off are settable here; per-city exclusion on the others is cities.enabled.",
    )
    g_enroll = p_enroll.add_mutually_exclusive_group()
    g_enroll.add_argument(
        "--remove",
        action="store_true",
        help="Write an explicit non-member 0 (persists if the channel's default ever "
        "flips to member), rather than removing the setting.",
    )
    g_enroll.add_argument(
        "--clear",
        action="store_true",
        help="Clear the setting back to NULL, i.e. follow the channel default.",
    )
    p_enroll.add_argument(
        "--list",
        dest="list_only",
        action="store_true",
        help="List the channel's current members and exit; CITY is then optional. "
        "Read-only, so it accepts a default-membership channel too (the answer "
        "there is every enabled city). Cannot be combined with --remove/--clear.",
    )
    p_enroll.add_argument(
        "--all",
        dest="all_cities",
        action="store_true",
        help="Apply to every enabled city the setting would CHANGE, cheapest first "
        "(issue #282). Takes no CITY. DRY RUN unless --execute is given.",
    )
    p_enroll.add_argument(
        "--limit",
        type=int,
        default=None,
        metavar="N",
        help="With --all, take only the N cheapest candidates — one tranche of a "
        "staged widening. Reproducible: ties break on city_id.",
    )
    p_enroll.add_argument(
        "--execute",
        action="store_true",
        help="With --all, actually write. Without it --all only reports, because "
        "its blast radius is the whole catalog.",
    )
    p_regen = sub.add_parser(
        "regenerate-aggregate",
        help="Rebuild cities.json.gz from the catalog (no collection)",
    )
    _add_global_flags(p_regen)
    p_regen.add_argument(
        "--publish", action="store_true", help="Also rsync data/ to the web server afterward"
    )
    p_rec = sub.add_parser(
        "reconcile-walks",
        help="Catalog road walks that finished but were never registered",
    )
    _add_global_flags(p_rec)
    p_rec.add_argument(
        "--date", default=None, help="Walk date to scan, YYYY-MM-DD (default: today, UTC)"
    )
    p_rec.add_argument(
        "--dry-run", action="store_true", help="List what would be reconciled; no catalog writes"
    )
    p_plan = sub.add_parser(
        "fetch-driving-plan",
        help="Snapshot Google's published Street View driving-plan feed",
    )
    _add_global_flags(p_plan)
    p_plan.add_argument(
        "--force", action="store_true", help="Re-fetch even if today's snapshot exists"
    )
    p_plan.add_argument(
        "--from-file",
        default=None,
        help="Ingest saved raw feed JSON (.json or .json.gz) instead of fetching (backfill)",
    )
    p_plan.add_argument(
        "--date",
        default=None,
        help="Snapshot date YYYY-MM-DD (default: today, UTC); use with --from-file for backfill",
    )
    p_run = sub.add_parser("run-due", help="Collect today's due cities")
    _add_global_flags(p_run)
    p_run.add_argument("--dry-run", action="store_true", help="Print what would run; no downloads")
    p_run.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Process at most N cities (N >= 1; a smaller value exits "
        f"{USAGE_EXIT_CODE} rather than collecting nothing). Given explicitly, "
        "this OVERRIDES [schedule].max_cities_per_day for this run (issue #214).",
    )
    p_run.add_argument(
        "--provider",
        action="append",
        dest="providers",
        metavar="CHANNEL",
        help="Restrict this run to one or more enabled channels (repeatable, or "
        "comma-separated): gsv, gsv_streets, mapillary, mapillary_streets. With "
        "--limit, this is the supported way to run an on-demand catch-up for one "
        "provider (issue #214) — never a detached bespoke script.",
    )
    p_assess = sub.add_parser(
        "assess-city",
        help="Register a new city, walk its streets on both providers, publish, "
        "and print the deployment numbers (issue #215)",
    )
    _add_global_flags(p_assess)
    p_assess.add_argument("city", help='City query, e.g. "Newport, Kentucky"')
    p_assess.add_argument(
        "--lat", type=float, default=None, help="Grid center latitude (with --lng)"
    )
    p_assess.add_argument(
        "--lng", type=float, default=None, help="Grid center longitude (with --lat)"
    )
    p_assess.add_argument(
        "--width",
        type=float,
        default=None,
        help="Grid width in meters. Must be given with --height AND --lat/--lng, "
        "because size alone would center the grid on the OSM bounding-box "
        "midpoint rather than the city.",
    )
    p_assess.add_argument(
        "--height", type=float, default=None, help="Grid height in meters. See --width."
    )
    p_assess.add_argument(
        "--step", type=float, default=20, help="Grid spacing in meters (new cities only)"
    )
    p_assess.add_argument(
        "--provider",
        action="append",
        dest="providers",
        metavar="CHANNEL",
        help=f"Restrict to a subset of {', '.join(ASSESS_CHANNELS)} (repeatable, or "
        f"comma-separated). The GSV grid run is never part of this command.",
    )
    p_assess.add_argument(
        "--estimate",
        action="store_true",
        help="Register the city and report boundary fit and per-channel cost, then "
        "stop. No provider request is issued.",
    )
    p_assess.add_argument(
        "--yes", action="store_true", help="Skip the confirmation prompt (required on a non-TTY)"
    )
    p_assess.add_argument(
        "--no-publish", action="store_true", help="Regenerate the published JSON but do not rsync"
    )
    _add_global_flags(
        sub.add_parser(
            "notify-failure", help="Email the recent log (for a systemd OnFailure= hook)"
        )
    )
    p_bstatus = sub.add_parser(
        "backup-status",
        help="Report catalog-backup health and single-copy asset inventory (issue #145)",
    )
    _add_global_flags(p_bstatus)
    p_bstatus.add_argument(
        "--alert",
        action="store_true",
        help="Also email the report via [alerts] when unhealthy (for the "
        "out-of-band monitor timer, issue #193). Exit status is unchanged.",
    )
    p_restore = sub.add_parser(
        "restore-backup", help="Restore a dated catalog backup (refuses to clobber a live catalog)"
    )
    _add_global_flags(p_restore)
    p_restore.add_argument("backup_path", help="Path to a backups/*.backup file")
    p_restore.add_argument(
        "--to",
        dest="dest",
        default=None,
        help="Destination path (default: the configured db_path). Must not already exist.",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    # getattr fallbacks: SUPPRESS means the attr is absent unless the flag was given.
    cfg = load_scheduler_config(getattr(args, "config", None))
    setup_logging(cfg, verbose=getattr(args, "verbose", False))

    if args.command == "status":
        return cmd_status(cfg)
    if args.command == "assign":
        return cmd_assign(cfg)
    if args.command == "enroll-city":
        return cmd_enroll_city(
            cfg,
            args.city,
            channel=args.channel,
            remove=args.remove,
            clear=args.clear,
            list_only=args.list_only,
            all_cities=args.all_cities,
            limit=args.limit,
            execute=args.execute,
        )
    if args.command == "regenerate-aggregate":
        return cmd_regenerate(cfg, publish=args.publish)
    if args.command == "notify-failure":
        return cmd_notify_failure(cfg)
    if args.command == "backup-status":
        return cmd_backup_status(cfg, alert=args.alert)
    if args.command == "restore-backup":
        return cmd_restore_backup(cfg, args.backup_path, args.dest)
    if args.command == "reconcile-walks":
        target = date.fromisoformat(args.date) if args.date else None
        return cmd_reconcile_walks(cfg, target_date=target, dry_run=args.dry_run)
    if args.command == "fetch-driving-plan":
        target = date.fromisoformat(args.date) if args.date else None
        return cmd_fetch_driving_plan(
            cfg, force=args.force, from_file=args.from_file, target_date=target
        )
    if args.command == "assess-city":
        return cmd_assess_city(
            cfg,
            args.city,
            lat=args.lat,
            lng=args.lng,
            width=args.width,
            height=args.height,
            step=args.step,
            requested_providers=args.providers,
            estimate_only=args.estimate,
            assume_yes=args.yes,
            publish=not args.no_publish,
        )
    if args.command == "run-due":
        try:
            return cmd_run_due(
                cfg,
                dry_run=args.dry_run,
                limit=args.limit,
                requested_providers=args.providers,
            )
        except Exception:
            # A crash (not just a failed city) — email the traceback before the
            # process dies, so a silent nightly failure can't go unnoticed.
            send_alert(
                cfg.alerts,
                f"run-due CRASHED on {socket.gethostname()}",
                f"Uncaught exception in run-due:\n\n{traceback.format_exc()}",
            )
            raise
    # Unknown subcommand. Kept as 2 (argparse's own usage status) rather than
    # USAGE_EXIT_CODE precisely so the two stay distinguishable: a wrapper seeing
    # 2 has a malformed command line, while 64 means the command line parsed and
    # run-due rejected an argument's value.
    return 2


def _exit(rc: int) -> None:
    """Exit with ``rc``, surviving an output stream whose reader has gone away.

    CPython flushes sys.stdout/sys.stderr during finalization and, if that flush
    fails, **replaces the process exit status with 120**. That silently clobbers
    this module's whole exit-code vocabulary — 0/1, USAGE_EXIT_CODE (64), the
    host codes (75/76) and the host-busy codes (79/80) — and there is reliably
    something buffered to fail on, because setup_logging installs a
    StreamHandler(sys.stdout) whose per-emit flush errors are swallowed by
    logging's own handleError while the unwritten bytes stay in the buffer.

    So a piped `run-due` that this file's other guards carried all the way
    through a healthy night still reported failure to systemd. Flushing here
    ourselves, and pointing a broken fd at /dev/null so finalization's flush
    finds nothing to write, makes the status we computed the status we report.

    Both streams, not just stdout: finalization flushes both.
    """
    for stream in (sys.stdout, sys.stderr):
        if stream is None:
            continue  # a detached interpreter has no stream to flush
        try:
            stream.flush()
        except (BrokenPipeError, ValueError, OSError):
            # ValueError covers an already-closed stream; OSError catches the
            # rest of the EPIPE/EBADF family. A stream we cannot even take a
            # fileno() from (pytest capture, an embedded interpreter) has no
            # fd to redirect and no finalization flush to break.
            with contextlib.suppress(Exception):
                os.dup2(os.open(os.devnull, os.O_WRONLY), stream.fileno())
    sys.exit(rc)


if __name__ == "__main__":
    _exit(main())
