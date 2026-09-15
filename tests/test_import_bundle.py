"""
`scheduler import-bundle` — landing a laptop investigation in this catalog (issue #330).

What these pin, and why each one exists, is in docs/testing.md. The short version:
the importer writes into the production catalog and the published data directory
from a file its operator copied in, so every check that stands between those two
facts gets a test, and the refusals are pinned INDIVIDUALLY — a bundle that is
refused for the wrong reason is a bundle whose real problem went unlooked-at.
"""

import gzip
import json
import os
import sqlite3
import sys
from datetime import date

import pytest

from streetscape_metadata_tracker import analysis, bundle_import, db, fileutils
from streetscape_metadata_tracker.naming import (
    generate_run_filename,
    generate_streetwalk_filename,
    streetwalk_coverage_filename,
)
from streetscape_metadata_tracker.scheduler import (
    ProviderConfig,
    SchedulerConfig,
    cmd_import_bundle,
)
from tests.conftest import make_mapillary_city_df, write_city_csv_gz

RUN_DATE = date(2026, 9, 10)
CITY = dict(
    city_name="San Luis Obispo",
    state_name="California",
    state_code="CA",
    country_name="United States",
    country_code="us",
    center_lat=35.2766,
    center_lon=-120.6704,
    grid_width_m=10400,
    grid_height_m=10400,
    step_m=20,
)
CITY_ID = "san-luis-obispo--california--united-states"
SAMPLE_POINTS = 12


def _walk_names(provider="gsv", network_type="drive", spacing_m=15):
    stem = generate_streetwalk_filename(
        CITY_ID,
        CITY["grid_width_m"],
        CITY["grid_height_m"],
        CITY["step_m"],
        spacing_m,
        RUN_DATE,
        provider=provider,
        network_type=network_type,
    )
    csv_name = stem + ".csv.gz"
    return csv_name, streetwalk_coverage_filename(csv_name)


def _run_names(provider="mapillary"):
    stem = generate_run_filename(
        CITY_ID,
        CITY["grid_width_m"],
        CITY["grid_height_m"],
        CITY["step_m"],
        RUN_DATE,
        provider=provider,
    )
    return stem + ".csv.gz", stem + ".json.gz"


def _write_walk_artifacts(root, csv_name, coverage_name, *, sample_points=SAMPLE_POINTS):
    """A walk CSV with one row per sample point, and the coverage GeoJSON beside it."""
    with gzip.open(os.path.join(root, csv_name), "wt", encoding="utf-8") as fh:
        fh.write("query_lat,query_lon,status\n")
        for i in range(sample_points):
            fh.write(f"35.{i:04d},-120.6,OK\n")
    geojson = {
        "type": "FeatureCollection",
        "features": [],
        "properties": {
            "metadata": {
                "schema_version": 1,
                "kind": "streetwalk_coverage",
                "spacing_m": 15,
                "match_dist_m": 25.0,
                "totals": {"edges": 40, "coverage_pct_by_length": 92.8, "length_km": 375.1},
            }
        },
    }
    with gzip.open(os.path.join(root, coverage_name), "wt", encoding="utf-8") as fh:
        json.dump(geojson, fh)


