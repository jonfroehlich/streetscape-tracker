#!/usr/bin/env python3
"""
Select the first European tranche of the Mapillary discovery screen and write
it as a ``register_frame.py`` manifest, with its advance prediction.

    python scripts/build_mapillary_discovery_europe1.py

WHAT
----
The 2026-11-25 Europe screen (``scripts/mapillary_discovery_score.py``, written
up in ``docs/experiments/mapillary-europe-screen.md``) ranked 176 uncatalogued
GeoNames places by V4 and wrote them, in that order, to the committed
``docs/experiments/mapillary-europe-screen_ranked.csv``. Every row there
already passed the screen's rules (V4 >= 0.7, top uploader share >= 0.6,
on-foot share < 0.5, median age <= 2 yr, outside every catalog grid and > 5 km
from its centre, at least 5 km from every higher-ranked row). The screen has no
validated positive in Europe, so this tranche is the measurement. It has two
arms:

1. **Small** (the arm the prediction covers): walk the record in order, keep a
   place under ``SMALL_POP_BELOW`` people unless its uploader already has
   ``PER_UPLOADER_CAP`` kept, stop at ``SMALL_N``. This is the kind of place
   the score was tested on in the US.
2. **Large** (exploratory, no prediction): the first ``LARGE_N`` places of
   ``SMALL_POP_BELOW`` people or more. A large city is scored on a 3 km core
   only, and nothing says how that carries to its whole grid.

The manifest lists the small arm first, each arm in record order.

THE PREDICTION
--------------
Written by this script, before any of the places is registered, into
``docs/experiments/mapillary-europe-screen_tranche1_metrics.json``. Its rate is
tranche 3's outcome (20 of 25, ``mapillary-discovery-screen_tranche3_outcome.json``),
the one OUT-OF-SAMPLE test of V4 >= 0.7: the threshold was read off tranches
1-2's own walks, so their 20 of 23 is in-sample and is left out. The floor is
the largest count whose lower tail at that rate is at most ``FLOOR_TAIL_MAX``;
fewer strict hits than that among the small arm's FIRST Mapillary drive walks
falsifies "the North American rate carries to small European places".

Each kept row is joined to vendored GeoNames by geonameid (cities15000 plus
``EUROPE1_SUPPLEMENT_IDS``, this tranche's rows of the shared
``data_sources/geonames_supplement.txt``). Identity (and so the frozen
``city_id``) comes from the GeoNames columns, except the three named
``*_DEPARTURES`` tables.

No network. Reads committed files only, so re-running it reproduces the
committed manifest and metrics byte for byte.
"""

import argparse
import csv
import json
import math
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.build_worldwide_frame import (  # noqa: E402
    _MANIFEST_HEADER,
    load_admin1,
    load_cities,
    load_countries,
    query_string,
)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_SOURCES = os.path.join(REPO_ROOT, "data_sources")
EXP = os.path.join(REPO_ROOT, "docs", "experiments")
RANKED = os.path.join(EXP, "mapillary-europe-screen_ranked.csv")
TRANCHE3_OUTCOME = os.path.join(EXP, "mapillary-discovery-screen_tranche3_outcome.json")

SMALL_N = 20
LARGE_N = 5
SMALL_POP_BELOW = 100_000
PER_UPLOADER_CAP = 2
FLOOR_TAIL_MAX = 0.05

# The eight rows of the tranche that are in cities500 but not cities15000. The
# supplement is shared by every purposive manifest, so the joinable set is
# pinned rather than read whole.
EUROPE1_SUPPLEMENT_IDS = frozenset(
    {
        "2751615",  # Lievendaal, NL
        "2766636",  # Sankt Johann in Tirol, AT
        "2832424",  # Siegsdorf, DE
        "2926147",  # Floeha, DE
        "3003874",  # Leguevin, FR
        "3018103",  # Fonsorbes, FR
        "3054651",  # Budakalasz, HU
        "3179911",  # Caselle Torinese, IT
    }
)

