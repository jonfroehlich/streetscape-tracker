"""
Summarize the 2026-10-08 Belgium screen's committed per-place record into the
numbers its writeup quotes.

    python scripts/belgium_screen_analyze.py --docs-dir docs/experiments

Reads ``docs/experiments/belgium-screen_places.csv`` (written by
``scripts/belgium_screen_collect.py``) and writes
``docs/experiments/belgium-screen_metrics.json``. No network, no catalog.

Everything here is a SCREENING SIGNAL. Mapillary density is recent-360° km
per km² of a 2 km disc from z6 geometry; the Panoramax figures are UPPER
BOUNDS summed over whole H3 cells, never coverage, and neighbouring places
share cells, so they never add up.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
from pathlib import Path

from scripts.experiment_stats import describe

PLACES_CSV = "belgium-screen_places.csv"
METRICS_JSON = "belgium-screen_metrics.json"

# The 18 places of >= 10,000 people GeoNames' cities500 files under admin-1
# BRU (Brussels-Capital Region). Woluwe-Saint-Pierre, the region's 19th
# commune, is not a cities500 place, so the screen never scored it. Pinned
# because the per-place record carries no admin column.
BRUSSELS_CAPITAL = (
    "Anderlecht",
    "Auderghem",
    "Berchem-Sainte-Agathe",
    "Brussels",
    "Etterbeek",
    "Evere",
    "Forest",
    "Ganshoren",
    "Ixelles",
    "Jette",
    "Koekelberg",
    "Molenbeek-Saint-Jean",
    "Saint-Gilles",
    "Saint-Josse-ten-Noode",
    "Schaerbeek",
    "Uccle",
    "Watermael-Boitsfort",
    "Woluwe-Saint-Lambert",
)

# The three places registered on production on 2026-10-08 (belgium_inquiry_cities.csv).
REGISTERED = ("Antwerp", "Mechelen", "Beringen")

# A density at or above this is listed by name outside Brussels-Capital.
DENSITY_LISTING_FLOOR = 7.0
# The places whose Panoramax bounds the writeup names.
PANORAMAX_NAMED = (
    "Boom",
    "Braine-l'Alleud",
    "Leuven",
    "Mechelen",
    "Menen",
    "Rumst",
    "Waterloo",
    "Wervik",
)
PANORAMAX_TOP_N = 25
# Distances the writeup quotes: a registered pair inside the default 25 km
# reuse radius of a tracked city, and two places sharing Panoramax cells.
NAMED_PAIRS = (("Mechelen", "Brussels"), ("Boom", "Rumst"))


def _km(a: dict, b: dict) -> float:
    """Equirectangular km between two place rows (as in the collect script)."""
    c = math.cos(math.radians((a["lat"] + b["lat"]) / 2))
    return math.hypot((a["lon"] - b["lon"]) * 111.32 * c, (a["lat"] - b["lat"]) * 110.57)


def load(path: Path) -> list[dict]:
    rows = []
    with open(path, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            share = r["mly_top_creator_share"]
            rows.append(
                {
                    "place": r["place"],
                    "pop": int(r["pop"]),
                    "lat": float(r["lat"]),
                    "lon": float(r["lon"]),
                    "mly_recent360_km": float(r["mly_recent360_km"]),
                    "mly_km_per_km2": float(r["mly_km_per_km2"]),
                    "mly_top_creator_share": float(share) if share else None,
                    "pnx_hexes": int(r["pnx_hexes"]),
                    "pnx_pics_ub": int(r["pnx_pics_ub"]),
                    "pnx_360_ub": int(r["pnx_360_ub"]),
                }
            )
    return rows


def _by_name(rows: list[dict], name: str) -> dict:
    hits = [r for r in rows if r["place"] == name]
    if len(hits) != 1:
        raise SystemExit(f"expected exactly one place named {name!r}, found {len(hits)}")
    return hits[0]


def _brief(r: dict, antwerp: dict | None = None) -> dict:
    out = {
        "place": r["place"],
        "pop": r["pop"],
        "mly_recent360_km": r["mly_recent360_km"],
        "mly_km_per_km2": r["mly_km_per_km2"],
        "mly_top_creator_share": r["mly_top_creator_share"],
        "pnx_pics_ub": r["pnx_pics_ub"],
        "pnx_360_ub": r["pnx_360_ub"],
    }
    if antwerp is not None:
        out["km_from_antwerp_point"] = round(_km(r, antwerp), 1)
    return out


def _flat_share(r: dict) -> float | None:
    if not r["pnx_pics_ub"]:
        return None
    return round(1 - r["pnx_360_ub"] / r["pnx_pics_ub"], 3)


def summarize(rows: list[dict]) -> dict:
    names = [r["place"] for r in rows]
    if len(names) != len(set(names)):
        raise SystemExit("place names are not unique; the by-name lookups would be ambiguous")
    bru = [_by_name(rows, n) for n in BRUSSELS_CAPITAL]
    bru_names = set(BRUSSELS_CAPITAL)
    rest = [r for r in rows if r["place"] not in bru_names]
    antwerp = _by_name(rows, "Antwerp")

    dense_rest = sorted(
        (r for r in rest if r["mly_km_per_km2"] >= DENSITY_LISTING_FLOOR),
        key=lambda r: -r["mly_km_per_km2"],
    )
    shares = [r["mly_top_creator_share"] for r in rows if r["mly_top_creator_share"] is not None]
    by_360 = sorted(rows, key=lambda r: -r["pnx_360_ub"])

    return {
        "places": {
            "n": len(rows),
            "brussels_capital_n": len(bru),
            "outside_brussels_capital_n": len(rest),
        },
        "mapillary": {
            "km_per_km2_all": describe([r["mly_km_per_km2"] for r in rows], 2),
            "km_per_km2_brussels_capital": describe([r["mly_km_per_km2"] for r in bru], 2),
            "km_per_km2_outside_brussels_capital": describe([r["mly_km_per_km2"] for r in rest], 2),
            "places_with_zero_recent360": sum(1 for r in rows if r["mly_recent360_km"] == 0),
            "top_creator_share_where_any": describe(shares, 2),
            "brussels_capital": sorted(
                (_brief(r) for r in bru), key=lambda b: -b["mly_km_per_km2"]
            ),
            "listing_floor_km_per_km2": DENSITY_LISTING_FLOOR,
            "outside_brussels_capital_at_or_above_floor": [_brief(r, antwerp) for r in dense_rest],
        },
        "panoramax": {
            "note": "upper bounds over whole H3 cells; neighbouring places share cells, never sum",
            "pnx_360_ub_all": describe([r["pnx_360_ub"] for r in rows], 0),
            "places_with_zero_360_ub": sum(1 for r in rows if r["pnx_360_ub"] == 0),
            "places_with_zero_pictures_ub": sum(1 for r in rows if r["pnx_pics_ub"] == 0),
            "top_by_360_ub": [
                {**_brief(r), "flat_share_ub": _flat_share(r)} for r in by_360[:PANORAMAX_TOP_N]
            ],
            "top_by_pictures_ub": [
                {**_brief(r), "flat_share_ub": _flat_share(r)}
                for r in sorted(rows, key=lambda r: -r["pnx_pics_ub"])[:PANORAMAX_TOP_N]
            ],
            "named": {
                n: {**_brief(_by_name(rows, n)), "flat_share_ub": _flat_share(_by_name(rows, n))}
                for n in PANORAMAX_NAMED
            },
        },
        "registered": {n: _brief(_by_name(rows, n)) for n in REGISTERED},
        "registered_pairwise_km": {
            f"{a}-{b}": round(_km(_by_name(rows, a), _by_name(rows, b)), 1)
            for i, a in enumerate(REGISTERED)
            for b in REGISTERED[i + 1 :]
        },
        "named_pairs_km": {
            f"{a}-{b}": round(_km(_by_name(rows, a), _by_name(rows, b)), 1) for a, b in NAMED_PAIRS
        },
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--docs-dir", type=Path, default=Path("docs/experiments"))
    args = ap.parse_args(argv)

    rows = load(args.docs_dir / PLACES_CSV)
    metrics = {
        "_about": {
            "experiment": "belgium-screen",
            "writeup": "docs/experiments/belgium-screen.md",
            "generated_by": "python scripts/belgium_screen_analyze.py --docs-dir docs/experiments",
            "collected_by": (
                "python scripts/belgium_screen_collect.py"
                " --mapillary-tiles experiments/mapillary-discovery-383/tiles"
                " --panoramax-tiles experiments/belgium-screen-2026-10-08"
                " --places experiments/mapillary-discovery-383/cities500.txt"
                " --out docs/experiments/belgium-screen_places.csv"
            ),
            "screen_date": "2026-10-08",
            "requests": {"mapillary_z6_sequence_tiles": 2, "panoramax_z6_grid_tiles": 2},
        },
        **summarize(rows),
    }
    out = args.docs_dir / METRICS_JSON
    with open(out, "w", encoding="utf-8") as f:
        json.dump(metrics, f, indent=2, ensure_ascii=False)
        f.write("\n")
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
