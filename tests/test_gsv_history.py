"""
Tests for the historical-dates harvester (issue #2). Pure logic + an
end-to-end sweep served entirely from memory (the unpublished endpoint is
mocked), so there is no network access.
"""

import asyncio
import json
import os
import re
import sys
from datetime import date

import aiohttp
import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from streetscape_metadata_tracker import db  # noqa: E402
from streetscape_metadata_tracker import download_gsv_history as dgh  # noqa: E402
from streetscape_metadata_tracker.naming import (  # noqa: E402
    ParsedHistoryFilename,
    generate_history_filename,
    parse_filename,
    parse_history_filename,
)

# ── Response-fixture builder ───────────────────────────────────────────────
#
# Reproduces the (undocumented) endpoint's deeply-nested shape: a callbackfunc
# wrapper around data where data[1][5][0] is the "subset", subset[3][0] is the
# pano list and subset[8] is the date list. Per the endpoint's quirk, the date
# list covers only the LAST n panos, so undated (user) panos must lead and
# dated (official Google) panos must trail.


def _pano_node(pano_id, lat, lon):
    return [[None, pano_id], None, [[None, None, lat, lon]]]


def _date_node(year, month):
    return [None, [year, month]]


def make_response(undated, dated):
    """
    undated: list of (pano_id, lat, lon) with no capture date (user panos)
    dated:   list of (pano_id, lat, lon, (year, month)) official Google panos
    Returns the raw callbackfunc(...) response body as a string.
    """
    raw_panos = [_pano_node(p, la, lo) for (p, la, lo) in undated]
    raw_panos += [_pano_node(p, la, lo) for (p, la, lo, _) in dated]
    raw_dates = [_date_node(y, m) for (_, _, _, (y, m)) in dated]
    subset = [None, None, None, [raw_panos], None, None, None, None, raw_dates]
    data = [None, [None, None, None, None, None, [subset]]]
    return f"callbackfunc( {json.dumps(data)} )"


# ── Parsing ────────────────────────────────────────────────────────────────


def test_parse_keeps_only_dated_official_panos():
    text = make_response(
        undated=[("userA", 45.0, -122.0), ("userB", 45.0, -122.0)],
        dated=[
            ("g2007", 45.31, -122.94, (2007, 10)),
            ("g2012", 45.31, -122.94, (2012, 6)),
            ("g2023", 45.31, -122.94, (2023, 6)),
        ],
    )
    panos = dgh.parse_search_response(text)

    assert {p.pano_id for p in panos} == {"g2007", "g2012", "g2023"}
    by_id = {p.pano_id: p for p in panos}
    # month-precision dates standardized to ISO with day defaulting to 01
    assert by_id["g2007"].capture_date == "2007-10-01"
    assert by_id["g2012"].capture_date == "2012-06-01"
    assert by_id["g2023"].capture_date == "2023-06-01"
    assert by_id["g2007"].lat == pytest.approx(45.31)


def test_parse_no_images_sentinel_returns_empty():
    text = f"callbackfunc( {json.dumps(dgh._NO_IMAGES_SENTINEL)} )"
    assert dgh.parse_search_response(text) == []


@pytest.mark.parametrize(
    "text",
    [
        "",
        "not javascript at all",
        "callbackfunc( {not json} )",
        "callbackfunc( [1,2,3] )",  # right wrapper, wrong shape
    ],
)
def test_parse_malformed_is_swallowed(text):
    # A shape change in the undocumented endpoint must not raise mid-sweep.
    assert dgh.parse_search_response(text) == []


# ── Circuit breaker ────────────────────────────────────────────────────────


def test_circuit_breaker_trips_on_consecutive_failures():
    cb = dgh._CircuitBreaker(limit=3)
    cb.record(ok=False)
    cb.record(ok=False)
    assert not cb.tripped
    cb.record(ok=False)
    assert cb.tripped


def test_circuit_breaker_resets_on_success():
    cb = dgh._CircuitBreaker(limit=3)
    cb.record(ok=False)
    cb.record(ok=False)
    cb.record(ok=True)  # an empty-but-valid result resets the streak
    cb.record(ok=False)
    assert not cb.tripped
    assert cb.total_failures == 3


# ── Filename contract ──────────────────────────────────────────────────────


