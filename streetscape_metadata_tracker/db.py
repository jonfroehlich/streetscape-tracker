"""
SQLite catalog for Streetscape Tracker temporal data.

The database (default: data/streetscape_tracker.db) is the operational source of
truth for city identity, frozen grid geometry, collection runs, run-to-run
diffs, the daily API-request budget ledger, and scheduler state. It is a
local catalog only — published artifacts (csv.gz / json.gz) are generated
from it and the raw files; the DB itself is never synced to the web server.

Key design point: grid geometry (center, dims, step) is FROZEN in the
cities table at registration. Future runs read geometry from the DB and
never re-geocode, so grids align exactly across quarters and diffs are
meaningful.

Uses stdlib sqlite3 with WAL mode; no ORM. All timestamps are UTC ISO 8601
strings; all dates are 'YYYY-MM-DD' strings.
"""

import hashlib
import logging
import os
import sqlite3
from dataclasses import dataclass
from datetime import UTC, date, datetime

from .naming import sanitize_city_query_str

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 8

_SCHEMA = """
CREATE TABLE IF NOT EXISTS cities (
    city_id        TEXT PRIMARY KEY,
    display_name   TEXT NOT NULL,
    city_name      TEXT NOT NULL,
    state_name     TEXT,
    state_code     TEXT,
    country_name   TEXT,
    country_code   TEXT,
    center_lat     REAL NOT NULL,
    center_lon     REAL NOT NULL,
    grid_width_m   INTEGER NOT NULL,
    grid_height_m  INTEGER NOT NULL,
    step_m         INTEGER NOT NULL,
    created_at     TEXT NOT NULL,
    enabled        INTEGER NOT NULL DEFAULT 1,
    notes          TEXT
);

CREATE TABLE IF NOT EXISTS city_aliases (
    alias_slug     TEXT PRIMARY KEY,
    city_id        TEXT NOT NULL REFERENCES cities(city_id)
);

CREATE TABLE IF NOT EXISTS runs (
    run_id              INTEGER PRIMARY KEY,
    city_id             TEXT NOT NULL REFERENCES cities(city_id),
    provider            TEXT NOT NULL DEFAULT 'gsv',
    run_date            TEXT NOT NULL,
    csv_filename        TEXT NOT NULL UNIQUE,
    json_filename       TEXT,
    is_baseline         INTEGER NOT NULL DEFAULT 0,
    started_at          TEXT,
    finished_at         TEXT,
    duration_seconds    REAL,
    total_points        INTEGER,
    status_ok           INTEGER,
    status_no_date      INTEGER,
    status_zero_results INTEGER,
    status_flat_only    INTEGER,
    status_other        INTEGER,
    unique_panos        INTEGER,
    unique_google_panos INTEGER,
    coverage_rate_pct   REAL,
    any_imagery_coverage_rate_pct REAL,
    num_flat_images     INTEGER,
    oldest_capture_date TEXT,
    newest_capture_date TEXT,
    median_pano_age_years REAL,
    api_requests        INTEGER,
    UNIQUE (city_id, provider, run_date)
);
CREATE INDEX IF NOT EXISTS idx_runs_city_date
    ON runs(city_id, provider, run_date DESC);

CREATE TABLE IF NOT EXISTS run_diffs (
    diff_id                INTEGER PRIMARY KEY,
    city_id                TEXT NOT NULL REFERENCES cities(city_id),
    from_run_id            INTEGER NOT NULL REFERENCES runs(run_id),
    to_run_id              INTEGER NOT NULL REFERENCES runs(run_id),
    grid_aligned           INTEGER NOT NULL,
    panos_added            INTEGER,
    panos_removed          INTEGER,
    panos_persisted        INTEGER,
    capture_date_changed   INTEGER,
    points_gained_coverage INTEGER,
    points_lost_coverage   INTEGER,
    coverage_delta_pct     REAL,
    detail_filename        TEXT,
    computed_at            TEXT NOT NULL,
    UNIQUE (from_run_id, to_run_id)
);

-- Daily request-budget ledger. Besides the collection providers ('gsv',
-- 'mapillary'), the strings 'gsv_streets' / 'mapillary_streets' are reserved
-- as isolated budget channels for street-coverage collection (issue #99):
-- separate API keys so street experiments can't exhaust the production grid
-- collector's quota. Reservation is by convention — no CHECK constraint.
CREATE TABLE IF NOT EXISTS api_usage (
    usage_date  TEXT NOT NULL,
    provider    TEXT NOT NULL DEFAULT 'gsv',
    requests    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (usage_date, provider)
);

CREATE TABLE IF NOT EXISTS schedule_state (
    city_id              TEXT NOT NULL REFERENCES cities(city_id),
    provider             TEXT NOT NULL DEFAULT 'gsv',
    day_of_cycle         INTEGER NOT NULL,
    last_attempt_at      TEXT,
    last_success_at      TEXT,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    last_error           TEXT,
    PRIMARY KEY (city_id, provider)
);

-- Historical-dates harvests (issue #2). One row per city per harvest pass of
-- the full official-Google capture history, sourced from an unpublished
-- endpoint with no guarantee it keeps working. This catalogs the harvest (like
-- runs catalogs snapshots); the panos themselves live in the sibling csv.gz.
-- Out-of-band from the sampled run series, so it is a separate table, not a
-- 'provider' in runs.
CREATE TABLE IF NOT EXISTS history_harvests (
    harvest_id           INTEGER PRIMARY KEY,
    city_id              TEXT NOT NULL REFERENCES cities(city_id),
    provider             TEXT NOT NULL DEFAULT 'gsv',
    harvest_date         TEXT NOT NULL,
    csv_filename         TEXT NOT NULL UNIQUE,
    grid_points_queried  INTEGER,
    unique_panos         INTEGER,
    oldest_capture_date  TEXT,
    newest_capture_date  TEXT,
    api_requests         INTEGER,
    started_at           TEXT,
    finished_at          TEXT,
    UNIQUE (city_id, provider, harvest_date)
);

-- Frozen OSM street networks (issue #103). A provider-agnostic city asset,
-- like frozen grid geometry: fetched once per city (bbox derived from the
-- frozen grid) and shared by all providers and street analyses, so coverage
-- numbers stay comparable across runs. network_type distinguishes future
-- 'walk'/'all' networks (issue #99) from the default 'drive'. The GraphML
-- itself lives unpublished under data/osm_cache/; a --refresh re-fetch
-- replaces the row (no history).
CREATE TABLE IF NOT EXISTS street_networks (
    network_id       INTEGER PRIMARY KEY,
    city_id          TEXT NOT NULL REFERENCES cities(city_id),
    network_type     TEXT NOT NULL DEFAULT 'drive',
    graphml_filename TEXT NOT NULL UNIQUE,
    node_count       INTEGER,
    edge_count       INTEGER,
    osmnx_version    TEXT,
    fetched_at       TEXT NOT NULL,
    UNIQUE (city_id, network_type)
);

-- Road-walk street-coverage collection runs (issue #99). A SECOND collection
-- modality, distinct from grid `runs`: it walks each frozen OSM edge, samples
-- on-street points every `spacing_m` metres, and queries GSV per point, so its
-- unit of observation is the per-edge fractional coverage in the sibling
-- coverage GeoJSON (not grid points). Kept in its own table for the same reason
-- history_harvests is: different unit, not a 'provider' in `runs`. `provider`
-- is the imagery provider ('gsv'); the request budget is metered separately in
-- api_usage under the isolated 'gsv_streets' channel (issue #141).
CREATE TABLE IF NOT EXISTS street_walks (
    walk_id                INTEGER PRIMARY KEY,
    city_id                TEXT NOT NULL REFERENCES cities(city_id),
    provider               TEXT NOT NULL DEFAULT 'gsv',
    run_date               TEXT NOT NULL,
    csv_filename           TEXT NOT NULL UNIQUE,
    coverage_filename      TEXT,
    network_type           TEXT NOT NULL DEFAULT 'drive',
    spacing_m              REAL,
    match_dist_m           REAL,
    sample_points          INTEGER,
    edges_total            INTEGER,
    edges_fully_covered    INTEGER,
    mean_edge_coverage     REAL,
    coverage_pct_by_length REAL,
    -- Any-imagery street coverage (v8): 360° + flat/perspective. Equal to
    -- coverage_pct_by_length for GSV, which never emits FLAT_ONLY; NULL on
    -- walks collected before the column existed.
    coverage_pct_by_length_any REAL,
    api_requests           INTEGER,
    started_at             TEXT,
    finished_at            TEXT,
    UNIQUE (city_id, provider, run_date)
);
"""

