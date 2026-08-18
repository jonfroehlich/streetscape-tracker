#!/usr/bin/env python3
"""
One-shot migration: register existing (pre-temporal-tracking) GSV data
files in the SQLite catalog as baseline runs.

For each legacy .csv.gz in the data directory:
1. Parse the filename (tolerates old int names, buggy float-step names, and
   legacy single-underscore slugs).
2. Resolve identity from the sibling .json.gz (city/state/country names +
   center) — no geocoding needed; falls back to rate-limited Nominatim for
   files without a readable JSON.
3. Derive the canonical city_id; alias duplicates (e.g. albany--ny vs
   albany--new-york) collapse onto one city, with the extra slugs recorded
   in city_aliases.
4. Freeze grid geometry into the cities table and register the file as an
   is_baseline=1 run, with run_date = max(query_timestamp).
5. Regenerate any .json.gz whose content is corrupt (legacy NaN bug).

Idempotent: already-registered files are skipped, so it is safe to re-run.

Usage:
    python scripts/migrate_to_db.py                # dry run (default)
    python scripts/migrate_to_db.py --execute      # apply changes
    python scripts/migrate_to_db.py --data-dir DIR --db-path PATH
"""

import argparse
import gzip
import json
import logging
import os
import sys
from collections import defaultdict
from dataclasses import dataclass, field

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from streetscape_metadata_tracker import db  # noqa: E402
from streetscape_metadata_tracker.analysis import calculate_run_stats  # noqa: E402
from streetscape_metadata_tracker.fileutils import load_city_csv_file  # noqa: E402
from streetscape_metadata_tracker.geoutils import (  # noqa: E402
    get_city_location_data,
    get_country_code,
    get_state_abbreviation,
)
from streetscape_metadata_tracker.json_summarizer import (  # noqa: E402
    generate_city_metadata_summary_as_json,
)
from streetscape_metadata_tracker.naming import parse_filename, slug_to_query_str  # noqa: E402
from streetscape_metadata_tracker.paths import get_default_data_dir  # noqa: E402
from streetscape_metadata_tracker.progress import progress  # noqa: E402

logger = logging.getLogger("migrate")


@dataclass
class FileInfo:
    """Everything we learn about one legacy csv.gz file."""

    csv_path: str
    slug: str
    width: int
    height: int
    step: int
    city_name: str | None = None
    state_name: str | None = None
    country_name: str | None = None
    center_lat: float | None = None
    center_lon: float | None = None
    run_date: pd.Timestamp | None = None
    json_path: str | None = None
    json_ok: bool = False  # sibling json exists and parses strictly
    identity_source: str = ""  # 'json' | 'nominatim' | 'failed'
    problems: list[str] = field(default_factory=list)

    @property
    def csv_filename(self) -> str:
        return os.path.basename(self.csv_path)

    @property
    def geometry(self) -> tuple[int, int, int]:
        return (self.width, self.height, self.step)


def strict_json_load(path: str) -> dict | None:
    """Load JSON, raising on NaN/Infinity tokens (returns None on failure)."""

    def _reject(token):
        raise ValueError(f"invalid JSON token {token}")

    try:
        with gzip.open(path, "rt", encoding="utf-8") as f:
            return json.load(f, parse_constant=_reject)
    except Exception:
        return None


def read_run_date(csv_path: str) -> pd.Timestamp | None:
    """Newest query_timestamp in the file (cheap usecols read)."""
    try:
        ts = pd.read_csv(csv_path, usecols=["query_timestamp"])["query_timestamp"]
        return pd.to_datetime(ts, format="ISO8601", utc=True).max()
    except Exception as e:
        logger.warning(f"Could not read query_timestamp from {csv_path}: {e}")
        return None


