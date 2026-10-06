"""
The capture-history summary JSON (issue #109): the loader it reads through, the
plausibility guard it applies, its published shape, and the two writers --
the harvester script and the backfill.

Nothing here contacts the unpublished endpoint: the harvester is replaced by a
stub that writes a synthetic CSV, and the autouse ``_no_real_network`` fixture
would fail any test that tried.
"""

import argparse
import asyncio
import gzip
import importlib.util
import json
import os
import subprocess
import sys
from datetime import date

import pandas as pd

from streetscape_metadata_tracker import db
from streetscape_metadata_tracker.download_gsv_history import HISTORY_DTYPES
from streetscape_metadata_tracker.fileutils import HISTORY_STRING_COLUMNS, load_history_csv_file
from streetscape_metadata_tracker.json_summarizer import (
    CAPTURE_HISTORY_CAVEAT,
    CAPTURE_HISTORY_SCHEMA_VERSION,
    _history_json_filename,
    generate_history_summary_as_json,
)
from streetscape_metadata_tracker.naming import generate_history_filename
from tests.conftest import make_history_df, write_city_csv_gz

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_HARVEST_SCRIPT = os.path.join(_PROJECT_ROOT, "scripts", "harvest_gsv_history.py")
_BACKFILL_SCRIPT = os.path.join(_PROJECT_ROOT, "scripts", "backfill_history_json.py")

HARVEST_DATE = date(2026, 4, 10)


def strict_load(path):
    """json.load that raises on NaN/Infinity literals."""

    def _reject(token):
        raise ValueError(f"invalid token {token}")

    with gzip.open(path, "rt", encoding="utf-8") as f:
        return json.load(f, parse_constant=_reject)


def _history_path(data_dir, city_id="bend--or", harvest_date=HARVEST_DATE):
    name = generate_history_filename(city_id, 100, 100, 20, harvest_date) + ".csv.gz"
    return os.path.join(data_dir, name)


def _write_history(data_dir, rows, **kw):
    path = _history_path(data_dir, **kw)
    write_city_csv_gz(make_history_df(rows), path)
    return path


def _summarize(path, harvest_date=HARVEST_DATE, **kw):
    out = generate_history_summary_as_json(
        path,
        load_history_csv_file(path),
        city_id="bend--or",
        harvest_date=harvest_date,
        force_recreate_file=True,
        **kw,
    )
    return strict_load(out)


# ── Loader ──────────────────────────────────────────────────────────────────


def test_history_loader_pins_iso8601_and_coerces(data_dir):
    # Day precision, month precision, a sentinel-shaped future date, garbage.
    path = _write_history(
        data_dir,
        [
            ("a", "2009-06-01", 44.0, -121.0),
            ("b", "2012-06", 44.0, -121.0),
            ("c", "2611-01-01", 44.0, -121.0),
            ("d", "garbage", 44.0, -121.0),
        ],
    )
    df = load_history_csv_file(path)
    assert len(df) == 4
    dates = dict(zip(df["pano_id"], df["capture_date"], strict=True))
    assert dates["a"] == pd.Timestamp("2009-06-01")
    # Month precision pins to the 1st -- the #226 failure was this row going NaT.
    assert dates["b"] == pd.Timestamp("2012-06-01")
    assert dates["c"] == pd.Timestamp("2611-01-01")
    assert pd.isna(dates["d"])


def test_history_loader_string_columns_are_a_subset_of_the_schema():
    # fileutils spells these rather than importing the downloader (aiohttp);
    # this is what keeps the two from drifting apart.
    assert set(HISTORY_STRING_COLUMNS) <= set(HISTORY_DTYPES)
    assert all(HISTORY_DTYPES[c] is str for c in HISTORY_STRING_COLUMNS)


# ── Summary ─────────────────────────────────────────────────────────────────