# v1 → v2: add the provider dimension. Three tables need constraint changes
# (a widened UNIQUE / composite PKs), which SQLite only supports via the
# standard rebuild: create new, copy with provider='gsv', drop, rename.
_MIGRATE_V1_TO_V2 = """
CREATE TABLE runs_v2 (
    run_id              INTEGER PRIMARY KEY,
    city_id             TEXT NOT NULL REFERENCES cities(city_id),
    provider            TEXT NOT NULL DEFAULT 'gsv',
    run_date            TEXT NOT NULL,
    csv_filename        TEXT NOT NULL UNIQUE,
    json_filename       TEXT,
    is_baseline         INTEGER NOT NULL DEFAULT 0,
    started_at          TEXT,
    finished_at         TEXT,
    duration_seconds    REAL,
    total_points        INTEGER,
    status_ok           INTEGER,
    status_zero_results INTEGER,
    status_other        INTEGER,
    unique_panos        INTEGER,
    unique_google_panos INTEGER,
    coverage_rate_pct   REAL,
    oldest_capture_date TEXT,
    newest_capture_date TEXT,
    median_pano_age_years REAL,
    api_requests        INTEGER,
    UNIQUE (city_id, provider, run_date)
);
INSERT INTO runs_v2
    (run_id, city_id, provider, run_date, csv_filename, json_filename,
     is_baseline, started_at, finished_at, duration_seconds, total_points,
     status_ok, status_zero_results, status_other, unique_panos,
     unique_google_panos, coverage_rate_pct, oldest_capture_date,
     newest_capture_date, median_pano_age_years, api_requests)
SELECT run_id, city_id, 'gsv', run_date, csv_filename, json_filename,
       is_baseline, started_at, finished_at, duration_seconds, total_points,
       status_ok, status_zero_results, status_other, unique_panos,
       unique_google_panos, coverage_rate_pct, oldest_capture_date,
       newest_capture_date, median_pano_age_years, api_requests
FROM runs;
DROP TABLE runs;
ALTER TABLE runs_v2 RENAME TO runs;
CREATE INDEX idx_runs_city_date ON runs(city_id, provider, run_date DESC);

CREATE TABLE api_usage_v2 (
    usage_date  TEXT NOT NULL,
    provider    TEXT NOT NULL DEFAULT 'gsv',
    requests    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (usage_date, provider)
);
INSERT INTO api_usage_v2 (usage_date, provider, requests)
SELECT usage_date, 'gsv', requests FROM api_usage;
DROP TABLE api_usage;
ALTER TABLE api_usage_v2 RENAME TO api_usage;

CREATE TABLE schedule_state_v2 (
    city_id              TEXT NOT NULL REFERENCES cities(city_id),
    provider             TEXT NOT NULL DEFAULT 'gsv',
    day_of_cycle         INTEGER NOT NULL,
    last_attempt_at      TEXT,
    last_success_at      TEXT,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    last_error           TEXT,
    PRIMARY KEY (city_id, provider)
);
INSERT INTO schedule_state_v2
    (city_id, provider, day_of_cycle, last_attempt_at, last_success_at,
     consecutive_failures, last_error)
SELECT city_id, 'gsv', day_of_cycle, last_attempt_at, last_success_at,
       consecutive_failures, last_error
FROM schedule_state;
DROP TABLE schedule_state;
ALTER TABLE schedule_state_v2 RENAME TO schedule_state;
"""

