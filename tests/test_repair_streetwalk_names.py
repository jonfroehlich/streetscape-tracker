"""
Tests for scripts/repair_streetwalk_names.py.

Road-walk artifacts collected before the provider token existed all share one
filename per (city, spacing, run_date), so a GSV walk and a Mapillary walk of
the same city could not coexist. The repair script renames the non-GSV ones and
repoints their catalog rows; these tests pin the parts that could silently lose
data — GSV names must be untouched, the catalog must never point at a file that
isn't there, and a second run must be a no-op.
"""

import gzip
import json
import os
from datetime import date

import pytest

from scripts import repair_streetwalk_names as repair
from streetscape_metadata_tracker import db


def _register(conn):
    return db.register_city(
        conn,
        city_name="Bend",
        state_name="Oregon",
        state_code="OR",
        country_name="United States",
        country_code="US",
        center_lat=44.05,
        center_lon=-121.30,
        grid_width_m=200,
        grid_height_m=200,
        step_m=20,
    )


def _legacy_name(city_id, run_date="2026-07-27"):
    """The old, tokenless name — what BOTH providers used to produce."""
    return f"{city_id}_width_200_height_200_step_20_streetwalk_sp15_{run_date}.csv.gz"


def _add_walk(
    conn, data_dir, city_id, provider, csv_name, *, write_files=True, network_type="drive"
):
    coverage_name = csv_name[: -len(".csv.gz")] + "_coverage.json.gz"
    if write_files:
        for name, payload in ((csv_name, "csv"), (coverage_name, "coverage")):
            with gzip.open(os.path.join(data_dir, name), "wt") as fh:
                fh.write(json.dumps({"marker": payload, "provider": provider}))
    db.register_street_walk(
        conn,
        city_id=city_id,
        run_date=date.fromisoformat(csv_name[-17:-7]),
        csv_filename=csv_name,
        provider=provider,
        network_type=network_type,
        coverage_filename=coverage_name,
        spacing_m=15,
        coverage_pct_by_length=1.9,
    )
    return coverage_name


@pytest.fixture
def walked(conn, data_dir):
    """A catalog with a legacy Mapillary walk and a correctly-named GSV one."""
    city_id = _register(conn)
    mly_csv = _legacy_name(city_id, "2026-07-27")
    gsv_csv = _legacy_name(city_id, "2026-07-17")  # tokenless IS correct for gsv
    _add_walk(conn, data_dir, city_id, "mapillary", mly_csv)
    _add_walk(conn, data_dir, city_id, "gsv", gsv_csv)
    return city_id, mly_csv, gsv_csv


def test_dry_run_reports_but_changes_nothing(walked, conn, data_dir):
    city_id, mly_csv, _ = walked

    items = repair.find_walks_to_repair(conn, data_dir)
    assert [i["row"]["provider"] for i in items] == ["mapillary"]
    assert "_mapillary_streetwalk_" in items[0]["new_csv"]

    for item in items:
        repair.repair_walk(conn, data_dir, item, execute=False)

    # Nothing moved, nothing repointed.
    assert os.path.exists(os.path.join(data_dir, mly_csv))
    assert not os.path.exists(os.path.join(data_dir, items[0]["new_csv"]))
    row = db.get_latest_street_walk(conn, city_id, provider="mapillary")
    assert row["csv_filename"] == mly_csv


def test_execute_renames_artifacts_and_repoints_the_catalog(walked, conn, data_dir):
    city_id, mly_csv, gsv_csv = walked

    items = repair.find_walks_to_repair(conn, data_dir)
    item = items[0]
    repair.repair_walk(conn, data_dir, item, execute=True)

    new_csv, new_coverage = item["new_csv"], item["new_coverage"]
    assert new_csv != mly_csv
    assert os.path.exists(os.path.join(data_dir, new_csv))
    assert os.path.exists(os.path.join(data_dir, new_coverage))
    assert not os.path.exists(os.path.join(data_dir, mly_csv))

    row = db.get_latest_street_walk(conn, city_id, provider="mapillary")
    assert row["csv_filename"] == new_csv
    assert row["coverage_filename"] == new_coverage

    # The renamed file is the Mapillary content, not a stray copy.
    with gzip.open(os.path.join(data_dir, new_csv), "rt") as fh:
        assert json.load(fh)["provider"] == "mapillary"

    # The GSV walk is untouched: tokenless is its correct name in both schemes,
    # and every GSV artifact ever published must keep resolving.
    assert os.path.exists(os.path.join(data_dir, gsv_csv))
    assert db.get_latest_street_walk(conn, city_id, provider="gsv")["csv_filename"] == gsv_csv


def test_repair_is_idempotent(walked, conn, data_dir):
    repair.repair_walk(conn, data_dir, repair.find_walks_to_repair(conn, data_dir)[0], execute=True)
    # Second pass finds nothing left to do.
    assert repair.find_walks_to_repair(conn, data_dir) == []


