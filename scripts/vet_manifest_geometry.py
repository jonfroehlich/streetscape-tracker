#!/usr/bin/env python3
"""
Preview the grid geometry ``register_frame.py --execute`` WOULD freeze for each
row of a city manifest, and what each grid would cost — without writing
anything.

    python scripts/vet_manifest_geometry.py --manifest panoramax_360_cities.csv
    python scripts/vet_manifest_geometry.py --manifest panoramax_360_cities.csv --limit 5
    python scripts/vet_manifest_geometry.py --manifest m.csv --csv /tmp/vet.csv

WHY
---
Grid geometry is frozen at registration and never re-derived, so a bad
geocode (a métropole, a different county, a polygon-less node) becomes a
permanent wrong rectangle. ``docs/worldwide_sampling.md`` asks for the geometry
to be computed and read BEFORE registering; this is that step as a command.
It calls ``register_frame.resolve_frame_geometry`` — the very function
``register_frame_city`` freezes from — so the preview cannot drift from the
registration it previews.

Per row it prints the geocode query that won (the manifest query, or
``register_frame.geocode_queries``' bare fallback), the OSM feature it matched,
the grid W x H after the 40 km cap, the offset from the geocoded center to the
manifest's GeoNames point (THE vetting number: #298's four bad matches sat
19.7-36.1 km off, the good ones within 5.5 km), and the request price of one
collection on each default channel plus Panoramax, from
``scheduler.estimate_requests`` — the same function the scheduler budgets with
(GSV grid points, Mapillary z14 tiles, Panoramax z15 tiles; all offline
arithmetic on the geometry).

NETWORK
-------
Nominatim only, through ``geoutils``' existing rate limiter (1.1 s between
requests); successful lookups are cached for the process, so a row costs one
or two geocodes. No provider API is called and the catalog is never opened.
It refuses to start on a ``makelab*`` host unless ``--allow-collection-host``:
a geocoding preview has no business sharing the nightly batch's IP.

Exit status: 0 when every row's geometry resolved unflagged, 1 when any row
failed or was flagged (offset over ``--flag-offset-km``, the 1000 x 1000 m
no-boundary default, the 40 km cap, or the bare fallback query winning), 64 on
a usage error. A flag asks for a reading, not necessarily a fix: a large city
is capped by design.
"""

import argparse
import contextlib
import csv
import logging
import math
import os
import socket
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts import register_frame as rf  # noqa: E402
from streetscape_metadata_tracker import db  # noqa: E402
from streetscape_metadata_tracker.scheduler import estimate_requests  # noqa: E402

logger = logging.getLogger(__name__)

USAGE_EXIT = 64  # the repo's usage-error code (scheduler.USAGE_EXIT_CODE)

# The offset past which a geocode is suspect. #298's batch split cleanly at
# it: nine good geocodes within 5.5 km, four bad ones at 19.7-36.1 km.
DEFAULT_FLAG_OFFSET_KM = 10.0

# get_search_dimensions' fallback when the geocode carries no bounding box.
# A grid of exactly this size means the boundary was not found, not that the
# commune is 1 km across.
NO_BOUNDARY_DIMS_M = (1000.0, 1000.0)

# The channels priced per row. gsv, gsv_streets and mapillary are
# default-membership (an enabled city is due on each the next night);
# kartaview and panoramax are the opt-in channels `scheduler enable-city`
# enrols behind their gates (#374) — KartaView at an estimate <= 1,000, which
# is this column. The other walks are not priced: each reads its grid run's
# census from the shared cache for 0 requests on a paired night (#290).
# gsv_streets and kartaview use the scheduler's geometry-only tier (no catalog,
# so no frozen street network and no prior run); for gsv_streets that is
# street-km from bbox area, which over-estimates by design.
PRICED_CHANNELS = ("gsv", "gsv_streets", "mapillary", "kartaview", "panoramax")

TABLE_COLUMNS = [
    "n",
    "city",
    "geonameid",
    "geocode_query",
    "osm_match",
    "center_lat",
    "center_lon",
    "width_m",
    "height_m",
    "gsv_points",
    "gsv_streets_samples",
    "mapillary_z14_tiles",
    "kartaview_requests",
    "panoramax_z15_tiles",
    "offset_km",
    "center",
    "flags",
]