@pytest.fixture
def bundle_dir(tmp_path):
    """
    A complete laptop bundle: one city, one Mapillary grid run, one GSV walk,
    the frozen network, and the spend ledger the laptop wrote.

    Deliberately built with the real ``db`` writers rather than hand-rolled SQL,
    so the fixture cannot drift from what a laptop run actually produces.
    """
    root = tmp_path / "bundle"
    (root / bundle_import.OSM_CACHE_DIRNAME).mkdir(parents=True)
    conn = db.connect(str(root / bundle_import.CATALOG_NAME))
    db.register_city(conn, **CITY)

    run_csv, run_json = _run_names()
    df = make_mapillary_city_df([("p1", "2024-05-01"), ("p2", "2023-07-01")], run_date=RUN_DATE)
    write_city_csv_gz(df, str(root / run_csv))
    (root / run_json).write_bytes(gzip.compress(b'{"metadata": {"schema_version": 2}}'))
    stats = analysis.calculate_run_stats(
        fileutils.load_city_csv_file(str(root / run_csv)), RUN_DATE, provider="mapillary"
    )
    db.register_run(
        conn,
        city_id=CITY_ID,
        run_date=RUN_DATE,
        csv_filename=run_csv,
        provider="mapillary",
        json_filename=run_json,
        api_requests=36,
        num_flat_images=0,
        census_fetched_by="mapillary",
        **stats,
    )

    walk_csv, walk_cov = _walk_names()
    _write_walk_artifacts(root, walk_csv, walk_cov)
    db.register_street_walk(
        conn,
        city_id=CITY_ID,
        run_date=RUN_DATE,
        csv_filename=walk_csv,
        provider="gsv",
        coverage_filename=walk_cov,
        network_type="drive",
        spacing_m=15,
        match_dist_m=25.0,
        sample_points=SAMPLE_POINTS,
        edges_total=40,
        coverage_pct_by_length=92.8,
        length_km=375.1,
        api_requests=SAMPLE_POINTS,
    )

    graphml = f"{CITY_ID}_streets_network.graphml"
    (root / bundle_import.OSM_CACHE_DIRNAME / graphml).write_text("<graphml/>")
    db.register_street_network(
        conn, city_id=CITY_ID, graphml_filename=graphml, network_type="drive", edge_count=40
    )

    db.add_api_usage(conn, RUN_DATE, SAMPLE_POINTS, provider="gsv_streets")
    db.add_api_usage(conn, RUN_DATE, 36, provider="mapillary_streets")
    conn.commit()
    conn.close()
    return root


@pytest.fixture
def cfg(tmp_path):
    """A scheduler config whose catalog and data dir are this host's, and publishing off."""
    data_dir = tmp_path / "prod"
    data_dir.mkdir()
    return SchedulerConfig(
        data_dir=str(data_dir),
        db_path=str(data_dir / "streetscape_tracker.db"),
        log_dir=str(tmp_path / "logs"),
        publish_enabled=False,
        providers={
            "gsv": ProviderConfig(),
            "gsv_streets": ProviderConfig(),
            "mapillary": ProviderConfig(),
            "mapillary_streets": ProviderConfig(),
        },
    )


@pytest.fixture(autouse=True)
def _no_batch_in_flight(monkeypatch):
    """
    Neutralize the `ps` scan suite-wide.

    Without this the suite's own pytest command line, or a real nightly batch on
    the machine running the tests, decides whether these tests pass.
    """
    monkeypatch.setattr("streetscape_metadata_tracker.scheduler._run_due_in_flight", lambda: None)


def _prod(cfg):
    conn = sqlite3.connect(cfg.db_path)
    conn.row_factory = sqlite3.Row
    return conn


# ── the happy path ────────────────────────────────────────────────────────


def test_dry_run_writes_nothing_to_either_the_catalog_or_the_data_dir(cfg, bundle_dir):
    assert cmd_import_bundle(cfg, str(bundle_dir)) == 0
    # Both halves matter: an importer that copied files and then declined to
    # catalog them would leave artifacts the aggregate never sees. The catalog
    # FILE is allowed to appear — db.connect creates the schema in order to ask
    # it anything — so the assertion is on rows, and on the data dir holding no
    # artifact.
    assert _prod(cfg).execute("SELECT COUNT(*) FROM cities").fetchone()[0] == 0
    assert [f for f in os.listdir(cfg.data_dir) if not f.startswith("streetscape_tracker.db")] == []


def test_execute_lands_rows_files_and_the_network(cfg, bundle_dir):
    assert cmd_import_bundle(cfg, str(bundle_dir), execute=True, enable=True) == 0
    conn = _prod(cfg)
    assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM street_walks").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM street_networks").fetchone()[0] == 1
    run_csv, _ = _run_names()
    walk_csv, walk_cov = _walk_names()
    for name in (run_csv, walk_csv, walk_cov):
        assert os.path.exists(os.path.join(cfg.data_dir, name)), name
    assert os.path.exists(
        os.path.join(
            cfg.data_dir, bundle_import.OSM_CACHE_DIRNAME, f"{CITY_ID}_streets_network.graphml"
        )
    )


