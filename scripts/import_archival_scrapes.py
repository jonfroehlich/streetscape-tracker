#!/usr/bin/env python3
"""
One-shot import: translate the 2023-2024 archival scrapes from the
predecessor project (gsv-capture-dates/gsv-bias-scraper) into the v2
catalog as is_baseline=1 dated runs (issue #93).

Each archival dataset becomes a dated csv.gz in the current 9-column
schema plus a per-run JSON summary, registered against the catalog:

1. Translate the source CSV (two format generations, see MANIFEST) into
   config.METADATA_DTYPES columns. Empty status -> OK; literal "None"
   pano_id/date -> null; capture_date YYYY-MM -> YYYY-MM-01;
   copyright_info null everywhere (never captured; the run JSON carries
   copyright_info_available=false); query_timestamp = the file's LAST
   git commit date (the honest precision of the scrape date — the commit
   that produced the content actually imported from the checkout).
2. Derive the run's grid extent from the sibling bounding_box.json
   (query-point extent when absent) for the filename's width/height;
   step is always 30 m (the predecessor's grid).
3. Resolve the city in the catalog; unknown cities are geocoded once via
   rate-limited Nominatim with the archival bbox center as
   disambiguation (e.g. Hamilton, New Zealand vs Hamilton, Ohio) and
   registered with a freshly geocoded rectangle at the default 20 m step
   (the archival 30 m grid lives only in the run's filename).
4. Register the run with is_baseline=1. No diffs are computed: archival
   runs are each city's earliest, and cli.py only auto-diffs consecutive
   same-geometry runs anyway.

Caveat: the v1-format files (Seattle, Point Roberts) recorded pano
coordinates, not query coordinates, on OK rows — their grid/coverage
stats are approximate; pano-level data is exact.

Idempotent: already-registered files are skipped, so it is safe to re-run.
Self-repairing: when a manifest date is corrected after an import, re-running
detects the already-imported run under its old date (same city + geometry,
different date), and with --execute re-dates it in place — old artifacts and
catalog row are replaced by freshly translated ones under the corrected date.
Datasets in SUPERSEDED are likewise purged if a past manifest imported them.
After a repair, publish with `./sync_data_to_server.sh --delete` so the
stale-dated files disappear from the web server too.

Usage:
    python scripts/import_archival_scrapes.py                # dry run
    python scripts/import_archival_scrapes.py --execute      # apply
    python scripts/import_archival_scrapes.py --source-root DIR --data-dir DIR
"""

import argparse
import gzip
import json
import logging
import os
import sys
from dataclasses import dataclass
from datetime import date

import pandas as pd
from geopy.distance import geodesic

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from streetscape_metadata_tracker import db  # noqa: E402
from streetscape_metadata_tracker.analysis import calculate_run_stats  # noqa: E402
from streetscape_metadata_tracker.config import METADATA_DTYPES  # noqa: E402
from streetscape_metadata_tracker.fileutils import load_city_csv_file  # noqa: E402
from streetscape_metadata_tracker.geoutils import (  # noqa: E402
    get_city_location_data,
    get_country_code,
    get_state_abbreviation,
)
from streetscape_metadata_tracker.json_summarizer import (  # noqa: E402
    generate_aggregate_v2,
    generate_city_metadata_summary_as_json,
)
from streetscape_metadata_tracker.naming import generate_run_filename  # noqa: E402
from streetscape_metadata_tracker.paths import get_default_data_dir  # noqa: E402

logger = logging.getLogger("archival_import")

ARCHIVAL_STEP_M = 30  # the predecessor scraper's grid step
DEFAULT_SOURCE_ROOT = os.path.expanduser("~/Git/gsv-capture-dates/gsv-bias-scraper/data")
NEW_CITY_STEP_M = 20  # frozen step for freshly registered cities

COLUMNS = list(METADATA_DTYPES.keys())


@dataclass(frozen=True)
class ArchivalDataset:
    """One source CSV to import."""

    rel_csv: str  # csv path relative to --source-root
    query: str  # geocodable query string; also the catalog lookup key
    run_date: date  # git first-commit date of the csv (the scrape date)
    fmt: str  # 'v1' | 'v2' | 'v2_headerless'
    # (city_name, state_name, country_name) override for locality-grade
    # datasets that Nominatim resolves to their parent city (East Hollywood
    # -> Los Angeles, Reinsletta -> Bodø). Registration then uses these
    # names with the archival extent as the frozen geometry — no geocoding.
    identity: tuple[str, str, str] | None = None


