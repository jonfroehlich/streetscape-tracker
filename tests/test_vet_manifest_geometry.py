"""
The pre-registration geometry preview (scripts/vet_manifest_geometry.py, #406).

Its one promise is that it previews what ``register_frame.py --execute`` would
FREEZE, so the tests pin that it goes through register_frame's own seam
(``resolve_frame_geometry``) — the same monkeypatched geocoder the
registration tests use, no network — and that it never touches a catalog.
"""

import csv
import socket
from types import SimpleNamespace

import pytest

from scripts import register_frame as rf
from scripts import vet_manifest_geometry as vet
from streetscape_metadata_tracker import db
from streetscape_metadata_tracker.scheduler import estimate_requests

HEADER = [
    "query_string",
    "city",
    "admin",
    "iso2",
    "country",
    "continent",
    "size_band",
    "population",
    "coverage_regime",
    "geonameid",
    "lat",
    "lon",
]


def _row(**overrides):
    row = dict.fromkeys(HEADER, "")
    row.update(
        query_string="Testville, Testshire, Testland",
        city="Testville",
        admin="Testshire",
        iso2="TL",
        country="Testland",
        geonameid="42",
        lat="48.0",
        lon="2.0",
    )
    row.update(overrides)
    return row


def _write(path, rows):
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=HEADER)
        writer.writeheader()
        writer.writerows(rows)
    return str(path)


@pytest.fixture
def fake_geocode(monkeypatch):
    """
    register_frame's geocoding seam, patched as tests/test_register_frame.py
    patches it. The fake location is a namespace carrying the query and a
    Nominatim-shaped ``raw``; ``queries`` records every geocode asked for.
    """
    state = {
        "center": (48.0, 2.0),
        "center_by_query": {},
        "dims": (6000.0, 4000.0),
        "fail": set(),
        "queries": [],
    }

    def fake_loc(query):
        state["queries"].append(query)
        if query in state["fail"]:
            return None
        return SimpleNamespace(query=query, raw={"class": "boundary", "type": "administrative"})

    monkeypatch.setattr(rf, "get_city_location_data", fake_loc)
    monkeypatch.setattr(
        rf, "_resolve_center", lambda loc: state["center_by_query"].get(loc.query, state["center"])
    )
    monkeypatch.setattr(rf, "get_search_dimensions", lambda q, w, h: state["dims"])
    monkeypatch.setattr(socket, "gethostname", lambda: "laptop")
    return state


def _vet(row, **kw):
    args = dict(step_m=20, max_center_km=10.0, use_geonames_center=False, flag_offset_km=10.0)
    args.update(kw)
    return vet.vet_row(row, **args)


def test_a_clean_row_reports_the_geometry_and_the_scheduler_prices(fake_geocode):
    record = _vet(_row())

    assert record["flags"] == ""
    assert record["geocode_query"] == "Testville, Testshire, Testland"
    assert record["osm_match"] == "boundary/administrative"
    assert (record["width_m"], record["height_m"]) == (6000, 4000)
    assert record["offset_km"] == 0.0
    assert record["center"] == "geocoded"
    assert (record["center_lat"], record["center_lon"]) == (48.0, 2.0)
    # The prices are the scheduler's own estimator over the stored geometry,
    # not a formula of this script's: GSV is (W//step + 1)(H//step + 1).
    assert record["gsv_points"] == 301 * 201
    city = db.CityRow(
        "x", "x", "x", None, None, None, None, 48.0, 2.0, 6000, 4000, 20, "", False, None
    )
    assert record["gsv_streets_samples"] == estimate_requests(city, "gsv_streets")
    assert record["gsv_streets_samples"] > 0
    assert record["mapillary_z14_tiles"] == estimate_requests(city, "mapillary")
    assert record["kartaview_requests"] == estimate_requests(city, "kartaview")
    assert record["panoramax_z15_tiles"] == estimate_requests(city, "panoramax")
    assert record["panoramax_z15_tiles"] > record["mapillary_z14_tiles"]