# v3 -> v4: split "present but dateless" panos out of the status_other error
# bucket so they can count toward coverage and pano totals. Purely additive —
# a plain ADD COLUMN, no table rebuild. Existing rows get status_no_date=NULL
# (unknown until scripts/recompute_run_stats.py backfills it from the CSVs and
# recomputes coverage_rate_pct / unique_panos).
_MIGRATE_V3_TO_V4 = """
ALTER TABLE runs ADD COLUMN status_no_date INTEGER;
"""

# v6 -> v7: stratify Mapillary coverage by imagery type (issue #116). Flat-only
# grid points now get a FLAT_ONLY row instead of ZERO_RESULTS, so runs gain a
# status_flat_only bucket (split out of status_other, like NO_DATE in v3), an
# any_imagery_coverage_rate_pct (360° + flat footprint), and num_flat_images
# (flat census magnitude). Purely additive — plain ADD COLUMNs, no rebuild.
# Existing (pre-v7) runs get NULL: their CSVs carry no FLAT_ONLY rows, so
# status_flat_only=0 / any_imagery==coverage would be recoverable via
# scripts/recompute_run_stats.py, but num_flat_images is not (the flat census
# was never collected), so it stays NULL.
_MIGRATE_V6_TO_V7 = """
ALTER TABLE runs ADD COLUMN status_flat_only INTEGER;
ALTER TABLE runs ADD COLUMN any_imagery_coverage_rate_pct REAL;
ALTER TABLE runs ADD COLUMN num_flat_images INTEGER;
"""


@dataclass
class CityRow:
    """A row from the cities table."""

    city_id: str
    display_name: str
    city_name: str
    state_name: str | None
    state_code: str | None
    country_name: str | None
    country_code: str | None
    center_lat: float
    center_lon: float
    grid_width_m: int
    grid_height_m: int
    step_m: int
    created_at: str
    enabled: bool
    notes: str | None


@dataclass
class RunRow:
    """A row from the runs table."""

    run_id: int
    city_id: str
    provider: str
    run_date: str
    csv_filename: str
    json_filename: str | None
    is_baseline: bool
    started_at: str | None
    finished_at: str | None
    duration_seconds: float | None
    total_points: int | None
    status_ok: int | None
    status_no_date: int | None
    status_zero_results: int | None
    status_flat_only: int | None
    status_other: int | None
    unique_panos: int | None
    unique_google_panos: int | None
    coverage_rate_pct: float | None
    any_imagery_coverage_rate_pct: float | None
    num_flat_images: int | None
    oldest_capture_date: str | None
    newest_capture_date: str | None
    median_pano_age_years: float | None
    api_requests: int | None


def utc_now_iso() -> str:
    """Current UTC time as an ISO 8601 string."""
    return datetime.now(UTC).isoformat()


def get_default_db_path(data_dir: str) -> str:
    """The catalog lives alongside the data it describes."""
    return os.path.join(data_dir, "streetscape_tracker.db")


# Legacy catalog filename from before the GSV Tracker → Streetscape Tracker
# rename. connect() transparently migrates it to the new name on first open so
# existing local/deployed catalogs are never orphaned.
_LEGACY_DB_FILENAME = "gsv_tracker.db"


def _migrate_legacy_db_if_present(db_path: str) -> None:
    """If the target DB is absent but a legacy ``gsv_tracker.db`` sits alongside
    it, rename the legacy file (and any WAL/SHM sidecars) to the new name. This
    is a no-op once migrated or when starting from a fresh catalog."""
    if os.path.exists(db_path):
        return
    legacy_path = os.path.join(os.path.dirname(os.path.abspath(db_path)), _LEGACY_DB_FILENAME)
    if not os.path.exists(legacy_path):
        return
    for suffix in ("", "-wal", "-shm"):
        src = legacy_path + suffix
        if os.path.exists(src):
            os.rename(src, db_path + suffix)
    logger.info("Migrated legacy catalog %s -> %s", legacy_path, db_path)


