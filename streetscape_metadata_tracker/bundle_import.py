"""
Land a laptop investigation's artifacts in this host's catalog (issue #330).

An inquiry about an untracked city arrives and the useful window for answering
it is the next hour, not the next night. Investigating from a laptop already
works — the collectors all take ``--download-dir``, so a scratch ``data/``
directory holds a complete, self-describing investigation: its own catalog, the
dated artifacts, and the frozen OSM network they were measured on. What did not
exist is any way to get that result into production. Registering the city
instead makes prod re-download everything it *can*, and silently discards what
it cannot: an opt-in channel nobody enrolled (KartaView) and a provider that is
not a scheduler channel at all (Panoramax).

So a "bundle" here is not a new format. It is exactly the ``data/`` directory a
laptop run already produced, and this module reads it the way prod's own
catalog would be read.

Two rules shape everything below.

**Geometry is frozen, and this host is the authority.** A run's filename encodes
width/height/step, so two machines freezing two different grids for one
``city_id`` desynchronises the artifacts *silently* — the immutable-snapshot
invariant breaks with no error anywhere. Nothing in this codebase checked that
before: every ``naming.same_grid_geometry`` call compares filename to filename,
never filename to the ``cities`` row. :func:`check_bundle` does, and refuses.

**A refusal rejects the whole bundle, before anything is written.** Half an
investigation in the catalog is worse than none: ``db.add_api_usage`` is
additive rather than idempotent, so a partial import that the operator retries
double-charges the ledger. The collision check is what makes a retry safe, and
it only makes it safe if nothing was written the first time.

Validation is all-or-nothing; the write is not, and cannot be — every ``db``
writer commits for itself, so an outer transaction here would not hold. What
stands in for atomicity is ORDER. The ledger is written last, after every row
that a retry's collision check would refuse on, so a crash part-way through
leaves an import that can be diagnosed and finished by hand but can never
double-charge: either the ledger write was not reached, or it was reached and
the rows in front of it will refuse the retry.
"""

from __future__ import annotations

import gzip
import json
import logging
import os
import shutil
import sqlite3
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from . import analysis, db, fileutils
from .json_summarizer import regenerate_run_json
from .naming import (
    generate_run_filename,
    generate_streetwalk_filename,
    streetwalk_coverage_filename,
)

logger = logging.getLogger(__name__)

# The catalog file inside a bundle, and the subdirectory a bundle's frozen OSM
# networks live in. The latter mirrors download_street_network._cache_dir, which
# is the authority; it is restated rather than imported because that module
# pulls osmnx, and neither the scheduler nor this importer has any business
# loading it to copy a file. scheduler.py already restates it the same way.
CATALOG_NAME = "streetscape_tracker.db"
OSM_CACHE_DIRNAME = "osm_cache"

# The five columns that must agree before a bundle can be imported into a city
# this host already tracks. Geometry is frozen at registration and shared by
# every provider precisely so diffs mean something; a bundle collected on a
# different rectangle is not a later snapshot of the same series, it is a
# different series wearing the same city_id.
GEOMETRY_FIELDS = ("center_lat", "center_lon", "grid_width_m", "grid_height_m", "step_m")

# Whose spend belongs in THIS host's api_usage ledger.
#
# Three of the four providers meter by IP address rather than by credential
# (docs/provider-access.md): Mapillary's tile CDN, Overpass, and — for the
# per-IP half of its limit — kartaview.org. A laptop investigation genuinely
# spent those against a different IP, so charging them here would tighten a
# budget gate against requests this host never made. That matters most for
# Mapillary, whose budget is the instrument in an open block investigation.
#
# Google is the exception, and the reason this set is not empty: GSV meters and
# bills per Cloud *project*, not per key or per IP, so a laptop walk on
# GMAPS_STREETS_API_KEY drew on exactly the pool prod's own walks draw on. Not
# recording it would hide real burn while still spending it — worse than doing
# nothing. KartaView is included for the same reason at a smaller scale: its
# documented 1,000/h ceiling is per TOKEN, and the laptop used prod's token.
LEDGERED_PROVIDERS = frozenset({"gsv", "gsv_streets", "kartaview", "kartaview_streets"})


