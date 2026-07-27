"""Archival import tests (issue #93): translate predecessor-project CSVs
into is_baseline=1 dated runs against a fixture source tree + manifest."""

import gzip
import json
import os
import subprocess
import sys

import pandas as pd
import pytest

from streetscape_metadata_tracker import db
from streetscape_metadata_tracker.fileutils import load_city_csv_file

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(PROJECT_ROOT, "scripts", "import_archival_scrapes.py")


def _write_v1_csv(path, ok_rows, fail_rows):
    """v1 headerless 5-col: coords, pano_id, YYYY-MM date, status-on-failure."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    lines = [f"{lat},{lon},{pano_id},{capture}," for lat, lon, pano_id, capture in ok_rows]
    lines += [f"{lat},{lon},None,None,ZERO_RESULTS" for lat, lon in fail_rows]
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def _write_v2_csv(path, ok_rows, fail_rows, header=True):
    """v2 7-col: lat,lon,query_lat,query_lon,pano_id,date,status."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    lines = ["lat,lon,query_lat,query_lon,pano_id,date,status"] if header else []
    lines += [
        f"{lat},{lon},{qlat},{qlon},{pano_id},{capture},"
        for lat, lon, qlat, qlon, pano_id, capture in ok_rows
    ]
    # Failure rows echo the query coords into lat/lon (old scraper behavior)
    lines += [f"{qlat},{qlon},{qlat},{qlon},None,None,ZERO_RESULTS" for qlat, qlon in fail_rows]
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def _write_bbox(csv_path, ymin, ymax, xmin, xmax):
    with open(
        os.path.join(os.path.dirname(csv_path), "bounding_box.json"), "w", encoding="utf-8"
    ) as f:
        json.dump({"ymin": ymin, "ymax": ymax, "xmin": xmin, "xmax": xmax}, f)


def _write_manifest(path, entries):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(entries, f)
    return path


def _register_city(data_dir, city_name, state_name, country_name):
    conn = db.connect(os.path.join(data_dir, "streetscape_tracker.db"))
    city_id = db.register_city(
        conn,
        city_name=city_name,
        state_name=state_name,
        state_code=None,
        country_name=country_name,
        country_code=None,
        center_lat=44.0,
        center_lon=-121.0,
        grid_width_m=5000,
        grid_height_m=5000,
        step_m=20,
    )
    conn.close()
    return city_id


def _run_import(source_root, data_dir, manifest_path, *extra):
    return subprocess.run(
        [
            sys.executable,
            SCRIPT,
            "--source-root",
            source_root,
            "--data-dir",
            data_dir,
            "--manifest",
            manifest_path,
            "--no-nominatim",
            *extra,
        ],
        capture_output=True,
        text=True,
        cwd=PROJECT_ROOT,
    )


@pytest.fixture
def source_root(tmp_path):
    d = tmp_path / "source"
    d.mkdir()
    return str(d)