def connect(db_path: str) -> sqlite3.Connection:
    """
    Open (creating if needed) the catalog database with WAL mode and
    foreign keys enabled, and ensure the schema exists.
    """
    os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
    _migrate_legacy_db_if_present(db_path)
    conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=10000")
    init_schema(conn)
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    """Create tables if needed, migrate old schemas, stamp the version."""
    user_version = conn.execute("PRAGMA user_version").fetchone()[0]
    if user_version > SCHEMA_VERSION:
        raise RuntimeError(
            f"Database schema version {user_version} is newer than this code "
            f"supports ({SCHEMA_VERSION}). Update the code before proceeding."
        )
    if user_version == 1:
        _migrate_v1_to_v2(conn)
        user_version = 2
    # v2 -> v3 is purely additive (the history_harvests table), so it needs no
    # rebuild migration: the CREATE TABLE IF NOT EXISTS in _SCHEMA creates it on
    # any older catalog, and the version stamp below records the upgrade.
    if user_version == 2:
        user_version = 3
    if user_version == 3:
        _migrate_v3_to_v4(conn)
        user_version = 4
    # v4 -> v5 (street_networks) and v5 -> v6 (street_walks, issue #99) are both
    # purely additive, so like v2 -> v3 they need no rebuild migration: the
    # CREATE TABLE IF NOT EXISTS in _SCHEMA creates the new table on any older
    # catalog, and the version stamp below records the upgrade.
    if user_version in (4, 5, 6):
        _migrate_v6_to_v7(conn)
        user_version = 7
    if user_version == 7:
        _migrate_v7_to_v8(conn)
    conn.executescript(_SCHEMA)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    conn.commit()


def _migrate_v1_to_v2(conn: sqlite3.Connection) -> None:
    """
    Rebuild runs/api_usage/schedule_state with the provider dimension; all
    existing rows become provider='gsv'. Foreign keys are disabled for the
    rebuild (DROP TABLE runs would otherwise trip run_diffs' references).
    """
    logger.info("Migrating catalog schema v1 -> v2 (adding provider dimension)")
    conn.execute("PRAGMA foreign_keys=OFF")
    try:
        conn.executescript(_MIGRATE_V1_TO_V2)
        violations = conn.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise RuntimeError(f"Schema migration produced foreign key violations: {violations}")
        conn.commit()
    finally:
        conn.execute("PRAGMA foreign_keys=ON")


def _migrate_v3_to_v4(conn: sqlite3.Connection) -> None:
    """Add the status_no_date column to runs (additive; no table rebuild).

    Idempotent: skips the ADD COLUMN if the column already exists, so a
    catalog created fresh at the current schema (runs already carries the
    column) can still be stamped forward through this step.
    """
    cols = {r[1] for r in conn.execute("PRAGMA table_info(runs)").fetchall()}
    if "status_no_date" in cols:
        return
    logger.info("Migrating catalog schema v3 -> v4 (adding runs.status_no_date)")
    conn.executescript(_MIGRATE_V3_TO_V4)
    conn.commit()


def _migrate_v6_to_v7(conn: sqlite3.Connection) -> None:
    """Add the imagery-type stratification columns to runs (issue #116).

    Additive (status_flat_only, any_imagery_coverage_rate_pct,
    num_flat_images); no table rebuild. Idempotent: skips the ADD COLUMNs if
    they already exist, so a catalog created fresh at the current schema can
    still be stamped forward through this step.
    """
    cols = {r[1] for r in conn.execute("PRAGMA table_info(runs)").fetchall()}
    if "status_flat_only" in cols:
        return
    logger.info(
        "Migrating catalog schema v6 -> v7 "
        "(adding runs.status_flat_only / any_imagery_coverage_rate_pct / num_flat_images)"
    )
    conn.executescript(_MIGRATE_V6_TO_V7)
    conn.commit()


def _migrate_v7_to_v8(conn: sqlite3.Connection) -> None:
    """Add street_walks.coverage_pct_by_length_any (any-imagery road coverage).

    Additive; no table rebuild. Idempotent like _migrate_v6_to_v7, so a catalog
    created fresh at the current schema can still be stamped forward. The
    column is nullable: walks collected before it existed keep NULL rather than
    claiming their 360° number covers flat imagery too.
    """
    cols = {r[1] for r in conn.execute("PRAGMA table_info(street_walks)").fetchall()}
    if "coverage_pct_by_length_any" in cols or not cols:
        # Absent table → the CREATE TABLE in _SCHEMA below builds it current.
        return
    logger.info("Migrating catalog schema v7 -> v8 (street_walks.coverage_pct_by_length_any)")
    conn.execute("ALTER TABLE street_walks ADD COLUMN coverage_pct_by_length_any REAL")
    conn.commit()


def derive_city_id(city_name: str, state_name: str | None, country_name: str | None) -> str:
    """
    Canonical city id: the sanitized slug of the full (never abbreviated)
    location names, e.g. 'albany--new-york--united-states'. Derived once at
    registration; thereafter it is a stored key immune to geocoder drift.
    """
    components = [c for c in (city_name, state_name, country_name) if c]
    return sanitize_city_query_str(", ".join(components))