def test_summary_applies_the_plausibility_mask_with_the_harvest_date_as_ceiling(data_dir):
    path = _write_history(
        data_dir,
        [
            ("pre_floor", "2006-12-01", 44.0, -121.0),
            ("ok2009", "2009-06-01", 44.0, -121.0),
            ("harvest_day", "2026-04-10", 44.0, -121.0),  # == harvest date: kept (inclusive)
            ("after", "2026-05-01", 44.0, -121.0),  # after the search saw it: dropped
            ("sentinel", "2611-01-01", 44.0, -121.0),
        ],
    )
    s = _summarize(path)
    panos = s["panos"]
    assert panos["unique_panos"] == 5
    assert panos["plausibly_dated_panos"] == 2
    assert panos["implausible_dates_dropped"] == 3
    assert panos["oldest_capture_date"] == "2009-06-01"
    assert panos["newest_capture_date"] == "2026-04-10"
    assert panos["years_with_imagery"] == 2
    assert s["histogram_of_capture_dates_by_year"] == {"2009": 1, "2026": 1}
    assert s["histogram_of_capture_dates_by_month"] == {"2009-06": 1, "2026-04": 1}


def test_summary_dedups_by_pano_id(data_dir):
    # Built in the harvester's order (sorted by capture_date), so the row
    # drop_duplicates keeps is the EARLIEST date, as the harvester's own is.
    path = _write_history(
        data_dir,
        [
            ("same", "2010-03-01", 44.0, -121.0),
            ("same", "2015-03-01", 44.0, -121.0),
        ],
    )
    s = _summarize(path)
    assert s["panos"]["unique_panos"] == 1
    assert s["panos"]["plausibly_dated_panos"] == 1
    assert s["panos"]["oldest_capture_date"] == "2010-03-01"
    assert s["panos"]["newest_capture_date"] == "2010-03-01"
    assert s["histogram_of_capture_dates_by_year"] == {"2010": 1}
    # data_file.rows is the CSV as written, before the defensive dedup.
    assert s["data_file"]["rows"] == 2


def test_summary_shape_and_schema(data_dir):
    path = _write_history(
        data_dir,
        [
            ("b", "2018-06-01", 44.0, -121.0),
            ("a", "2009-06-01", 44.0, -121.0),
            ("c", "2012-08-01", 44.0, -121.0),
        ],
    )
    s = _summarize(
        path,
        grid_points_queried=36,
        api_requests=37,
        started_at="2026-04-10T01:00:00+00:00",
        finished_at="2026-04-10T01:03:00+00:00",
    )
    assert s["schema_version"] == CAPTURE_HISTORY_SCHEMA_VERSION == 1
    assert s["artifact"] == "capture_history"
    assert s["provider"] == "gsv"
    assert s["city_id"] == "bend--or"
    assert s["harvest"] == {
        "harvest_date": "2026-04-10",
        "grid_points_queried": 36,
        "api_requests": 37,
        "started_at": "2026-04-10T01:00:00+00:00",
        "finished_at": "2026-04-10T01:03:00+00:00",
    }
    assert s["data_file"] == {
        "filename": os.path.basename(path),
        "format": "csv.gz",
        "rows": 3,
        "size_bytes": os.path.getsize(path),
    }
    assert s["source"]["caveat"] == CAPTURE_HISTORY_CAVEAT
    assert "unpublished" in s["source"]["endpoint"]
    assert set(s["panos"]) == {
        "unique_panos",
        "plausibly_dated_panos",
        "implausible_dates_dropped",
        "oldest_capture_date",
        "newest_capture_date",
        "years_with_imagery",
    }
    years = list(s["histogram_of_capture_dates_by_year"])
    assert years == sorted(years) == ["2009", "2012", "2018"]
    months = list(s["histogram_of_capture_dates_by_month"])
    assert months == sorted(months)
    # The sibling of the CSV, by the one derivation.
    assert os.path.exists(_history_json_filename(path))


