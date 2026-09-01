"""
Worldwide-frame registration (scripts/register_frame.py, issue #110).

Covers the safety rails around bulk registration: overlap reuse (an existing
catalog city under a different slug is aliased, never duplicated), new cities
registered disabled until boundary-vetted, the GeoNames center guard against
province-centroid geocodes, dry-run-by-default writing nothing, and idempotent
re-runs. The geocoding seam is monkeypatched — no network.
"""

import csv

import pytest

from scripts import register_frame as rf
from streetscape_metadata_tracker import db

MANIFEST_HEADER = [
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


def write_manifest(path, rows):
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=MANIFEST_HEADER)
        writer.writeheader()
        for row in rows:
            writer.writerow({**dict.fromkeys(MANIFEST_HEADER, ""), **row})


def frame_row(**overrides):
    row = {
        "query_string": "Testville, Testland",
        "city": "Testville",
        "admin": "",
        "iso2": "TL",
        "country": "Testland",
        "continent": "Europe",
        "size_band": "large",
        "population": "1000000",
        "coverage_regime": "present",
        "geonameid": "42",
        "lat": "10.0",
        "lon": "20.0",
    }
    row.update(overrides)
    return row


@pytest.fixture
def fake_geocode(monkeypatch):
    """
    Patch register_frame's geocoding seam; tunable center/dims + call count.
    ``fail`` lists queries that don't geocode; ``center_by_query`` overrides
    the returned center per query (the fake loc object is the query itself).
    """
    state = {
        "calls": 0,
        "center": (10.0, 20.0),
        "dims": (4000.0, 6000.0),
        "fail": set(),
        "center_by_query": {},
    }

    def fake_loc(query):
        state["calls"] += 1
        return None if query in state["fail"] else query

    monkeypatch.setattr(rf, "get_city_location_data", fake_loc)
    monkeypatch.setattr(
        rf, "_resolve_center", lambda loc: state["center_by_query"].get(loc, state["center"])
    )
    monkeypatch.setattr(rf, "get_search_dimensions", lambda q, w, h: state["dims"])
    return state


@pytest.fixture
def catalog(tmp_path):
    """Path to a fresh catalog DB (register_frame opens its own connection)."""
    return str(tmp_path / "catalog.db")


def _register_existing(catalog, city_name="Oldtown", center=(10.0, 20.05)):
    """An already-cataloged city ~5 km from the default frame row's GeoNames coords."""
    conn = db.connect(catalog)
    city_id = db.register_city(
        conn,
        city_name=city_name,
        state_name=None,
        state_code=None,
        country_name="Testland",
        country_code="TL",
        center_lat=center[0],
        center_lon=center[1],
        grid_width_m=5000,
        grid_height_m=5000,
        step_m=20,
    )
    conn.close()
    return city_id


def test_dry_run_writes_nothing(tmp_path, catalog, fake_geocode, capsys):
    existing_id = _register_existing(catalog)
    manifest = tmp_path / "frame.csv"
    write_manifest(
        manifest,
        [
            frame_row(),  # within 25 km of Oldtown -> overlap
            frame_row(query_string="Farville, Testland", city="Farville", lat="50.0", lon="60.0"),
        ],
    )

    rc = rf.main(["--manifest", str(manifest), "--db-path", catalog])

    assert rc == 0
    out = capsys.readouterr().out
    assert f"REUSE existing {existing_id}" in out
    assert "NEW (would geocode + register disabled)" in out
    assert "dry run" in out
    assert fake_geocode["calls"] == 0
    conn = db.connect(catalog)
    assert len(db.get_all_cities(conn)) == 1
    assert conn.execute("SELECT COUNT(*) FROM city_aliases").fetchone()[0] == 0
    conn.close()