# Run dates are the LAST git commit date of each CSV in the
# gsv-capture-dates repo — the commit whose content the checkout (and thus
# this import) carries. File mtimes are checkout times, and first-commit
# dates are unusable: the original import derived them via `git log
# --follow`, whose rename detection chained several single-commit files to
# unrelated 2023-11-03 ancestors, misdating 10 runs by up to ~10 months
# (caught post-import; the repair path below re-dates them). Do not derive
# rel_csv from the query: the Washington D.C. basename carries a trailing
# period.
# Queries are pinned to each city's catalog identity (sanitized, they equal
# the canonical city_id), so the catalog lookup never needs Nominatim once a
# city is registered — required for the re-date repair, whose dry run must
# resolve cities without geocoding to see their already-imported runs.
MANIFEST = [
    ArchivalDataset(
        "Berkeley/Berkeley_30_coords.csv",
        "Berkeley, California, United States",
        date(2023, 11, 7),
        "v2_headerless",
    ),
    ArchivalDataset(
        "Burnaby, Canada/Burnaby, Canada_30_coords.csv",
        "Burnaby, British Columbia, Canada",
        date(2024, 4, 11),
        "v2",
    ),
    ArchivalDataset(
        "Chicago/Chicago_30_coords.csv", "Chicago, Illinois", date(2023, 11, 7), "v2_headerless"
    ),
    ArchivalDataset(
        "Cuenca, Ecuador/Cuenca, Ecuador_30_coords.csv", "Cuenca, Ecuador", date(2023, 12, 12), "v2"
    ),
    ArchivalDataset(
        "Denison, Texas/Denison, Texas_30_coords.csv",
        "Denison, Texas, United States",
        date(2023, 12, 5),
        "v2",
    ),
    ArchivalDataset(
        "East Hollywood, CA/East Hollywood, CA_30_coords.csv",
        "East Hollywood, Los Angeles, CA",
        date(2024, 9, 16),
        "v2",
        identity=("East Hollywood", "California", "United States"),
    ),
    ArchivalDataset(
        "Hamilton, New Zealand/Hamilton, New Zealand_30_coords.csv",
        "Hamilton City, Waikato, New Zealand",
        date(2024, 7, 16),
        "v2",
    ),
    # Queries below match existing catalog display_names exactly (these
    # cities gained Dec-2024 baselines via the legacy migration; the
    # archival runs become their earlier first runs). La Piedad attaches
    # to the real identity, not the la_piedad_mx junk-slug squatter (#92).
    ArchivalDataset(
        "La Piedad de Cabadas, Mexico/La Piedad de Cabadas, Mexico_30_coords.csv",
        "La Piedad, Michoacán, Mexico",
        date(2024, 10, 3),
        "v2",
    ),
    ArchivalDataset(
        "Mendota, Illinois/Mendota, Illinois_30_coords.csv",
        "Mendota, Illinois, United States",
        date(2024, 7, 8),
        "v2",
    ),
    ArchivalDataset(
        "Mt Vernon, Ohio/Mt Vernon, Ohio_30_coords.csv",
        "Mount Vernon, Ohio, United States",
        date(2024, 5, 8),
        "v2",
    ),
    ArchivalDataset(
        "Oradell, New Jersey/Oradell, New Jersey_30_coords.csv",
        "Oradell, New Jersey, United States",
        date(2023, 12, 5),
        "v2",
    ),
    ArchivalDataset(
        "Pittsburgh, Pennsylvania/Pittsburgh, Pennsylvania_30_coords.csv",
        "Pittsburgh, Pennsylvania, United States",
        date(2023, 12, 12),
        "v2",
    ),
    ArchivalDataset(
        "Point Roberts, WA/Point Roberts, WA_30_coords.csv",
        "Point Roberts, Washington, United States",
        date(2023, 11, 5),
        "v1",
    ),
    ArchivalDataset(
        "Reinsletta, Norway/Reinsletta, Norway_30_coords.csv",
        "Reinsletta, Norway",
        date(2024, 3, 25),
        "v2",
        identity=("Reinsletta", "Nordland", "Norway"),
    ),
    ArchivalDataset(
        "Riverdale Park, Maryland/Riverdale Park, Maryland_30_coords.csv",
        "Riverdale Park, Maryland, United States",
        date(2024, 2, 14),
        "v2",
    ),
    ArchivalDataset("Seattle/Seattle_30_coords.csv", "Seattle, WA", date(2023, 11, 6), "v1"),
    # Query matches the catalog display_name exactly: "St. Louis, Missouri"
    # alone resolves nothing, and re-geocoding risks identity drift
    ArchivalDataset(
        "St. Louis, Missouri/St. Louis, Missouri_30_coords.csv",
        "St. Louis, Missouri, United States",
        date(2024, 3, 19),
        "v2",
    ),
    ArchivalDataset(
        "Swissvale, Pennsylvania/Swissvale, Pennsylvania_30_coords.csv",
        "Swissvale, Pennsylvania, United States",
        date(2024, 2, 21),
        "v2",
    ),
    ArchivalDataset(
        "Washington D.C/Washington D.C._30_coords.csv",
        "Washington, District of Columbia, United States",
        date(2024, 4, 11),
        "v2",
    ),
    # Query matches the catalog display_name exactly: a fresh geocode can
    # return "Zürich" (umlaut) and would mint a duplicate city_id
    ArchivalDataset(
        "Zurich, Switzerland/Zurich, Switzerland_30_coords.csv",
        "Zurich, Zurich, Switzerland",
        date(2023, 12, 12),
        "v2",
    ),
]