def test_city_is_registered_disabled_unless_enable_is_passed(cfg, bundle_dir):
    # The whole point of the default: a city arriving through an investigation
    # has a boundary nobody vetted, and a fresh city sorts to the head of the
    # next night's stalest-first queue.
    assert cmd_import_bundle(cfg, str(bundle_dir), execute=True) == 0
    assert _prod(cfg).execute("SELECT enabled FROM cities").fetchone()[0] == 0


def test_enable_turns_on_a_city_that_is_already_registered_and_off(cfg, bundle_dir):
    # Through the importer, not db.set_city_enabled directly: the branch under
    # test is apply_bundle's "existing city, disabled, --enable", which a
    # helper call would leave unexercised.
    conn = db.connect(cfg.db_path)
    db.register_city(conn, **CITY, enabled=False)
    conn.close()
    assert cmd_import_bundle(cfg, str(bundle_dir), execute=True, enable=True) == 0
    assert _prod(cfg).execute("SELECT enabled FROM cities").fetchone()[0] == 1


# ── what the import must NOT do ───────────────────────────────────────────


def test_the_bundles_one_city_aggregate_is_never_copied_over_this_hosts(cfg, bundle_dir):
    """
    The footgun this importer exists next to.

    A bundle's data/ also holds cities.json.gz, streetwalks.json.gz and
    driving_plan.json.gz that the laptop generated over a ONE-CITY catalog. A
    directory sync would replace this host's published index with them — a
    site-wide outage produced by a successful import — so the copy step is
    file-by-file over the names catalog rows carry, and nothing else.
    """
    (bundle_dir / "cities.json.gz").write_bytes(gzip.compress(b'{"cities": ["only-me"]}'))
    bundle = bundle_import.read_bundle(bundle_dir)
    assert "cities.json.gz" not in bundle_import._artifact_sources(bundle)

    conn = db.connect(cfg.db_path)
    existing, problems = bundle_import.check_bundle(bundle, conn, cfg.data_dir)
    assert problems == []
    result = bundle_import.apply_bundle(
        bundle, conn, data_dir=cfg.data_dir, existing=existing, enable=False
    )
    assert not os.path.exists(os.path.join(cfg.data_dir, "cities.json.gz"))
    assert "cities.json.gz" not in result.files


def test_spend_is_ledgered_only_for_credentials_this_host_shares(cfg, bundle_dir):
    """
    The ABSENCE is the assertion.

    Mapillary meters by IP, so a laptop's tile spend was drawn against a
    different allowance; charging it here would tighten a budget gate against
    requests this host never made — on the very channel whose budget is the
    instrument in an open block investigation.
    """
    assert cmd_import_bundle(cfg, str(bundle_dir), execute=True) == 0
    charged = dict(_prod(cfg).execute("SELECT provider, requests FROM api_usage").fetchall())
    assert charged == {"gsv_streets": SAMPLE_POINTS}
    assert "mapillary_streets" not in charged


def test_no_cadence_row_for_a_channel_the_scheduler_cannot_run(cfg, bundle_dir, monkeypatch):
    """
    A success on an unwired channel would suppress its FIRST real collection.

    The bundle's Mapillary GRID run has a channel, so it gets a row. An unwired
    one must not — even though it is a perfectly good provider token with a
    perfectly good run.

    Driven through a SYNTHETIC UNWIRED_CHANNELS entry since #335, the way the
    scheduler's own cluster of these tests already is: `panoramax` really was
    unwired when this was written and is a real channel now, so the dict is
    empty and the property would otherwise be pinned vacuously. Monkeypatching
    keeps the mechanism under test — the import path must consult that dict,
    whatever is in it — rather than deleting the test with the entry.
    """
    from streetscape_metadata_tracker.scheduler import UNWIRED_CHANNELS

    monkeypatch.setitem(UNWIRED_CHANNELS, "panoramax", "a synthetic test reason")
    conn = db.connect(str(bundle_dir / bundle_import.CATALOG_NAME))
    conn.execute("UPDATE runs SET provider = 'panoramax'")
    run_csv, run_json = _run_names(provider="panoramax")
    old_csv, old_json = _run_names(provider="mapillary")
    conn.execute("UPDATE runs SET csv_filename = ?, json_filename = ?", (run_csv, run_json))
    conn.commit()
    conn.close()
    os.rename(bundle_dir / old_csv, bundle_dir / run_csv)
    os.rename(bundle_dir / old_json, bundle_dir / run_json)

    assert cmd_import_bundle(cfg, str(bundle_dir), execute=True, enable=True) == 0
    channels = {
        r[0]
        for r in _prod(cfg).execute(
            "SELECT provider FROM schedule_state WHERE last_success_at IS NOT NULL"
        )
    }
    assert "panoramax" not in channels
    assert "gsv_streets" in channels  # the walk still recorded one


