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
    python -m streetscape_metadata_tracker.scheduler [--config PATH] run-due [--dry-run] [--limit N]
    python -m streetscape_metadata_tracker.scheduler [--config PATH] regenerate-aggregate [--publish]
    python -m streetscape_metadata_tracker.scheduler [--config PATH] reconcile-walks [--date D] [--dry-run]
    python -m streetscape_metadata_tracker.scheduler [--config PATH] fetch-driving-plan [--force] [--from-file P --date D]

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
import subprocess
import sys
import threading
import time
import tomllib
import traceback
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path

from tabulate import tabulate

from . import db, driving_plan
from .alerting import AlertConfig, send_alert, should_alert
from .download_common import redact_credentials
from .download_mapillary import estimate_tile_count
from .json_summarizer import (
    generate_aggregate_v2,
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
    the whole worldwide feed. Artifacts live OUTSIDE data/ so the publish
    rsync never sees them (its whitelist would republish Google's content).
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
    # [publish]
    publish_enabled: bool = False
    publish_script: str = str(_PROJECT_ROOT / "sync_data_to_server.sh")
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
        publish_enabled=pub.get("enabled", False),
        publish_script=pub.get("publish_script", str(_PROJECT_ROOT / "sync_data_to_server.sh")),
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
# Floor for a deadline-clamped timeout. A city is only started while the batch
# deadline still has room, so the clamp should shorten a run — never hand a
# child a timeout too short to reach its first request.
_MIN_CLAMPED_TIMEOUT_S = 300
# Stop reason for an exception escaping the city loop. Distinct from the benign
# early exits (day cap, deadline, SIGTERM) because it must still fail the run:
# the batch publishes what it collected, but the night is not healthy.
_STOP_REASON_ERROR = "unexpected error in the city loop"


@contextlib.contextmanager
def _stop_on_sigterm():
    """Turn SIGTERM into a stop *request* instead of an abrupt death.

    systemd stops a unit with SIGTERM (and escalates to SIGKILL only after
    TimeoutStopSec). Under the default handler that lands wherever the loop
    happens to be, so the night's aggregate/manifest/publish tail never runs and
    everything already collected stays unpublished (issue #167). Here it just
    sets a flag the city loop checks between cities, letting the batch wind down
    and still publish.

    Because the unit's default KillMode is control-group, the SIGTERM also
    reaches the running child, so the in-flight ``subprocess.run`` returns
    promptly rather than holding the loop for the rest of its timeout.

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
    is not actually achieved (see the constant). The derived value never drops
    below the configured floor, so small cities and the (fast, bulk-metadata)
    Mapillary provider keep the flat timeout.

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

    # Only the two per-request GSV channels are paced by request count. Both
    # Mapillary channels read a handful of tiles in seconds, so they keep the
    # flat floor. gsv_streets scales exactly like gsv — a 247k-sample city
    # (Seattle) needs ~20 minutes of querying, and a flat floor would SIGKILL
    # the biggest ones.
    if provider not in ("gsv", "gsv_streets"):
        return clamp(floor)
    pc = (cfg.providers or {}).get(provider)
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


def _publish(cfg: SchedulerConfig, context: str) -> int:
    """
    Run the publish script (rsync data/ to the web server), returning its exit
    code and emailing the operator on failure. ``context`` (e.g. the run-due
    summary) is prepended to the alert body so the email is self-explanatory.
    """
    logger.info(f"Publishing via {cfg.publish_script}")
    result = subprocess.run(["bash", cfg.publish_script], cwd=str(_PROJECT_ROOT))
    if result.returncode != 0:
        logger.error("Publish script failed")
        send_alert(
            cfg.alerts,
            f"publish script FAILED on {socket.gethostname()}",
            f"{context}\n\nPublish step exited nonzero.\n\nRecent log:\n{_recent_log_tail(cfg)}",
        )
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


def cmd_regenerate(cfg: SchedulerConfig, publish: bool = False) -> int:
    """
    Rebuild the aggregate ``cities.json.gz`` from the catalog without collecting
    anything, then optionally publish. Useful after a code change to the
    aggregate schema, a manual/back-filled run, or to refresh stale published
    data — a clean one-liner instead of an inline Python snippet.
    """
    conn = db.connect(cfg.db_path)
    logger.info("Regenerating aggregate cities.json.gz")
    agg = generate_aggregate_v2(conn, cfg.data_dir)
    manifest = generate_streetwalk_manifest(conn, cfg.data_dir)
    print(
        f"Regenerated {cfg.data_dir}/cities.json.gz ({agg['cities_count']} cities); "
        f"streetwalks.json.gz ({len(manifest['walks'])} walks)."
    )

    if publish:
        # An explicit --publish overrides [publish].enabled: the operator is
        # asking for it directly on the command line.
        if _publish(cfg, "regenerate-aggregate (manual)") != 0:
            return 1
        print("Published to the web server.")
    return 0


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
        cmd += [
            "--max-requests-per-minute",
            str(pc.max_requests_per_minute or cfg.max_requests_per_minute),
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
        why = f"exited {result.returncode}"
    except subprocess.TimeoutExpired:
        why = f"timed out after {timeout_s // 60} minutes"

    tail = _tail_lines(log_path, _CHILD_LOG_TAIL_LINES)
    message = f"{city.city_id} [{provider}]: {why}; full output in {log_path}"
    if tail:
        message += f"\n--- last {_CHILD_LOG_TAIL_LINES} lines of {log_path.name} ---\n{tail}"
    logger.error(message)
    # The log name, not the full path: the catalog outlives any given checkout
    # location, and `logs/<name>` is what an operator actually greps for.
    return CollectionOutcome(False, f"{why} (see {log_path.name})")


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
        # '--' so a display name can never be parsed as a flag
        "--",
        city.display_name,
    ]
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


def _collect_due(conn, cfg: SchedulerConfig, today: date):
    """
    Due work for today: an ordered city list (stalest-first, gsv's order
    leading since it's the expensive series) and, per city, which enabled
    providers are due. Providers pair on the same cycle day by design, so
    most cities are due for all providers at once; they only diverge after
    per-provider failures or when a provider was enabled later.
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
        for provider in cfg.enabled_providers()
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


def _fetch_driving_plan_nightly(cfg: SchedulerConfig, conn, today: date) -> str | None:
    """
    Snapshot the driving-plan feed (issue #176). Returns an error string for
    the batch summary, or None. Never raises: a broken Google feed must not
    cost a night of collection (the issue #167 lesson), and a failure here is
    advisory — the feed is an undocumented asset with no uptime contract.
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


def cmd_run_due(
    cfg: SchedulerConfig,
    dry_run: bool = False,
    limit: int | None = None,
    today: date | None = None,
) -> int:
    """
    Collect all cities due today, within per-provider budgets, publish.

    ``today`` is injectable so tests can pin a date (a wall-clock read here
    can cross UTC midnight mid-test and flake); production callers omit it.
    """
    conn = db.connect(cfg.db_path)
    if today is None:
        today = datetime.now(UTC).date()
    providers = cfg.enabled_providers()
    batch_deadline = time.monotonic() + cfg.max_batch_hours * 3600.0

    # Ensure new cities (and newly enabled providers) have stagger assignments
    db.assign_schedule(conn, cfg.cycle_days, providers=tuple(providers))

    due, providers_for_city = _collect_due(conn, cfg, today)
    if limit is not None:
        due = due[:limit]
    day_cap = min(len(due), cfg.max_cities_per_day)

    budget_str = ", ".join(f"{cfg.providers[p].daily_request_budget:,} {p}" for p in providers)
    logger.info(
        f"{len(due)} cities due on {today}; "
        f"processing up to {day_cap} within daily budgets of "
        f"{budget_str} requests"
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
        return 0

    # Snapshot the driving-plan feed BEFORE the city loop: upstream of the
    # batch deadline and any mid-loop kill, and on zero-due nights too. Cheap
    # (one request), and a failure never fails the night.
    plan_error = _fetch_driving_plan_nightly(cfg, conn, today)

    # The tail below (aggregate, manifest, backup, publish) is what makes a
    # night visible, and it only runs if the loop returns. Everything that can
    # end the loop early therefore lives inside _run_city_loop, which always
    # returns counters rather than propagating (issue #167).
    with _stop_on_sigterm() as sigterm_seen:
        processed, succeeded, attempted, skipped_budget, stop_reason = _run_city_loop(
            cfg, conn, today, due, providers_for_city, batch_deadline, sigterm_seen
        )

    if stop_reason:
        logger.info(f"Stopped early: {stop_reason}")

    summary = (
        f"run-due {today}: {succeeded}/{attempted} runs succeeded across "
        f"{processed} cities"
        + (f"; {skipped_budget} deferred for budget" if skipped_budget else "")
        + (f"; stopped early ({stop_reason})" if stop_reason else "")
        + (f"; {plan_error}" if plan_error else "")
    )
    logger.info("Done: " + summary)
    return _finish_batch(
        cfg, conn, summary, succeeded, attempted, errored=stop_reason == _STOP_REASON_ERROR
    )


def _run_city_loop(
    cfg: SchedulerConfig,
    conn,
    today: date,
    due: list,
    providers_for_city: dict[str, list[str]],
    batch_deadline: float,
    sigterm_seen,
) -> tuple[int, int, int, int, str | None]:
    """Collect due cities until the day cap, the batch deadline, or SIGTERM.

    Returns ``(processed, succeeded, attempted, skipped_budget, stop_reason)``;
    ``stop_reason`` is None when the whole due list was worked through. Split
    out of ``cmd_run_due`` so that every way of ending the night still reaches
    the publish tail — an unexpected exception here is logged and converted into
    a stop reason rather than discarding a night's collected data (issue #167).
    """
    processed = succeeded = attempted = skipped_budget = 0
    stop_reason: str | None = None
    try:
        for city in due:
            if processed >= cfg.max_cities_per_day:
                stop_reason = "daily city cap reached"
                break
            if sigterm_seen.is_set():
                stop_reason = "received SIGTERM"
                break
            remaining_s = batch_deadline - time.monotonic()
            if remaining_s <= _MIN_CLAMPED_TIMEOUT_S:
                stop_reason = (
                    f"batch deadline reached ({cfg.max_batch_hours:g} h); "
                    f"{len(due) - processed:,} due cities not attempted"
                )
                break

            ran_any = False
            for provider in providers_for_city[city.city_id]:
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
                    remaining_s=batch_deadline - time.monotonic(),
                )
                ran_any = True
                attempted += 1
                # getattr, not attribute access: tests (and any future caller)
                # may hand back a plain bool, which CollectionOutcome is
                # deliberately compatible with.
                reason = getattr(ok, "reason", None)
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
                if ok:
                    succeeded += 1
                    db.record_attempt(conn, city.city_id, success=True, provider=provider)
                else:
                    db.record_attempt(
                        conn,
                        city.city_id,
                        success=False,
                        error=reason or f"subprocess failed on {today}",
                        provider=provider,
                    )
                    logger.error(f"{city.city_id} [{provider}]: collection failed")

            if ran_any:
                processed += 1
                if processed < len(due):
                    time.sleep(cfg.sleep_between_cities_s)
    except Exception:
        # One city's unexpected error must not cost the night its publish:
        # everything collected so far is already committed to the catalog but
        # stays invisible on the public site until the aggregate is rebuilt.
        # The caller still treats this as an unhealthy night (nonzero exit +
        # alert) — swallowing it here buys the publish, not silence.
        logger.exception("City loop aborted by an unexpected error")
        stop_reason = _STOP_REASON_ERROR

    return processed, succeeded, attempted, skipped_budget, stop_reason


def _finish_batch(
    cfg: SchedulerConfig,
    conn,
    summary: str,
    succeeded: int,
    attempted: int,
    errored: bool = False,
) -> int:
    """Rebuild the published indexes, back up the catalog, publish, alert.

    Kept separate from the city loop so it runs no matter how the night ended
    (issue #167). ``errored`` marks a night whose loop raised: it still
    publishes what was collected, but the batch reports failure and alerts, so
    a bug in the loop can't hide behind a green exit code.
    """
    # Regenerate the aggregate once for the whole batch
    if succeeded > 0:
        logger.info("Regenerating aggregate cities.json.gz")
        generate_aggregate_v2(conn, cfg.data_dir)
        generate_streetwalk_manifest(conn, cfg.data_dir)

    # Nightly catalog backup (keep one rolling copy alongside the logs)
    backup_path = os.path.join(cfg.log_dir, "streetscape_tracker.db.backup")
    try:
        import sqlite3

        with sqlite3.connect(backup_path) as backup_conn:
            conn.backup(backup_conn)
        logger.info(f"Catalog backed up to {backup_path}")
    except Exception as e:
        logger.error(f"Catalog backup failed: {e}")

    if cfg.publish_enabled and succeeded > 0:
        if _publish(cfg, summary) != 0:
            return 1

    # Operator email when the batch finished unhealthy (threshold-controlled so
    # an occasional single flaky city doesn't page every night). No-op unless
    # [alerts] enabled.
    failures = attempted - succeeded
    if errored or should_alert(failures, cfg.alerts.failure_threshold):
        send_alert(
            cfg.alerts,
            f"{failures} failed collection(s) on {socket.gethostname()}",
            f"{summary}\n\nRecent log:\n{_recent_log_tail(cfg)}",
        )

    return 0 if succeeded == attempted and not errored else 1


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
    p_run.add_argument("--limit", type=int, default=None, help="Process at most N cities (testing)")
    _add_global_flags(
        sub.add_parser(
            "notify-failure", help="Email the recent log (for a systemd OnFailure= hook)"
        )
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
    if args.command == "reconcile-walks":
        target = date.fromisoformat(args.date) if args.date else None
        return cmd_reconcile_walks(cfg, target_date=target, dry_run=args.dry_run)
    if args.command == "fetch-driving-plan":
        target = date.fromisoformat(args.date) if args.date else None
        return cmd_fetch_driving_plan(
            cfg, force=args.force, from_file=args.from_file, target_date=target
        )
    if args.command == "run-due":
        try:
            return cmd_run_due(cfg, dry_run=args.dry_run, limit=args.limit)
        except Exception:
            # A crash (not just a failed city) — email the traceback before the
            # process dies, so a silent nightly failure can't go unnoticed.
            send_alert(
                cfg.alerts,
                f"run-due CRASHED on {socket.gethostname()}",
                f"Uncaught exception in run-due:\n\n{traceback.format_exc()}",
            )
            raise
    return 2


if __name__ == "__main__":
    sys.exit(main())