def test_history_filename_round_trip():
    base = generate_history_filename(
        "bend--oregon--united-states", 5000, 5000, 20, date(2026, 7, 8)
    )
    assert base == (
        "bend--oregon--united-states_width_5000_height_5000_step_20_gsv_history_2026-07-08"
    )
    parsed = parse_history_filename(base + ".csv.gz")
    assert isinstance(parsed, ParsedHistoryFilename)
    assert (parsed.width_meters, parsed.step_meters) == (5000, 20)
    assert parsed.harvest_date == date(2026, 7, 8)
    assert parsed.slug == "bend--oregon--united-states"


def test_history_and_run_filenames_do_not_cross_parse():
    history = "bend--or_width_5000_height_5000_step_20_gsv_history_2026-07-08.csv.gz"
    run = "bend--or_width_5000_height_5000_step_20_2026-07-08.csv.gz"
    # A run parser must reject a history file...
    with pytest.raises(ValueError):
        parse_filename(history)
    # ...and the history parser must reject a normal run file.
    with pytest.raises(ValueError):
        parse_history_filename(run)


def test_parse_history_filename_reports_gsv_as_its_provider():
    # Issue #109: the history parser answers "which provider?" like the run and
    # walk parsers do, so the e2e fixture's token sweep can place a history
    # artifact. The marker is gsv-specific, so the answer is a constant.
    parsed = parse_history_filename(
        "bend--or_width_5000_height_5000_step_20_gsv_history_2026-07-08.json.gz"
    )
    assert parsed.provider == "gsv"


# ── Catalog table ──────────────────────────────────────────────────────────


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
        grid_width_m=40,
        grid_height_m=40,
        step_m=20,
    )


def test_register_and_get_history_harvest(tmp_path):
    conn = db.connect(str(tmp_path / "cat.db"))
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    city_id = _register_city(conn)

    hid = db.register_history_harvest(
        conn,
        city_id=city_id,
        harvest_date=date(2026, 7, 8),
        csv_filename="bend_gsv_history_2026-07-08.csv.gz",
        grid_points_queried=9,
        unique_panos=3,
        oldest_capture_date="2007-10-01",
        newest_capture_date="2023-06-01",
        api_requests=9,
    )
    assert hid > 0

    latest = db.get_latest_history_harvest(conn, city_id)
    assert latest["unique_panos"] == 3
    assert latest["oldest_capture_date"] == "2007-10-01"


def test_get_latest_history_harvests_all_picks_the_newest_per_city_and_provider(tmp_path):
    conn = db.connect(str(tmp_path / "cat.db"))
    bend = _register_city(conn)
    other = db.register_city(
        conn,
        city_name="Salem",
        state_name="Oregon",
        state_code="OR",
        country_name="United States",
        country_code="US",
        center_lat=44.94,
        center_lon=-123.03,
        grid_width_m=40,
        grid_height_m=40,
        step_m=20,
    )
    # Registered newest-first, so "first row" and "newest row" differ.
    for city_id, d in [
        (bend, date(2026, 9, 1)),
        (bend, date(2026, 7, 8)),
        (other, date(2026, 8, 1)),
    ]:
        db.register_history_harvest(
            conn,
            city_id=city_id,
            harvest_date=d,
            csv_filename=f"{city_id}_gsv_history_{d}.csv.gz",
        )

    latest = db.get_latest_history_harvests_all(conn)
    assert set(latest) == {(bend, "gsv"), (other, "gsv")}
    assert latest[(bend, "gsv")]["harvest_date"] == "2026-09-01"
    assert latest[(other, "gsv")]["harvest_date"] == "2026-08-01"
    # Agrees with the per-city lookup it replaces in the aggregate.
    per_city = db.get_latest_history_harvest(conn, bend)
    assert latest[(bend, "gsv")]["harvest_id"] == per_city["harvest_id"]
    # The backfill's lister returns every row, oldest first.
    assert [r["harvest_date"] for r in db.get_history_harvests(conn, bend)] == [
        "2026-07-08",
        "2026-09-01",
    ]
    assert len(db.get_history_harvests(conn)) == 3


def test_register_history_harvest_is_idempotent_per_date(tmp_path):
    conn = db.connect(str(tmp_path / "cat.db"))
    city_id = _register_city(conn)

    first = db.register_history_harvest(
        conn,
        city_id=city_id,
        harvest_date=date(2026, 7, 8),
        csv_filename="a.csv.gz",
        unique_panos=3,
    )
    second = db.register_history_harvest(  # same city + date -> replace
        conn,
        city_id=city_id,
        harvest_date=date(2026, 7, 8),
        csv_filename="a.csv.gz",
        unique_panos=5,
    )
    assert first == second
    assert db.get_latest_history_harvest(conn, city_id)["unique_panos"] == 5
    assert conn.execute("SELECT COUNT(*) FROM history_harvests").fetchone()[0] == 1