# ── stats ─────────────────────────────────────────────────────────────────


def test_grid_stats_are_recomputed_from_the_csv_not_carried(cfg, bundle_dir):
    """
    Pinned by MUTATING the bundle's stored value, not by reading a default.

    A test that only checked the imported number equalled the recomputed one
    would stay green if the importer carried the bundle's row instead — the two
    agree whenever both checkouts are in step, which is exactly when the bug is
    invisible.
    """
    conn = sqlite3.connect(str(bundle_dir / bundle_import.CATALOG_NAME))
    conn.execute("UPDATE runs SET coverage_rate_pct = 99.0, total_points = 1")
    conn.commit()
    conn.close()

    assert cmd_import_bundle(cfg, str(bundle_dir), execute=True) == 0
    row = _prod(cfg).execute("SELECT coverage_rate_pct, total_points FROM runs").fetchone()
    assert row["coverage_rate_pct"] != 99.0
    assert row["total_points"] != 1


def test_walk_row_disagreeing_with_its_coverage_artifact_is_refused(cfg, bundle_dir):
    conn = sqlite3.connect(str(bundle_dir / bundle_import.CATALOG_NAME))
    conn.execute("UPDATE street_walks SET edges_total = 4000")
    conn.commit()
    conn.close()
    assert cmd_import_bundle(cfg, str(bundle_dir), execute=True) == 64
    assert _prod(cfg).execute("SELECT COUNT(*) FROM street_walks").fetchone()[0] == 0


def test_a_truncated_walk_csv_is_refused(cfg, bundle_dir):
    """An rsync cut short leaves a readable gzip whose row count is simply wrong."""
    walk_csv, walk_cov = _walk_names()
    _write_walk_artifacts(bundle_dir, walk_csv, walk_cov, sample_points=SAMPLE_POINTS - 3)
    assert cmd_import_bundle(cfg, str(bundle_dir), execute=True) == 64


# ── the refusals, one at a time ───────────────────────────────────────────


def test_geometry_mismatch_is_refused(cfg, bundle_dir):
    """
    The load-bearing check, and the one nothing in this codebase had.

    Every same_grid_geometry call compares filename to filename; none compares a
    filename to the cities row. A bundle collected on a different rectangle is
    not a later snapshot of the same series.
    """
    conn = db.connect(cfg.db_path)
    db.register_city(conn, **{**CITY, "grid_width_m": 9000})
    conn.close()
    assert cmd_import_bundle(cfg, str(bundle_dir), execute=True) == 64
    assert _prod(cfg).execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0


def test_a_filename_the_generators_would_not_produce_is_refused(cfg, bundle_dir):
    run_csv, _ = _run_names()
    conn = sqlite3.connect(str(bundle_dir / bundle_import.CATALOG_NAME))
    # Drop the provider token — the exact shape that silently collides two
    # providers sharing a run date.
    conn.execute("UPDATE runs SET csv_filename = ?", (run_csv.replace("_mapillary_", "_"),))
    conn.commit()
    conn.close()
    assert cmd_import_bundle(cfg, str(bundle_dir), execute=True) == 64


def test_an_existing_run_for_the_same_city_provider_and_date_is_refused(cfg, bundle_dir):
    conn = db.connect(cfg.db_path)
    db.register_city(conn, **CITY)
    run_csv, _ = _run_names()
    db.register_run(
        conn, city_id=CITY_ID, run_date=RUN_DATE, csv_filename=run_csv, provider="mapillary"
    )
    conn.close()
    assert cmd_import_bundle(cfg, str(bundle_dir), execute=True) == 64