def test_v1_import_end_to_end(source_root, data_dir, tmp_path):
    csv_path = os.path.join(source_root, "Oldtown/Oldtown_30_coords.csv")
    _write_v1_csv(
        csv_path,
        ok_rows=[(44.001, -121.001, "pA", "2021-08"), (44.002, -121.001, "pB", "2022-03")],
        fail_rows=[(44.003, -121.001)],
    )
    city_id = _register_city(data_dir, "Oldtown", None, "Testland")
    manifest = _write_manifest(
        str(tmp_path / "m.json"),
        [
            {
                "rel_csv": "Oldtown/Oldtown_30_coords.csv",
                "query": "Oldtown, Testland",
                "run_date": "2023-11-05",
                "fmt": "v1",
            },
        ],
    )

    # Dry run: reports the plan, writes no run files and registers nothing
    result = _run_import(source_root, data_dir, manifest)
    assert result.returncode == 0, result.stderr
    assert "Would import: 1" in result.stdout
    assert "Dry run complete" in result.stdout
    assert not [f for f in os.listdir(data_dir) if f.endswith(".csv.gz")]
    conn = db.connect(os.path.join(data_dir, "streetscape_tracker.db"))
    assert db.get_runs_for_city(conn, city_id) == []
    conn.close()

    # Execute
    result = _run_import(source_root, data_dir, manifest, "--execute")
    assert result.returncode == 0, result.stderr
    assert "1 baseline runs registered" in result.stdout

    conn = db.connect(os.path.join(data_dir, "streetscape_tracker.db"))
    run = db.get_latest_run(conn, city_id)
    assert run.is_baseline
    assert run.run_date == "2023-11-05"
    assert run.provider == "gsv"
    assert run.csv_filename.startswith(f"{city_id}_width_")
    assert "_step_30_2023-11-05" in run.csv_filename
    assert run.unique_panos == 2
    assert run.unique_google_panos is None  # copyright never captured
    assert run.status_ok == 2 and run.status_zero_results == 1
    # No diff: archival runs are the city's earliest
    assert conn.execute("SELECT COUNT(*) FROM run_diffs").fetchone()[0] == 0

    # The written artifact round-trips through the canonical loader
    df = load_city_csv_file(os.path.join(data_dir, run.csv_filename))
    assert list(df.columns) == [
        "query_lat",
        "query_lon",
        "query_timestamp",
        "pano_lat",
        "pano_lon",
        "pano_id",
        "capture_date",
        "copyright_info",
        "status",
    ]
    assert df["copyright_info"].isna().all()
    ok = df[df["status"] == "OK"]
    # YYYY-MM source dates land as first-of-month (loader parses to datetime)
    assert sorted(ok["capture_date"]) == [pd.Timestamp("2021-08-01"), pd.Timestamp("2022-03-01")]
    assert set(df["status"]) == {"OK", "ZERO_RESULTS"}
    assert df["query_timestamp"].eq("2023-11-05T00:00:00+00:00").all()

    # Per-run JSON: baseline + copyright flag, google_panos omitted
    with gzip.open(os.path.join(data_dir, run.json_filename), "rt") as f:
        summary = json.load(f, parse_constant=lambda t: (_ for _ in ()).throw(ValueError(t)))
    assert summary["run"] == {"run_date": "2023-11-05", "is_baseline": True}
    assert summary["copyright_info_available"] is False
    assert "google_panos" not in summary
    conn.close()


def test_v2_headered_and_headerless_translate_identically(source_root, data_dir, tmp_path):
    ok = [(44.0011, -121.0011, 44.001, -121.001, "pA", "2020-05")]
    fail = [(44.002, -121.001)]
    h_path = os.path.join(source_root, "Htown/Htown_30_coords.csv")
    n_path = os.path.join(source_root, "Ntown/Ntown_30_coords.csv")
    _write_v2_csv(h_path, ok, fail, header=True)
    _write_v2_csv(n_path, ok, fail, header=False)
    _write_bbox(h_path, 44.0, 44.01, -121.01, -121.0)
    # Ntown's bbox has deliberately swapped extents (seen in real sidecars)
    _write_bbox(n_path, 44.01, 44.0, -121.0, -121.01)
    h_id = _register_city(data_dir, "Htown", None, "Testland")
    n_id = _register_city(data_dir, "Ntown", None, "Testland")
    manifest = _write_manifest(
        str(tmp_path / "m.json"),
        [
            {
                "rel_csv": "Htown/Htown_30_coords.csv",
                "query": "Htown, Testland",
                "run_date": "2024-02-14",
                "fmt": "v2",
            },
            {
                "rel_csv": "Ntown/Ntown_30_coords.csv",
                "query": "Ntown, Testland",
                "run_date": "2024-02-14",
                "fmt": "v2_headerless",
            },
        ],
    )

    result = _run_import(source_root, data_dir, manifest, "--execute")
    assert result.returncode == 0, result.stderr

    conn = db.connect(os.path.join(data_dir, "streetscape_tracker.db"))
    dfs = {}
    for city_id in (h_id, n_id):
        run = db.get_latest_run(conn, city_id)
        # min/max-normalized bboxes -> identical geometry in both filenames
        assert "_step_30_2024-02-14" in run.csv_filename
        dfs[city_id] = load_city_csv_file(os.path.join(data_dir, run.csv_filename))
    conn.close()

    a, b = dfs[h_id], dfs[n_id]
    pd.testing.assert_frame_equal(a, b)
    ok_rows = a[a["status"] == "OK"]
    fail_rows = a[a["status"] == "ZERO_RESULTS"]
    # OK rows keep distinct pano vs query coords; failure rows only echoed
    # the query coords, so their pano fields must be null
    assert float(ok_rows.iloc[0]["pano_lat"]) == 44.0011
    assert float(ok_rows.iloc[0]["query_lat"]) == 44.001
    assert fail_rows["pano_lat"].isna().all()
    assert fail_rows["pano_id"].isna().all()