class BundleError(ValueError):
    """A bundle that cannot be read at all — malformed, missing, or wrong schema."""


@dataclass(frozen=True)
class BundleRun:
    """One ``runs`` row from a bundle, with the artifact names it points at."""

    row: dict[str, Any]

    @property
    def provider(self) -> str:
        return self.row["provider"]

    @property
    def run_date(self) -> date:
        return date.fromisoformat(self.row["run_date"])

    @property
    def artifacts(self) -> list[str]:
        return [n for n in (self.row["csv_filename"], self.row["json_filename"]) if n]


@dataclass(frozen=True)
class BundleWalk:
    """One ``street_walks`` row from a bundle, with the artifact names it points at."""

    row: dict[str, Any]

    @property
    def provider(self) -> str:
        return self.row["provider"]

    @property
    def run_date(self) -> date:
        return date.fromisoformat(self.row["run_date"])

    @property
    def artifacts(self) -> list[str]:
        return [n for n in (self.row["csv_filename"], self.row["coverage_filename"]) if n]


@dataclass
class Bundle:
    """A laptop ``data/`` directory, read but not yet validated against this host."""

    root: Path
    city: dict[str, Any]
    runs: list[BundleRun] = field(default_factory=list)
    walks: list[BundleWalk] = field(default_factory=list)
    networks: list[dict[str, Any]] = field(default_factory=list)
    api_usage: list[dict[str, Any]] = field(default_factory=list)

    @property
    def city_id(self) -> str:
        return self.city["city_id"]


def _resolve_root(path: str | os.PathLike[str]) -> Path:
    """
    The directory holding the bundle's catalog.

    Accepts either the ``data/`` directory itself or a parent containing one, so
    an operator can point this at whichever of the two they happen to have
    rsynced. Anything else is a BundleError rather than an empty import.
    """
    p = Path(path)
    for candidate in (p, p / "data"):
        if (candidate / CATALOG_NAME).is_file():
            return candidate
    raise BundleError(f"No {CATALOG_NAME} found in {p} or {p / 'data'}")


def read_bundle(path: str | os.PathLike[str]) -> Bundle:
    """
    Read a bundle's catalog without touching it.

    Deliberately NOT ``db.connect``: that runs ``init_schema``, which would
    migrate the bundle in place — mutating the operator's evidence, and doing it
    on a copy that may be mid-rsync. A read-only URI connection also makes the
    schema check below honest, since a connection that can silently upgrade the
    file cannot report what version it arrived as.
    """
    root = _resolve_root(path)
    db_path = root / CATALOG_NAME

    # A live WAL means the last writes are not in the main file, and a read-only
    # connection cannot replay one. Reading anyway would import a bundle that is
    # quietly missing its most recent rows. CLAUDE.md states the sidecar rule for
    # backups; it is the same rule here.
    wal = db_path.with_name(db_path.name + "-wal")
    if wal.exists() and wal.stat().st_size > 0:
        raise BundleError(
            f"{wal.name} is non-empty, so {db_path.name} may be missing its most recent "
            "rows. Close the collector on the machine that wrote it and re-copy the bundle."
        )

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        conn.row_factory = sqlite3.Row
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version != db.SCHEMA_VERSION:
            raise BundleError(
                f"Bundle catalog is schema v{version}, this host is v{db.SCHEMA_VERSION}. "
                "The stat columns are the contract between the two; re-collect the bundle "
                "on a checkout matching this host."
            )

        cities = [dict(r) for r in conn.execute("SELECT * FROM cities ORDER BY city_id")]
        if len(cities) != 1:
            raise BundleError(
                f"Bundle holds {len(cities)} cities; expected exactly 1. "
                "A bundle is one investigation of one city."
            )
        city = cities[0]

        runs = [
            BundleRun(dict(r))
            for r in conn.execute(
                "SELECT * FROM runs WHERE city_id = ? ORDER BY provider, run_date",
                (city["city_id"],),
            )
        ]
        walks = [
            BundleWalk(dict(r))
            for r in conn.execute(
                "SELECT * FROM street_walks WHERE city_id = ? "
                "ORDER BY provider, network_type, run_date",
                (city["city_id"],),
            )
        ]
        networks = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM street_networks WHERE city_id = ? ORDER BY network_type",
                (city["city_id"],),
            )
        ]
        usage = [
            dict(r) for r in conn.execute("SELECT * FROM api_usage ORDER BY usage_date, provider")
        ]
    finally:
        conn.close()

    if not runs and not walks:
        raise BundleError("Bundle holds no runs and no street walks; nothing to import.")

    return Bundle(root=root, city=city, runs=runs, walks=walks, networks=networks, api_usage=usage)