def register_city(
    conn: sqlite3.Connection,
    *,
    city_name: str,
    state_name: str | None,
    state_code: str | None,
    country_name: str | None,
    country_code: str | None,
    center_lat: float,
    center_lon: float,
    grid_width_m: float,
    grid_height_m: float,
    step_m: float,
    notes: str | None = None,
    enabled: bool = True,
) -> str:
    """
    Register a city with its frozen grid geometry. Idempotent: if the city
    already exists, the existing row wins (geometry is never overwritten).

    ``enabled=False`` registers the city outside the scheduler rotation (e.g.
    sampling-frame cities awaiting boundary vetting, issue #110).

    Returns the canonical city_id.
    """
    city_id = derive_city_id(city_name, state_name, country_name)
    display_parts = [c for c in (city_name, state_name, country_name) if c]
    conn.execute(
        """INSERT OR IGNORE INTO cities
           (city_id, display_name, city_name, state_name, state_code,
            country_name, country_code, center_lat, center_lon,
            grid_width_m, grid_height_m, step_m, created_at, enabled, notes)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            city_id,
            ", ".join(display_parts),
            city_name,
            state_name,
            state_code,
            country_name,
            country_code,
            center_lat,
            center_lon,
            int(grid_width_m),
            int(grid_height_m),
            int(step_m),
            utc_now_iso(),
            int(enabled),
            notes,
        ),
    )
    conn.commit()
    return city_id


def update_city_geometry(
    conn: sqlite3.Connection,
    *,
    city_id: str,
    center_lat: float,
    center_lon: float,
    grid_width_m: float,
    grid_height_m: float,
    notes: str | None = None,
) -> None:
    """
    Overwrite a city's frozen grid geometry (center + dimensions).

    Geometry is normally immutable (register_city is INSERT OR IGNORE); this is
    the deliberate escape hatch used only by the one-time boundary
    re-registration (issue #91) to correct grids that were centered on the
    geocoder's point instead of the OSM bounding-box midpoint. Callers own the
    policy of when this is safe — normal collection runs never mutate geometry.

    If ``notes`` is given it is appended (newline-separated) to any existing
    notes so the correction leaves an audit trail on the row.
    """
    if notes:
        existing = conn.execute("SELECT notes FROM cities WHERE city_id = ?", (city_id,)).fetchone()
        prior = existing["notes"] if existing and existing["notes"] else None
        notes = f"{prior}\n{notes}" if prior else notes

    cur = conn.execute(
        """UPDATE cities
           SET center_lat = ?, center_lon = ?,
               grid_width_m = ?, grid_height_m = ?,
               notes = COALESCE(?, notes)
           WHERE city_id = ?""",
        (center_lat, center_lon, int(grid_width_m), int(grid_height_m), notes, city_id),
    )
    if cur.rowcount == 0:
        raise KeyError(f"Cannot update geometry: unknown city_id '{city_id}'")
    conn.commit()


def add_alias(conn: sqlite3.Connection, alias_slug: str, city_id: str) -> None:
    """Map a legacy filename slug (e.g. 'albany--ny') to a canonical city."""
    conn.execute(
        "INSERT OR IGNORE INTO city_aliases (alias_slug, city_id) VALUES (?, ?)",
        (alias_slug, city_id),
    )
    conn.commit()


def resolve_city(conn: sqlite3.Connection, query: str) -> CityRow | None:
    """
    Resolve a city query string or slug to its catalog row.

    Tries, in order: exact city_id match on the sanitized query, an alias
    match, then a display_name match (case-insensitive).
    """
    slug = sanitize_city_query_str(query)
    row = conn.execute("SELECT * FROM cities WHERE city_id = ?", (slug,)).fetchone()
    if row is None:
        row = conn.execute(
            """SELECT c.* FROM cities c
               JOIN city_aliases a ON a.city_id = c.city_id
               WHERE a.alias_slug = ?""",
            (slug,),
        ).fetchone()
    if row is None:
        row = conn.execute(
            "SELECT * FROM cities WHERE lower(display_name) = lower(?)", (query.strip(),)
        ).fetchone()
    if row is None:
        return None
    d = dict(row)
    d["enabled"] = bool(d["enabled"])
    return CityRow(**d)


def register_run(
    conn: sqlite3.Connection,
    *,
    city_id: str,
    run_date: date,
    csv_filename: str,
    provider: str = "gsv",
    json_filename: str | None = None,
    is_baseline: bool = False,
    started_at: str | None = None,
    finished_at: str | None = None,
    duration_seconds: float | None = None,
    total_points: int | None = None,
    status_ok: int | None = None,
    status_no_date: int | None = None,
    status_zero_results: int | None = None,
    status_flat_only: int | None = None,
    status_other: int | None = None,
    unique_panos: int | None = None,
    unique_google_panos: int | None = None,
    coverage_rate_pct: float | None = None,
    any_imagery_coverage_rate_pct: float | None = None,
    num_flat_images: int | None = None,
    oldest_capture_date: str | None = None,
    newest_capture_date: str | None = None,
    median_pano_age_years: float | None = None,
    api_requests: int | None = None,
) -> int:
    """
    Register a completed collection run. Raises sqlite3.IntegrityError if a
    run already exists for (city_id, provider, run_date) or the csv_filename
    is taken.

    Returns the new run_id.
    """
    cur = conn.execute(
        """INSERT INTO runs
           (city_id, provider, run_date, csv_filename, json_filename,
            is_baseline, started_at, finished_at, duration_seconds,
            total_points, status_ok, status_no_date, status_zero_results,
            status_flat_only, status_other, unique_panos, unique_google_panos,
            coverage_rate_pct, any_imagery_coverage_rate_pct, num_flat_images,
            oldest_capture_date, newest_capture_date, median_pano_age_years,
            api_requests)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            city_id,
            provider,
            run_date.isoformat(),
            csv_filename,
            json_filename,
            int(is_baseline),
            started_at,
            finished_at,
            duration_seconds,
            total_points,
            status_ok,
            status_no_date,
            status_zero_results,
            status_flat_only,
            status_other,
            unique_panos,
            unique_google_panos,
            coverage_rate_pct,
            any_imagery_coverage_rate_pct,
            num_flat_images,
            oldest_capture_date,
            newest_capture_date,
            median_pano_age_years,
            api_requests,
        ),
    )
    conn.commit()
    return cur.lastrowid


def update_run_json_filename(conn: sqlite3.Connection, run_id: int, json_filename: str) -> None:
    """Record the per-run summary JSON filename after it is generated."""
    conn.execute("UPDATE runs SET json_filename = ? WHERE run_id = ?", (json_filename, run_id))
    conn.commit()