def test_the_tile_prices_are_taken_at_the_geocoded_center(fake_geocode):
    """
    A tile census's price depends on where the grid sits on the tile lattice,
    so it must be computed at the center registration freezes, never at the
    GeoNames point. The geocode here lands ~6.7 km off, inside the guard, at a
    point whose tile counts differ from the GeoNames point's (asserted, so the
    test cannot pass by the two coinciding).
    """
    fake_geocode["center"] = (48.05, 2.05)
    record = _vet(_row())

    def city_at(lat, lon):
        return db.CityRow(
            "x", "x", "x", None, None, None, None, lat, lon, 6000, 4000, 20, "", False, None
        )

    geocoded, geonames = city_at(48.05, 2.05), city_at(48.0, 2.0)
    for channel, column in (
        ("mapillary", "mapillary_z14_tiles"),
        ("panoramax", "panoramax_z15_tiles"),
    ):
        assert estimate_requests(geocoded, channel) != estimate_requests(geonames, channel)
        assert record[column] == estimate_requests(geocoded, channel)


def test_the_step_is_passed_through_to_the_price(fake_geocode):
    """A --step other than 20 must reach the estimator (pins the pass-through)."""
    assert _vet(_row(), step_m=40)["gsv_points"] == 151 * 101


def test_an_offset_past_the_flag_is_flagged_with_its_distance(fake_geocode):
    # ~11.1 km north of the GeoNames point: inside the 50 km default guard,
    # outside the 10 km vetting flag.
    fake_geocode["center"] = (48.1, 2.0)
    record = _vet(_row(), max_center_km=50.0)

    assert record["offset_km"] == pytest.approx(11.1, abs=0.1)
    assert "OFFSET>10km" in record["flags"]


def test_the_flag_threshold_is_passed_through(fake_geocode):
    fake_geocode["center"] = (48.1, 2.0)
    assert "OFFSET" not in _vet(_row(), max_center_km=50.0, flag_offset_km=12.0)["flags"]


def test_a_guard_failure_is_a_failed_row_not_a_crash(fake_geocode):
    """Center guard at 10 km, both candidates 11 km off: registration would skip it."""
    fake_geocode["center"] = (48.1, 2.0)
    record = _vet(_row())

    assert record["flags"].startswith("FAILED: geocoded center is 11 km")
    assert record["gsv_points"] == ""
    # It tried the manifest query, then register_frame's bare fallback.
    assert fake_geocode["queries"] == ["Testville, Testshire, Testland", "Testville, Testland"]


def test_center_from_geonames_reports_the_geocoded_offset_and_recenters(fake_geocode):
    fake_geocode["center"] = (48.1, 2.0)
    record = _vet(_row(), use_geonames_center=True)

    assert record["center"] == "GeoNames"
    assert (record["center_lat"], record["center_lon"]) == (48.0, 2.0)
    # The offset shown is the GEOCODE's, the number vetting reads, never the 0.0
    # of the GeoNames center that replaced it.
    assert record["offset_km"] == pytest.approx(11.1, abs=0.1)


def test_the_fallback_query_winning_is_flagged(fake_geocode):
    fake_geocode["fail"].add("Testville, Testshire, Testland")
    record = _vet(_row())

    assert record["geocode_query"] == "Testville, Testland"
    assert "FALLBACK-QUERY" in record["flags"]


def test_the_no_boundary_default_and_the_cap_are_flagged(fake_geocode):
    fake_geocode["dims"] = (1000.0, 1000.0)
    assert "NO-BOUNDARY" in _vet(_row())["flags"]

    fake_geocode["dims"] = (55_000.0, 30_000.0)
    record = _vet(_row())
    assert (record["width_m"], record["height_m"]) == (40_000, 30_000)
    assert "CAPPED-FROM-55000x30000" in record["flags"]