# Source datasets deliberately NOT imported (always listed in the report).
SKIPPED = [
    ("Burnaby, Canada - Old/Burnaby, Canada_30_coords.csv", "35-row aborted run"),
    ("East Hollywood, CA - Old/East Hollywood, CA_30_coords.csv", "35-row aborted run"),
    (
        "La Piedad de Cabadas, Mexico - 1k x 1k/La Piedad de Cabadas, Mexico_30_coords.csv",
        "35-row aborted run",
    ),
    (
        "Reinsletta, Norway 7500x5000m/Reinsletta, Norway_30_coords.csv",
        "smaller variant of the imported Reinsletta run, same scrape date "
        "(runs are unique per city+date)",
    ),
    (
        "Washington D.C 10000x10000/Washington D.C._30_coords.csv",
        "smaller variant of the imported D.C. run, same scrape date "
        "(runs are unique per city+date; both files landed in the same "
        "2024-04-11 commit)",
    ),
]

# Runs a PAST manifest imported that the current one no longer includes.
# The date-correction repair collapsed both D.C. datasets onto 2024-04-11
# (see MANIFEST comment), so the smaller variant — imported before the
# collision existed — must be purged before the main D.C. run can take its
# true date. Keyed by exact registered csv_filename; ignored when absent.
SUPERSEDED = [
    (
        "washington--district-of-columbia--united-states_width_9899_height_9980"
        "_step_30_2024-04-11.csv.gz",
        "superseded by the full-extent D.C. run re-dated to 2024-04-11",
    ),
]

V2_HEADER = ["lat", "lon", "query_lat", "query_lon", "pano_id", "date", "status"]


def sniff_format(path: str) -> str:
    """Detect the source format from the first line; see MANIFEST fmt."""
    with open(path, encoding="utf-8") as f:
        first = f.readline().strip()
    fields = first.split(",")
    if fields[:3] == V2_HEADER[:3]:
        return "v2"
    if len(fields) == 5:
        return "v1"
    if len(fields) == 7:
        return "v2_headerless"
    raise ValueError(f"Unrecognized archival CSV format ({len(fields)} fields): {path}")