def test_two_runs_one_city_different_geometry(source_root, data_dir, tmp_path):
    # The Washington D.C. pattern: two scrapes of one city, different
    # extents and dates -> one city, two baseline runs, no diff
    small = os.path.join(source_root, "DC/DC_30_coords.csv")
    big = os.path.join(source_root, "DC 10k/DC_30_coords.csv")
    _write_v2_csv(
        small, [(44.0011, -121.0011, 44.001, -121.001, "p1", "2020-05")], [(44.002, -121.001)]
    )
    _write_v2_csv(
        big,
        [
            (44.0011, -121.0011, 44.001, -121.001, "p1", "2020-05"),
            (44.0021, -121.0011, 44.002, -121.001, "p2", "2021-06"),
        ],
        [(44.003, -121.001)],
    )
    _write_bbox(small, 44.0, 44.01, -121.01, -121.0)
    _write_bbox(big, 44.0, 44.09, -121.09, -121.0)
    city_id = _register_city(data_dir, "Deecee", None, "Testland")
    manifest = _write_manifest(
        str(tmp_path / "m.json"),
        [
            {
                "rel_csv": "DC/DC_30_coords.csv",
                "query": "Deecee, Testland",
                "run_date": "2023-11-03",
                "fmt": "v2",
            },
            {
                "rel_csv": "DC 10k/DC_30_coords.csv",
                "query": "Deecee, Testland",
                "run_date": "2024-04-11",
                "fmt": "v2",
            },
        ],
    )

    result = _run_import(source_root, data_dir, manifest, "--execute")
    assert result.returncode == 0, result.stderr

    conn = db.connect(os.path.join(data_dir, "streetscape_tracker.db"))
    runs = db.get_runs_for_city(conn, city_id)
    assert [r.run_date for r in runs] == ["2023-11-03", "2024-04-11"]
    assert all(r.is_baseline for r in runs)
    assert runs[0].csv_filename != runs[1].csv_filename
    assert conn.execute("SELECT COUNT(*) FROM run_diffs").fetchone()[0] == 0

    # Aggregate regenerated with both runs and the copyright flag
    with gzip.open(os.path.join(data_dir, "cities.json.gz"), "rt") as f:
        agg = json.load(f)
    rec = next(c for c in agg["cities"] if c["city_id"] == city_id)
    gsv = rec["providers"]["gsv"]
    assert len(gsv["runs"]) == 2
    assert gsv["latest"]["copyright_info_available"] is False
    assert "unique_google_panos" not in gsv["latest"]["panorama_counts"]
    conn.close()


def test_idempotent_rerun(source_root, data_dir, tmp_path):
    csv_path = os.path.join(source_root, "Oldtown/Oldtown_30_coords.csv")
    _write_v1_csv(csv_path, ok_rows=[(44.001, -121.001, "pA", "2021-08")], fail_rows=[])
    _register_city(data_dir, "Oldtown", None, "Testland")
    manifest = _write_manifest(
        str(tmp_path / "m.json"),
        [
            {
                "rel_csv": "Oldtown/Oldtown_30_coords.csv",
                "query": "Oldtown, Testland",
                "run_date": "2023-11-05",
                "fmt": "v1",
            },
        ],
    )

    result = _run_import(source_root, data_dir, manifest, "--execute")
    assert result.returncode == 0, result.stderr
    assert "1 baseline runs registered" in result.stdout

    result = _run_import(source_root, data_dir, manifest, "--execute")
    assert result.returncode == 0, result.stderr
    assert "Imported: 0   already registered: 1" in result.stdout


def test_unresolved_city_skipped_without_nominatim(source_root, data_dir, tmp_path):
    csv_path = os.path.join(source_root, "Ghost/Ghost_30_coords.csv")
    _write_v1_csv(csv_path, ok_rows=[(44.001, -121.001, "pA", "2021-08")], fail_rows=[])
    manifest = _write_manifest(
        str(tmp_path / "m.json"),
        [
            {
                "rel_csv": "Ghost/Ghost_30_coords.csv",
                "query": "Ghost, Nowhere",
                "run_date": "2023-11-05",
                "fmt": "v1",
            },
        ],
    )

    result = _run_import(source_root, data_dir, manifest, "--execute")
    assert result.returncode == 0, result.stderr
    assert "needs geocoding" in result.stdout
    assert "0 baseline runs registered" in result.stdout
    assert not [f for f in os.listdir(data_dir) if f.endswith(".csv.gz")]