def test_main_prints_the_table_writes_csv_and_exits_on_flags(tmp_path, fake_geocode, capsys):
    manifest = _write(
        tmp_path / "m.csv",
        [_row(), _row(query_string="Far, Testland", city="Far", geonameid="43")],
    )
    fake_geocode["center_by_query"]["Far, Testland"] = (48.1, 2.0)
    out_csv = tmp_path / "vet.csv"

    code = vet.main(["--manifest", manifest, "--csv", str(out_csv)])

    out = capsys.readouterr().out
    assert code == 1  # the second row failed the guard
    assert "| 1 | Testville | Testville, Testshire, Testland |" in out
    assert "FAILED" in out
    assert "**Total (1 resolved)**" in out
    with open(out_csv, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert [r["city"] for r in rows] == ["Testville", "Far"]
    assert rows[0]["gsv_points"] == str(301 * 201)


def test_main_is_clean_when_nothing_is_flagged_and_limit_is_honoured(tmp_path, fake_geocode):
    manifest = _write(
        tmp_path / "m.csv",
        [_row(), _row(query_string="Far, Testland", city="Far", geonameid="43")],
    )
    fake_geocode["center_by_query"]["Far, Testland"] = (48.1, 2.0)

    assert vet.main(["--manifest", manifest, "--limit", "1"]) == 0
    assert fake_geocode["queries"] == ["Testville, Testshire, Testland"] * 2  # +1 cached re-read


def test_main_passes_max_center_km_through(tmp_path, fake_geocode):
    """An 11 km geocode fails the 10 km default guard and passes a 50 km one."""
    manifest = _write(tmp_path / "m.csv", [_row()])
    fake_geocode["center"] = (48.1, 2.0)
    out_csv = tmp_path / "vet.csv"

    assert vet.main(["--manifest", manifest, "--csv", str(out_csv)]) == 1
    with open(out_csv, encoding="utf-8") as f:
        assert next(csv.DictReader(f))["flags"].startswith("FAILED")

    argv = ["--manifest", manifest, "--csv", str(out_csv), "--max-center-km", "50"]
    assert vet.main([*argv, "--flag-offset-km", "20"]) == 0
    with open(out_csv, encoding="utf-8") as f:
        row = next(csv.DictReader(f))
    assert (row["flags"], row["center"]) == ("", "geocoded")


def test_main_passes_center_from_geonames_through(tmp_path, fake_geocode):
    """The same guard-failing geocode, recentered on the GeoNames point."""
    manifest = _write(tmp_path / "m.csv", [_row()])
    fake_geocode["center"] = (48.1, 2.0)
    out_csv = tmp_path / "vet.csv"

    vet.main(["--manifest", manifest, "--csv", str(out_csv), "--center-from-geonames"])
    with open(out_csv, encoding="utf-8") as f:
        row = next(csv.DictReader(f))
    assert row["center"] == "GeoNames"
    assert (float(row["center_lat"]), float(row["center_lon"])) == (48.0, 2.0)
    assert not row["flags"].startswith("FAILED")


def test_it_never_opens_a_catalog(tmp_path, fake_geocode, monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("the vetting preview opened a catalog")

    monkeypatch.setattr(db, "connect", refuse)
    monkeypatch.setattr(db, "register_city", refuse)
    assert vet.main(["--manifest", _write(tmp_path / "m.csv", [_row()])]) == 0


def test_it_refuses_a_collection_host(tmp_path, fake_geocode, monkeypatch):
    monkeypatch.setattr(socket, "gethostname", lambda: "makelab2")
    manifest = _write(tmp_path / "m.csv", [_row()])

    with pytest.raises(SystemExit) as excinfo:
        vet.main(["--manifest", manifest])
    assert excinfo.value.code == vet.USAGE_EXIT
    assert fake_geocode["queries"] == []
    assert vet.main(["--manifest", manifest, "--allow-collection-host"]) == 0


@pytest.mark.parametrize(
    "argv",
    [
        ["--manifest", "m.csv", "--step", "0"],
        ["--manifest", "m.csv", "--limit", "0"],
        ["--manifest", "m.csv", "--max-center-km", "-1"],
        ["--manifest", "m.csv", "--flag-offset-km", "nan"],
        [],
    ],
)
def test_usage_errors_exit_64(argv, fake_geocode):
    assert vet.main(argv) == vet.USAGE_EXIT
    assert fake_geocode["queries"] == []