def test_summary_of_an_empty_harvest_is_nulls_not_zero_dates(data_dir):
    # The harvester writes a header-only CSV for a city with no history.
    path = _write_history(data_dir, [])
    s = _summarize(path)
    assert s["panos"] == {
        "unique_panos": 0,
        "plausibly_dated_panos": 0,
        "implausible_dates_dropped": 0,
        "oldest_capture_date": None,
        "newest_capture_date": None,
        "years_with_imagery": 0,
    }
    assert s["histogram_of_capture_dates_by_year"] == {}
    assert s["histogram_of_capture_dates_by_month"] == {}


def test_summary_honours_force_recreate_like_the_run_summarizer(data_dir):
    path = _write_history(data_dir, [("a", "2009-06-01", 44.0, -121.0)])
    df = load_history_csv_file(path)
    json_path = generate_history_summary_as_json(
        path, df, city_id="bend--or", harvest_date=HARVEST_DATE
    )
    before = open(json_path, "rb").read()
    os.utime(json_path, (1_000_000_000, 1_000_000_000))

    # Without force: untouched, same path back.
    other = make_history_df([("z", "2020-01-01", 44.0, -121.0)])
    assert (
        generate_history_summary_as_json(path, other, city_id="bend--or", harvest_date=HARVEST_DATE)
        == json_path
    )
    assert os.path.getmtime(json_path) == 1_000_000_000
    assert open(json_path, "rb").read() == before

    # With force: rewritten from the frame given.
    generate_history_summary_as_json(
        path, other, city_id="bend--or", harvest_date=HARVEST_DATE, force_recreate_file=True
    )
    assert strict_load(json_path)["panos"]["oldest_capture_date"] == "2020-01-01"


# ── The two writers ─────────────────────────────────────────────────────────


def _register_city(conn):
    return db.register_city(
        conn,
        city_name="Bend",
        state_name="Oregon",
        state_code="OR",
        country_name="United States",
        country_code="US",
        center_lat=44.05,
        center_lon=-121.31,
        grid_width_m=100,
        grid_height_m=100,
        step_m=20,
    )