def test_v2_catalog_gains_history_table_on_connect(tmp_path):
    # The real upgrade path most installs hit: an existing v2 catalog (with
    # data) must gain the history_harvests table and stamp v3 on reconnect,
    # without losing anything. Simulated by building a full DB, then reverting
    # it to look like v2 (drop the new table, stamp user_version=2).
    import sqlite3

    db_path = str(tmp_path / "v2.db")
    conn = db.connect(db_path)
    city_id = _register_city(conn)
    conn.close()

    raw = sqlite3.connect(db_path)
    raw.execute("DROP TABLE history_harvests")
    raw.execute("PRAGMA user_version = 2")
    raw.commit()
    raw.close()

    conn2 = db.connect(db_path)  # additive v2 -> v3 upgrade
    assert conn2.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    assert db.resolve_city(conn2, city_id) is not None  # data preserved
    hid = db.register_history_harvest(  # new table usable
        conn2,
        city_id=city_id,
        harvest_date=date(2026, 7, 8),
        csv_filename="x.csv.gz",
        unique_panos=2,
    )
    assert hid > 0


# ── End-to-end sweep (endpoint mocked) ─────────────────────────────────────


def _patch_fetch(monkeypatch, responder):
    """Replace the network fetch with an in-memory responder(url) -> text."""

    async def fake_fetch(session, url, timeout):
        return responder(url)

    monkeypatch.setattr(dgh, "_fetch_search", fake_fetch)


def test_harvest_end_to_end_dedups_across_grid(monkeypatch, tmp_path):
    # Every grid point's search surfaces the same 3 official panos, so the
    # census must dedup them to 3 unique rows regardless of grid size.
    response = make_response(
        undated=[("user1", 44.05, -121.31)],
        dated=[
            ("g2007", 44.05, -121.31, (2007, 10)),
            ("g2012", 44.05, -121.31, (2012, 6)),
            ("g2023", 44.05, -121.31, (2023, 6)),
        ],
    )
    _patch_fetch(monkeypatch, lambda url: response)

    out = str(tmp_path / "bend_gsv_history_2026-07-08.csv.gz")
    result = asyncio.run(
        dgh.harvest_gsv_history_async(
            city_name="Bend",
            center_lat=44.05,
            center_lon=-121.31,
            grid_width=40,
            grid_height=40,
            step_length=20,
            output_csv_gz_path=out,
            jitter_seconds=(0.0, 0.0),
        )
    )

    assert result["grid_points"] == 9  # (2+1)*(2+1)
    assert result["api_requests"] == 9  # one search per point
    assert result["unique_panos"] == 3
    assert result["oldest_capture_date"] == "2007-10-01"
    assert result["newest_capture_date"] == "2023-06-01"

    df = pd.read_csv(out)
    assert list(df.columns) == list(dgh.HISTORY_DTYPES.keys())
    assert len(df) == 3
    assert set(df["pano_id"]) == {"g2007", "g2012", "g2023"}
    # checkpoint cleaned up on success
    assert not os.path.exists(out + ".harvesting")


def test_harvest_writes_empty_file_when_no_history(monkeypatch, tmp_path):
    empty = f"callbackfunc( {json.dumps(dgh._NO_IMAGES_SENTINEL)} )"
    _patch_fetch(monkeypatch, lambda url: empty)

    out = str(tmp_path / "nowhere_gsv_history_2026-07-08.csv.gz")
    result = asyncio.run(
        dgh.harvest_gsv_history_async(
            city_name="Nowhere",
            center_lat=0.0,
            center_lon=0.0,
            grid_width=40,
            grid_height=40,
            step_length=20,
            output_csv_gz_path=out,
            jitter_seconds=(0.0, 0.0),
        )
    )

    assert result["unique_panos"] == 0
    assert result["oldest_capture_date"] is None
    df = pd.read_csv(out) if os.path.getsize(out) else pd.DataFrame()
    assert len(df) == 0