def test_a_colliding_csv_filename_under_a_DIFFERENT_key_is_refused(cfg, bundle_dir):
    """
    The composite key does not catch this one.

    `runs.csv_filename` carries its own UNIQUE, independent of
    (city_id, provider, run_date) — so a row filed under another city's id can
    hold the name this bundle is about to write, and register_run would raise
    IntegrityError mid-import rather than refuse cleanly.
    """
    conn = db.connect(cfg.db_path)
    db.register_city(conn, **CITY)
    db.register_city(conn, **{**CITY, "city_name": "Elsewhere"})
    run_csv, _ = _run_names()
    db.register_run(
        conn,
        city_id=db.derive_city_id("Elsewhere", "California", "United States"),
        run_date=RUN_DATE,
        csv_filename=run_csv,
        provider="mapillary",
    )
    conn.close()
    assert cmd_import_bundle(cfg, str(bundle_dir), execute=True) == 64


def test_a_row_naming_an_artifact_the_bundle_lacks_is_refused(cfg, bundle_dir):
    run_csv, _ = _run_names()
    os.remove(bundle_dir / run_csv)
    assert cmd_import_bundle(cfg, str(bundle_dir), execute=True) == 64


def test_a_bundle_on_another_schema_version_is_refused(cfg, bundle_dir):
    conn = sqlite3.connect(str(bundle_dir / bundle_import.CATALOG_NAME))
    conn.execute(f"PRAGMA user_version = {db.SCHEMA_VERSION - 1}")
    conn.commit()
    conn.close()
    assert cmd_import_bundle(cfg, str(bundle_dir), execute=True) == 64


def test_a_live_wal_beside_the_bundle_catalog_is_refused(cfg, bundle_dir):
    """
    A read-only connection cannot replay a WAL, so reading anyway would import a
    bundle quietly missing its most recent rows.
    """
    (bundle_dir / f"{bundle_import.CATALOG_NAME}-wal").write_bytes(b"not empty")
    assert cmd_import_bundle(cfg, str(bundle_dir), execute=True) == 64


def test_a_directory_with_no_catalog_is_refused(cfg, tmp_path):
    assert cmd_import_bundle(cfg, str(tmp_path / "nothing-here"), execute=True) == 64


def test_an_import_is_refused_while_a_batch_looks_like_it_is_running(cfg, bundle_dir, monkeypatch):
    monkeypatch.setattr(
        "streetscape_metadata_tracker.scheduler._run_due_in_flight",
        lambda: "pid 1234: python -m streetscape_metadata_tracker.scheduler run-due",
    )
    assert cmd_import_bundle(cfg, str(bundle_dir), execute=True) == 64
    # ...and --force is the documented way past a false positive.
    assert cmd_import_bundle(cfg, str(bundle_dir), execute=True, force=True) == 0


def test_re_running_a_landed_import_refuses_rather_than_double_charging(cfg, bundle_dir):
    """
    add_api_usage is ADDITIVE, so this refusal is what makes a retry safe: an
    operator who re-runs after a partial-looking failure must not pay twice.
    """
    assert cmd_import_bundle(cfg, str(bundle_dir), execute=True) == 0
    before = _prod(cfg).execute("SELECT requests FROM api_usage").fetchone()[0]
    assert cmd_import_bundle(cfg, str(bundle_dir), execute=True) == 64
    assert _prod(cfg).execute("SELECT requests FROM api_usage").fetchone()[0] == before


# ── importing into a city this host ALREADY tracks ────────────────────────
#
# Every test above lands in an empty catalog. These pre-register the city and
# give it history, because the re-import path is the one the geometry check
# exists for — and the one where an importer can quietly re-base a series.


def _prod_network(cfg, content):
    """Register the city on this host with its own frozen drive network."""
    conn = db.connect(cfg.db_path)
    db.register_city(conn, **CITY)
    graphml = f"{CITY_ID}_streets_network.graphml"
    cache = os.path.join(cfg.data_dir, bundle_import.OSM_CACHE_DIRNAME)
    os.makedirs(cache, exist_ok=True)
    path = os.path.join(cache, graphml)
    with open(path, "w") as fh:
        fh.write(content)
    db.register_street_network(
        conn,
        city_id=CITY_ID,
        graphml_filename=graphml,
        network_type="drive",
        edge_count=999,
        osmnx_version="prod-1.0",
    )
    conn.close()
    return path