def read_archival_csv(path: str, fmt: str, run_date: date) -> pd.DataFrame:
    """
    Translate one archival CSV into the current 9-column schema (raw
    string-typed, like a freshly written run CSV).

    The old scraper wrote status only on failure (empty = pano found) and
    the literal string "None" for missing pano_id/date. v1 files have no
    separate query coordinates: their first two columns are pano
    coordinates on OK rows and query coordinates on failures.
    """
    sniffed = sniff_format(path)
    if sniffed != fmt:
        raise ValueError(f"{path}: manifest says format {fmt!r} but file sniffs as {sniffed!r}")

    if fmt == "v1":
        raw = pd.read_csv(
            path,
            header=None,
            names=["c1", "c2", "pano_id", "capture_date", "status"],
            dtype=str,
            keep_default_na=False,
        )
        query_lat, query_lon = raw["c1"], raw["c2"]
        pano_lat, pano_lon = raw["c1"], raw["c2"]
    else:
        raw = pd.read_csv(
            path,
            header=0 if fmt == "v2" else None,
            names=V2_HEADER,
            dtype=str,
            keep_default_na=False,
        )
        raw = raw.rename(columns={"date": "capture_date"})
        query_lat, query_lon = raw["query_lat"], raw["query_lon"]
        pano_lat, pano_lon = raw["lat"], raw["lon"]

    ok = raw["status"].str.strip() == ""
    df = pd.DataFrame(
        {
            "query_lat": query_lat,
            "query_lon": query_lon,
            "query_timestamp": f"{run_date.isoformat()}T00:00:00+00:00",
            "pano_lat": pano_lat.where(ok, ""),
            "pano_lon": pano_lon.where(ok, ""),
            "pano_id": raw["pano_id"].replace("None", ""),
            "capture_date": raw["capture_date"].replace("None", ""),
            "copyright_info": "",  # never captured; unknown, not blank-Google
            "status": raw["status"].where(~ok, "OK"),
        },
        columns=COLUMNS,
    )

    # Old scraper wrote YYYY-MM; current convention is first-of-month
    month_only = df["capture_date"].str.match(r"^\d{4}-\d{2}$")
    df.loc[month_only, "capture_date"] += "-01"
    return df


def dims_from_extent(csv_path: str, df: pd.DataFrame) -> tuple[int, int, float, float]:
    """
    Grid extent for the output filename: (width_m, height_m, center_lat,
    center_lon). Prefers the sibling bounding_box.json; axis values are
    min/max-normalized (some sidecars have swapped extents). Falls back to
    the translated query-point extent when no sidecar exists.
    """
    bbox_path = os.path.join(os.path.dirname(csv_path), "bounding_box.json")
    if os.path.exists(bbox_path):
        with open(bbox_path, encoding="utf-8") as f:
            bb = json.load(f)
        south, north = sorted((float(bb["ymin"]), float(bb["ymax"])))
        west, east = sorted((float(bb["xmin"]), float(bb["xmax"])))
    else:
        lats = pd.to_numeric(df["query_lat"])
        lons = pd.to_numeric(df["query_lon"])
        south, north = float(lats.min()), float(lats.max())
        west, east = float(lons.min()), float(lons.max())

    mid_lat = (north + south) / 2
    width = geodesic((mid_lat, west), (mid_lat, east)).meters
    height = geodesic((south, west), (north, west)).meters
    return max(1, round(width)), max(1, round(height)), mid_lat, (west + east) / 2


def dims_from_boundingbox_raw(loc) -> tuple[int, int]:
    """
    Frozen-geometry rectangle from a geocoded location's Nominatim
    boundingbox ([south, north, west, east] strings). Mirrors
    geoutils.get_search_dimensions but reuses the already-disambiguated
    location instead of re-geocoding the bare query.
    """
    bbox = loc.raw.get("boundingbox") if hasattr(loc, "raw") else None
    if not bbox:
        logger.warning(f"No boundingbox for {loc}; using 1000x1000 default")
        return 1000, 1000
    south, north, west, east = map(float, bbox)
    mid_lat = (north + south) / 2
    width = geodesic((mid_lat, west), (mid_lat, east)).meters
    height = geodesic((south, west), (north, west)).meters
    return max(1, round(width)), max(1, round(height))


