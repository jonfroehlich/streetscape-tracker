"""
Worldwide sampling-frame selection (scripts/build_worldwide_frame.py).

Pure, network-/file-free tests: select_frame / eligible_countries / query_string
operate on in-memory synthetic City/Country tables, so we assert the selection
rules directly (size bands, geographic separation of the small pick, per-
continent quota, coverage-regime force-inclusion, determinism, query format).
"""

import importlib.util
import os

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location(
    "build_worldwide_frame", os.path.join(PROJECT_ROOT, "scripts", "build_worldwide_frame.py")
)
bwf = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bwf)

City = bwf.City
Country = bwf.Country
CONFIG = bwf.DEFAULT_CONFIG


# --- synthetic data ----------------------------------------------------------


def city(gid, name, iso2, pop, lat=0.0, lon=0.0, admin1="01"):
    return City(
        geonameid=gid, name=name, iso2=iso2, admin1=admin1, population=pop, lat=lat, lon=lon
    )


def _base_countries():
    # Two European + two Asian countries so per-continent ranking is testable.
    return {
        "AA": Country("Alphaland", "EU"),
        "BB": Country("Betamark", "EU"),
        "CC": Country("Gammastan", "AS"),
        "DD": Country("Deltasia", "AS"),
        "ZZ": Country("Zedland", "AN"),  # Antarctica -> never selected
    }


def _base_cities():
    # Each present country has a large city and a far-away (~1000km) small city
    # near the 100k target, plus a near suburb to test the separation guard.
    return [
        # Alphaland (EU)
        city("a1", "Alphacity", "AA", 5_000_000, lat=0.0, lon=0.0),
        city("a2", "Alphaburb", "AA", 240_000, lat=0.1, lon=0.1),  # ~15km: suburb
        city("a3", "Alphatown", "AA", 101_000, lat=9.0, lon=0.0),  # ~1000km: distinct
        # Betamark (EU)
        city("b1", "Betacity", "BB", 2_000_000, lat=10.0, lon=10.0),
        city("b2", "Betatown", "BB", 99_000, lat=19.0, lon=10.0),
        # Gammastan (AS)
        city("c1", "Gammacity", "CC", 8_000_000, lat=20.0, lon=20.0),
        city("c2", "Gammatown", "CC", 100_500, lat=29.0, lon=20.0),
        # Deltasia (AS)
        city("d1", "Deltacity", "DD", 3_000_000, lat=30.0, lon=30.0),
        city("d2", "Deltatown", "DD", 98_000, lat=39.0, lon=30.0),
        # Zedland (AN) -- excluded continent
        city("z1", "Zedcity", "ZZ", 4_000_000, lat=-80.0, lon=0.0),
        city("z2", "Zedtown", "ZZ", 100_000, lat=-79.0, lon=0.0),
    ]


# --- eligibility & size bands ------------------------------------------------


def test_size_band_thresholds():
    assert bwf._size_band(1_000_000, CONFIG) == "large"
    assert bwf._size_band(999_999, CONFIG) is None  # gap between bands
    assert bwf._size_band(250_000, CONFIG) == "small"
    assert bwf._size_band(50_000, CONFIG) == "small"
    assert bwf._size_band(49_999, CONFIG) is None


def test_requires_both_large_and_small():
    countries = _base_countries()
    cities = [city("x1", "Onlybig", "AA", 2_000_000), city("y1", "Onlysmall", "BB", 80_000)]
    assert bwf.eligible_countries(cities, countries, CONFIG) == {}


def test_excludes_non_inhabited_continents():
    records = bwf.select_frame(_base_cities(), _base_countries(), {}, CONFIG)
    assert all(r.continent != "Antarctica" for r in records)
    assert "ZZ" not in {r.iso2 for r in records}


# --- small-pick selection rules ----------------------------------------------


def test_small_pick_is_separated_from_large():
    # Alphaland's 240k suburb is ~15km from the primary city; the ~1000km
    # 101k town must be chosen instead despite its smaller population.
    eligible = bwf.eligible_countries(_base_cities(), _base_countries(), CONFIG)
    _, small = eligible["AA"]
    assert small.name == "Alphatown"


def test_small_pick_targets_small_population():
    # Two distinct far-away small cities: one near the 100k target, one at the
    # 250k band edge. The one nearest small_target wins.
    countries = {"AA": Country("Alphaland", "EU")}
    cities = [
        city("a1", "Big", "AA", 5_000_000, lat=0.0, lon=0.0),
        city("a2", "NearTarget", "AA", 105_000, lat=9.0, lon=0.0),
        city("a3", "BandEdge", "AA", 249_000, lat=-9.0, lon=0.0),
    ]
    _, small = bwf.eligible_countries(cities, countries, CONFIG)["AA"]
    assert small.name == "NearTarget"


# --- continent quota & force-inclusion ---------------------------------------