def scan_files(data_dir: str, use_nominatim_fallback: bool) -> tuple[list[FileInfo], list[str]]:
    """Parse + identify every legacy csv.gz in data_dir."""
    import glob

    all_csvs = sorted(glob.glob(os.path.join(data_dir, "**/*.csv.gz"), recursive=True))
    infos, unparseable = [], []

    for csv_path in progress(all_csvs, desc="Scanning data files", unit="file"):
        try:
            parsed = parse_filename(csv_path)
        except ValueError:
            unparseable.append(csv_path)
            continue
        if parsed.run_date is not None:
            continue  # already a dated (post-migration) file

        info = FileInfo(
            csv_path=csv_path,
            slug=parsed.slug,
            width=parsed.width_meters,
            height=parsed.height_meters,
            step=parsed.step_meters,
        )

        # Identity from the sibling JSON when possible
        json_path = csv_path.rsplit(".csv.gz", 1)[0] + ".json.gz"
        if os.path.exists(json_path):
            info.json_path = json_path
            data = strict_json_load(json_path)
            if data is not None:
                info.json_ok = True
                try:
                    info.city_name = data["city"]["name"]
                    info.state_name = data["city"]["state"]["name"]
                    info.country_name = data["city"]["country"]["name"]
                    info.center_lat = data["city"]["center"]["latitude"]
                    info.center_lon = data["city"]["center"]["longitude"]
                    info.identity_source = "json"
                except (KeyError, TypeError):
                    info.problems.append("json missing city fields")
            else:
                info.problems.append("json corrupt (NaN or invalid)")
                # Content is unusable for identity but structure may be fine;
                # try a lenient read just for the city block
                try:
                    with gzip.open(json_path, "rt", encoding="utf-8") as f:
                        data = json.load(f)  # lenient: accepts NaN
                    info.city_name = data["city"]["name"]
                    info.state_name = data["city"]["state"]["name"]
                    info.country_name = data["city"]["country"]["name"]
                    info.center_lat = data["city"]["center"]["latitude"]
                    info.center_lon = data["city"]["center"]["longitude"]
                    info.identity_source = "json"
                except Exception:
                    pass
        else:
            info.problems.append("no sibling json")

        if info.city_name is None and use_nominatim_fallback:
            query = slug_to_query_str(info.slug.replace("_", ", "))
            loc = get_city_location_data(query)
            if loc and loc.city:
                info.city_name = loc.city
                info.state_name = loc.state
                info.country_name = loc.country
                info.identity_source = "nominatim"
            else:
                info.identity_source = "failed"
                info.problems.append("identity resolution failed")
        elif info.city_name is None:
            info.identity_source = "failed"

        info.run_date = read_run_date(csv_path)
        if info.run_date is None:
            info.problems.append("no readable query_timestamp")

        infos.append(info)

    return infos, unparseable