def resolve_or_register_city(
    conn,
    entry: ArchivalDataset,
    center: tuple[float, float],
    extent: tuple[int, int],
    use_nominatim: bool,
    execute: bool,
):
    """
    Resolve the entry's city in the catalog, registering it if unknown.
    Returns (CityRow | None, note); a None CityRow means not importable
    yet (dry run for a new city, or skipped).

    New cities freeze a rectangle at the default 20 m step — the frozen
    geometry governs future scheduled runs, while the archival run keeps
    its own 30 m geometry in its filename. Identity + rectangle come from
    a fresh geocode (the archival bbox center disambiguates: Hamilton NZ,
    not Hamilton OH), or from the manifest identity + archival extent for
    locality-grade datasets that Nominatim resolves to a parent city.
    """
    city_row = db.resolve_city(conn, entry.query)
    # Identity-carrying entries also resolve via their derived canonical id,
    # so a registered city is found without geocoding (the query alone may
    # not match: "East Hollywood, Los Angeles, CA" was a geocoder hint)
    if city_row is None and entry.identity is not None:
        city_row = db.resolve_city(conn, db.derive_city_id(*entry.identity))
    if city_row is not None:
        return city_row, "existing city"

    if entry.identity is not None:
        city_name, state_name, country_name = entry.identity
        if not execute:
            cid = db.derive_city_id(city_name, state_name, country_name)
            return None, f"NEW: would register from manifest identity ({cid})"
        city_id = db.register_city(
            conn,
            city_name=city_name,
            state_name=state_name,
            state_code=get_state_abbreviation(state_name),
            country_name=country_name,
            country_code=get_country_code(country_name),
            center_lat=center[0],
            center_lon=center[1],
            grid_width_m=extent[0],
            grid_height_m=extent[1],
            step_m=NEW_CITY_STEP_M,
        )
        return (
            db.resolve_city(conn, city_id),
            f"NEW city registered from manifest identity ({extent[0]}x{extent[1]}m)",
        )

    if not use_nominatim:
        return None, "needs geocoding (--no-nominatim)"
    if not execute:
        return None, "NEW: would geocode + register"

    loc = get_city_location_data(entry.query, center[0], center[1])
    if loc is None or not loc.city:
        return None, "geocoding FAILED"
    width, height = dims_from_boundingbox_raw(loc)
    city_id = db.register_city(
        conn,
        city_name=loc.city,
        state_name=loc.state,
        state_code=get_state_abbreviation(loc.state),
        country_name=loc.country,
        country_code=get_country_code(loc.country),
        center_lat=loc.latitude,
        center_lon=loc.longitude,
        grid_width_m=width,
        grid_height_m=height,
        step_m=NEW_CITY_STEP_M,
    )
    return db.resolve_city(conn, city_id), f"NEW city registered ({width}x{height}m)"


def find_misdated_run(conn, city_id: str, width: int, height: int, run_date: date):
    """
    An already-imported archival run of the same city and grid extent under a
    DIFFERENT date — the signature of a manifest date correction (the original
    import derived dates from rename-polluted `git --follow` history). Matched
    on the filename's geometry prefix in Python, not SQL LIKE, so underscores
    in city ids can't act as wildcards.
    """
    prefix = f"{city_id}_width_{width}_height_{height}_step_{ARCHIVAL_STEP_M}_"
    rows = conn.execute(
        """SELECT * FROM runs
           WHERE city_id = ? AND provider = 'gsv' AND is_baseline = 1
             AND run_date != ?""",
        (city_id, run_date.isoformat()),
    ).fetchall()
    return next((r for r in rows if r["csv_filename"].startswith(prefix)), None)


def delete_run(conn, data_dir: str, run_row, execute: bool) -> list[str]:
    """
    Remove one archival run: its csv/json artifacts first, then the catalog
    row (file-first ordering, like purge_tainted_runs, so a failed delete
    can't orphan a published file behind a missing row). Returns the artifact
    basenames for the stale-publish report. Refuses runs referenced by
    run_diffs — archival runs never are, so a reference means this row is not
    what we think it is.
    """
    run_id = run_row["run_id"]
    n_diffs = conn.execute(
        "SELECT COUNT(*) FROM run_diffs WHERE from_run_id = ? OR to_run_id = ?",
        (run_id, run_id),
    ).fetchone()[0]
    if n_diffs:
        raise RuntimeError(
            f"Refusing to delete run {run_row['csv_filename']}: "
            f"{n_diffs} run_diffs row(s) reference it"
        )
    json_filename = run_row["json_filename"] or run_row["csv_filename"].replace(
        ".csv.gz", ".json.gz"
    )
    basenames = [run_row["csv_filename"], json_filename]
    if execute:
        for name in basenames:
            path = os.path.join(data_dir, name)
            if os.path.exists(path):
                os.remove(path)
        conn.execute("DELETE FROM runs WHERE run_id = ?", (run_id,))
        conn.commit()
    return basenames