def _row_to_run(row: sqlite3.Row) -> RunRow:
    d = dict(row)
    d["is_baseline"] = bool(d["is_baseline"])
    return RunRow(**d)


def get_latest_run(conn: sqlite3.Connection, city_id: str, provider: str = "gsv") -> RunRow | None:
    """Most recent run for a (city, provider) by run_date, or None."""
    row = conn.execute(
        """SELECT * FROM runs WHERE city_id = ? AND provider = ?
           ORDER BY run_date DESC LIMIT 1""",
        (city_id, provider),
    ).fetchone()
    return _row_to_run(row) if row else None


def get_previous_run(
    conn: sqlite3.Connection, city_id: str, before_date: date, provider: str = "gsv"
) -> RunRow | None:
    """Most recent run strictly before the given date, or None."""
    row = conn.execute(
        """SELECT * FROM runs WHERE city_id = ? AND provider = ?
           AND run_date < ?
           ORDER BY run_date DESC LIMIT 1""",
        (city_id, provider, before_date.isoformat()),
    ).fetchone()
    return _row_to_run(row) if row else None


def get_runs_for_city(
    conn: sqlite3.Connection, city_id: str, provider: str | None = "gsv"
) -> list[RunRow]:
    """
    Runs for a city, oldest first. provider=None returns runs for all
    providers (used by the aggregate builder, which groups them itself).
    """
    if provider is None:
        rows = conn.execute(
            "SELECT * FROM runs WHERE city_id = ? ORDER BY run_date ASC", (city_id,)
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT * FROM runs WHERE city_id = ? AND provider = ?
               ORDER BY run_date ASC""",
            (city_id, provider),
        ).fetchall()
    return [_row_to_run(r) for r in rows]


def get_all_cities(conn: sqlite3.Connection, enabled_only: bool = False) -> list[CityRow]:
    """All registered cities, ordered by city_id."""
    sql = "SELECT * FROM cities"
    if enabled_only:
        sql += " WHERE enabled = 1"
    sql += " ORDER BY city_id"
    out = []
    for row in conn.execute(sql).fetchall():
        d = dict(row)
        d["enabled"] = bool(d["enabled"])
        out.append(CityRow(**d))
    return out


def record_diff(
    conn: sqlite3.Connection,
    *,
    city_id: str,
    from_run_id: int,
    to_run_id: int,
    grid_aligned: bool,
    panos_added: int,
    panos_removed: int,
    panos_persisted: int,
    capture_date_changed: int,
    points_gained_coverage: int | None,
    points_lost_coverage: int | None,
    coverage_delta_pct: float | None,
    detail_filename: str | None,
) -> int:
    """Store a run-to-run diff summary. Idempotent on (from_run, to_run)."""
    cur = conn.execute(
        """INSERT OR REPLACE INTO run_diffs
           (city_id, from_run_id, to_run_id, grid_aligned,
            panos_added, panos_removed, panos_persisted, capture_date_changed,
            points_gained_coverage, points_lost_coverage, coverage_delta_pct,
            detail_filename, computed_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            city_id,
            from_run_id,
            to_run_id,
            int(grid_aligned),
            panos_added,
            panos_removed,
            panos_persisted,
            capture_date_changed,
            points_gained_coverage,
            points_lost_coverage,
            coverage_delta_pct,
            detail_filename,
            utc_now_iso(),
        ),
    )
    conn.commit()
    return cur.lastrowid


def get_diff_for_run(conn: sqlite3.Connection, to_run_id: int) -> sqlite3.Row | None:
    """The diff whose 'to' side is the given run, or None."""
    return conn.execute("SELECT * FROM run_diffs WHERE to_run_id = ?", (to_run_id,)).fetchone()


# ── Historical-dates harvests (issue #2) ───────────────────────────────────


def register_history_harvest(
    conn: sqlite3.Connection,
    *,
    city_id: str,
    harvest_date: date,
    csv_filename: str,
    provider: str = "gsv",
    grid_points_queried: int | None = None,
    unique_panos: int | None = None,
    oldest_capture_date: str | None = None,
    newest_capture_date: str | None = None,
    api_requests: int | None = None,
    started_at: str | None = None,
    finished_at: str | None = None,
) -> int:
    """
    Catalog a completed historical-dates harvest. Idempotent on the filename
    and on (city_id, provider, harvest_date): re-harvesting the same city on the
    same day replaces the prior row rather than erroring, since a harvest is a
    full re-census, not an incremental append.

    Returns the harvest_id.
    """
    conn.execute(
        """INSERT INTO history_harvests
           (city_id, provider, harvest_date, csv_filename, grid_points_queried,
            unique_panos, oldest_capture_date, newest_capture_date,
            api_requests, started_at, finished_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(city_id, provider, harvest_date) DO UPDATE SET
             csv_filename = excluded.csv_filename,
             grid_points_queried = excluded.grid_points_queried,
             unique_panos = excluded.unique_panos,
             oldest_capture_date = excluded.oldest_capture_date,
             newest_capture_date = excluded.newest_capture_date,
             api_requests = excluded.api_requests,
             started_at = excluded.started_at,
             finished_at = excluded.finished_at""",
        (
            city_id,
            provider,
            harvest_date.isoformat(),
            csv_filename,
            grid_points_queried,
            unique_panos,
            oldest_capture_date,
            newest_capture_date,
            api_requests,
            started_at,
            finished_at,
        ),
    )
    conn.commit()
    row = conn.execute(
        """SELECT harvest_id FROM history_harvests
           WHERE city_id = ? AND provider = ? AND harvest_date = ?""",
        (city_id, provider, harvest_date.isoformat()),
    ).fetchone()
    return row["harvest_id"]