def test_an_existing_byte_identical_network_is_kept_not_rewritten(cfg, bundle_dir):
    """
    The GraphML name is deterministic per (city_id, network_type) — no date, no
    host token — so a bundle's network always names the SAME file this host
    holds. The bytes decide: identical means this host's row and file are kept
    and nothing is written for it.
    """
    _prod_network(cfg, "<graphml/>")  # what the fixture's bundle carries
    assert cmd_import_bundle(cfg, str(bundle_dir), execute=True) == 0
    row = _prod(cfg).execute("SELECT edge_count, osmnx_version FROM street_networks").fetchone()
    assert (row["edge_count"], row["osmnx_version"]) == (999, "prod-1.0")


def test_an_existing_network_with_different_bytes_is_refused(cfg, bundle_dir):
    """
    The bundle's walk was measured on another OSM snapshot than this host's
    series; replacing the file would silently re-base every walk cataloged here.
    """
    ours = "<graphml>this host's frozen network</graphml>"
    path = _prod_network(cfg, ours)
    assert cmd_import_bundle(cfg, str(bundle_dir), execute=True) == 64
    assert open(path).read() == ours
    assert _prod(cfg).execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0


def test_a_networks_fetched_at_is_carried_not_restamped(cfg, bundle_dir):
    """fetched_at is when the OSM snapshot was taken — the network's provenance."""
    conn = sqlite3.connect(str(bundle_dir / bundle_import.CATALOG_NAME))
    conn.execute("UPDATE street_networks SET fetched_at = '2026-09-09T12:00:00+00:00'")
    conn.commit()
    conn.close()
    assert cmd_import_bundle(cfg, str(bundle_dir), execute=True) == 0
    fetched = _prod(cfg).execute("SELECT fetched_at FROM street_networks").fetchone()[0]
    assert fetched == "2026-09-09T12:00:00+00:00"


def _prod_run(cfg, run_date, panos):
    """Register the city on this host with one Mapillary run (CSV on disk)."""
    conn = db.connect(cfg.db_path)
    db.register_city(conn, **CITY)
    stem = generate_run_filename(
        CITY_ID,
        CITY["grid_width_m"],
        CITY["grid_height_m"],
        CITY["step_m"],
        run_date,
        provider="mapillary",
    )
    write_city_csv_gz(
        make_mapillary_city_df(panos, run_date=run_date),
        os.path.join(cfg.data_dir, stem + ".csv.gz"),
    )
    db.register_run(
        conn,
        city_id=CITY_ID,
        run_date=run_date,
        csv_filename=stem + ".csv.gz",
        provider="mapillary",
        json_filename=stem + ".json.gz",
    )
    conn.close()


def test_a_run_extending_this_hosts_series_is_diffed_against_the_previous_run(cfg, bundle_dir):
    """
    The bundle's catalog knew only the laptop's runs, so nothing in it describes
    the change since THIS host's previous snapshot — and regenerate_run_json
    replays a run_diffs row rather than computing one. Without the row the
    JSON's change block is null and city.js falls back to constructing the
    detail filename from run history: a 404 on the site.
    """
    _prod_run(cfg, date(2026, 6, 1), [("p1", "2024-05-01")])  # the bundle adds p2
    assert cmd_import_bundle(cfg, str(bundle_dir), execute=True) == 0
    conn = _prod(cfg)
    diff = conn.execute("SELECT panos_added, detail_filename FROM run_diffs").fetchone()
    assert diff is not None and diff["panos_added"] == 1
    assert os.path.exists(os.path.join(cfg.data_dir, diff["detail_filename"]))
    _, run_json = _run_names()
    with gzip.open(os.path.join(cfg.data_dir, run_json), "rt", encoding="utf-8") as fh:
        change = json.load(fh)["change_from_previous_run"]
    assert change is not None
    assert change["diff_file"] == diff["detail_filename"]


def test_a_run_older_than_this_hosts_newest_is_refused(cfg, bundle_dir):
    """A series is append-only: an older snapshot cannot be inserted behind the newest."""
    _prod_run(cfg, date(2026, 12, 1), [("p1", "2024-05-01")])
    assert cmd_import_bundle(cfg, str(bundle_dir), execute=True) == 64
    assert _prod(cfg).execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1