def test_execute_overlap_reuses_existing_city(tmp_path, catalog, fake_geocode):
    existing_id = _register_existing(catalog)
    manifest = tmp_path / "frame.csv"
    write_manifest(manifest, [frame_row()])

    rc = rf.main(["--manifest", str(manifest), "--db-path", catalog, "--execute"])

    assert rc == 0
    assert fake_geocode["calls"] == 0  # the reuse path never geocodes
    conn = db.connect(catalog)
    assert len(db.get_all_cities(conn)) == 1  # no duplicate row
    # the frame slug now resolves to the pre-existing city
    assert db.resolve_city(conn, "Testville, Testland").city_id == existing_id
    conn.close()


def test_execute_registers_new_city_disabled(tmp_path, catalog, fake_geocode):
    manifest = tmp_path / "frame.csv"
    # a city-prefixed admin is dropped from identity (build_worldwide_frame rule)
    write_manifest(manifest, [frame_row(admin="Testville Province")])

    rc = rf.main(["--manifest", str(manifest), "--db-path", catalog, "--execute"])

    assert rc == 0
    assert fake_geocode["calls"] == 1
    conn = db.connect(catalog)
    row = db.resolve_city(conn, "Testville, Testland")
    assert row.city_id == "testville--testland"
    assert row.enabled is False  # out of the scheduler until boundary-vetted
    assert (row.center_lat, row.center_lon) == (10.0, 20.0)
    assert (row.grid_width_m, row.grid_height_m) == (4000, 6000)
    assert "geonameid 42" in row.notes
    conn.close()


def test_notes_label_identifies_the_manifest_batch(tmp_path, catalog, fake_geocode):
    """
    cities.notes carries the batch label, so a later reader (and the enable
    step) can tell which manifest a registered city came from. Asserting the
    default alone would pass even if the flag were dropped on the floor, so
    both the default and an override are exercised.
    """
    manifest = tmp_path / "frame.csv"
    write_manifest(manifest, [frame_row()])
    assert rf.main(["--manifest", str(manifest), "--db-path", catalog, "--execute"]) == 0

    other = tmp_path / "other.csv"
    write_manifest(
        other,
        [frame_row(query_string="Newville, Testland", city="Newville", lat="50.0", lon="60.0")],
    )
    fake_geocode["center_by_query"] = {"Newville, Testland": (50.0, 60.0)}
    assert (
        rf.main(
            [
                "--manifest",
                str(other),
                "--db-path",
                catalog,
                "--execute",
                "--notes-label",
                "mapillary 360 leaders",
            ]
        )
        == 0
    )

    conn = db.connect(catalog)
    assert db.resolve_city(conn, "Testville, Testland").notes.startswith("worldwide frame (")
    assert db.resolve_city(conn, "Newville, Testland").notes.startswith(
        "mapillary 360 leaders (geonameid 42)"
    )
    conn.close()


def test_center_guard_rejects_far_geocode(tmp_path, catalog, fake_geocode, capsys):
    fake_geocode["center"] = (11.0, 20.0)  # ~111 km from the GeoNames coords
    manifest = tmp_path / "frame.csv"
    write_manifest(manifest, [frame_row()])

    rc = rf.main(["--manifest", str(manifest), "--db-path", catalog, "--execute"])

    assert rc == 1
    out = capsys.readouterr().out
    assert "Needs manual review" in out
    assert "Testville, Testland" in out
    conn = db.connect(catalog)
    assert db.get_all_cities(conn) == []
    conn.close()


def test_center_from_geonames_fallback(tmp_path, catalog, fake_geocode):
    fake_geocode["center"] = (11.0, 20.0)
    manifest = tmp_path / "frame.csv"
    write_manifest(manifest, [frame_row()])

    rc = rf.main(
        ["--manifest", str(manifest), "--db-path", catalog, "--execute", "--center-from-geonames"]
    )

    assert rc == 0
    conn = db.connect(catalog)
    row = db.resolve_city(conn, "Testville, Testland")
    # registered with the GeoNames coordinates, not the bad geocode
    assert (row.center_lat, row.center_lon) == (10.0, 20.0)
    conn.close()