def get_latest_history_harvest(
    conn: sqlite3.Connection, city_id: str, provider: str = "gsv"
) -> sqlite3.Row | None:
    """Most recent history harvest for a (city, provider), or None."""
    return conn.execute(
        """SELECT * FROM history_harvests
           WHERE city_id = ? AND provider = ?
           ORDER BY harvest_date DESC LIMIT 1""",
        (city_id, provider),
    ).fetchone()


# ── Frozen OSM street networks (issue #103) ────────────────────────────────


def register_street_network(
    conn: sqlite3.Connection,
    *,
    city_id: str,
    graphml_filename: str,
    network_type: str = "drive",
    node_count: int | None = None,
    edge_count: int | None = None,
    osmnx_version: str | None = None,
) -> int:
    """
    Catalog a city's frozen OSM street network. Idempotent on
    (city_id, network_type): a --refresh re-fetch replaces the prior row
    (counts, osmnx version, fetched_at) rather than erroring — the network is
    a frozen asset with replace-on-refresh semantics, not a history.

    Returns the network_id.
    """
    conn.execute(
        """INSERT INTO street_networks
           (city_id, network_type, graphml_filename, node_count, edge_count,
            osmnx_version, fetched_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(city_id, network_type) DO UPDATE SET
             graphml_filename = excluded.graphml_filename,
             node_count = excluded.node_count,
             edge_count = excluded.edge_count,
             osmnx_version = excluded.osmnx_version,
             fetched_at = excluded.fetched_at""",
        (
            city_id,
            network_type,
            graphml_filename,
            node_count,
            edge_count,
            osmnx_version,
            utc_now_iso(),
        ),
    )
    conn.commit()
    row = conn.execute(
        """SELECT network_id FROM street_networks
           WHERE city_id = ? AND network_type = ?""",
        (city_id, network_type),
    ).fetchone()
    return row["network_id"]


def get_street_network(
    conn: sqlite3.Connection, city_id: str, network_type: str = "drive"
) -> sqlite3.Row | None:
    """The city's frozen street network of the given type, or None."""
    return conn.execute(
        """SELECT * FROM street_networks
           WHERE city_id = ? AND network_type = ?""",
        (city_id, network_type),
    ).fetchone()


# ── Road-walk street-coverage collection runs (issue #99) ──────────────────


def register_street_walk(
    conn: sqlite3.Connection,
    *,
    city_id: str,
    run_date: date,
    csv_filename: str,
    provider: str = "gsv",
    coverage_filename: str | None = None,
    network_type: str = "drive",
    spacing_m: float | None = None,
    match_dist_m: float | None = None,
    sample_points: int | None = None,
    edges_total: int | None = None,
    edges_fully_covered: int | None = None,
    mean_edge_coverage: float | None = None,
    coverage_pct_by_length: float | None = None,
    coverage_pct_by_length_any: float | None = None,
    api_requests: int | None = None,
    started_at: str | None = None,
    finished_at: str | None = None,
) -> int:
    """
    Catalog a completed road-walk collection. Idempotent on the filename and on
    (city_id, provider, run_date): re-collecting the same city/provider on the
    same day replaces the prior row rather than erroring (a road-walk is a full
    re-census of the frozen network, not an incremental append).

    Returns the walk_id.
    """
    conn.execute(
        """INSERT INTO street_walks
           (city_id, provider, run_date, csv_filename, coverage_filename,
            network_type, spacing_m, match_dist_m, sample_points, edges_total,
            edges_fully_covered, mean_edge_coverage, coverage_pct_by_length,
            coverage_pct_by_length_any, api_requests, started_at, finished_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(city_id, provider, run_date) DO UPDATE SET
             csv_filename = excluded.csv_filename,
             coverage_filename = excluded.coverage_filename,
             network_type = excluded.network_type,
             spacing_m = excluded.spacing_m,
             match_dist_m = excluded.match_dist_m,
             sample_points = excluded.sample_points,
             edges_total = excluded.edges_total,
             edges_fully_covered = excluded.edges_fully_covered,
             mean_edge_coverage = excluded.mean_edge_coverage,
             coverage_pct_by_length = excluded.coverage_pct_by_length,
             coverage_pct_by_length_any = excluded.coverage_pct_by_length_any,
             api_requests = excluded.api_requests,
             started_at = excluded.started_at,
             finished_at = excluded.finished_at""",
        (
            city_id,
            provider,
            run_date.isoformat(),
            csv_filename,
            coverage_filename,
            network_type,
            spacing_m,
            match_dist_m,
            sample_points,
            edges_total,
            edges_fully_covered,
            mean_edge_coverage,
            coverage_pct_by_length,
            coverage_pct_by_length_any,
            api_requests,
            started_at,
            finished_at,
        ),
    )
    conn.commit()
    row = conn.execute(
        """SELECT walk_id FROM street_walks
           WHERE city_id = ? AND provider = ? AND run_date = ?""",
        (city_id, provider, run_date.isoformat()),
    ).fetchone()
    return row["walk_id"]


def get_latest_street_walk(
    conn: sqlite3.Connection, city_id: str, provider: str = "gsv"
) -> sqlite3.Row | None:
    """Most recent road-walk collection for a (city, provider), or None."""
    return conn.execute(
        """SELECT * FROM street_walks
           WHERE city_id = ? AND provider = ?
           ORDER BY run_date DESC LIMIT 1""",
        (city_id, provider),
    ).fetchone()


