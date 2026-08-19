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
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from tabulate import tabulate

from . import catalog_backup, db, driving_plan
from .alerting import AlertConfig, send_alert, should_alert
from .city_registration import (
    MAX_GRID_DIM_M,
    CityResolutionError,
    resolve_or_register_city,
)
from .download_common import (
    HOST_BY_BUSY_EXIT_CODE,
    HOST_BY_EXIT_CODE,
    HOST_LABELS,
    HOST_MAPILLARY_TILES,
    HOST_OVERPASS,
    redact_credentials,
)
from .download_mapillary import DEFAULT_TILE_REQUESTS_PER_MINUTE, estimate_tile_count
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
STREET_CHANNELS = {
    "gsv_streets": "gsv",
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
}


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
    # [download]
    batch_size: int = 100
    connection_limit: int = 50
    request_timeout_s: float = 30.0
    sleep_between_cities_s: int = 60
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
        """Enabled channel names, most expensive first.

        Order matters: a city's channels run back-to-back within one night's
        budget, so the series that can actually exhaust a budget should claim
        it before the cheap ones. gsv (grid) leads, then gsv_streets (the other
        per-request channel), then the two tile-census Mapillary channels.
        """
        rank = {"gsv": 0, "gsv_streets": 1, "mapillary": 2, "mapillary_streets": 3}
        return sorted(
            (p for p, pc in self.providers.items() if pc.enabled),
            key=lambda p: (rank.get(p, 99), p),
        )


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
            providers[name] = ProviderConfig(
                enabled=p.get("enabled", True),
                daily_request_budget=p.get("daily_request_budget", 250_000),
                max_requests_per_minute=p.get("max_requests_per_minute"),
                spacing_m=p.get("spacing_m", 15),
                network_type=network_type,
            )

    return SchedulerConfig(
        cycle_days=sched.get("cycle_days", 90),
        grace_days=sched.get("grace_days", 7),
        daily_request_budget=sched.get("daily_request_budget", 10_000_000),
        max_cities_per_day=sched.get("max_cities_per_day", 20),
        max_consecutive_failures=sched.get("max_consecutive_failures", 5),
        city_timeout_minutes=sched.get("city_timeout_minutes", 180),
        max_batch_hours=sched.get("max_batch_hours", 10.0),
        batch_size=dl.get("batch_size", 100),
        connection_limit=dl.get("connection_limit", 50),
        request_timeout_s=dl.get("request_timeout_s", 30.0),
        sleep_between_cities_s=dl.get("sleep_between_cities_s", 60),
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

    ``conn`` is required only for ``gsv_streets``; without it the sample
    estimate falls back to the area proxy.
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
    per-IP rate (see _mapillary_timeout_seconds). The derived value never drops
    below the configured floor, so small cities keep the flat timeout whatever
    their provider.

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
    if provider not in ("gsv", "gsv_streets", "mapillary", "mapillary_streets"):
        return clamp(floor)
    pc = (cfg.providers or {}).get(provider)
    if provider in ("mapillary", "mapillary_streets"):
        return clamp(_mapillary_timeout_seconds(city, provider, pc, floor))
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
    instead of sending a partial email and returning early.
    """
    cmd = ["bash", cfg.publish_script]
    if cfg.publish_local:
        cmd.append("--local")
    logger.info(f"Publishing via {' '.join(cmd[1:])}")
    os.makedirs(cfg.log_dir, exist_ok=True)
    log_path = Path(cfg.log_dir) / f"publish_{date.today().isoformat()}.log"
    # Time the rsync. It is the publish tail's largest component (~6,300 files)
    # and was its only UNMEASURED one: everything else in the tail is either
    # bounded in code (catalog_backup.BACKUP_TIMEOUT_S) or already visible in the
    # log's timestamps. The tail is exactly what the unit's TimeoutStopSec has to
    # cover when `systemctl stop` winds a night down, so this line is what any
    # future re-sizing of that number should be argued from (issue #206).
    # Monotonic rather than wall clock: an NTP step must not be able to report a
    # negative publish.
    started = time.monotonic()
    try:
        # Append, not truncate: a night can publish more than once (a manual
        # regenerate-aggregate after the batch), and the earlier attempt is
        # exactly what an operator is trying to read.
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write(f"\n===== {datetime.now(UTC).isoformat()} =====\n")
            fh.write(redact_credentials(" ".join(cmd)) + "\n\n")
            fh.flush()
            result = subprocess.run(
                cmd,
                cwd=str(_PROJECT_ROOT),
                stdout=fh,
                stderr=subprocess.STDOUT,
            )
    except OSError as e:
        # The log itself is unwritable (full or read-only disk) — which is also
        # a good reason for the publish to be about to fail. Report it as the
        # publish failing rather than taking down the tail.
        logger.error(f"Could not open {log_path} for the publish script: {e}")
        return 1

    elapsed = time.monotonic() - started
    if result.returncode != 0:
        # Elapsed on the failure line too: a publish that failed in 2 s (bad
        # path, auth) is a different incident from one that failed at 25 minutes
        # (a stalled NFS transfer), and the message alone could not tell them
        # apart.
        logger.error(
            f"Publish script failed (exit {result.returncode}) after {elapsed:.1f} s; "
            f"output in {log_path}"
        )
        if alert_on_failure:
            tail = _tail_lines(log_path, _CHILD_LOG_TAIL_LINES)
            send_alert(
                cfg.alerts,
                f"publish script FAILED on {socket.gethostname()}",
                f"{context}\n\nPublish step exited {result.returncode}.\n\n"
                f"--- last {_CHILD_LOG_TAIL_LINES} lines of {log_path.name} ---\n{tail}\n\n"
                f"Recent log:\n{_recent_log_tail(cfg)}",
            )
        return result.returncode

    logger.info(f"Published in {elapsed:.1f} s")
    return result.returncode


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

    rows = conn.execute(
        """SELECT c.city_id, c.enabled, s.provider, s.day_of_cycle,
                  s.last_success_at, s.consecutive_failures, s.last_error,
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
            provider=provider,
        )
        due_counts[provider] = len(due)
        due_pairs.update((c.city_id, provider) for c in due)

    table = [
        [
            r["city_id"],
            r["provider"] or "—",
            "yes" if r["enabled"] else "no",
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


def cmd_assign(cfg: SchedulerConfig) -> int:
    """(Re)compute the day-of-cycle stagger assignment for all cities."""
    conn = db.connect(cfg.db_path)
    providers = tuple(cfg.enabled_providers())
    n = db.assign_schedule(conn, cfg.cycle_days, providers=providers)
    print(
        f"Assigned day_of_cycle for {n} enabled cities x "
        f"{len(providers)} provider(s) over a {cfg.cycle_days}-day cycle "
        f"(~{n / max(cfg.cycle_days, 1):.1f} cities/day)."
    )
    return 0


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
    tile_rates: set[int] = set()
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
            tile_rates.add(pc.max_requests_per_minute or DEFAULT_TILE_REQUESTS_PER_MINUTE)
    if mapillary_tiles:
        # Every rate in play, not just the last channel's: the two Mapillary
        # channels hold independent [providers.*] blocks and run back-to-back, so
        # one figure beside a summed total would misreport a config that paces
        # them differently.
        rates = " and ".join(f"{r}/min" for r in sorted(tile_rates))
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
    attempted, succeeded, skipped_budget = _run_city_channels(
        cfg,
        conn,
        city,
        today,
        channels,
        blocked_hosts=blocked_hosts,
        busy_hosts=busy_hosts,
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
    #   - a budget skip counts against it. On a nightly run a skip is a normal
    #     deferral — the city rolls to tomorrow inside a 90-day cycle, and
    #     _finish_batch scores only `attempted - succeeded`. Here the skipped
    #     channel IS the job, and today is the deadline.
    #   - a refused or busy host is never clean, which _finish_batch does agree
    #     with (it alerts unconditionally on either).
    collected_everything = attempted > 0 and succeeded == attempted
    nothing_deferred = skipped_budget == 0
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
    elif channel == "mapillary_streets" and pc.max_requests_per_minute is not None:
        # Paces the tile CDN, which limits per IP rather than per token — so
        # this channel and the grid one can ban each other (issue #198). Unset
        # leaves the collector's own conservative default in force; the GSV
        # fallback above would be nonsensically large here.
        cmd += [
            "--mapillary-max-requests-per-minute",
            str(pc.max_requests_per_minute),
        ]
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
    multi-hour run in this process's memory (``capture_output=True`` would, on a
    host whose cgroup is capped at MemoryHigh=4G).

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
    """
    conn_limit = cfg.connection_limit if connection_limit is None else connection_limit

    if is_street_channel(provider):
        cmd = _street_collect_cmd(cfg, city, today, provider, conn_limit, daily_budget)
        pc = (cfg.providers or {}).get(provider) or ProviderConfig()
        estimated = estimate_requests(
            city, provider, conn=conn, spacing_m=pc.spacing_m, network_type=pc.network_type
        )
        logger.info(
            f"Collecting streets for {city.city_id} [{provider}] "
            f"(~{estimated:,} requests estimated)"
        )
        logger.debug(f"Command: {' '.join(cmd)}")
        timeout_s = city_timeout_seconds(cfg, city, provider, conn=conn, remaining_s=remaining_s)
        return _run_collection_subprocess(cfg, cmd, timeout_s, city, provider, today)

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
        rate = ((cfg.providers or {}).get(provider) or ProviderConfig()).max_requests_per_minute
        if rate is not None:
            cmd += ["--mapillary-max-requests-per-minute", str(rate)]
    # '--' so a display name can never be parsed as a flag
    cmd += ["--", city.display_name]
    logger.info(
        f"Collecting {city.city_id} [{provider}] "
        f"(~{estimate_requests(city, provider):,} requests estimated)"
    )
    logger.debug(f"Command: {' '.join(cmd)}")
    timeout_s = city_timeout_seconds(cfg, city, provider, remaining_s=remaining_s)
    return _run_collection_subprocess(cfg, cmd, timeout_s, city, provider, today)


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


def _collect_due(conn, cfg: SchedulerConfig, today: date, providers: list[str]):
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
    """
    due_by_provider = {
        provider: db.get_due_cities(
            conn,
            today=today,
            cycle_days=cfg.cycle_days,
            grace_days=cfg.grace_days,
            max_consecutive_failures=cfg.max_consecutive_failures,
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
    return ordered, providers_for_city


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

    due, providers_for_city = _collect_due(conn, cfg, today, providers)
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
    day_cap = min(len(due), max_cities)

    budget_str = ", ".join(f"{cfg.providers[p].daily_request_budget:,} {p}" for p in providers)
    filter_note = f" [--provider {','.join(providers)}]" if requested_providers is not None else ""
    logger.info(
        f"{len(due)} cities due on {today}{filter_note}; "
        f"processing up to {day_cap} within daily budgets of "
        f"{budget_str} requests"
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
                est = estimate_requests(
                    city,
                    provider,
                    conn=conn,
                    spacing_m=cfg.providers[provider].spacing_m,
                    network_type=cfg.providers[provider].network_type,
                )
                fits = "ok" if est <= budget_left[provider] else "OVER BUDGET (deferred)"
                print(f"  {city.city_id:60s} {provider:16s} ~{est:>9,} req  {fits}")
                budget_left[provider] -= est if est <= budget_left[provider] else 0
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
    # off. See the falsifier in CLAUDE.md's Mapillary budget section.
    elapsed_h = (time.monotonic() - batch_started) / 3600.0
    summary = (
        f"run-due {today}{filter_note}: {succeeded}/{attempted} runs succeeded across "
        f"{processed} cities in {elapsed_h:.2f} h"
        + (f"; {skipped_budget} deferred for budget" if skipped_budget else "")
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


def _log_stop_declined(city_id: str, declined: list[str]) -> None:
    """Name the channels a wind-down is choosing not to start (issue #206).

    Shared by BOTH of ``_run_city_channels``' stop exits, because the one an
    operator actually hits is not the one that reads like the main path. The
    unit's ``KillMode`` defaults to control-group, so a ``systemctl stop``
    reaches the in-flight child too: it dies first, and the loop therefore
    leaves via the killed-child branch and never comes back around to the
    top-of-loop check. While this message lived only at the top of the loop, a
    real stop named no declined channel at all — and with Mapillary enabled
    those are exactly the ones that would otherwise have fired into a live
    per-IP tile block (#205), i.e. usually the thing the operator typing
    ``stop`` was trying to prevent. Duplicating the wording at both exits would
    have re-opened the same gap on the next edit, so it lives here once.

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
    batch_deadline: float | None,
    stop_requested: threading.Event | None,
    record_failures: bool = True,
) -> tuple[int, int, int]:
    """
    Collect one city on each of ``providers``, in order, with every guard the
    nightly batch applies: the per-IP host breaker, both daily-budget checks,
    the resource guard, the stop signal, orphan salvage, and cadence
    bookkeeping.

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

    ``batch_deadline`` (a ``time.monotonic()`` value) clamps each child's
    timeout so no collection outlives the window reserved for the publish tail;
    None means no deadline, which is right for a single-city operator run. It has
    no default deliberately — the same reason ``_collect_due``'s ``providers``
    doesn't (issue #214): a caller that silently inherited "no deadline" would
    lose the guard that keeps a night from being SIGKILLed before it publishes.

    ``stop_requested`` is the wind-down flag from ``_stop_on_sigterm``, and has
    no default for exactly the same reason: a caller that silently inherited
    "nothing can stop this" would look correct until someone typed
    ``systemctl stop``, which is the one moment it matters (issue #206). ``None``
    means no supervisor can ask this run to stop — right for an operator's
    foreground command, wrong for a batch. It is named for the *contract* rather
    than the mechanism so ``None`` reads as "nothing can ask us to stop" rather
    than "we don't know whether SIGTERM was seen".

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
    for i, provider in enumerate(providers):
        # A stop was requested (systemd's SIGTERM) while this city was in
        # flight. BREAK, not continue: every other guard in this loop is a
        # property of one CHANNEL — this host refused us, this channel's budget
        # is spent — so a later channel can still answer differently. A stop is
        # a property of the PROCESS, so none of them can. It sits FIRST, above
        # the blocked-host guard, for that guard's own stated reason: there is
        # no point pricing work we already know we will not do.
        #
        # This is NOT the exit a real `systemctl stop` usually takes — see
        # _log_stop_declined, which both exits share. It fires when the stop
        # lands in a gap between children (the budget queries, the resource
        # guard) or when a child finished before the signal reached it.
        if stop_requested is not None and stop_requested.is_set():
            _log_stop_declined(city.city_id, providers[i:])
            break

        # A host this channel needs already refused us during this run. Skip
        # (not break) for the same reason the budget guard skips: the other
        # channels of this and later cities are still worth running.
        # Deliberately BEFORE the budget checks — there is no point pricing
        # work we already know we will not do.
        unavailable = blocked_hosts.intersection(CHANNEL_HOSTS.get(provider, ()))
        if unavailable:
            logger.info(
                f"{city.city_id} [{provider}]: skipping — "
                f"{_host_names(unavailable)} "
                f"already refused this host."
            )
            continue

        budget = cfg.providers[provider].daily_request_budget
        est = estimate_requests(
            city,
            provider,
            conn=conn,
            spacing_m=cfg.providers[provider].spacing_m,
            network_type=cfg.providers[provider].network_type,
        )
        if est > budget:
            # This city can NEVER fit the daily budget — skipping (not
            # breaking) so it can't starve every smaller city behind it
            # in the stalest-first queue. Needs a manual run or a config
            # change; surfaced loudly so it doesn't rot silently.
            logger.warning(
                f"{city.city_id} [{provider}]: ~{est:,} estimated requests "
                f"exceeds the entire daily budget ({budget:,}). "
                f"Skipping — run manually with streetscape_tracker.py --force, "
                f"raise daily_request_budget, or set enabled=0."
            )
            skipped_budget += 1
            continue

        used = db.get_api_usage(conn, today, provider)
        if used + est > budget:
            # Doesn't fit in what's LEFT today — try the next (smaller)
            # city rather than ending the day; this one rolls to tomorrow
            # when the budget is fresh.
            logger.info(
                f"{city.city_id} [{provider}] (~{est:,} req) doesn't fit "
                f"remaining budget ({budget - used:,} left); skipping."
            )
            skipped_budget += 1
            continue

        conn_limit, throttle_reason = plan_connection_limit(
            cfg.connection_limit, read_system_pressure(), cfg.resource_guard
        )
        if throttle_reason:
            logger.info(
                f"Resource guard: {throttle_reason}; connection limit "
                f"{cfg.connection_limit} → {conn_limit} for {city.city_id} [{provider}]"
            )
        ok = _run_one_city(
            cfg,
            city,
            today,
            provider,
            connection_limit=conn_limit,
            # The channel's FULL ceiling, not `budget - used`: the street
            # collector subtracts today's spend from this itself, so
            # passing the remainder would count it twice.
            daily_budget=budget,
            conn=conn,
            # Never let one child run past the batch deadline; the point
            # of the deadline is to reserve time for the publish tail.
            remaining_s=None if batch_deadline is None else batch_deadline - time.monotonic(),
        )
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

        # This child died of the SIGTERM that is stopping US. The unit's default
        # KillMode is control-group, so a `systemctl stop` reaches the whole
        # cgroup — the 2026-08-13 log shows the in-flight child as "exited -15",
        # which is in neither HOST_BY_EXIT_CODE nor HOST_BY_BUSY_EXIT_CODE and
        # therefore reads as an ordinary collection failure. Charging it to the
        # city would be wrong twice over, and both are the argument the blocked-
        # and busy-host branches above already make: it burns one of five
        # `consecutive_failures` that ONLY a success ever resets, and it makes
        # attempted > succeeded, so every deliberate stop would email a failure
        # alert and end the unit red (issue #206).
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
            # `i + 1`, not `i`: this channel WAS started, and the line above
            # already accounts for it. Everything after it is what the stop
            # declines, and this is the exit that actually reaches an operator's
            # log — see _log_stop_declined.
            _log_stop_declined(city.city_id, providers[i + 1 :])
            break

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
) -> tuple[int, int, int, int, str | None, set[str], Counter[str]]:
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
    blocked_hosts, busy_hosts)``; ``stop_reason`` is None when the whole due
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
    """
    processed = succeeded = attempted = skipped_budget = 0
    stop_reason: str | None = None
    blocked_hosts: set[str] = set()
    busy_hosts: Counter[str] = Counter()
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
            # the inter-city sleep below, which would spend a full minute of a
            # stop window whose entire purpose is the publish tail — and worse,
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

    return processed, succeeded, attempted, skipped_budget, stop_reason, blocked_hosts, busy_hosts


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
            publish_error = f"publish script FAILED (exit {rc}); see logs/publish_*.log"

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
        send_alert(cfg.alerts, subject, f"{body}\n\nRecent log:\n{_recent_log_tail(cfg)}")

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