def test_harvest_unions_distinct_panos_across_grid(monkeypatch, tmp_path):
    # Each grid point surfaces a DIFFERENT pano (keyed off its coordinates), so
    # the census is the union of all points, not just one point's result.
    import re as _re

    def responder(url):
        lat, lon = _re.search(r"!3d([-\d.]+)!4d([-\d.]+)", url).groups()
        pid = f"g_{lat}_{lon}"
        return make_response(undated=[], dated=[(pid, float(lat), float(lon), (2018, 5))])

    _patch_fetch(monkeypatch, responder)

    out = str(tmp_path / "bend_gsv_history_2026-07-08.csv.gz")
    result = asyncio.run(
        dgh.harvest_gsv_history_async(
            city_name="Bend",
            center_lat=44.05,
            center_lon=-121.31,
            grid_width=40,
            grid_height=40,
            step_length=20,
            output_csv_gz_path=out,
            jitter_seconds=(0.0, 0.0),
        )
    )

    assert result["grid_points"] == 9
    assert result["unique_panos"] == 9  # every point contributed one
    assert len(set(pd.read_csv(out)["pano_id"])) == 9


def test_harvest_resume_skips_done_points_and_keeps_earliest_date(monkeypatch, tmp_path):
    # Pre-seed a checkpoint as if 8 of 9 points were already harvested (with one
    # pano gOLD@2010), leaving a single point (1, 1) to do. The resume must
    # query ONLY that point and must keep the earlier checkpoint date for gOLD
    # even though the live point returns gOLD with a later date.
    out = str(tmp_path / "bend_gsv_history_2026-07-08.csv.gz")
    done = [[i, j] for i in (-1, 0, 1) for j in (-1, 0, 1) if (i, j) != (1, 1)]
    seed = {
        "done": done,
        "panos": {
            "gOLD": {
                "capture_date": "2010-01-01",
                "pano_lat": 44.0,
                "pano_lon": -121.0,
                "nearest_query_lat": 44.0,
                "nearest_query_lon": -121.0,
            }
        },
        "api_requests": 8,
    }
    with open(out + ".harvesting", "w", encoding="utf-8") as f:
        json.dump(seed, f)

    calls = {"n": 0}

    def responder(url):
        calls["n"] += 1
        return make_response(
            undated=[],
            dated=[
                ("gOLD", 44.05, -121.31, (2020, 1)),  # later date -> must NOT win
                ("gNEW", 44.05, -121.31, (2018, 5)),
            ],
        )

    _patch_fetch(monkeypatch, responder)

    result = asyncio.run(
        dgh.harvest_gsv_history_async(
            city_name="Bend",
            center_lat=44.05,
            center_lon=-121.31,
            grid_width=40,
            grid_height=40,
            step_length=20,
            output_csv_gz_path=out,
            jitter_seconds=(0.0, 0.0),
        )
    )

    assert calls["n"] == 1  # only the one remaining point
    assert result["api_requests"] == 9  # 8 carried over + 1 new
    df = pd.read_csv(out).set_index("pano_id")
    assert set(df.index) == {"gOLD", "gNEW"}
    assert df.loc["gOLD", "capture_date"] == "2010-01-01"  # earliest kept
    assert df.loc["gNEW", "capture_date"] == "2018-05-01"
    assert not os.path.exists(out + ".harvesting")


def test_parse_skips_malformed_date_node_keeps_good_sibling():
    # A single corrupt date entry must not sink the whole response, nor its
    # well-formed siblings — one odd row can't abort a sweep.
    good = _pano_node("gGOOD", 45.0, -122.0)
    bad = _pano_node("gBAD", 45.0, -122.0)
    raw_dates = [_date_node(2015, 6), [None, ["oops"]]]  # 2nd node malformed
    subset = [None, None, None, [[good, bad]], None, None, None, None, raw_dates]
    data = [None, [None, None, None, None, None, [subset]]]
    panos = dgh.parse_search_response(f"callbackfunc( {json.dumps(data)} )")
    assert [p.pano_id for p in panos] == ["gGOOD"]
    assert panos[0].capture_date == "2015-06-01"