def get_latest_street_walks_all(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """
    The most recent road-walk collection for every (city_id, provider) that has
    one, ordered by city then provider. Backs the published ``streetwalks.json.gz``
    manifest (issue #155): one row per city+provider so the city page can look up
    its coverage artifact by (city_id, provider).
    """
    return conn.execute(
        """SELECT sw.* FROM street_walks sw
           JOIN (
               SELECT city_id, provider, MAX(run_date) AS max_date
               FROM street_walks
               GROUP BY city_id, provider
           ) latest
             ON sw.city_id = latest.city_id
            AND sw.provider = latest.provider
            AND sw.run_date = latest.max_date
           ORDER BY sw.city_id, sw.provider""",
    ).fetchall()


# ── API budget ledger ──────────────────────────────────────────────────────


def add_api_usage(
    conn: sqlite3.Connection, usage_date: date, n: int, provider: str = "gsv"
) -> None:
    """Add n requests to the given (date, provider) ledger row."""
    conn.execute(
        """INSERT INTO api_usage (usage_date, provider, requests)
           VALUES (?, ?, ?)
           ON CONFLICT(usage_date, provider)
           DO UPDATE SET requests = requests + ?""",
        (usage_date.isoformat(), provider, n, n),
    )
    conn.commit()


def get_api_usage(conn: sqlite3.Connection, usage_date: date, provider: str = "gsv") -> int:
    """Requests recorded for the given (date, provider) (0 if none)."""
    row = conn.execute(
        "SELECT requests FROM api_usage WHERE usage_date = ? AND provider = ?",
        (usage_date.isoformat(), provider),
    ).fetchone()
    return row[0] if row else 0


# ── Scheduler state ────────────────────────────────────────────────────────


def compute_day_of_cycle(city_id: str, cycle_days: int) -> int:
    """
    Stable stagger assignment: hash the city_id onto a day of the cycle.
    Deterministic across machines and runs.
    """
    digest = hashlib.sha256(city_id.encode("utf-8")).hexdigest()
    return int(digest, 16) % cycle_days


def assign_schedule(conn: sqlite3.Connection, cycle_days: int, providers: tuple = ("gsv",)) -> int:
    """
    Ensure every enabled city has a schedule_state row per provider with its
    day_of_cycle. The day is hashed from city_id alone, so all providers of
    a city land on the same day (paired same-day snapshots). Recomputed each
    call (stable hash, so assignments only change if cycle_days changed).

    Returns the number of cities assigned.
    """
    cities = get_all_cities(conn, enabled_only=True)
    for city in cities:
        day = compute_day_of_cycle(city.city_id, cycle_days)
        for provider in providers:
            conn.execute(
                """INSERT INTO schedule_state (city_id, provider, day_of_cycle)
                   VALUES (?, ?, ?)
                   ON CONFLICT(city_id, provider)
                   DO UPDATE SET day_of_cycle = ?""",
                (city.city_id, provider, day, day),
            )
    conn.commit()
    return len(cities)


def get_due_cities(
    conn: sqlite3.Connection,
    *,
    today: date,
    cycle_days: int,
    grace_days: int,
    max_consecutive_failures: int,
    provider: str = "gsv",
) -> list[CityRow]:
    """
    Cities due for collection today for the given provider, ordered
    stalest-first so backlog self-heals after outages.

    A city is due when it is enabled, hasn't exceeded the failure cap, and
    either has never succeeded or its last success is at least
    (cycle_days - grace_days) old.
    """
    threshold = cycle_days - grace_days
    rows = conn.execute(
        """SELECT c.*, s.last_success_at, s.consecutive_failures
           FROM cities c
           LEFT JOIN schedule_state s
             ON s.city_id = c.city_id AND s.provider = ?
           WHERE c.enabled = 1
             AND COALESCE(s.consecutive_failures, 0) < ?
             AND (s.last_success_at IS NULL
                  OR julianday(?) - julianday(s.last_success_at) >= ?)
           ORDER BY s.last_success_at ASC NULLS FIRST, c.city_id ASC""",
        (provider, max_consecutive_failures, today.isoformat(), threshold),
    ).fetchall()
    out = []
    for row in rows:
        d = {k: row[k] for k in row.keys() if k not in ("last_success_at", "consecutive_failures")}
        d["enabled"] = bool(d["enabled"])
        out.append(CityRow(**d))
    return out


def record_attempt(
    conn: sqlite3.Connection,
    city_id: str,
    *,
    success: bool,
    error: str | None = None,
    provider: str = "gsv",
) -> None:
    """Update schedule_state after a collection attempt."""
    now = utc_now_iso()
    if success:
        conn.execute(
            """INSERT INTO schedule_state
               (city_id, provider, day_of_cycle, last_attempt_at,
                last_success_at, consecutive_failures, last_error)
               VALUES (?, ?, 0, ?, ?, 0, NULL)
               ON CONFLICT(city_id, provider) DO UPDATE SET
                 last_attempt_at = ?, last_success_at = ?,
                 consecutive_failures = 0, last_error = NULL""",
            (city_id, provider, now, now, now, now),
        )
    else:
        conn.execute(
            """INSERT INTO schedule_state
               (city_id, provider, day_of_cycle, last_attempt_at,
                consecutive_failures, last_error)
               VALUES (?, ?, 0, ?, 1, ?)
               ON CONFLICT(city_id, provider) DO UPDATE SET
                 last_attempt_at = ?,
                 consecutive_failures = consecutive_failures + 1,
                 last_error = ?""",
            (city_id, provider, now, error, now, error),
        )
    conn.commit()
