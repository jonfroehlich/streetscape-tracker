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

Config: TOML (see config/scheduler.toml). Requires Python 3.11+ (tomllib).
"""

import argparse
import logging
import logging.handlers
import os
import socket
import subprocess
import sys
import time
import tomllib
import traceback
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path

from tabulate import tabulate

from . import db
from .alerting import AlertConfig, send_alert, should_alert
from .download_common import redact_credentials
from .download_mapillary import estimate_tile_count
from .json_summarizer import (
    generate_aggregate_v2,
    generate_streetwalk_manifest,
    regenerate_run_json,
)
from .naming import KNOWN_PROVIDERS

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
class SchedulerConfig:
    # [schedule]
    cycle_days: int = 90
    grace_days: int = 7
    daily_request_budget: int = 10_000_000  # legacy gsv budget ([providers] overrides)
    max_cities_per_day: int = 20
    max_consecutive_failures: int = 5
    city_timeout_minutes: int = 180
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
            providers[name] = ProviderConfig(
                enabled=p.get("enabled", True),
                daily_request_budget=p.get("daily_request_budget", 250_000),
                max_requests_per_minute=p.get("max_requests_per_minute"),
                spacing_m=p.get("spacing_m", 15),
            )

    return SchedulerConfig(
        cycle_days=sched.get("cycle_days", 90),
        grace_days=sched.get("grace_days", 7),
        daily_request_budget=sched.get("daily_request_budget", 10_000_000),
        max_cities_per_day=sched.get("max_cities_per_day", 20),
        max_consecutive_failures=sched.get("max_consecutive_failures", 5),
        city_timeout_minutes=sched.get("city_timeout_minutes", 180),
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


def estimate_street_samples(conn, city: db.CityRow, spacing_m: int) -> int:
    """
    Estimated on-street sample points for a road walk, WITHOUT touching OSM.

    The collector's own ``--estimate`` is exact but fetches (and on a first walk
    downloads) the street network, which is far too expensive to do for every
    due city while merely planning a night's work. Precedence, most to least
    trustworthy:

    1. This city's last road walk — exact sample count, rescaled if the
       configured spacing changed.
    2. Its frozen OSM network's edge count (#103) × the observed samples-per-
       edge ratio at that spacing.
    3. Grid area × a street-density constant.

    Args:
        conn: open catalog connection.
        city: the city row (frozen grid geometry).
        spacing_m: along-edge sample spacing the walk will use.

    Returns:
        Estimated number of sample points (== GSV requests; Mapillary pays
        tiles instead — see estimate_requests).
    """
    spacing = max(1, int(spacing_m))

    prior = conn.execute(
        """SELECT sample_points, spacing_m FROM street_walks
           WHERE city_id = ? AND sample_points IS NOT NULL AND spacing_m > 0
           ORDER BY run_date DESC LIMIT 1""",
        (city.city_id,),
    ).fetchone()
    if prior:
        # Samples scale inversely with spacing along a fixed network length.
        return max(1, int(prior["sample_points"] * (prior["spacing_m"] / spacing)))

    network = conn.execute(
        "SELECT edge_count FROM street_networks WHERE city_id = ? ORDER BY network_id DESC LIMIT 1",
        (city.city_id,),
    ).fetchone()
    if network and network["edge_count"]:
        # Seattle: 59,218 graph edges → 247k samples at 15 m ≈ 4.2 samples per
        # edge per 15 m of spacing.
        return max(1, int(network["edge_count"] * 4.2 * (15.0 / spacing)))

    area_km2 = (city.grid_width_m / 1000.0) * (city.grid_height_m / 1000.0)
    street_km = area_km2 * _STREET_KM_PER_KM2
    return max(1, int(street_km * 1000.0 / spacing))


def estimate_requests(
    city: db.CityRow, provider: str = "gsv", conn=None, spacing_m: int = 15
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
            return max(1, int(area_km2 * _STREET_KM_PER_KM2 * 1000.0 / max(1, spacing_m)))
        return estimate_street_samples(conn, city, spacing_m)
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


def city_timeout_seconds(cfg: SchedulerConfig, city: db.CityRow, provider: str, conn=None) -> int:
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
    """
    floor = cfg.city_timeout_minutes * 60
    # Only the two per-request GSV channels are paced by request count. Both
    # Mapillary channels read a handful of tiles in seconds, so they keep the
    # flat floor. gsv_streets scales exactly like gsv — a 247k-sample city
    # (Seattle) needs ~20 minutes of querying, and a flat floor would SIGKILL
    # the biggest ones.
    if provider not in ("gsv", "gsv_streets"):
        return floor
    pc = (cfg.providers or {}).get(provider)
    rate = (pc.max_requests_per_minute if pc else None) or cfg.max_requests_per_minute
    if rate <= 0:
        return floor
    spacing = pc.spacing_m if pc else 15
    effective_rate = rate * _ACHIEVED_RATE_FRACTION
    estimated = estimate_requests(city, provider, conn=conn, spacing_m=spacing)
    paced_seconds = estimated / effective_rate * 60.0
    return int(max(floor, paced_seconds * _TIMEOUT_HEADROOM + _TIMEOUT_FIXED_SLACK_S))


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

    n_cities = conn.execute("SELECT COUNT(*) FROM cities").fetchone()[0]
    due_str = ", ".join(f"{due_counts[p]} {p}" for p in providers)
    print(f"\n{n_cities} cities; due today ({today}): {due_str}.")
    for provider in providers:
        used = db.get_api_usage(conn, today, provider)
        budget = cfg.providers[provider].daily_request_budget
        print(f"{provider} budget today: {used:,} / {budget:,} requests used.")
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