def test_per_continent_quota():
    # 3 eligible EU countries but a quota of 2 -> only 2 selected from EU.
    countries = {"AA": Country("A", "EU"), "BB": Country("B", "EU"), "CC": Country("C", "EU")}
    cities = []
    for i, iso2 in enumerate(("AA", "BB", "CC")):
        base = (i + 1) * 10.0
        cities += [
            city(f"{iso2}1", f"{iso2}big", iso2, (i + 1) * 1_000_000, lat=base, lon=0.0),
            city(f"{iso2}2", f"{iso2}small", iso2, 100_000, lat=base + 9.0, lon=0.0),
        ]
    cfg = CONFIG._replace(countries_per_continent=2)
    records = bwf.select_frame(cities, countries, {}, cfg)
    eu = {r.iso2 for r in records}
    assert len(eu) == 2
    # ranked by large-pick population desc -> the two biggest (CC, BB)
    assert eu == {"CC", "BB"}


def test_coverage_regime_force_inclusion():
    # An 'absent' country ranked below the quota is still force-included.
    countries = {"AA": Country("A", "EU"), "BB": Country("B", "EU"), "CC": Country("C", "EU")}
    cities = []
    for i, iso2 in enumerate(("AA", "BB", "CC")):
        base = (i + 1) * 10.0
        cities += [
            city(f"{iso2}1", f"{iso2}big", iso2, (i + 1) * 1_000_000, lat=base, lon=0.0),
            city(f"{iso2}2", f"{iso2}small", iso2, 100_000, lat=base + 9.0, lon=0.0),
        ]
    # AA is the smallest (rank 3) and would be cut by a quota of 2...
    cfg = CONFIG._replace(countries_per_continent=2)
    coverage = {"AA": "absent"}
    records = bwf.select_frame(cities, countries, coverage, cfg)
    assert "AA" in {r.iso2 for r in records}
    aa = [r for r in records if r.iso2 == "AA"]
    assert {r.size_band for r in aa} == {"large", "small"}
    assert all(r.regime == "absent" for r in aa)


# --- structure, determinism, query format ------------------------------------


def test_one_large_one_small_per_country():
    records = bwf.select_frame(_base_cities(), _base_countries(), {}, CONFIG)
    by_country = {}
    for r in records:
        by_country.setdefault(r.iso2, []).append(r.size_band)
    for iso2, bands in by_country.items():
        assert sorted(bands) == ["large", "small"], iso2


def test_deterministic():
    a = bwf.select_frame(_base_cities(), _base_countries(), {}, CONFIG)
    b = bwf.select_frame(_base_cities(), _base_countries(), {}, CONFIG)
    assert a == b


def test_selected_populations_respect_bands():
    records = bwf.select_frame(_base_cities(), _base_countries(), {}, CONFIG)
    for r in records:
        if r.size_band == "large":
            assert r.city.population >= CONFIG.large_min
        else:
            assert CONFIG.small_min <= r.city.population <= CONFIG.small_max


def test_query_string_with_and_without_admin():
    rec = bwf.FrameRecord(
        city=city("g1", "Munich", "DE", 1_500_000, admin1="02"),
        iso2="DE",
        country="Germany",
        continent="Europe",
        size_band="large",
        regime="sparse",
    )
    admin = {"DE.02": "Bavaria"}
    assert bwf.query_string(rec, admin) == "Munich, Bavaria, Germany"
    assert bwf.query_string(rec, {}) == "Munich, Germany"


def test_query_string_skips_admin_equal_to_city():
    # City-states etc.: admin name == city name -> don't duplicate it.
    rec = bwf.FrameRecord(
        city=city("s1", "Singapore", "SG", 5_000_000, admin1="00"),
        iso2="SG",
        country="Singapore",
        continent="Asia",
        size_band="large",
        regime="present",
    )
    assert bwf.query_string(rec, {"SG.00": "Singapore"}) == "Singapore, Singapore"


def test_query_string_drops_city_prefixed_admin():
    # GeoNames city-state admins like "Ho Chi Minh City (HCMC)" would otherwise
    # leak a redundant, parenthesized component into queries and slugs.
    rec = bwf.FrameRecord(
        city=city("v1", "Ho Chi Minh City", "VN", 14_000_000, admin1="20"),
        iso2="VN",
        country="Vietnam",
        continent="Asia",
        size_band="large",
        regime="present",
    )
    admin = {"VN.20": "Ho Chi Minh City (HCMC)"}
    assert bwf.query_string(rec, admin) == "Ho Chi Minh City, Vietnam"


def test_effective_admin_rules():
    assert bwf.effective_admin("Munich", "Bavaria") == "Bavaria"
    assert bwf.effective_admin("Munich", None) is None
    assert bwf.effective_admin("Munich", "") is None
    # equal, city-prefixed, or parenthesized-duplicate admins are dropped
    assert bwf.effective_admin("Singapore", "Singapore") is None
    assert bwf.effective_admin("Lima", "Lima Province") is None
    assert bwf.effective_admin("Bogota", "Bogota D.C.") is None
    assert bwf.effective_admin("Ho Chi Minh City", "Ho Chi Minh City (HCMC)") is None
    # a trailing parenthetical is stripped even when the admin is kept
    assert bwf.effective_admin("Hue", "Thua Thien (Hue)") == "Thua Thien"