def _prod_walk(cfg, run_date, *, with_artifacts):
    """Register the city on this host with one GSV drive walk."""
    conn = db.connect(cfg.db_path)
    db.register_city(conn, **CITY)
    stem = generate_streetwalk_filename(
        CITY_ID,
        CITY["grid_width_m"],
        CITY["grid_height_m"],
        CITY["step_m"],
        15,
        run_date,
        provider="gsv",
        network_type="drive",
    )
    csv_name = stem + ".csv.gz"
    cov_name = streetwalk_coverage_filename(csv_name)
    if with_artifacts:
        _write_walk_artifacts(cfg.data_dir, csv_name, cov_name)
    db.register_street_walk(
        conn,
        city_id=CITY_ID,
        run_date=run_date,
        csv_filename=csv_name,
        provider="gsv",
        coverage_filename=cov_name,
        network_type="drive",
        spacing_m=15,
        match_dist_m=25.0,
        sample_points=SAMPLE_POINTS,
        edges_total=40,
        coverage_pct_by_length=92.8,
        length_km=375.1,
    )
    conn.close()


def test_a_walk_extending_this_hosts_series_is_diffed_against_the_previous_walk(cfg, bundle_dir):
    _prod_walk(cfg, date(2026, 6, 1), with_artifacts=True)
    assert cmd_import_bundle(cfg, str(bundle_dir), execute=True) == 0
    assert _prod(cfg).execute("SELECT COUNT(*) FROM street_walk_diffs").fetchone()[0] == 1


def test_a_walk_older_than_this_hosts_newest_is_refused(cfg, bundle_dir):
    _prod_walk(cfg, date(2026, 12, 1), with_artifacts=False)
    assert cmd_import_bundle(cfg, str(bundle_dir), execute=True) == 64
    assert _prod(cfg).execute("SELECT COUNT(*) FROM street_walks").fetchone()[0] == 1


def test_a_walk_on_another_network_type_leaves_the_channels_cadence_alone(cfg, bundle_dir):
    """
    gsv_streets walks 'drive'. An all_public walk is its own series and says
    nothing about whether the drive walk is due, so stamping a success from it
    would suppress a walk nobody has collected.
    """
    old_csv, old_cov = _walk_names()
    new_csv, new_cov = _walk_names(network_type="all_public")
    conn = sqlite3.connect(str(bundle_dir / bundle_import.CATALOG_NAME))
    conn.execute(
        "UPDATE street_walks SET network_type = 'all_public', csv_filename = ?, "
        "coverage_filename = ?",
        (new_csv, new_cov),
    )
    conn.commit()
    conn.close()
    os.rename(bundle_dir / old_csv, bundle_dir / new_csv)
    os.rename(bundle_dir / old_cov, bundle_dir / new_cov)

    assert cmd_import_bundle(cfg, str(bundle_dir), execute=True, enable=True) == 0
    channels = {
        r[0]
        for r in _prod(cfg).execute(
            "SELECT provider FROM schedule_state WHERE last_success_at IS NOT NULL"
        )
    }
    assert "gsv_streets" not in channels
    assert "mapillary" in channels  # the grid run still recorded its own


def test_an_unknown_provider_is_refused_not_a_traceback(cfg, bundle_dir):
    """The filename generators raise ValueError on it; that must read as exit 64."""
    conn = sqlite3.connect(str(bundle_dir / bundle_import.CATALOG_NAME))
    conn.execute("UPDATE runs SET provider = 'lookaround'")
    conn.commit()
    conn.close()
    assert cmd_import_bundle(cfg, str(bundle_dir), execute=True) == 64


def test_a_crash_at_the_ledger_leaves_the_cadence_rows_already_written(
    cfg, bundle_dir, monkeypatch
):
    """
    The ledger is the LAST write, after the cadence rows: a crash there leaves
    a retry refused (the rows exist) AND the city not due tonight. Before this
    ordering the cadence was written by the caller after the ledger, so the
    same crash left a refused retry and a city the next night re-collected.
    """

    def boom(*_a, **_k):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(bundle_import.db, "add_api_usage", boom)
    with pytest.raises(sqlite3.OperationalError):
        cmd_import_bundle(cfg, str(bundle_dir), execute=True)
    channels = {
        r[0]
        for r in _prod(cfg).execute(
            "SELECT provider FROM schedule_state WHERE last_success_at IS NOT NULL"
        )
    }
    assert {"mapillary", "gsv_streets"} <= channels
    monkeypatch.undo()
    assert cmd_import_bundle(cfg, str(bundle_dir), execute=True) == 64
    assert _prod(cfg).execute("SELECT COUNT(*) FROM api_usage").fetchone()[0] == 0