def _expected_run_names(city: dict[str, Any], run: BundleRun) -> tuple[str, str]:
    """CSV and JSON names this host's generators produce for a bundle's run row."""
    stem = generate_run_filename(
        city["city_id"],
        city["grid_width_m"],
        city["grid_height_m"],
        city["step_m"],
        run.run_date,
        provider=run.provider,
    )
    return stem + ".csv.gz", stem + ".json.gz"


def _expected_walk_names(city: dict[str, Any], walk: BundleWalk) -> tuple[str, str]:
    """CSV and coverage-JSON names this host's generators produce for a bundle's walk row."""
    stem = generate_streetwalk_filename(
        city["city_id"],
        city["grid_width_m"],
        city["grid_height_m"],
        city["step_m"],
        walk.row["spacing_m"],
        walk.run_date,
        provider=walk.provider,
        network_type=walk.row["network_type"],
    )
    csv_name = stem + ".csv.gz"
    return csv_name, streetwalk_coverage_filename(csv_name)


def check_bundle(
    bundle: Bundle, conn: sqlite3.Connection, data_dir: str
) -> tuple[db.CityRow | None, list[str]]:
    """
    Everything that must be true before a single byte is written.

    Returns ``(existing city row or None, problems)``. A non-empty ``problems``
    list rejects the whole bundle — see the module docstring for why partial
    imports are not offered.
    """
    problems: list[str] = []
    existing = db.resolve_city(conn, bundle.city_id)

    if existing is not None:
        # The load-bearing check. A bundle collected on a different rectangle is
        # a different series, and importing it would put two geometries in one
        # city's history with nothing anywhere to say so.
        for f in GEOMETRY_FIELDS:
            theirs, ours = bundle.city[f], getattr(existing, f)
            if theirs != ours:
                problems.append(
                    f"geometry mismatch on {f}: bundle has {theirs}, this host has {ours}. "
                    "Grid geometry is frozen; the bundle was not collected on this city's grid."
                )
    else:
        # Registering from the bundle is allowed — that is how a city first
        # arrives — but only if this checkout derives the same city_id from the
        # same name parts. A disagreement means the two checkouts' naming rules
        # differ, and every artifact filename is downstream of that.
        derived = db.derive_city_id(
            bundle.city["city_name"], bundle.city["state_name"], bundle.city["country_name"]
        )
        if derived != bundle.city_id:
            problems.append(
                f"city_id mismatch: the bundle says {bundle.city_id!r} but this checkout "
                f"derives {derived!r} from the same name parts. The two checkouts disagree "
                "about naming, which every artifact filename depends on."
            )

    # Filenames are regenerated, never trusted. A per-(city, provider) artifact
    # whose provider token is missing silently collides the moment two providers
    # share a run date, and the second collection then skips as a no-op.
    for run in bundle.runs:
        want_csv, want_json = _expected_run_names(bundle.city, run)
        if run.row["csv_filename"] != want_csv:
            problems.append(
                f"run [{run.provider} {run.row['run_date']}] names {run.row['csv_filename']}, "
                f"but this host's generator produces {want_csv}"
            )
        if run.row["json_filename"] and run.row["json_filename"] != want_json:
            problems.append(
                f"run [{run.provider} {run.row['run_date']}] names "
                f"{run.row['json_filename']}, but this host's generator produces {want_json}"
            )
    for walk in bundle.walks:
        want_csv, want_cov = _expected_walk_names(bundle.city, walk)
        if walk.row["csv_filename"] != want_csv:
            problems.append(
                f"walk [{walk.provider}/{walk.row['network_type']} {walk.row['run_date']}] names "
                f"{walk.row['csv_filename']}, but this host's generator produces {want_csv}"
            )
        if walk.row["coverage_filename"] and walk.row["coverage_filename"] != want_cov:
            problems.append(
                f"walk [{walk.provider}/{walk.row['network_type']} {walk.row['run_date']}] names "
                f"{walk.row['coverage_filename']}, but this host's generator produces {want_cov}"
            )

    # Collisions. BOTH checks are needed: csv_filename carries its own UNIQUE
    # independent of the composite key, and db.register_street_walk UPSERTS on
    # the composite key — so an unguarded import would overwrite a walk rather
    # than refuse. This is also what makes a retry after a refusal safe.
    already_cataloged: set[str] = set()
    for run in bundle.runs:
        if conn.execute(
            "SELECT 1 FROM runs WHERE city_id = ? AND provider = ? AND run_date = ?",
            (bundle.city_id, run.provider, run.row["run_date"]),
        ).fetchone():
            problems.append(
                f"this host already has a {run.provider} run for {run.row['run_date']}; "
                "refusing to overwrite an immutable snapshot"
            )
            already_cataloged.update(run.artifacts)
        elif conn.execute(
            "SELECT 1 FROM runs WHERE csv_filename = ?", (run.row["csv_filename"],)
        ).fetchone():
            problems.append(
                f"this host already has a run filed under {run.row['csv_filename']} "
                "(a different city or provider); refusing to collide"
            )
    for walk in bundle.walks:
        if conn.execute(
            "SELECT 1 FROM street_walks WHERE city_id = ? AND provider = ? "
            "AND network_type = ? AND run_date = ?",
            (bundle.city_id, walk.provider, walk.row["network_type"], walk.row["run_date"]),
        ).fetchone():
            problems.append(
                f"this host already has a {walk.provider}/{walk.row['network_type']} walk for "
                f"{walk.row['run_date']}; refusing to overwrite it"
            )
            already_cataloged.update(walk.artifacts)
        elif conn.execute(
            "SELECT 1 FROM street_walks WHERE csv_filename = ?", (walk.row["csv_filename"],)
        ).fetchone():
            problems.append(
                f"this host already has a walk filed under {walk.row['csv_filename']}; "
                "refusing to collide"
            )

    # Every file a row names has to be here. A catalog row pointing at a missing
    # artifact is invisible to the aggregate and reads as a lost collection.
    for name in _artifact_sources(bundle):
        if not (bundle.root / name).is_file():
            problems.append(f"row names {name} but it is not in the bundle")
    for net in bundle.networks:
        src = bundle.root / OSM_CACHE_DIRNAME / net["graphml_filename"]
        if not src.is_file():
            problems.append(
                f"street_networks names {net['graphml_filename']} but it is not in "
                f"{OSM_CACHE_DIRNAME}/"
            )
        # register_street_network upserts on (city_id, network_type), so an
        # existing row naming a different file would be replaced without a word.
        # The graphml IS the network the walk's coverage was measured against.
        row = conn.execute(
            "SELECT graphml_filename FROM street_networks WHERE city_id = ? AND network_type = ?",
            (bundle.city_id, net["network_type"]),
        ).fetchone()
        if row is not None and row["graphml_filename"] != net["graphml_filename"]:
            problems.append(
                f"this host already has a {net['network_type']} network for this city "
                f"({row['graphml_filename']}) differing from the bundle's "
                f"({net['graphml_filename']}); refusing to replace the network a walk was "
                "measured against"
            )

    # Destination collisions on disk. Checked separately from the catalog ones
    # above because they are a different problem — a stray artifact with no row
    # would otherwise be overwritten silently — but skipped for anything already
    # reported there, since a cataloged run's files existing is that finding
    # restated once per file rather than a second finding.
    for name in _artifact_sources(bundle):
        if name in already_cataloged:
            continue
        if (Path(data_dir) / name).exists():
            problems.append(f"{name} already exists in {data_dir}; refusing to overwrite")

    problems.extend(verify_walk_artifacts(bundle))

    return existing, problems