# Two rows take the common English name instead of GeoNames' ASCII name: one
# carries an apostrophe into the city id, the other is a transliteration nobody
# searches for. geonameid -> (GeoNames asciiname, manifest name).
NAME_DEPARTURES = {
    "627907": ("Homyel'", "Gomel"),
    "2886242": ("Koeln", "Cologne"),
}

# GeoNames' admin-1 name for Gomel carries two apostrophes into the city id,
# which is also a file-name stem. geonameid -> (GeoNames admin name, manifest admin).
ADMIN_DEPARTURES = {"627907": ("Homyel' Voblasc'", "Gomel Oblast")}

# GeoNames says "The Netherlands"; the catalog's one Dutch city is registered
# under "Netherlands", and a second spelling would split the country.
# iso2 -> (GeoNames country name, manifest country).
COUNTRY_DEPARTURES = {"NL": ("The Netherlands", "Netherlands")}

# The three rows registered as a DISTRICT, at the grid vetting geocoded. The
# boundary audit looks a name up as city/state/country and may grow a district
# to its parent municipality unattended, so the runbook restores each with
# scripts/resize_city.py after the audit (which also stamps the note that stops
# a later audit growing it again). geonameid -> (center_lat, center_lon, width_m, height_m).
DISTRICT_GRIDS = {
    "2751615": (51.442672, 5.434239, 1390, 2225),  # Lievendaal, a neighbourhood of Eindhoven
    "655808": (62.601416, 29.761591, 2054, 4458),  # Joensuu's centre district
    "2761164": (47.252929, 11.388894, 2393, 2259),  # Wilten, a suburb of Innsbruck
}

# Geocode-query overrides found by scripts/vet_manifest_geometry.py (the _vet
# CSV) and re-vetted before being committed (the _vet_retry CSV).
# geonameid -> query.
QUERY_OVERRIDES = {
    # bare: the 2,381 km2 municipality's bbox midpoint, 31 km off. The centre
    # district is inside the disc the screen scored.
    "655808": "Keskusta, Joensuu, Finland",
    "627907": "Gomel, Belarus",  # with the region name: the railway station, 680 x 1,112 m
    "3054651": "Budakalasz, Hungary",  # the county name matches nothing; the fallback won
    "2766824": "Salzburg, Austria",  # likewise for "State of Salzburg"
}


def select(ranked):
    """(small arm, large arm) of the ranked record, each in record order."""
    small, large, per_user = [], [], {}
    for row in ranked:
        if int(row["pop"]) >= SMALL_POP_BELOW:
            if len(large) < LARGE_N:
                large.append(row)
            continue
        if len(small) == SMALL_N or per_user.get(row["user"], 0) >= PER_UPLOADER_CAP:
            continue
        per_user[row["user"]] = per_user.get(row["user"], 0) + 1
        small.append(row)
    return small, large


def lower_tail(k, n, p):
    """P(X < k) for X ~ Binomial(n, p)."""
    return sum(math.comb(n, i) * p**i * (1 - p) ** (n - i) for i in range(k))


def prediction(n, hits, walked):
    """
    The advance prediction for ``n`` places at the recorded rate ``hits`` /
    ``walked``: falsified below the largest count whose lower tail at that rate
    is at most ``FLOOR_TAIL_MAX``.
    """
    rate = hits / walked
    floor = max(k for k in range(n + 1) if lower_tail(k, n, rate) <= FLOOR_TAIL_MAX)
    return {
        "n": n,
        "basis": "tranche 3 (US and Canada), the out-of-sample test of V4 >= 0.7: "
        f"{hits}/{walked} strict hits",
        "rate": round(rate, 4),
        "expected_strict_hits": round(n * rate, 1),
        "falsified_below": floor,
        "p_below_floor_at_rate": round(lower_tail(floor, n, rate), 3),
    }


def out_of_sample_record():
    """(strict hits, walked) of tranche 3, from its committed outcome."""
    with open(TRANCHE3_OUTCOME, encoding="utf-8") as f:
        t3 = json.load(f)
    assert t3["verdict"] != "undecided" and t3["walked"] == t3["n"]
    return t3["strict_hits"], t3["walked"]