def test_manifest_identity_registers_without_geocoding(source_root, data_dir, tmp_path):
    # Locality-grade datasets (East Hollywood, Reinsletta) carry an explicit
    # identity so no Nominatim call happens even without --no-nominatim
    csv_path = os.path.join(source_root, "Quarter/Quarter_30_coords.csv")
    _write_v2_csv(
        csv_path, [(44.0011, -121.0011, 44.001, -121.001, "p1", "2020-05")], [(44.002, -121.001)]
    )
    _write_bbox(csv_path, 44.0, 44.01, -121.01, -121.0)
    manifest = _write_manifest(
        str(tmp_path / "m.json"),
        [
            {
                "rel_csv": "Quarter/Quarter_30_coords.csv",
                "query": "Quarter, Big City, Testland",
                "run_date": "2023-11-03",
                "fmt": "v2",
                "identity": ["Quarter", None, "Testland"],
            },
        ],
    )

    result = _run_import(source_root, data_dir, manifest)
    assert result.returncode == 0, result.stderr
    assert "would register from manifest identity (quarter--testland)" in result.stdout

    result = _run_import(source_root, data_dir, manifest, "--execute")
    assert result.returncode == 0, result.stderr
    assert "1 baseline runs registered" in result.stdout

    conn = db.connect(os.path.join(data_dir, "streetscape_tracker.db"))
    city = db.resolve_city(conn, "quarter--testland")
    assert city is not None
    assert city.step_m == 20  # frozen at the default step, not the archival 30
    assert (city.grid_width_m, city.grid_height_m) > (0, 0)
    run = db.get_latest_run(conn, city.city_id)
    assert run.is_baseline and "_step_30_" in run.csv_filename
    conn.close()


def test_redate_after_manifest_date_correction(source_root, data_dir, tmp_path):
    # A manifest date correction (issue #93 follow-up: the original dates
    # came from rename-polluted `git --follow` history) re-dates the
    # already-imported run in place instead of importing a duplicate
    csv_path = os.path.join(source_root, "Oldtown/Oldtown_30_coords.csv")
    _write_v1_csv(csv_path, ok_rows=[(44.001, -121.001, "pA", "2021-08")], fail_rows=[])
    city_id = _register_city(data_dir, "Oldtown", None, "Testland")
    entry = {
        "rel_csv": "Oldtown/Oldtown_30_coords.csv",
        "query": "Oldtown, Testland",
        "run_date": "2023-11-03",
        "fmt": "v1",
    }
    wrong = _write_manifest(str(tmp_path / "wrong.json"), [entry])
    result = _run_import(source_root, data_dir, wrong, "--execute")
    assert result.returncode == 0, result.stderr

    conn = db.connect(os.path.join(data_dir, "streetscape_tracker.db"))
    old_run = db.get_latest_run(conn, city_id)
    conn.close()
    assert old_run.run_date == "2023-11-03"

    corrected = _write_manifest(
        str(tmp_path / "corrected.json"), [{**entry, "run_date": "2024-07-16"}]
    )

    # Dry run narrates the re-date but changes nothing
    result = _run_import(source_root, data_dir, corrected)
    assert result.returncode == 0, result.stderr
    assert "REDATE" in result.stdout
    assert "2023-11-03 -> 2024-07-16" in result.stdout
    assert os.path.exists(os.path.join(data_dir, old_run.csv_filename))

    result = _run_import(source_root, data_dir, corrected, "--execute")
    assert result.returncode == 0, result.stderr
    assert "re-dated: 1" in result.stdout
    assert "sync_data_to_server.sh --delete" in result.stdout

    conn = db.connect(os.path.join(data_dir, "streetscape_tracker.db"))
    runs = db.get_runs_for_city(conn, city_id)
    conn.close()
    assert [r.run_date for r in runs] == ["2024-07-16"]
    new_run = runs[0]
    assert new_run.is_baseline and "_step_30_2024-07-16" in new_run.csv_filename
    # Old artifacts are gone; the new snapshot carries the corrected date
    for name in (old_run.csv_filename, old_run.json_filename):
        assert not os.path.exists(os.path.join(data_dir, name))
    df = load_city_csv_file(os.path.join(data_dir, new_run.csv_filename))
    assert df["query_timestamp"].eq("2024-07-16T00:00:00+00:00").all()

    # A further re-run is a no-op
    result = _run_import(source_root, data_dir, corrected, "--execute")
    assert result.returncode == 0, result.stderr
    assert "already registered: 1" in result.stdout
    assert "re-dated: 0" in result.stdout