def _load_harvest_script():
    spec = importlib.util.spec_from_file_location("harvest_gsv_history_script", _HARVEST_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_harvest_script_writes_the_summary_after_cataloging(conn, data_dir, monkeypatch):
    city_id = _register_city(conn)
    module = _load_harvest_script()
    calls = []

    async def fake_harvest(**kwargs):
        calls.append(kwargs)
        out = kwargs["output_csv_gz_path"]
        df = make_history_df(
            [("h1", "2009-06-01", 44.0, -121.0), ("h2", "2018-06-01", 44.0, -121.0)]
        )
        write_city_csv_gz(df, out)
        return {
            "df": df,
            "filename_with_path": out,
            "api_requests": 9,
            "grid_points": 9,
            "unique_panos": 2,
            "oldest_capture_date": "2009-06-01",
            "newest_capture_date": "2018-06-01",
            "started_at": "2026-04-10T01:00:00+00:00",
            "finished_at": "2026-04-10T01:03:00+00:00",
        }

    monkeypatch.setattr(module, "harvest_gsv_history_async", fake_harvest)
    args = argparse.Namespace(
        city=city_id,
        data_dir=data_dir,
        db_path=os.path.join(data_dir, "streetscape_tracker.db"),
        harvest_date=HARVEST_DATE.isoformat(),
        force=False,
        connection_limit=2,
        verbose=False,
    )
    assert asyncio.run(module._run(args)) == 0
    assert len(calls) == 1, "the stub, never the real harvester, is what ran"

    row = db.get_latest_history_harvest(conn, city_id)
    assert row is not None
    json_path = os.path.join(data_dir, _history_json_filename(row["csv_filename"]))
    summary = strict_load(json_path)
    assert summary["panos"]["unique_panos"] == row["unique_panos"] == 2
    assert summary["harvest"]["grid_points_queried"] == 9
    assert summary["harvest"]["finished_at"] == "2026-04-10T01:03:00+00:00"


def _run_backfill(data_dir, *extra):
    return subprocess.run(
        [sys.executable, _BACKFILL_SCRIPT, "--data-dir", data_dir, *extra],
        cwd=_PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_backfill_dry_run_writes_nothing_and_execute_writes_once(conn, data_dir):
    city_id = _register_city(conn)
    present = _write_history(
        data_dir, [("a", "2009-06-01", 44.0, -121.0)], city_id=city_id, harvest_date=HARVEST_DATE
    )
    absent_name = os.path.basename(
        _history_path(data_dir, city_id=city_id, harvest_date=date(2026, 5, 1))
    )
    for harvest_date, name in [
        (HARVEST_DATE, os.path.basename(present)),
        (date(2026, 5, 1), absent_name),
    ]:
        db.register_history_harvest(
            conn,
            city_id=city_id,
            harvest_date=harvest_date,
            csv_filename=name,
            grid_points_queried=4,
            unique_panos=1,
            api_requests=4,
        )
    present_json = _history_json_filename(present)

    dry = _run_backfill(data_dir)
    assert dry.returncode == 1, dry.stdout + dry.stderr  # a CSV is missing
    assert not os.path.exists(present_json), "a dry run must write nothing"
    assert f"would write {os.path.basename(present_json)}" in dry.stdout
    assert f"MISSING CSV {absent_name}" in dry.stdout
    assert "2 rows: 1 would write, 0 up to date, 1 missing CSV" in dry.stdout

    run = _run_backfill(data_dir, "--execute")
    assert run.returncode == 1, run.stdout + run.stderr
    summary = strict_load(present_json)
    assert summary["panos"]["unique_panos"] == 1
    assert summary["harvest"]["grid_points_queried"] == 4
    assert "2 rows: 1 written, 0 up to date, 1 missing CSV" in run.stdout

    os.utime(present_json, (1_000_000_000, 1_000_000_000))
    again = _run_backfill(data_dir, "--execute")
    assert "1 up to date" in again.stdout
    assert os.path.getmtime(present_json) == 1_000_000_000

    forced = _run_backfill(data_dir, "--execute", "--force")
    assert "1 written" in forced.stdout
    assert os.path.getmtime(present_json) != 1_000_000_000


def test_backfill_exits_zero_when_every_csv_is_present(conn, data_dir):
    city_id = _register_city(conn)
    path = _write_history(data_dir, [("a", "2009-06-01", 44.0, -121.0)], city_id=city_id)
    db.register_history_harvest(
        conn, city_id=city_id, harvest_date=HARVEST_DATE, csv_filename=os.path.basename(path)
    )
    result = _run_backfill(data_dir, "--execute", "--city", city_id)
    assert result.returncode == 0, result.stdout + result.stderr
    assert os.path.exists(_history_json_filename(path))


def test_backfill_refuses_a_missing_data_dir_and_creates_nothing(tmp_path):
    """A mistyped --data-dir must not read as "0 rows, nothing to do" (#436 review)."""
    missing = tmp_path / "typo" / "data"
    result = _run_backfill(str(missing))
    assert result.returncode == 2, result.stdout + result.stderr
    assert "Refusing" in result.stderr
    assert "0 rows" not in result.stdout
    assert not (tmp_path / "typo").exists(), "a refused dry run must create nothing"


def test_backfill_refuses_a_data_dir_with_no_catalog_and_creates_none(data_dir):
    """An existing directory with no catalog (the code checkout's ./data) is refused too."""
    result = _run_backfill(data_dir)
    assert result.returncode == 2, result.stdout + result.stderr
    assert "no catalog" in result.stderr
    assert "0 rows" not in result.stdout
    assert os.listdir(data_dir) == [], "no empty catalog may be left behind"


def test_backfill_reports_zero_rows_for_a_real_catalog_with_no_harvests(conn, data_dir):
    """The refusal is about a MISSING catalog; an empty-of-harvests one still answers 0."""
    result = _run_backfill(data_dir)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "0 rows: 0 would write, 0 up to date, 0 missing CSV" in result.stdout