def test_harvest_blocks_and_leaves_checkpoint_then_resumes(monkeypatch, tmp_path):
    out = str(tmp_path / "bend_gsv_history_2026-07-08.csv.gz")

    # First attempt: every search throttled -> circuit breaker trips.
    async def always_throttled(session, url, timeout):
        raise aiohttp.ClientError("simulated 429")

    monkeypatch.setattr(dgh, "_fetch_search", always_throttled)

    with pytest.raises(dgh.HarvestBlockedError):
        asyncio.run(
            dgh.harvest_gsv_history_async(
                city_name="Bend",
                center_lat=44.05,
                center_lon=-121.31,
                grid_width=40,
                grid_height=40,
                step_length=20,
                output_csv_gz_path=out,
                jitter_seconds=(0.0, 0.0),
                circuit_breaker_limit=3,
            )
        )
    assert os.path.exists(out + ".harvesting")  # progress preserved
    assert not os.path.exists(out)  # no output yet

    # Resume: endpoint recovers -> sweep completes, checkpoint removed.
    response = make_response(undated=[], dated=[("g2020", 44.05, -121.31, (2020, 5))])
    _patch_fetch(monkeypatch, lambda url: response)
    result = asyncio.run(
        dgh.harvest_gsv_history_async(
            city_name="Bend",
            center_lat=44.05,
            center_lon=-121.31,
            grid_width=40,
            grid_height=40,
            step_length=20,
            output_csv_gz_path=out,
            jitter_seconds=(0.0, 0.0),
        )
    )

    assert result["unique_panos"] == 1
    assert os.path.exists(out)
    assert not os.path.exists(out + ".harvesting")


# ── Isolated-failure retry / refuse-to-finalize (audit 2026-07-11) ─────────


def _point_from_url(url):
    """Extract the (lat, lon) a search URL queries."""
    m = re.search(r"!3d([-\d.]+)!4d([-\d.]+)!", url)
    return float(m.group(1)), float(m.group(2))


def test_isolated_failure_is_retried_within_the_run(monkeypatch, tmp_path):
    """One transient failure (far below the breaker limit) must be re-queried
    by the in-run retry pass, not silently finalized as a hole."""
    response = make_response(undated=[], dated=[("g1", 44.05, -121.31, (2020, 1))])
    state = {"calls": 0, "failed_once": False}

    def responder(url):
        state["calls"] += 1
        if not state["failed_once"]:
            state["failed_once"] = True
            raise aiohttp.ClientError("transient blip")
        return response

    _patch_fetch(monkeypatch, responder)
    out = str(tmp_path / "bend_gsv_history_2026-07-08.csv.gz")
    result = asyncio.run(
        dgh.harvest_gsv_history_async(
            city_name="Bend",
            center_lat=44.05,
            center_lon=-121.31,
            grid_width=40,
            grid_height=40,
            step_length=20,
            output_csv_gz_path=out,
            jitter_seconds=(0.0, 0.0),
        )
    )
    assert result["unique_panos"] == 1
    assert state["calls"] == 10  # 9 points + 1 retry of the failed one
    assert os.path.exists(out)
    assert not os.path.exists(out + ".harvesting")  # complete → checkpoint gone


def test_refuses_to_finalize_harvest_with_holes(monkeypatch, tmp_path):
    """A point that fails every retry pass (without tripping the breaker)
    must abort finalization: no final csv.gz, checkpoint kept for resume —
    previously the sweep 'completed', published the holey harvest, and
    deleted the checkpoint, permanently losing the failed points."""
    response = make_response(undated=[], dated=[("g1", 44.05, -121.31, (2020, 1))])

    def responder(url):
        lat, lon = _point_from_url(url)
        if abs(lat - 44.05) < 1e-9 and abs(lon - (-121.31)) < 1e-9:
            raise aiohttp.ClientError("this one point always fails")
        return response

    _patch_fetch(monkeypatch, responder)
    out = str(tmp_path / "bend_gsv_history_2026-07-08.csv.gz")
    with pytest.raises(dgh.HarvestIncompleteError, match="1 grid points"):
        asyncio.run(
            dgh.harvest_gsv_history_async(
                city_name="Bend",
                center_lat=44.05,
                center_lon=-121.31,
                grid_width=40,
                grid_height=40,
                step_length=20,
                output_csv_gz_path=out,
                jitter_seconds=(0.0, 0.0),
            )
        )
    assert not os.path.exists(out), "must not publish a harvest with holes"
    assert os.path.exists(out + ".harvesting"), "checkpoint kept for resume"
    # HarvestIncompleteError extends HarvestBlockedError so existing
    # operator handling (resume messaging) applies unchanged.
    assert issubclass(dgh.HarvestIncompleteError, dgh.HarvestBlockedError)