def manifest_row(geonameid, cities, admin, countries):
    """One register_frame.py manifest row, joined from GeoNames by geonameid."""
    city = cities[geonameid]
    name = city.name
    if geonameid in NAME_DEPARTURES:
        expected, name = NAME_DEPARTURES[geonameid]
        assert city.name == expected, geonameid  # else the departure is stale
    named = city._replace(name=name)
    country = countries[city.iso2]
    country_name = country.name
    if city.iso2 in COUNTRY_DEPARTURES:
        expected, country_name = COUNTRY_DEPARTURES[city.iso2]
        assert country.name == expected, city.iso2
    admin_key = f"{city.iso2}.{city.admin1}"
    admin_name = admin.get(admin_key, "")
    if geonameid in ADMIN_DEPARTURES:
        expected, admin_name = ADMIN_DEPARTURES[geonameid]
        assert admin.get(admin_key) == expected, geonameid
    generated = query_string(
        SimpleNamespace(city=named, iso2=city.iso2, country=country_name),
        {**admin, admin_key: admin_name},
    )
    return {
        "query_string": QUERY_OVERRIDES.get(geonameid, generated),
        "city": name,
        "admin": admin_name,
        "iso2": city.iso2,
        "country": country_name,
        "continent": country.continent,
        "size_band": "",
        "population": city.population,
        "coverage_regime": "",
        "geonameid": city.geonameid,
        "lat": city.lat,
        "lon": city.lon,
    }


def record_row(arm, row):
    """What the selection knew about one kept place, copied from the ranked record."""
    keep = ("geonameid", "name", "cc", "user", "uploader_walked", "uploader_strict")
    nums = ("pop", "top_share", "foot_share", "med_age", "v4")
    return {"arm": arm, **{k: row[k] for k in keep}, **{k: float(row[k]) for k in nums}}


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--ranked", default=RANKED, help="the Europe screen's ranked record")
    p.add_argument(
        "--manifest-out",
        default=os.path.join(REPO_ROOT, "mapillary_discovery_cities_europe1.csv"),
    )
    p.add_argument(
        "--metrics-out",
        default=os.path.join(EXP, "mapillary-europe-screen_tranche1_metrics.json"),
    )
    p.add_argument("--data-sources", default=DATA_SOURCES)
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    cities = {
        c.geonameid: c for c in load_cities(os.path.join(args.data_sources, "cities15000.txt"))
    }
    cities.update(
        {
            c.geonameid: c
            for c in load_cities(os.path.join(args.data_sources, "geonames_supplement.txt"))
            if c.geonameid in EUROPE1_SUPPLEMENT_IDS
        }
    )
    admin = load_admin1(os.path.join(args.data_sources, "admin1CodesASCII.txt"))
    countries = load_countries(os.path.join(args.data_sources, "countryInfo.txt"))
    with open(args.ranked, encoding="utf-8") as f:
        ranked = list(csv.DictReader(f))

    small, large = select(ranked)
    with open(args.manifest_out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=_MANIFEST_HEADER)
        w.writeheader()
        for row in small + large:
            w.writerow(manifest_row(row["geonameid"], cities, admin, countries))

    hits, walked = out_of_sample_record()
    metrics = {
        "generated_by": "scripts/build_mapillary_discovery_europe1.py",
        "ranked_record": "docs/experiments/mapillary-europe-screen_ranked.csv",
        "rules": {
            "small_n": SMALL_N,
            "small_pop_below": SMALL_POP_BELOW,
            "per_uploader_cap": PER_UPLOADER_CAP,
            "large_n": LARGE_N,
        },
        "advance_prediction_small_arm": prediction(len(small), hits, walked),
        "large_arm": "exploratory: scored on a 3 km core only, no prediction",
        "small_arm_uploaders": len({r["user"] for r in small}),
        "small_arm_with_an_uploader_record": sum(int(r["uploader_walked"]) > 0 for r in small),
        "rows": [record_row("small", r) for r in small] + [record_row("large", r) for r in large],
    }
    with open(args.metrics_out, "w", encoding="utf-8") as f:
        json.dump(metrics, f, indent=1)
        f.write("\n")
    print(f"{len(small)} small + {len(large)} large of {len(ranked)} ranked", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
