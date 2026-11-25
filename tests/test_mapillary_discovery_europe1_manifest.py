"""
The first European Mapillary discovery manifest
(mapillary_discovery_cities_europe1.csv): 25 places selected by rule from the
2026-11-25 Europe screen's committed ranked record
(docs/experiments/mapillary-europe-screen.md, "First tranche").

Held here:

* the manifest and the metrics are what the generator writes, byte for byte;
* the SELECTION, re-derived independently of the generator from the ranked
  record: a small arm (first 20 places under 100,000 people, at most two per
  uploader) and a large arm (first five of 100,000 or more);
* the advance prediction, recomputed from the committed US records it cites;
* each row's VALUES, a join against vendored GeoNames by geonameid, with the
  named departures and no others;
* each row's query, which must be the one the committed vetting record
  geocoded, and the city_ids registration will freeze;
* the reuse margins ``register_frame.py --overlap-km`` sees, from the committed
  overlap record, against the radius the runbook passes;
* the runbook's night table and the docs' totals, against the vetting record.

It pins NO frozen geometry: nothing was registered when this was written.
"""

import csv
import json
import math
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import build_mapillary_discovery_europe1 as gen
from scripts.build_worldwide_frame import (
    _MANIFEST_HEADER,
    load_admin1,
    load_cities,
    load_countries,
    query_string,
)
from scripts.register_frame import _haversine_km as frame_km
from scripts.register_frame import frame_identity
from streetscape_metadata_tracker import db, scheduler

REPO_ROOT = Path(__file__).resolve().parents[1]
MANIFEST = REPO_ROOT / "mapillary_discovery_cities_europe1.csv"
EXP = REPO_ROOT / "docs" / "experiments"
RANKED = EXP / "mapillary-europe-screen_ranked.csv"
METRICS = EXP / "mapillary-europe-screen_tranche1_metrics.json"
VET = EXP / "mapillary-europe-screen_tranche1_vet.csv"
VET_RETRY = EXP / "mapillary-europe-screen_tranche1_vet_retry.csv"
OVERLAPS = EXP / "mapillary-europe-screen_tranche1_overlaps.json"
OPS = REPO_ROOT / "docs" / "operations.md"
WRITEUP = EXP / "mapillary-europe-screen.md"
DATA_SOURCES = REPO_ROOT / "data_sources"

# The slugs registration will freeze. Order matches the manifest: the small
# arm, then the large arm.
EXPECTED_CITY_IDS = {
    "2751615": "lievendaal--north-brabant--netherlands",
    "655808": "joensuu--north-karelia--finland",
    "3178650": "cirie--piedmont--italy",
    "2761164": "wilten--tyrol--austria",
    "3054651": "budakalasz--pest-county--hungary",
    "2624341": "ballerup--capital-region--denmark",
    "715126": "szolnok--jasz-nagykun-szolnok--hungary",
    "2766636": "sankt-johann-in-tirol--tyrol--austria",
    "2875881": "loerrach--baden-wurttemberg--germany",
    "2986933": "plaisance-du-touch--occitanie--france",
    "864723": "marupe--latvia",
    "2787989": "rixensart--wallonia--belgium",
    "2801494": "boom--flanders--belgium",
    "2794117": "kontich--flanders--belgium",
    "2832424": "siegsdorf--bavaria--germany",
    "3003874": "leguevin--occitanie--france",
    "3018103": "fonsorbes--occitanie--france",
    "2926147": "floeha--saxony--germany",
    "3179911": "caselle-torinese--piedmont--italy",
    "2786743": "sint-amandsberg--flanders--belgium",
    "627907": "gomel--belarus",  # the region repeats the city, so identity drops it
    "2753801": "hoofddorp--north-holland--netherlands",
    "2766824": "salzburg--state-of-salzburg--austria",
    "2907669": "heilbronn--baden-wurttemberg--germany",
    "2886242": "cologne--north-rhine-westphalia--germany",
}
SMALL_N = 20