def test_execute_is_idempotent(tmp_path, catalog, fake_geocode, capsys):
    manifest = tmp_path / "frame.csv"
    write_manifest(manifest, [frame_row()])

    assert rf.main(["--manifest", str(manifest), "--db-path", catalog, "--execute"]) == 0
    capsys.readouterr()
    assert rf.main(["--manifest", str(manifest), "--db-path", catalog, "--execute"]) == 0

    out = capsys.readouterr().out
    assert "already registered as testville--testland" in out
    assert fake_geocode["calls"] == 1  # only the first run geocoded
    conn = db.connect(catalog)
    assert len(db.get_all_cities(conn)) == 1
    conn.close()


def test_fallback_query_when_manifest_query_fails_geocode(tmp_path, catalog, fake_geocode):
    # "Testville, Badmin, Testland" doesn't geocode (Nominatim doesn't know the
    # GeoNames admin name); the bare "Testville, Testland" fallback does.
    fake_geocode["fail"] = {"Testville, Badmin, Testland"}
    manifest = tmp_path / "frame.csv"
    write_manifest(
        manifest, [frame_row(query_string="Testville, Badmin, Testland", admin="Badmin")]
    )

    rc = rf.main(["--manifest", str(manifest), "--db-path", catalog, "--execute"])

    assert rc == 0
    conn = db.connect(catalog)
    row = db.resolve_city(conn, "Testville, Badmin, Testland")  # aliased manifest query
    assert row.city_id == "testville--badmin--testland"
    assert (row.center_lat, row.center_lon) == (10.0, 20.0)
    conn.close()


def test_fallback_query_when_manifest_query_trips_center_guard(tmp_path, catalog, fake_geocode):
    # The manifest query geocodes to the wrong feature entirely (Berlin was
    # 379 km off); the fallback geocodes near the GeoNames coordinates.
    fake_geocode["center_by_query"] = {
        "Testville, Badmin, Testland": (14.0, 20.0),  # ~445 km off
        "Testville, Testland": (10.1, 20.0),  # ~11 km off
    }
    manifest = tmp_path / "frame.csv"
    write_manifest(
        manifest, [frame_row(query_string="Testville, Badmin, Testland", admin="Badmin")]
    )

    rc = rf.main(["--manifest", str(manifest), "--db-path", catalog, "--execute"])

    assert rc == 0
    conn = db.connect(catalog)
    row = db.resolve_city(conn, "Testville, Badmin, Testland")
    assert (row.center_lat, row.center_lon) == (10.1, 20.0)  # the fallback's center
    conn.close()


def test_center_from_geonames_uses_first_geocoded_candidate(tmp_path, catalog, fake_geocode):
    # Both candidates trip the guard -> with the flag, register anyway using
    # the GeoNames coordinates as center.
    fake_geocode["center_by_query"] = {
        "Testville, Badmin, Testland": (14.0, 20.0),
        "Testville, Testland": (13.0, 20.0),
    }
    manifest = tmp_path / "frame.csv"
    write_manifest(
        manifest, [frame_row(query_string="Testville, Badmin, Testland", admin="Badmin")]
    )

    rc = rf.main(
        ["--manifest", str(manifest), "--db-path", catalog, "--execute", "--center-from-geonames"]
    )

    assert rc == 0
    conn = db.connect(catalog)
    row = db.resolve_city(conn, "Testville, Badmin, Testland")
    assert (row.center_lat, row.center_lon) == (10.0, 20.0)  # GeoNames coords
    conn.close()


def test_find_overlap_picks_nearest_within_threshold():
    class Pt:
        def __init__(self, city_id, lat, lon):
            self.city_id = city_id
            self.center_lat = lat
            self.center_lon = lon

    near = Pt("near", 10.0, 20.01)
    far = Pt("far", 10.0, 20.2)
    assert rf.find_overlap([far, near], 10.0, 20.0, 25.0) is near
    assert rf.find_overlap([far], 10.0, 20.0, 5.0) is None
    assert rf.find_overlap([], 10.0, 20.0, 25.0) is None