def test_superseded_variant_purged_before_redate(source_root, data_dir, tmp_path):
    # The Washington D.C. repair: the main run's corrected date lands on the
    # day its smaller variant occupies, so the variant is purged first and
    # the main run then takes that date
    main = os.path.join(source_root, "DC/DC_30_coords.csv")
    variant = os.path.join(source_root, "DC 10k/DC_30_coords.csv")
    _write_v2_csv(
        main,
        [
            (44.0011, -121.0011, 44.001, -121.001, "p1", "2020-05"),
            (44.0021, -121.0011, 44.002, -121.001, "p2", "2021-06"),
        ],
        [(44.003, -121.001)],
    )
    _write_v2_csv(
        variant, [(44.0011, -121.0011, 44.001, -121.001, "p1", "2020-05")], [(44.002, -121.001)]
    )
    _write_bbox(main, 44.0, 44.09, -121.09, -121.0)
    _write_bbox(variant, 44.0, 44.01, -121.01, -121.0)
    city_id = _register_city(data_dir, "Deecee", None, "Testland")
    main_entry = {
        "rel_csv": "DC/DC_30_coords.csv",
        "query": "Deecee, Testland",
        "run_date": "2023-11-03",
        "fmt": "v2",
    }
    original = _write_manifest(
        str(tmp_path / "original.json"),
        [
            main_entry,
            {**main_entry, "rel_csv": "DC 10k/DC_30_coords.csv", "run_date": "2024-04-11"},
        ],
    )
    result = _run_import(source_root, data_dir, original, "--execute")
    assert result.returncode == 0, result.stderr

    conn = db.connect(os.path.join(data_dir, "streetscape_tracker.db"))
    runs = db.get_runs_for_city(conn, city_id)
    conn.close()
    assert [r.run_date for r in runs] == ["2023-11-03", "2024-04-11"]
    variant_run = runs[1]

    corrected = _write_manifest(
        str(tmp_path / "corrected.json"),
        {
            "datasets": [{**main_entry, "run_date": "2024-04-11"}],
            "superseded": [[variant_run.csv_filename, "superseded by the full-extent run"]],
        },
    )
    result = _run_import(source_root, data_dir, corrected, "--execute")
    assert result.returncode == 0, result.stderr
    assert "PURGE" in result.stdout
    assert "purged: 1" in result.stdout and "re-dated: 1" in result.stdout

    conn = db.connect(os.path.join(data_dir, "streetscape_tracker.db"))
    runs = db.get_runs_for_city(conn, city_id)
    conn.close()
    assert len(runs) == 1
    run = runs[0]
    assert run.run_date == "2024-04-11"
    assert run.total_points == 3  # the main dataset's extent, not the variant's
    assert not os.path.exists(os.path.join(data_dir, variant_run.csv_filename))

    # Aggregate reflects the single surviving run
    with gzip.open(os.path.join(data_dir, "cities.json.gz"), "rt") as f:
        agg = json.load(f)
    rec = next(c for c in agg["cities"] if c["city_id"] == city_id)
    assert len(rec["providers"]["gsv"]["runs"]) == 1


def test_format_mismatch_is_reported_not_fatal(source_root, data_dir, tmp_path):
    csv_path = os.path.join(source_root, "Mix/Mix_30_coords.csv")
    _write_v2_csv(
        csv_path, [(44.0011, -121.0011, 44.001, -121.001, "p1", "2020-05")], [], header=True
    )
    _register_city(data_dir, "Mix", None, "Testland")
    manifest = _write_manifest(
        str(tmp_path / "m.json"),
        [
            {
                "rel_csv": "Mix/Mix_30_coords.csv",
                "query": "Mix, Testland",
                "run_date": "2023-11-05",
                "fmt": "v1",
            },  # wrong on purpose
        ],
    )

    result = _run_import(source_root, data_dir, manifest, "--execute")
    assert result.returncode == 0, result.stderr
    assert "ERROR" in result.stdout
    assert "sniffs as 'v2'" in result.stdout
    assert "0 baseline runs registered" in result.stdout