def test_a_previous_run_on_another_grid_geometry_is_not_diffed(cfg, bundle_dir):
    """
    The collector's own gate, restated: check_bundle proves the bundle matches
    this host's CURRENT frozen grid, not the previous run's — which predates a
    catalog-only resize (#166) or is an archival baseline (#93). A
    cross-geometry diff compares different sampled areas, and the site renders
    its counts as imagery churn with no grid_aligned check.
    """
    conn = db.connect(cfg.db_path)
    db.register_city(conn, **CITY)
    earlier = date(2026, 6, 1)
    stem = generate_run_filename(CITY_ID, 5000, 5000, 50, earlier, provider="mapillary")
    write_city_csv_gz(
        make_mapillary_city_df([("p1", "2024-05-01")], run_date=earlier),
        os.path.join(cfg.data_dir, stem + ".csv.gz"),
    )
    db.register_run(
        conn, city_id=CITY_ID, run_date=earlier, csv_filename=stem + ".csv.gz", provider="mapillary"
    )
    conn.close()
    assert cmd_import_bundle(cfg, str(bundle_dir), execute=True) == 0
    assert _prod(cfg).execute("SELECT COUNT(*) FROM run_diffs").fetchone()[0] == 0


def test_a_graphml_on_disk_with_no_catalog_row_is_not_overwritten(cfg, bundle_dir):
    """osm_cache/ is outside the artifact sweep, so this is the only guard for a row-less file."""
    conn = db.connect(cfg.db_path)
    db.register_city(conn, **CITY)
    conn.close()
    cache = os.path.join(cfg.data_dir, bundle_import.OSM_CACHE_DIRNAME)
    os.makedirs(cache)
    path = os.path.join(cache, f"{CITY_ID}_streets_network.graphml")
    with open(path, "w") as fh:
        fh.write("<graphml>this host's, unregistered</graphml>")
    assert cmd_import_bundle(cfg, str(bundle_dir), execute=True) == 64
    assert open(path).read() == "<graphml>this host's, unregistered</graphml>"


def test_a_failed_diff_import_does_not_leave_a_half_imported_run(cfg, bundle_dir, monkeypatch):
    """
    The lazy `from .cli import` pulls in every downloader; if that fails, the
    run is already committed, so the failure must be cheap: cadence and ledger
    still land, and only the diff is missing.
    """
    _prod_run(cfg, date(2026, 6, 1), [("p1", "2024-05-01")])
    monkeypatch.setitem(sys.modules, "streetscape_metadata_tracker.cli", None)
    assert cmd_import_bundle(cfg, str(bundle_dir), execute=True) == 0
    conn = _prod(cfg)
    assert conn.execute("SELECT COUNT(*) FROM run_diffs").fetchone()[0] == 0
    channels = {
        r[0]
        for r in conn.execute(
            "SELECT provider FROM schedule_state WHERE last_success_at IS NOT NULL"
        )
    }
    assert {"mapillary", "gsv_streets"} <= channels
    assert conn.execute("SELECT COUNT(*) FROM api_usage").fetchone()[0] == 1


def test_every_problem_is_reported_not_just_the_first(cfg, bundle_dir, capsys):
    """
    A bundle that is both filename-colliding AND out of order has two problems;
    a refusal naming only one sends the operator to fix the wrong thing.
    """
    _prod_run(cfg, date(2026, 12, 1), [("p1", "2024-05-01")])  # newer than the bundle
    conn = db.connect(cfg.db_path)
    db.register_city(conn, **{**CITY, "city_name": "Elsewhere"})
    run_csv, _ = _run_names()
    db.register_run(
        conn,
        city_id=db.derive_city_id("Elsewhere", "California", "United States"),
        run_date=RUN_DATE,
        csv_filename=run_csv,
        provider="mapillary",
    )
    conn.close()
    assert cmd_import_bundle(cfg, str(bundle_dir), execute=True) == 64
    out = capsys.readouterr().out
    assert "refusing to collide" in out
    assert "append-only" in out