def refuse_on_collection_host(allow: bool) -> None:
    """Refuse a makelab* host: the nightly batch's IP is not a scratch machine."""
    host = socket.gethostname().lower()
    if host.startswith("makelab") and not allow:
        print(
            f"Refusing to geocode from {host!r}: it is a production collection host. "
            f"Vet from a laptop, or pass --allow-collection-host.",
            file=sys.stderr,
        )
        raise SystemExit(USAGE_EXIT)


def _osm_match(loc) -> str:
    """'class/type' of the Nominatim result, e.g. 'boundary/administrative'."""
    raw = getattr(loc, "raw", None)
    if not isinstance(raw, dict):
        return ""
    return f"{raw.get('class', '?')}/{raw.get('type', '?')}"


def price_geometry(geometry: rf.FrameGeometry, step_m: int) -> dict[str, int]:
    """
    Requests one collection would make on each PRICED_CHANNELS channel, from
    the scheduler's own estimator over the geometry as the catalog would store
    it (``db.register_city`` truncates the dimensions to int).
    """
    city = db.CityRow(
        city_id="vet",
        display_name="vet",
        city_name="vet",
        state_name=None,
        state_code=None,
        country_name=None,
        country_code=None,
        center_lat=geometry.center_lat,
        center_lon=geometry.center_lon,
        grid_width_m=int(geometry.width_m),
        grid_height_m=int(geometry.height_m),
        step_m=int(step_m),
        created_at="",
        enabled=False,
        notes=None,
    )
    return {channel: estimate_requests(city, channel) for channel in PRICED_CHANNELS}


def vet_row(row, *, step_m, max_center_km, use_geonames_center, flag_offset_km) -> dict:
    """
    One manifest row's vetting record (a TABLE_COLUMNS dict minus ``n``).

    A row whose geometry cannot be resolved (nothing geocodes, or every
    geocode fails the center guard without ``use_geonames_center``) is
    returned with ``flags`` = ``FAILED: <reason>`` rather than raised, so one
    bad row does not hide the rest of the table.
    """
    record = dict.fromkeys(TABLE_COLUMNS, "")
    record.pop("n")
    record.update(city=row["city"], geonameid=row["geonameid"])
    # geoutils prints its boundary chatter to stdout; keep stdout for the table.
    with contextlib.redirect_stdout(sys.stderr):
        try:
            geometry = rf.resolve_frame_geometry(row, use_geonames_center, max_center_km)
        except ValueError as e:
            record["flags"] = f"FAILED: {e}"
            return record
        loc = rf.get_city_location_data(geometry.geocode_query)  # cached: no new request
    prices = price_geometry(geometry, step_m)

    flags = []
    if geometry.geocoded_offset_km > flag_offset_km:
        flags.append(f"OFFSET>{flag_offset_km:g}km")
    if (geometry.uncapped_width_m, geometry.uncapped_height_m) == NO_BOUNDARY_DIMS_M:
        flags.append("NO-BOUNDARY")
    if (geometry.width_m, geometry.height_m) != (
        geometry.uncapped_width_m,
        geometry.uncapped_height_m,
    ):
        flags.append(
            f"CAPPED-FROM-{geometry.uncapped_width_m:.0f}x{geometry.uncapped_height_m:.0f}"
        )
    if geometry.geocode_query != row["query_string"]:
        flags.append("FALLBACK-QUERY")

    record.update(
        geocode_query=geometry.geocode_query,
        osm_match=_osm_match(loc),
        center_lat=round(geometry.center_lat, 6),
        center_lon=round(geometry.center_lon, 6),
        width_m=int(geometry.width_m),
        height_m=int(geometry.height_m),
        gsv_points=prices["gsv"],
        gsv_streets_samples=prices["gsv_streets"],
        mapillary_z14_tiles=prices["mapillary"],
        kartaview_requests=prices["kartaview"],
        panoramax_z15_tiles=prices["panoramax"],
        offset_km=round(geometry.geocoded_offset_km, 1),
        center="GeoNames" if geometry.center_from_geonames else "geocoded",
        flags=" ".join(flags),
    )
    return record