def load_manifest_override(
    path: str,
) -> tuple[list[ArchivalDataset], list[tuple[str, str]]]:
    """
    Load a manifest override — for tests. Either a JSON list of dataset dicts
    (legacy) or an object {"datasets": [...], "superseded": [[csv, reason]]}.
    """
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    entries = raw["datasets"] if isinstance(raw, dict) else raw
    superseded = [tuple(s) for s in raw.get("superseded", [])] if isinstance(raw, dict) else []
    datasets = [
        ArchivalDataset(
            rel_csv=e["rel_csv"],
            query=e["query"],
            run_date=date.fromisoformat(e["run_date"]),
            fmt=e["fmt"],
            identity=(tuple(e["identity"]) if e.get("identity") else None),
        )
        for e in entries
    ]
    return datasets, superseded


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--source-root", default=DEFAULT_SOURCE_ROOT, help="gsv-bias-scraper data directory"
    )
    parser.add_argument("--data-dir", default=get_default_data_dir())
    parser.add_argument(
        "--db-path", default=None, help="default: {data-dir}/streetscape_tracker.db"
    )
    parser.add_argument(
        "--manifest", default=None, help="JSON manifest overriding the embedded one (tests)"
    )
    parser.add_argument(
        "--execute", action="store_true", help="Apply changes (default is a dry run)"
    )
    parser.add_argument(
        "--no-nominatim",
        action="store_true",
        help="Never geocode; unknown cities are reported and skipped",
    )
    parser.add_argument(
        "--no-publish-json",
        action="store_true",
        help="Skip regenerating the aggregate cities.json.gz",
    )
    parser.add_argument(
        "--log-level", default="WARNING", choices=["DEBUG", "INFO", "WARNING", "ERROR"]
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    if args.manifest:
        manifest, superseded = load_manifest_override(args.manifest)
    else:
        manifest, superseded = MANIFEST, SUPERSEDED
    db_path = args.db_path or db.get_default_db_path(args.data_dir)
    mode = "EXECUTE" if args.execute else "DRY RUN"
    print(f"=== Archival scrape import ({mode}) ===")
    print(f"Source:  {args.source_root}")
    print(f"Data dir: {args.data_dir}")
    print(f"Catalog:  {db_path}\n")

    conn = db.connect(db_path)
    n_imported = n_already = n_skipped = n_redated = n_purged = 0
    report_lines = []
    stale_artifacts = []  # removed basenames, possibly still on the server
    pending_removal = set()  # csv_filenames slated for deletion this pass

    # Purge superseded runs first: the D.C. date correction re-dates the main
    # run onto the day its smaller variant occupies, so the variant must go
    # before the re-date can register (runs are UNIQUE per city+date).
    for fname, reason in superseded:
        row = conn.execute("SELECT * FROM runs WHERE csv_filename = ?", (fname,)).fetchone()
        if row is None:
            continue
        report_lines.append(f"  PURGE    {fname}: {reason}")
        pending_removal.add(fname)
        n_purged += 1
        stale_artifacts.extend(delete_run(conn, args.data_dir, row, args.execute))

    for entry in manifest:
        src_path = os.path.join(args.source_root, entry.rel_csv)
        label = entry.rel_csv
        try:
            df = read_archival_csv(src_path, entry.fmt, entry.run_date)
        except (OSError, ValueError) as e:
            report_lines.append(f"  ERROR    {label}: {e}")
            n_skipped += 1
            continue
        width, height, center_lat, center_lon = dims_from_extent(src_path, df)
        n_ok = int((df["status"] == "OK").sum())

        city_row, note = resolve_or_register_city(
            conn,
            entry,
            (center_lat, center_lon),
            (width, height),
            use_nominatim=not args.no_nominatim,
            execute=args.execute,
        )
        if city_row is None:
            if note.startswith("NEW:"):
                # Dry run: the canonical city_id (and thus the output
                # filename) comes from the geocoded names, so it is only
                # known at execute time. A "new" entry may even resolve to
                # an existing city once geocoded.
                report_lines.append(
                    f"  IMPORT   {label}: {note} ({len(df)} rows, {n_ok} OK, {width}x{height}m)"
                )
                n_imported += 1
            else:
                report_lines.append(f"  SKIP     {label}: {note}")
                n_skipped += 1
            continue

        csv_filename = (
            generate_run_filename(city_row.city_id, width, height, ARCHIVAL_STEP_M, entry.run_date)
            + ".csv.gz"
        )

        if conn.execute("SELECT 1 FROM runs WHERE csv_filename = ?", (csv_filename,)).fetchone():
            report_lines.append(f"  ALREADY  {label} -> {csv_filename}")
            n_already += 1
            continue

        # A same-city+extent run under a different date is this dataset,
        # imported before a manifest date correction: re-date it (replace row
        # and artifacts) instead of importing a duplicate.
        misdated = find_misdated_run(conn, city_row.city_id, width, height, entry.run_date)

        conflict = conn.execute(
            "SELECT csv_filename FROM runs WHERE city_id = ? AND provider = 'gsv' AND run_date = ?",
            (city_row.city_id, entry.run_date.isoformat()),
        ).fetchone()
        # Ignore a conflicting row already slated for removal this pass (a
        # dry run leaves purged rows in place but the plan must match what
        # --execute would do)
        if conflict and conflict["csv_filename"] not in pending_removal:
            report_lines.append(
                f"  SKIP     {label}: {city_row.city_id} already has a gsv run on {entry.run_date}"
            )
            n_skipped += 1
            continue

        if misdated is not None:
            report_lines.append(
                f"  REDATE   {label}: {misdated['run_date']} -> {entry.run_date} "
                f"({misdated['csv_filename']} -> {csv_filename})"
            )
            n_redated += 1
            stale_artifacts.extend(delete_run(conn, args.data_dir, misdated, args.execute))
        else:
            report_lines.append(
                f"  IMPORT   {label} -> {csv_filename} "
                f"({len(df)} rows, {n_ok} OK, {width}x{height}m; {note})"
            )

        if not args.execute:
            n_imported += 1
            continue

        csv_path = os.path.join(args.data_dir, csv_filename)
        with gzip.open(csv_path, "wt", encoding="utf-8", newline="") as f:
            df.to_csv(f, index=False)

        # Reload through the canonical loader so stats and JSON are computed
        # from the same dtypes as any live run
        df_loaded = load_city_csv_file(csv_path)
        json_path = generate_city_metadata_summary_as_json(
            csv_path,
            df_loaded,
            city_row.city_name,
            city_row.state_name,
            city_row.country_name,
            width,
            height,
            ARCHIVAL_STEP_M,
            force_recreate_file=True,
            run_date=entry.run_date,
            is_baseline=True,
        )
        stats = calculate_run_stats(df_loaded, entry.run_date)
        # api_requests stays NULL and no api_usage row: that budget was
        # spent in 2023/24, not today. No diff either — archival runs are
        # each city's earliest run.
        db.register_run(
            conn,
            city_id=city_row.city_id,
            run_date=entry.run_date,
            csv_filename=csv_filename,
            json_filename=os.path.basename(json_path),
            is_baseline=True,
            finished_at=f"{entry.run_date.isoformat()}T00:00:00+00:00",
            **stats,
        )
        n_imported += 1

    # ── Report ────────────────────────────────────────────────────────────
    verb = "Imported" if args.execute else "Would import"
    print(
        f"{verb}: {n_imported}   already registered: {n_already}   "
        f"skipped: {n_skipped}   re-dated: {n_redated}   purged: {n_purged}\n"
    )
    for line in report_lines:
        print(line)

    print("\nSource datasets deliberately not imported:")
    for rel, reason in SKIPPED:
        print(f"  {rel}: {reason}")
    print(
        "\nCaveat: v1-format files (Seattle, Point Roberts) recorded pano "
        "coordinates,\nnot query coordinates, on OK rows — grid/coverage "
        "stats are approximate."
    )

    if not args.execute:
        print("\nDry run complete. Re-run with --execute to apply.")
        return 0

    db.assign_schedule(conn, cycle_days=90)
    if not args.no_publish_json:
        generate_aggregate_v2(conn, args.data_dir)
        print(f"\nRegenerated aggregate: {os.path.join(args.data_dir, 'cities.json.gz')}")
    if stale_artifacts:
        print(
            "\nRemoved artifacts that may still be published; run "
            "./sync_data_to_server.sh --delete to drop them from the server:"
        )
        for name in stale_artifacts:
            print(f"  {name}")
    print(f"\nDone. {n_imported} baseline runs registered.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