# The runbook's --overlap-km and the headroom every reuse margin keeps above it.
OVERLAP_KM = 3.0
HEADROOM_KM = 1.5

# What each row's query matched. Three are districts, not municipalities.
DISTRICT_MATCHES = {
    "2751615": "place/neighbourhood",  # Lievendaal, in Eindhoven
    "655808": "place/suburb",  # Joensuu's centre district (the override)
    "2761164": "place/suburb",  # Wilten, in Innsbruck
    "2624341": "place/town",  # Ballerup
}

# The runbook's four nights, by manifest city name.
NIGHTS = {
    1: "Lievendaal, Joensuu, Cirie, Wilten, Budakalasz, Ballerup, Loerrach, Plaisance-du-Touch, "
    "Marupe, Rixensart, Boom, Kontich, Leguevin, Fonsorbes, Floeha, Caselle Torinese, "
    "Sint-Amandsberg",
    2: "Szolnok, Sankt Johann in Tirol, Siegsdorf, Hoofddorp, Salzburg",
    3: "Heilbronn, Cologne",
    4: "Gomel",
}


def _read_csv(path):
    with open(path, encoding="utf-8") as f:
        return list(csv.DictReader(f))


@pytest.fixture(scope="module")
def manifest_rows():
    return _read_csv(MANIFEST)


@pytest.fixture(scope="module")
def ranked():
    return _read_csv(RANKED)