def _artifact_sources(bundle: Bundle) -> list[str]:
    """
    Every published artifact a bundle row names — and deliberately nothing else.

    This is why the copy step below is file-by-file rather than a directory
    sync. A bundle's ``data/`` also holds ``cities.json.gz``,
    ``streetwalks.json.gz`` and ``driving_plan.json.gz`` that the laptop
    generated over a ONE-CITY catalog, plus osmnx's raw HTTP cache. Syncing the
    directory would overwrite this host's published aggregates with a one-city
    index — a site-wide outage produced by a successful import.
    """
    names: list[str] = []
    for run in bundle.runs:
        names.extend(run.artifacts)
    for walk in bundle.walks:
        names.extend(walk.artifacts)
    return names


def _copy_atomic(src: Path, dst: Path) -> None:
    """Copy into place via a temp sibling, so a torn copy can never be read as an artifact."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(dst.name + ".importtmp")
    shutil.copyfile(src, tmp)
    os.replace(tmp, dst)


@dataclass
class ImportResult:
    """What an import did, for the operator report and for the caller's cadence bookkeeping."""

    city_id: str
    registered_city: bool
    files: list[str] = field(default_factory=list)
    run_providers: list[str] = field(default_factory=list)
    walk_providers: list[str] = field(default_factory=list)
    usage: list[tuple[str, str, int]] = field(default_factory=list)
    stat_corrections: list[str] = field(default_factory=list)
    networks: list[str] = field(default_factory=list)