def choose_baseline(files: list[FileInfo], city_id: str) -> FileInfo:
    """
    Pick the baseline file for a (city_id, geometry) group: prefer the file
    whose slug is the canonical full-name slug, then the newest data.
    """

    def sort_key(f: FileInfo):
        return (
            f.slug == city_id,  # canonical slug wins
            f.run_date or pd.Timestamp(0, tz="UTC"),
        )  # then newest

    return max(files, key=sort_key)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--data-dir", default=get_default_data_dir())
    parser.add_argument(
        "--db-path", default=None, help="default: {data-dir}/streetscape_tracker.db"
    )
    parser.add_argument(
        "--execute", action="store_true", help="Apply changes (default is a dry run)"
    )
    parser.add_argument(
        "--no-nominatim",
        action="store_true",
        help="Skip the Nominatim fallback for files without JSON",
    )
    parser.add_argument(
        "--log-level", default="WARNING", choices=["DEBUG", "INFO", "WARNING", "ERROR"]
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    db_path = args.db_path or db.get_default_db_path(args.data_dir)
    mode = "EXECUTE" if args.execute else "DRY RUN"
    print(f"=== Streetscape Tracker migration ({mode}) ===")
    print(f"Data dir: {args.data_dir}")
    print(f"Catalog:  {db_path}\n")

    infos, unparseable = scan_files(args.data_dir, not args.no_nominatim)

    resolved = [i for i in infos if i.city_name and i.run_date is not None]
    failed = [i for i in infos if i not in resolved]

    # Group by canonical identity, then by geometry within each city
    by_city: dict[str, list[FileInfo]] = defaultdict(list)
    for info in resolved:
        city_id = db.derive_city_id(info.city_name, info.state_name, info.country_name)
        by_city[city_id].append(info)

    plan_rows = []  # (city_id, baseline FileInfo, aliases, variant files)
    for city_id, files in sorted(by_city.items()):
        # Canonical geometry: that of the LARGEST capture for this city
        # (by compressed size, tie-break newest). Junk/degenerate test
        # files can be newer and even reverse-geocode to a real city's
        # identity (e.g. a 4-point "balance--tennessee" file resolving to
        # Nashville) — size, not recency, identifies the real capture.
        biggest = max(files, key=lambda f: (os.path.getsize(f.csv_path), f.run_date))
        canonical_geom = biggest.geometry
        group = [f for f in files if f.geometry == canonical_geom]
        variants = [f for f in files if f.geometry != canonical_geom]

        baseline = choose_baseline(group, city_id)
        aliases = sorted({f.slug for f in group if f.slug != city_id})
        dup_files = [f for f in group if f is not baseline]
        plan_rows.append((city_id, baseline, aliases, dup_files, variants))

    # ── Report ────────────────────────────────────────────────────────────
    n_alias_groups = sum(1 for _, _, a, _, _ in plan_rows if a)
    n_dupes = sum(len(d) for _, _, _, d, _ in plan_rows)
    n_variants = sum(len(v) for _, _, _, _, v in plan_rows)
    n_corrupt_json = sum(1 for i in resolved if "json corrupt (NaN or invalid)" in i.problems)

    print(f"Legacy csv.gz files found:       {len(infos)}")
    print(f"  identity resolved:             {len(resolved)}")
    print(f"  identity FAILED:               {len(failed)}")
    print(f"  unparseable filenames:         {len(unparseable)}")
    print(f"Canonical cities:                {len(plan_rows)}")
    print(f"  cities with alias slugs:       {n_alias_groups}")
    print(f"  duplicate files (aliased):     {n_dupes}")
    print(f"  geometry-variant files:        {n_variants}")
    print(f"  corrupt JSONs to regenerate:   {n_corrupt_json}")

    if failed:
        print("\nFiles with FAILED identity (not registered):")
        for i in failed:
            print(f"  {i.csv_filename}: {'; '.join(i.problems) or 'unknown'}")
    if unparseable:
        print("\nUnparseable filenames (not registered):")
        for p in unparseable:
            print(f"  {os.path.basename(p)}")

    alias_rows = [(cid, a) for cid, _, a, _, _ in plan_rows if a]
    if alias_rows:
        print("\nAlias consolidations:")
        for cid, aliases in alias_rows:
            print(f"  {cid}  <-  {', '.join(aliases)}")

    variant_rows = [(cid, v) for cid, _, _, _, v in plan_rows if v]
    if variant_rows:
        print("\nGeometry-variant files (left unregistered; review manually):")
        for cid, variants in variant_rows:
            for v in variants:
                print(f"  {cid}: {v.csv_filename}")

    if not args.execute:
        print("\nDry run complete. Re-run with --execute to apply.")
        return 0

    # ── Execute ───────────────────────────────────────────────────────────
    conn = db.connect(db_path)
    n_cities = n_runs = n_regen = n_skipped = 0

    for city_id, baseline, aliases, _dup_files, _ in progress(
        plan_rows, desc="Registering", unit="city"
    ):
        already = conn.execute(
            "SELECT 1 FROM runs WHERE csv_filename = ?", (baseline.csv_filename,)
        ).fetchone()
        if already:
            n_skipped += 1
            continue

        df = load_city_csv_file(baseline.csv_path)
        run_date = baseline.run_date.date()

        registered_id = db.register_city(
            conn,
            city_name=baseline.city_name,
            state_name=baseline.state_name,
            state_code=get_state_abbreviation(baseline.state_name),
            country_name=baseline.country_name,
            country_code=get_country_code(baseline.country_name),
            center_lat=float(df["query_lat"].mean()),
            center_lon=float(df["query_lon"].mean()),
            grid_width_m=baseline.width,
            grid_height_m=baseline.height,
            step_m=baseline.step,
            notes=f"migrated from {baseline.csv_filename}",
        )
        assert registered_id == city_id, (registered_id, city_id)
        n_cities += 1

        for alias in aliases:
            db.add_alias(conn, alias, city_id)

        # Regenerate the sibling JSON if corrupt (or missing), pinned to run_date
        json_path = baseline.json_path
        if json_path is None or not baseline.json_ok:
            json_path = generate_city_metadata_summary_as_json(
                baseline.csv_path,
                df,
                baseline.city_name,
                baseline.state_name,
                baseline.country_name,
                baseline.width,
                baseline.height,
                baseline.step,
                force_recreate_file=True,
                run_date=run_date,
                is_baseline=True,
            )
            n_regen += 1

        stats = calculate_run_stats(df, run_date)
        db.register_run(
            conn,
            city_id=city_id,
            run_date=run_date,
            csv_filename=baseline.csv_filename,
            json_filename=os.path.basename(json_path),
            is_baseline=True,
            finished_at=baseline.run_date.isoformat(),
            **stats,
        )
        n_runs += 1

    db.assign_schedule(conn, cycle_days=90)

    print(
        f"\nDone. Registered {n_cities} cities, {n_runs} baseline runs "
        f"({n_skipped} already registered, {n_regen} JSONs regenerated)."
    )
    if n_dupes or n_variants:
        print(
            "Aliased duplicate and geometry-variant files were left on disk "
            "unregistered — see the report above to review/remove them."
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