@pytest.fixture(scope="module")
def metrics():
    return json.loads(METRICS.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def geonames():
    return SimpleNamespace(
        big={c.geonameid: c for c in load_cities(DATA_SOURCES / "cities15000.txt")},
        small={c.geonameid: c for c in load_cities(DATA_SOURCES / "geonames_supplement.txt")},
        admin=load_admin1(DATA_SOURCES / "admin1CodesASCII.txt"),
        countries=load_countries(DATA_SOURCES / "countryInfo.txt"),
    )


@pytest.fixture(scope="module")
def vetted():
    """geonameid -> the vetting row that stands (a retry replaces the first attempt)."""
    out = {}
    for path in (VET, VET_RETRY):
        out.update({r["geonameid"]: r for r in _read_csv(path) if r["width_m"]})
    return out


@pytest.fixture(scope="module")
def overlap_rows():
    return {r["geonameid"]: r for r in json.loads(OVERLAPS.read_text(encoding="utf-8"))["rows"]}


def test_manifest_and_metrics_are_what_the_generator_writes(tmp_path):
    m, j = tmp_path / "m.csv", tmp_path / "m.json"
    assert gen.main(["--manifest-out", str(m), "--metrics-out", str(j)]) == 0
    assert m.read_bytes() == MANIFEST.read_bytes()
    assert j.read_bytes() == METRICS.read_bytes()


def test_header_rows_and_city_ids(manifest_rows):
    """register_frame.py reads this file; the slugs are what it will freeze."""
    assert list(manifest_rows[0]) == _MANIFEST_HEADER
    assert [r["geonameid"] for r in manifest_rows] == list(EXPECTED_CITY_IDS)
    for row in manifest_rows:
        city_id = db.derive_city_id(*frame_identity(row))
        assert city_id == EXPECTED_CITY_IDS[row["geonameid"]]
        # a city id is a file-name stem: lowercase ASCII letters, digits and hyphens only
        assert city_id.isascii() and city_id.replace("-", "").isalnum(), city_id
        assert row["size_band"] == "" and row["coverage_regime"] == ""


def test_selection_rederives_from_the_ranked_record(manifest_rows, ranked, metrics):
    """Written without the generator's ``select``: two passes over the record."""
    small, held = [], {}
    for r in ranked:
        if int(r["pop"]) >= 100_000 or held.get(r["user"], 0) >= 2:
            continue
        held[r["user"]] = held.get(r["user"], 0) + 1
        small.append(r["geonameid"])
    large = [r["geonameid"] for r in ranked if int(r["pop"]) >= 100_000][:5]
    ids = [r["geonameid"] for r in manifest_rows]
    assert ids[:SMALL_N] == small[:SMALL_N] and ids[SMALL_N:] == large
    assert max(held.values()) == 2
    assert [r["arm"] for r in metrics["rows"]] == ["small"] * SMALL_N + ["large"] * 5
    assert [r["geonameid"] for r in metrics["rows"]] == ids
    by_id = {r["geonameid"]: r for r in ranked}
    for r in metrics["rows"]:  # the record of what the selection knew is the ranked record's
        src = by_id[r["geonameid"]]
        assert (r["user"], r["v4"], r["med_age"]) == (
            src["user"],
            float(src["v4"]),
            float(src["med_age"]),
        )
    assert metrics["small_arm_uploaders"] == len({by_id[i]["user"] for i in ids[:SMALL_N]}) == 16
    assert metrics["small_arm_with_an_uploader_record"] == 4


def test_advance_prediction_is_recomputed_from_the_us_records(metrics):
    pred = metrics["advance_prediction_small_arm"]
    t3 = json.loads((EXP / "mapillary-discovery-screen_tranche3_outcome.json").read_text())
    hits, walked = 20 + t3["strict_hits"], 23 + t3["walked"]
    assert (hits, walked) == (40, 48)
    assert pred["basis"].endswith(f"{hits}/{walked} strict hits")
    rate = hits / walked

    def tail(k):
        return sum(math.comb(20, i) * rate**i * (1 - rate) ** (20 - i) for i in range(k))

    assert (pred["n"], pred["falsified_below"]) == (SMALL_N, 14)
    assert tail(14) <= 0.05 < tail(15)  # the floor is the largest count inside the 5% tail
    assert pred["p_below_floor_at_rate"] == round(tail(14), 3) == 0.037
    assert pred["expected_strict_hits"] == round(20 * rate, 1) == 16.7
    writeup = WRITEUP.read_text(encoding="utf-8")
    assert "falsified below 14 strict hits" in writeup
    assert "40 of 48" in writeup and "16.7" in writeup and "0.037" in writeup


def test_every_row_joins_its_vendored_geonames_record(manifest_rows, geonames):
    """The join, in reverse, with the named departures and no others."""
    for row in manifest_rows:
        gid = row["geonameid"]
        assert (gid in geonames.small) == (gid in gen.EUROPE1_SUPPLEMENT_IDS), gid
        assert (gid in geonames.big) != (gid in geonames.small), gid
        city = geonames.big.get(gid) or geonames.small[gid]
        country = geonames.countries[city.iso2]
        name = gen.NAME_DEPARTURES.get(gid, (city.name, city.name))
        admin = geonames.admin.get(f"{city.iso2}.{city.admin1}", "")
        admin = gen.ADMIN_DEPARTURES.get(gid, (admin, admin))
        nation = gen.COUNTRY_DEPARTURES.get(city.iso2, (country.name, country.name))
        assert (name[0], admin[0], nation[0]) == (city.name, admin[0], country.name), gid
        assert (row["city"], row["admin"], row["country"]) == (name[1], admin[1], nation[1]), gid
        assert row["iso2"] == city.iso2 and row["continent"] == country.continent == "EU", gid
        assert int(row["population"]) == city.population, gid
        assert float(row["lat"]) == pytest.approx(city.lat), gid
        assert float(row["lon"]) == pytest.approx(city.lon), gid
    assert set(gen.NAME_DEPARTURES) == {"627907", "2886242"}
    assert set(gen.ADMIN_DEPARTURES) == {"627907"} and set(gen.COUNTRY_DEPARTURES) == {"NL"}


def test_queries_are_the_generators_except_the_named_overrides(manifest_rows, geonames):
    for row in manifest_rows:
        gid = row["geonameid"]
        city = geonames.big.get(gid) or geonames.small[gid]
        record = SimpleNamespace(
            city=city._replace(name=row["city"]), iso2=city.iso2, country=row["country"]
        )
        admin = {**geonames.admin, f"{city.iso2}.{city.admin1}": row["admin"]}
        generated = query_string(record, admin)
        assert row["query_string"] == gen.QUERY_OVERRIDES.get(gid, generated), gid
    assert set(gen.QUERY_OVERRIDES) == {"655808", "627907", "3054651", "2766824"}


def test_every_query_is_the_one_vetting_geocoded(manifest_rows, vetted):
    """
    A row's price and margins describe the grid vetting saw only if
    registration sends the same query. Every row resolved unflagged, from the
    geocoded centre, inside the runbook's 10 km guard; all but the named
    districts on an administrative boundary.
    """
    assert set(vetted) == set(EXPECTED_CITY_IDS)
    for row in manifest_rows:
        gid, v = row["geonameid"], vetted[row["geonameid"]]
        assert v["geocode_query"] == row["query_string"], row["city"]
        assert v["osm_match"] == DISTRICT_MATCHES.get(gid, "boundary/administrative"), row["city"]
        assert v["center"] == "geocoded" and v["flags"] == "", row["city"]
        assert float(v["offset_km"]) <= 3.7, row["city"]


def test_the_first_vetting_pass_is_why_each_override_exists():
    first = {r["geonameid"]: r for r in _read_csv(VET)}
    assert first["655808"]["width_m"] == ""
    assert "31 km from the GeoNames coordinates" in first["655808"]["flags"]
    assert first["627907"]["osm_match"] == "railway/station"
    assert (first["627907"]["width_m"], first["627907"]["height_m"]) == ("680", "1112")
    for gid in ("3054651", "2766824"):
        assert first[gid]["flags"] == "FALLBACK-QUERY"
    retried = {r["geonameid"] for r in _read_csv(VET_RETRY)}
    # the two Dutch rows were re-vetted when the country name changed, not overridden
    assert retried == set(gen.QUERY_OVERRIDES) | {"2751615", "2753801"}


def test_registration_reuse_margins_leave_headroom_at_the_runbooks_radius(
    manifest_rows, overlap_rows, vetted
):
    """
    find_overlap measures from a row's GeoNames point to every catalog centre,
    and with --execute to the GEOCODED centres of rows the same run registered
    earlier. The record holds both margins per row, on the vetted grid.
    """
    assert list(overlap_rows) == [r["geonameid"] for r in manifest_rows]
    for gid, r in overlap_rows.items():
        v = vetted[gid]
        assert r["grid"] == [
            float(v["center_lat"]),
            float(v["center_lon"]),
            int(v["width_m"]),
            int(v["height_m"]),
        ]
        assert r["gsv_lattice_points"] == int(v["gsv_points"])
    margins = [r["nearest_catalog_km"] for r in overlap_rows.values()]
    margins += [
        r["nearest_earlier_row_km"] for r in overlap_rows.values() if "nearest_earlier_row_km" in r
    ]
    assert min(margins) >= OVERLAP_KM + HEADROOM_KM
    earlier = []
    for row in manifest_rows:  # the in-batch margins, recomputed rather than trusted
        lat, lon = float(row["lat"]), float(row["lon"])
        if earlier:
            name, km = min(
                ((n, frame_km(lat, lon, clat, clon)) for n, clat, clon in earlier),
                key=lambda x: x[1],
            )
            recorded = overlap_rows[row["geonameid"]]
            assert recorded["nearest_earlier_row"] == name, row["city"]
            assert recorded["nearest_earlier_row_km"] == pytest.approx(km, abs=0.001), row["city"]
        v = vetted[row["geonameid"]]
        earlier.append((row["city"], float(v["center_lat"]), float(v["center_lon"])))
    # The margin that sets the radius.
    fonsorbes = overlap_rows["3018103"]
    assert fonsorbes["nearest_earlier_row"] == "Plaisance-du-Touch"
    assert fonsorbes["nearest_earlier_row_km"] == pytest.approx(5.116, abs=0.001)
    assert min(margins) == fonsorbes["nearest_earlier_row_km"]
    ops = OPS.read_text(encoding="utf-8")
    assert '--notes-label "mapillary discovery screen 2026-11-25 europe 1" --overlap-km 3 ' in ops


def test_the_writeups_overlaps_are_the_overlap_record(overlap_rows):
    """Three grids share ground with a tracked neighbour; the writeup says how much."""
    shares = {
        r["city"]: r["catalog_overlaps"] for r in overlap_rows.values() if r["catalog_overlaps"]
    }
    assert shares == {
        "Budakalasz": {"budapest--budapest--hungary": 0.235},
        "Kontich": {"antwerp--flanders--belgium": 0.13},
        "Hoofddorp": {"amsterdam--north-holland--netherlands": 0.061},
    }
    writeup = WRITEUP.read_text(encoding="utf-8")
    assert "23.5% of Budakalasz's grid lies inside Budapest's" in writeup
    assert "13.0% of Kontich's inside Antwerp's" in writeup
    assert "6.1% of Hoofddorp's inside Amsterdam's" in writeup


def test_the_runbooks_night_table_is_the_vetting_record(manifest_rows, vetted):
    by_name = {r["city"]: r["geonameid"] for r in manifest_rows}
    assert sorted(n for names in NIGHTS.values() for n in names.split(", ")) == sorted(by_name)
    ops = OPS.read_text(encoding="utf-8")
    for night, names in NIGHTS.items():
        rows = [vetted[by_name[n]] for n in names.split(", ")]
        gsv, tiles, px = (
            sum(int(r[k]) for r in rows)
            for k in ("gsv_points", "mapillary_z14_tiles", "panoramax_z15_tiles")
        )
        line = (
            f"| {night} | {names} | {gsv:,} ({gsv / 48000 / 60:.2f}) | {tiles} | "
            f"≤ {px:,} ({round(px / 60)}) |"
        )
        assert line in ops, line


def test_the_docs_totals_are_the_vetting_record(manifest_rows, vetted):
    def total(rows, key):
        return sum(int(vetted[r["geonameid"]][key]) for r in rows)

    small, large = manifest_rows[:SMALL_N], manifest_rows[SMALL_N:]
    gsv, tiles = total(manifest_rows, "gsv_points"), total(manifest_rows, "mapillary_z14_tiles")
    assert (gsv, tiles) == (7_654_613, 1_553)
    assert (total(small, "mapillary_z14_tiles"), total(large, "mapillary_z14_tiles")) == (610, 943)
    for path in (OPS, WRITEUP):
        text = path.read_text(encoding="utf-8")
        assert "7,654,613 GSV grid points and 1,553 Mapillary z14 tiles" in text, path.name
    writeup = WRITEUP.read_text(encoding="utf-8")
    assert "610 tiles for the 20 small places and 943 for the five large ones" in writeup


def test_the_runbook_excludes_every_row_from_kartaview_before_enabling(vetted):
    """
    enable-city would enrol every row on KartaView (each estimate is under its
    ceiling), against production's no-widening position at the anonymous pace;
    the runbook pre-sets the exclusion on both channels, before the enable step.
    """
    ops = OPS.read_text(encoding="utf-8")
    section = ops[ops.index("first European tranche (`mapillary_discovery_cities_europe1.csv`)") :]
    section = section[: section.index("\n## ")]
    exclude = section.index("**4. Exclude all 25 from both KartaView channels")
    enable = section.index("**5. Enable, one night at a time")
    assert exclude < enable
    step = section[exclude:enable]
    assert "for c in $IDS; do" in step
    for channel in ("kartaview", "kartaview_streets"):
        assert (
            "scheduler --config config/scheduler.makelab1.toml "
            f'enroll-city "$c" --channel {channel} --remove' in step
        )
    estimates = [int(r["kartaview_requests"]) for r in vetted.values()]
    assert max(estimates) == 756 <= scheduler.OPT_IN_KARTAVIEW_ENROLL_MAX_REQUESTS
    assert "KartaView requests" not in section