def apply_bundle(
    bundle: Bundle,
    conn: sqlite3.Connection,
    *,
    data_dir: str,
    existing: db.CityRow | None,
    enable: bool,
) -> ImportResult:
    """
    Write the bundle. Call only on a :func:`check_bundle` that returned no problems.

    Files land before rows, because a row naming a missing artifact is worse
    than an orphan file: the aggregate skips it, so the run reads as lost rather
    than as needing a re-copy.
    """
    result = ImportResult(city_id=bundle.city_id, registered_city=existing is None)

    if existing is None:
        city_id = db.register_city(
            conn,
            city_name=bundle.city["city_name"],
            state_name=bundle.city["state_name"],
            state_code=bundle.city["state_code"],
            country_name=bundle.city["country_name"],
            country_code=bundle.city["country_code"],
            center_lat=bundle.city["center_lat"],
            center_lon=bundle.city["center_lon"],
            grid_width_m=bundle.city["grid_width_m"],
            grid_height_m=bundle.city["grid_height_m"],
            step_m=bundle.city["step_m"],
            notes=bundle.city["notes"],
            # Off unless asked. A city arriving through an investigation has a
            # boundary nobody has vetted, and a fresh city sorts to the head of
            # the next night's stalest-first queue — so registering it enabled
            # commits the whole grid to collection the same night it is first
            # looked at. scripts/register_frame.py takes the same position.
            enabled=enable,
        )
        assert city_id == bundle.city_id  # check_bundle proved this
    elif enable and not existing.enabled:
        db.set_city_enabled(conn, bundle.city_id, True)

    for name in _artifact_sources(bundle):
        _copy_atomic(bundle.root / name, Path(data_dir) / name)
        result.files.append(name)
    for net in bundle.networks:
        name = net["graphml_filename"]
        _copy_atomic(
            bundle.root / OSM_CACHE_DIRNAME / name,
            Path(data_dir) / OSM_CACHE_DIRNAME / name,
        )
        result.files.append(f"{OSM_CACHE_DIRNAME}/{name}")

    for net in bundle.networks:
        db.register_street_network(
            conn,
            city_id=bundle.city_id,
            graphml_filename=net["graphml_filename"],
            network_type=net["network_type"],
            node_count=net["node_count"],
            edge_count=net["edge_count"],
            osmnx_version=net["osmnx_version"],
        )
        result.networks.append(net["network_type"])

    for run in bundle.runs:
        _register_run(bundle, run, conn, data_dir, result)
    for walk in bundle.walks:
        _register_walk(walk, conn, result)

    # LAST, deliberately — see the module docstring. Every row a retry would
    # collide on is already committed by this point, so a crash here cannot be
    # followed by a successful re-import that charges the ledger twice.
    for row in bundle.api_usage:
        provider, requests = row["provider"], row["requests"]
        if provider not in LEDGERED_PROVIDERS or not requests:
            continue
        # The bundle's own usage_date, not today's: api_usage is a per-day
        # record of what a credential spent, and the spend happened then.
        db.add_api_usage(conn, date.fromisoformat(row["usage_date"]), requests, provider=provider)
        result.usage.append((row["usage_date"], provider, requests))

    conn.commit()
    return result