def format_markdown(records: list[dict]) -> str:
    """The vetting table as Markdown, with a totals line over resolved rows."""
    header = (
        "| # | City | Geocode query | OSM match | Grid W x H (m) | GSV points "
        "| GSV walk samples (area proxy) | Mapillary z14 tiles | KartaView requests "
        "| Panoramax z15 tiles | Offset (km) | Center | Flags |"
    )
    lines = [header, "|" + "---|" * 13]
    for r in records:
        dims = f"{r['width_m']:,} x {r['height_m']:,}" if r["width_m"] != "" else ""
        lines.append(
            f"| {r['n']} | {r['city']} | {r['geocode_query']} | {r['osm_match']} | {dims} "
            f"| {_num(r['gsv_points'])} | {_num(r['gsv_streets_samples'])} "
            f"| {_num(r['mapillary_z14_tiles'])} | {_num(r['kartaview_requests'])} "
            f"| {_num(r['panoramax_z15_tiles'])} | {r['offset_km']} | {r['center']} "
            f"| {r['flags']} |"
        )
    resolved = [r for r in records if r["gsv_points"] != ""]
    lines.append(
        f"| | **Total ({len(resolved)} resolved)** | | | "
        f"| {_num(sum(r['gsv_points'] for r in resolved))} "
        f"| {_num(sum(r['gsv_streets_samples'] for r in resolved))} "
        f"| {_num(sum(r['mapillary_z14_tiles'] for r in resolved))} "
        f"| {_num(sum(r['kartaview_requests'] for r in resolved))} "
        f"| {_num(sum(r['panoramax_z15_tiles'] for r in resolved))} | | | |"
    )
    return "\n".join(lines)


def _num(value) -> str:
    return f"{value:,}" if isinstance(value, int) else str(value)


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--manifest", required=True, help="manifest CSV in the frame format")
    p.add_argument("--step", type=int, default=20, help="grid step in meters (default: 20)")
    p.add_argument("--limit", type=int, default=None, help="only vet the first N rows")
    p.add_argument(
        "--max-center-km",
        type=float,
        default=10.0,
        help="register_frame.py's center guard, as the registration will pass it "
        "(default: %(default)s, the purposive-manifest value)",
    )
    p.add_argument(
        "--center-from-geonames",
        action="store_true",
        help="as register_frame.py: recenter a guard-failing geocode on the GeoNames point",
    )
    p.add_argument(
        "--flag-offset-km",
        type=float,
        default=DEFAULT_FLAG_OFFSET_KM,
        help="flag a geocoded center farther than this from the GeoNames point "
        "(default: %(default)s)",
    )
    p.add_argument("--csv", default=None, help="also write the table as CSV to this path")
    p.add_argument(
        "--allow-collection-host",
        action="store_true",
        help="run even on a makelab* host (the nightly batch's IP)",
    )
    p.add_argument("--log-level", default="WARNING")
    args = p.parse_args(argv)
    if args.step < 1:
        p.error("--step must be at least 1")
    if args.limit is not None and args.limit < 1:
        p.error("--limit must be at least 1")
    for name in ("max_center_km", "flag_offset_km"):
        value = getattr(args, name)
        if not (value > 0 and math.isfinite(value)):
            p.error(f"--{name.replace('_', '-')} must be a positive number")
    return args


def main(argv=None) -> int:
    try:
        args = parse_args(argv)
    except SystemExit as e:
        return USAGE_EXIT if e.code not in (0, None) else 0
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.WARNING),
        format="%(levelname)s %(message)s",
        stream=sys.stderr,
    )
    refuse_on_collection_host(args.allow_collection_host)

    rows = rf.load_frame(args.manifest)
    if args.limit is not None:
        rows = rows[: args.limit]

    records = []
    for i, row in enumerate(rows, 1):
        print(f"[{i}/{len(rows)}] {row['query_string']}", file=sys.stderr)
        record = vet_row(
            row,
            step_m=args.step,
            max_center_km=args.max_center_km,
            use_geonames_center=args.center_from_geonames,
            flag_offset_km=args.flag_offset_km,
        )
        records.append({"n": i, **record})

    print(format_markdown(records))
    if args.csv:
        with open(args.csv, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=TABLE_COLUMNS)
            writer.writeheader()
            writer.writerows(records)
    return 1 if any(r["flags"] for r in records) else 0


if __name__ == "__main__":
    sys.exit(main())