def test_missing_snapshot_is_skipped_not_repointed(conn, data_dir):
    """Repointing a row at a file that isn't on disk would turn a recoverable
    gap into a broken manifest entry."""
    city_id = _register(conn)
    csv_name = _legacy_name(city_id)
    _add_walk(conn, data_dir, city_id, "mapillary", csv_name, write_files=False)

    items = repair.find_walks_to_repair(conn, data_dir)
    assert len(items) == 1
    assert items[0]["csv_present"] is False

    # main() skips these; the row must still point at the original name.
    row = db.get_latest_street_walk(conn, city_id, provider="mapillary")
    assert row["csv_filename"] == csv_name


def test_broad_walk_is_renamed_with_its_network_token(conn, data_dir):
    """A pre-token broad walk must not be relabeled as a drive walk.

    --network-type predates the network token in filenames, so an all_public
    walk can carry a tokenless name too. Regenerating that name from the default
    would produce the DRIVE name — permanently losing which network was walked,
    and (on a date the city also has a drive walk) pointing two catalog rows at
    one artifact. The tokens come from the catalog row, which is the only place
    the truth survives.
    """
    city_id = _register(conn)
    csv_name = _legacy_name(city_id)
    _add_walk(conn, data_dir, city_id, "gsv", csv_name, network_type="all_public")

    items = repair.find_walks_to_repair(conn, data_dir)
    assert len(items) == 1
    assert "_streetwalk_allpublic_sp15_" in items[0]["new_csv"]
    # gsv stays tokenless — only the network token is added.
    assert "_gsv_" not in items[0]["new_csv"]

    repair.repair_walk(conn, data_dir, items[0], execute=True)
    row = db.get_latest_street_walk(conn, city_id, provider="gsv", network_type="all_public")
    assert row["csv_filename"] == items[0]["new_csv"]
    assert os.path.exists(os.path.join(data_dir, items[0]["new_csv"]))


def test_correctly_named_broad_walk_is_left_alone(conn, data_dir):
    """A walk already carrying its network token is not "corrected" again."""
    city_id = _register(conn)
    csv_name = (
        f"{city_id}_width_200_height_200_step_20"
        f"_mapillary_streetwalk_allpublic_sp15_2026-07-27.csv.gz"
    )
    _add_walk(conn, data_dir, city_id, "mapillary", csv_name, network_type="all_public")
    assert repair.find_walks_to_repair(conn, data_dir) == []


def test_target_name_owned_by_another_artifact_aborts_the_row(conn, data_dir):
    """Renaming onto an existing artifact would clobber it, and repointing the
    catalog at it would hand two rows one file. Leave the row misnamed instead —
    that is at least self-consistent."""
    city_id = _register(conn)
    csv_name = _legacy_name(city_id)
    _add_walk(conn, data_dir, city_id, "mapillary", csv_name)

    items = repair.find_walks_to_repair(conn, data_dir)
    assert len(items) == 1
    # Some other artifact already occupies the corrected name.
    with gzip.open(os.path.join(data_dir, items[0]["new_csv"]), "wt") as fh:
        fh.write(json.dumps({"marker": "someone else"}))

    assert repair.repair_walk(conn, data_dir, items[0], execute=True) is False

    # Nothing moved, nothing repointed, and the incumbent is intact.
    assert os.path.exists(os.path.join(data_dir, csv_name))
    with gzip.open(os.path.join(data_dir, items[0]["new_csv"]), "rt") as fh:
        assert json.load(fh)["marker"] == "someone else"
    row = db.get_latest_street_walk(conn, city_id, provider="mapillary")
    assert row["csv_filename"] == csv_name


def test_main_end_to_end_regenerates_the_manifest(walked, conn, data_dir, monkeypatch):
    """The published manifest is what the frontend reads, so a rename that
    didn't reach it would leave the city page fetching a 404."""
    monkeypatch.setattr(db, "connect", lambda path: conn)
    monkeypatch.setattr(
        "sys.argv",
        ["repair_streetwalk_names.py", "--data-dir", data_dir, "--execute"],
    )
    assert repair.main() == 0

    manifest_path = os.path.join(data_dir, "streetwalks.json.gz")
    with gzip.open(manifest_path, "rt") as fh:
        walks = json.load(fh)["walks"]
    by_provider = {w["provider"]: w for w in walks}
    assert "_mapillary_streetwalk_" in by_provider["mapillary"]["coverage_filename"]
    # The GSV entry keeps its tokenless name.
    assert "_mapillary_" not in by_provider["gsv"]["coverage_filename"]

    # Every advertised artifact actually exists on disk.
    for walk in walks:
        assert os.path.exists(os.path.join(data_dir, walk["coverage_filename"]))