def _register_run(
    bundle: Bundle,
    run: BundleRun,
    conn: sqlite3.Connection,
    data_dir: str,
    result: ImportResult,
) -> None:
    """
    Catalog one grid run, with its stats RECOMPUTED from the copied CSV.

    Recomputing rather than carrying the numbers is what makes the imported row
    indistinguishable from one this host collected: the CSV is the artifact, and
    a stat definition that moved between the two checkouts would otherwise enter
    the series as a step change with nothing to attribute it to. It is one
    pandas pass, and it is the same call the collector makes.

    ``num_flat_images`` is the one exception — it is not recoverable from a CSV
    (see scripts/recompute_run_stats.py) — as are the provenance columns, which
    describe the collection rather than the data.
    """
    csv_path = os.path.join(data_dir, run.row["csv_filename"])
    df = fileutils.load_city_csv_file(csv_path)
    stats = analysis.calculate_run_stats(df, run.run_date, provider=run.provider)

    for key, value in stats.items():
        was = run.row.get(key)
        if was != value and not _equalish(was, value):
            result.stat_corrections.append(
                f"{run.provider} {run.row['run_date']}: {key} {was!r} -> {value!r}"
            )

    run_id = db.register_run(
        conn,
        city_id=bundle.city_id,
        run_date=run.run_date,
        csv_filename=run.row["csv_filename"],
        provider=run.provider,
        json_filename=run.row["json_filename"],
        is_baseline=bool(run.row["is_baseline"]),
        started_at=run.row["started_at"],
        finished_at=run.row["finished_at"],
        duration_seconds=run.row["duration_seconds"],
        num_flat_images=run.row["num_flat_images"],
        api_requests=run.row["api_requests"],
        census_fetched_by=run.row["census_fetched_by"],
        census_fetched_at=run.row["census_fetched_at"],
        **stats,
    )
    result.run_providers.append(run.provider)

    # Regenerated here rather than copied. The bundle's JSON carries a
    # change_from_previous_run block computed against a catalog holding only
    # this one run — i.e. "no previous run" — while THIS host may well have a
    # series to diff against. Only the destination can write that block
    # correctly, so the copied file is replaced immediately.
    try:
        regenerate_run_json(conn, run_id, data_dir)
    except Exception:
        logger.exception(
            f"{bundle.city_id} [{run.provider}]: per-run JSON could not be regenerated; "
            "the run is cataloged and the bundle's own JSON is in place"
        )