def _run_one_city(
    cfg: SchedulerConfig,
    city: db.CityRow,
    today: date,
    provider: str = "gsv",
    connection_limit: int | None = None,
    daily_budget: int = 0,
    conn=None,
) -> bool:
    """Collect one (city, channel) in a subprocess.

    Grid providers run ``streetscape_tracker.py``; street channels (issue #99)
    run the road-walk collector instead. Both are metered, timed out and
    failure-counted the same way by the caller.

    ``connection_limit`` overrides ``cfg.connection_limit`` for this run (the
    resource guard lowers it when the shared host is under pressure); None uses
    the configured default. ``daily_budget`` is the street channel's full daily
    ceiling — see _street_collect_cmd on why it is not the remainder.
    """
    conn_limit = cfg.connection_limit if connection_limit is None else connection_limit

    if is_street_channel(provider):
        cmd = _street_collect_cmd(cfg, city, today, provider, conn_limit, daily_budget)
        spacing = (cfg.providers or {}).get(provider, ProviderConfig()).spacing_m
        estimated = estimate_requests(city, provider, conn=conn, spacing_m=spacing)
        logger.info(
            f"Collecting streets for {city.city_id} [{provider}] "
            f"(~{estimated:,} requests estimated)"
        )
        logger.debug(f"Command: {' '.join(cmd)}")
        timeout_s = city_timeout_seconds(cfg, city, provider, conn=conn)
        try:
            result = subprocess.run(cmd, timeout=timeout_s, cwd=str(_PROJECT_ROOT))
            return result.returncode == 0
        except subprocess.TimeoutExpired:
            logger.error(f"{city.city_id} [{provider}]: timed out after {timeout_s // 60} minutes")
            return False

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
    timeout_s = city_timeout_seconds(cfg, city, provider)
    try:
        result = subprocess.run(cmd, timeout=timeout_s, cwd=str(_PROJECT_ROOT))
        return result.returncode == 0
    except subprocess.TimeoutExpired:
        logger.error(f"{city.city_id} [{provider}]: timed out after {timeout_s // 60} minutes")
        return False


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
                    city, provider, conn=conn, spacing_m=cfg.providers[provider].spacing_m
                )
                fits = "ok" if est <= budget_left[provider] else "OVER BUDGET (deferred)"
                print(f"  {city.city_id:60s} {provider:16s} ~{est:>9,} req  {fits}")
                budget_left[provider] -= est if est <= budget_left[provider] else 0
        return 0

    processed = succeeded = attempted = skipped_budget = 0
    for city in due:
        if processed >= cfg.max_cities_per_day:
            logger.info("Daily city cap reached; stopping for today")
            break

        ran_any = False
        for provider in providers_for_city[city.city_id]:
            budget = cfg.providers[provider].daily_request_budget
            est = estimate_requests(
                city, provider, conn=conn, spacing_m=cfg.providers[provider].spacing_m
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
            )
            ran_any = True
            attempted += 1
            # A subprocess can report failure yet still have cataloged a valid
            # run (killed in the diff/JSON tail after register_run committed);
            # salvage it rather than re-spending the whole download next cycle.
            # Street channels write no `runs` row (they catalog street_walks),
            # so there is nothing of that shape to reconcile.
            if not ok and not is_street_channel(provider):
                if _reconcile_orphaned_run(conn, cfg, city, provider, today):
                    ok = True
            if ok:
                succeeded += 1
                db.record_attempt(conn, city.city_id, success=True, provider=provider)
            else:
                db.record_attempt(
                    conn,
                    city.city_id,
                    success=False,
                    error=f"subprocess failed on {today}",
                    provider=provider,
                )
                logger.error(f"{city.city_id} [{provider}]: collection failed")

        if ran_any:
            processed += 1
            if processed < len(due):
                time.sleep(cfg.sleep_between_cities_s)

    summary = (
        f"run-due {today}: {succeeded}/{attempted} runs succeeded across "
        f"{processed} cities"
        + (f"; {skipped_budget} deferred for budget" if skipped_budget else "")
    )
    logger.info("Done: " + summary)

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
    if should_alert(failures, cfg.alerts.failure_threshold):
        send_alert(
            cfg.alerts,
            f"{failures} failed collection(s) on {socket.gethostname()}",
            f"{summary}\n\nRecent log:\n{_recent_log_tail(cfg)}",
        )

    return 0 if succeeded == attempted else 1


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
