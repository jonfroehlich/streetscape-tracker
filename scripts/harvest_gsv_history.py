#!/usr/bin/env python3
"""
Harvest the FULL official-Google capture-date history for a city (issue #2).

Unlike a normal collection run (which records one *current* capture date per
grid point), this sweeps the city's frozen sampling grid and pulls every
official Google panorama's capture month from an unpublished endpoint — the
"all previous capture dates for an area" issue #2 asked for. There is no
guarantee that endpoint keeps working, so this banks the data now; it does NOT
require an API key.

It is intentionally OUT OF BAND from the cadenced run pipeline: it only reads a
city's frozen geometry from the catalog and writes a separate dated
`*_gsv_history_*.csv.gz` artifact, a `history_harvests` catalog row, and the
artifact's sibling capture-history summary `*_gsv_history_*.json.gz` (issue
#109). It does not touch runs, diffs or per-run JSON; the aggregate points at
the new summary on the next `scheduler regenerate-aggregate` or nightly tail.

The city must already be registered in the catalog (run a normal collection
first, which freezes the grid). History is near-static, so harvest a city once
and re-sweep only occasionally.

Usage:
    python scripts/harvest_gsv_history.py "Seattle, WA"
    python scripts/harvest_gsv_history.py "Seattle, WA" --harvest-date 2026-07-08
    python scripts/harvest_gsv_history.py "Seattle, WA" --force --connection-limit 2
    python scripts/harvest_gsv_history.py "Seattle, WA" --data-dir DIR --db-path PATH
"""

import argparse
import asyncio
import logging
import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from streetscape_metadata_tracker import db  # noqa: E402
from streetscape_metadata_tracker.download_gsv_history import (  # noqa: E402
    HarvestBlockedError,
    harvest_gsv_history_async,
)
from streetscape_metadata_tracker.fileutils import load_history_csv_file  # noqa: E402
from streetscape_metadata_tracker.json_summarizer import (  # noqa: E402
    generate_history_summary_as_json,
)
from streetscape_metadata_tracker.naming import generate_history_filename  # noqa: E402
from streetscape_metadata_tracker.paths import get_default_data_dir  # noqa: E402


def parse_args():
    p = argparse.ArgumentParser(
        description="Harvest full official-Google capture-date history for a "
        "city from an unpublished endpoint (issue #2)."
    )
    p.add_argument("city", help="City query or slug (must already be registered)")
    p.add_argument(
        "--data-dir",
        default=get_default_data_dir(),
        help="Directory for the output csv.gz (default: ./data)",
    )
    p.add_argument(
        "--db-path",
        default=None,
        help="Catalog DB path (default: <data-dir>/streetscape_tracker.db)",
    )
    p.add_argument(
        "--harvest-date", default=None, help="ISO date recorded for this harvest (default: today)"
    )
    p.add_argument(
        "--force", action="store_true", help="Re-harvest even if this date's file already exists"
    )
    p.add_argument(
        "--connection-limit",
        type=int,
        default=2,
        help="Max concurrent requests — keep low (default: 2)",
    )
    p.add_argument("--verbose", action="store_true", help="Debug logging")
    return p.parse_args()


async def _run(args) -> int:
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    harvest_date = date.fromisoformat(args.harvest_date) if args.harvest_date else date.today()
    db_path = args.db_path or db.get_default_db_path(args.data_dir)

    conn = db.connect(db_path)
    city = db.resolve_city(conn, args.city)
    if city is None:
        print(
            f"City {args.city!r} is not registered in the catalog. Run a "
            f'normal collection first (python streetscape_tracker.py "{args.city}") '
            f"so its grid geometry is frozen, then harvest history.",
            file=sys.stderr,
        )
        return 2

    base = generate_history_filename(
        city.city_id, city.grid_width_m, city.grid_height_m, city.step_m, harvest_date
    )
    output_csv_gz_path = os.path.join(args.data_dir, f"{base}.csv.gz")

    if os.path.exists(output_csv_gz_path) and not args.force:
        print(
            f"A history harvest already exists for {city.city_id} on "
            f"{harvest_date} ({os.path.basename(output_csv_gz_path)}). "
            f"Use --force to re-harvest."
        )
        return 0

    print(f"City: {city.display_name} ({city.city_id})")
    print(
        f"Grid: {city.grid_width_m}m x {city.grid_height_m}m, "
        f"step {city.step_m}m, centered at "
        f"{city.center_lat:.5f}, {city.center_lon:.5f}"
    )
    print(
        f"Harvesting official-Google capture-date history "
        f"(gentle mode, connection_limit={args.connection_limit})..."
    )

    try:
        result = await harvest_gsv_history_async(
            city_name=city.display_name,
            center_lat=city.center_lat,
            center_lon=city.center_lon,
            grid_width=city.grid_width_m,
            grid_height=city.grid_height_m,
            step_length=city.step_m,
            output_csv_gz_path=output_csv_gz_path,
            connection_limit=args.connection_limit,
        )
    except HarvestBlockedError as e:
        print(f"BLOCKED: {e}", file=sys.stderr)
        return 1

    db.register_history_harvest(
        conn,
        city_id=city.city_id,
        harvest_date=harvest_date,
        csv_filename=os.path.basename(output_csv_gz_path),
        grid_points_queried=result["grid_points"],
        unique_panos=result["unique_panos"],
        oldest_capture_date=result["oldest_capture_date"],
        newest_capture_date=result["newest_capture_date"],
        api_requests=result["api_requests"],
        started_at=result["started_at"],
        finished_at=result["finished_at"],
    )

    # The summary is written AFTER the catalog row, from the file just written
    # and read back through the same loader the backfill uses, so a summary
    # written here and one backfilled later cannot disagree (issue #109).
    json_path = generate_history_summary_as_json(
        output_csv_gz_path,
        load_history_csv_file(output_csv_gz_path),
        city_id=city.city_id,
        harvest_date=harvest_date,
        grid_points_queried=result["grid_points"],
        api_requests=result["api_requests"],
        started_at=result["started_at"],
        finished_at=result["finished_at"],
        force_recreate_file=True,
    )

    print(
        f"Done: {result['unique_panos']} unique official Google panos, "
        f"capture dates {result['oldest_capture_date']}..."
        f"{result['newest_capture_date']}, from {result['api_requests']} "
        f"searches."
    )
    print(f"Wrote {output_csv_gz_path}")
    print(f"Wrote {json_path}")
    return 0


def main() -> int:
    return asyncio.run(_run(parse_args()))


if __name__ == "__main__":
    sys.exit(main())