def _register_walk(walk: BundleWalk, conn: sqlite3.Connection, result: ImportResult) -> None:
    """
    Catalog one road walk, carrying the bundle's own coverage numbers.

    A deliberate asymmetry with :func:`_register_run`, and worth stating because
    it looks like an oversight. A grid run's stats are a pandas pass over the
    CSV that was just copied. A walk's are the output of an OSM edge join —
    re-running it means re-reading the frozen network and redoing the spatial
    match, for a result that can only equal what the coverage GeoJSON beside it
    already records. That GeoJSON *is* the artifact: ``_reconcile_orphaned_walk``
    rebuilds a lost catalog row from it for exactly this reason.

    So the row is carried, and integrity is checked instead — the coverage
    artifact's own totals must agree with the row, which catches the failure a
    recompute would have caught (a row and an artifact that are not about the
    same walk) at a fraction of the cost.
    """
    db.register_street_walk(
        conn,
        city_id=walk.row["city_id"],
        run_date=walk.run_date,
        csv_filename=walk.row["csv_filename"],
        provider=walk.provider,
        coverage_filename=walk.row["coverage_filename"],
        network_type=walk.row["network_type"],
        spacing_m=walk.row["spacing_m"],
        match_dist_m=walk.row["match_dist_m"],
        sample_points=walk.row["sample_points"],
        edges_total=walk.row["edges_total"],
        edges_fully_covered=walk.row["edges_fully_covered"],
        mean_edge_coverage=walk.row["mean_edge_coverage"],
        coverage_pct_by_length=walk.row["coverage_pct_by_length"],
        coverage_pct_by_length_any=walk.row["coverage_pct_by_length_any"],
        coverage_by_highway=walk.row["coverage_by_highway"],
        length_km=walk.row["length_km"],
        length_km_covered=walk.row["length_km_covered"],
        length_km_covered_any=walk.row["length_km_covered_any"],
        median_covered_age_years=walk.row["median_covered_age_years"],
        api_requests=walk.row["api_requests"],
        census_fetched_by=walk.row["census_fetched_by"],
        census_fetched_at=walk.row["census_fetched_at"],
        started_at=walk.row["started_at"],
        finished_at=walk.row["finished_at"],
    )
    result.walk_providers.append(walk.provider)


def verify_walk_artifacts(bundle: Bundle) -> list[str]:
    """
    Cross-check each walk row against the coverage artifact it names.

    Cheap stand-in for recomputing a walk (see :func:`_register_walk`): if the
    row and the artifact disagree about how many edges the walk covered, they
    are not describing the same walk and neither number can be trusted.
    Returned as problems so they refuse the bundle alongside everything else.
    """
    problems: list[str] = []
    for walk in bundle.walks:
        # The CSV holds exactly one row per sample point, so its row count
        # recovers `sample_points`. This is the check that catches a truncated
        # copy: an rsync cut short leaves a readable gzip whose row count is
        # simply wrong, and nothing else here would notice.
        csv_path = bundle.root / walk.row["csv_filename"]
        if csv_path.is_file() and walk.row["sample_points"] is not None:
            counted = fileutils.count_streetwalk_samples(csv_path)
            if counted is not None and counted != walk.row["sample_points"]:
                problems.append(
                    f"{walk.row['csv_filename']} holds {counted:,} sample rows but its "
                    f"catalog row says {walk.row['sample_points']:,}; the snapshot is "
                    "truncated or is not the walk the row describes"
                )

        name = walk.row["coverage_filename"]
        if not name:
            continue
        path = bundle.root / name
        if not path.is_file():
            continue  # already reported by check_bundle
        try:
            with gzip.open(path, "rt", encoding="utf-8") as fh:
                totals = json.load(fh)["properties"]["metadata"]["totals"]
        except (OSError, EOFError, ValueError, KeyError, TypeError) as e:
            problems.append(f"{name} could not be read as a coverage artifact ({e})")
            continue
        # (key in the artifact's totals block, column in the catalog row).
        # The two names differ for the edge count: the artifact calls it
        # `edges`, the catalog `edges_total`.
        for totals_key, column in (("edges", "edges_total"), ("coverage_pct_by_length",) * 2):
            theirs, ours = totals.get(totals_key), walk.row[column]
            if not _equalish(theirs, ours):
                problems.append(
                    f"{name} reports {totals_key}={theirs!r} but its catalog row says "
                    f"{column}={ours!r}; the row and the artifact are not describing "
                    "the same walk"
                )
    return problems


def _equalish(a: Any, b: Any) -> bool:
    """Tolerant compare, so a float round-trip through SQLite is not a 'correction'."""
    if a is None or b is None:
        return a is b or a == b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return abs(float(a) - float(b)) <= 1e-6 * max(1.0, abs(float(a)), abs(float(b)))
    return a == b
