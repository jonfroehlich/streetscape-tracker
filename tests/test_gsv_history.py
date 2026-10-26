"""
Tests for the historical-dates harvester (issues #2, #442). Pure logic + a
work-list crawl served entirely from memory (the unpublished endpoint is
mocked), so there is no network access.
"""

import asyncio
import gzip
import json
import os
import sys
from datetime import UTC, date, datetime, timedelta

import aiohttp
import pandas as pd
import pytest
import yarl
from multidict import CIMultiDict, CIMultiDictProxy

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
# Reproduces the live RPC response shape (measured 2026-10-17, offsets after
# sk-zk/streetlevel): data[0][0] is the response code; msg = data[1];
# msg[1][1] the current pano's id, msg[6][7] its [year, month],
# msg[5][0][1][0][2:4] its position; msg[5][0][3][0] every related pano node
# [[type, id], _, [[_, _, lat, lon]]] (the current pano first), and
# msg[5][0][8] the history as [index, [year, month], ...] keyed into that list.


def _node(pano_id, lat, lon, kind=2, pose=None):
    """pose: (elevation, heading, pitch_served, roll) as the service encodes them."""
    geo = [[None, None, lat, lon]]
    if pose is not None:
        elev, heading, pitch_served, roll = pose
        geo += [[elev, None, 28.1], [heading, pitch_served, roll]]
    return [[kind, pano_id], None, geo]


def make_response(main, historical=(), neighbors=(), code=0, pose=None, extras=None):
    """
    main:       (pano_id, lat, lon, (year, month[, day])) -- the pano served at the location now
    historical: (pano_id, lat, lon, (year, month[, day])[, type[, pose]]) -- dated panos keyed by index
    neighbors:  (pano_id, lat, lon) -- undated nodes (links), never history
    pose:       the current pano's (elevation, heading, pitch_served, roll)
    extras:     dict with any of country_code, street_name, upload_date, exact_timestamp
    Returns the raw JSON response body as a string.
    """
    if code != 0:
        return json.dumps([[code, "generic", "Search returned no images."]])
    extras = extras or {}
    mid, mlat, mlon, mdate = main
    nodes = [_node(mid, mlat, mlon, pose=pose)]
    dates = []
    for h in historical:
        pid, la, lo, hdate = h[:4]
        kind = h[4] if len(h) > 4 else 2
        hpose = h[5] if len(h) > 5 else None
        dates.append([len(nodes), list(hdate), None, None, None, kind])
        nodes.append(_node(pid, la, lo, kind, pose=hpose))
    for pid, la, lo in neighbors:
        nodes.append(_node(pid, la, lo))
    # The current pano's own geo node: position, [elevation], [pose], _, country.
    geo = [[None, None, mlat, mlon], None, None, None, extras.get("country_code")]
    if pose is not None:
        geo[1] = [pose[0], None, 28.1]
        geo[2] = [pose[1], pose[2], pose[3]]
    subset = [None, geo, None, [nodes], None, None, None, None, dates]
    if extras.get("street_name"):
        subset += [None, None, None, [[[[None, None, [extras["street_name"], "en"]]], [12.0]]]]
    msg = [
        None,
        [None, mid],
        None,
        None,
        [[[["© 2026 Google"]]]],
        [subset],
        [None] * 7 + [list(mdate), extras.get("upload_date")],
    ]
    if extras.get("exact_timestamp"):
        msg += [None] * 5 + [[extras["exact_timestamp"]]]
    return json.dumps([[0], msg])


# ── Parsing ────────────────────────────────────────────────────────────────

TP_ID = "CIHM0ogKEICAgIDO44iHaw"  # a user photosphere id, as the live endpoint returns them


def test_parse_returns_the_current_pano_and_its_history_with_third_party_flagged():
    text = make_response(
        main=("g2023", 45.31, -122.94, (2023, 6)),
        historical=[
            ("g2007", 45.31, -122.94, (2007, 10)),
            ("g2012", 45.31, -122.94, (2012, 6)),
            (TP_ID, 45.31, -122.94, (2019, 11), 10),
        ],
        neighbors=[("userA", 45.0, -122.0), ("userB", 45.0, -122.0)],
    )
    result = dgh.parse_search(text)
    assert result.response_code == 0 and result.panos_total == 6
    panos = result.panos
    assert [p.pano_id for p in panos] == ["g2023", "g2007", "g2012", TP_ID]
    assert panos[0].is_current and not any(p.is_current for p in panos[1:])
    by_id = {p.pano_id: p for p in panos}
    # month-precision dates standardized to ISO with day defaulting to 01
    assert by_id["g2023"].capture_date == "2023-06-01"
    assert by_id["g2007"].capture_date == "2007-10-01"
    assert by_id["g2012"].capture_date == "2012-06-01"
    assert by_id["g2007"].lat == pytest.approx(45.31)
    # A date no longer marks official imagery: the user photosphere is dated
    # and KEPT, flagged, never silently folded into the official history.
    assert by_id[TP_ID].third_party and not by_id["g2007"].third_party
    assert dgh.parse_search_response(text) == list(panos)


def test_parse_third_party_falls_back_to_the_id_shape_when_the_type_is_missing():
    text = make_response(
        main=("g2023", 45.0, -122.0, (2023, 6)),
        historical=[
            (TP_ID, 45.0, -122.0, (2019, 11), None),
            ("x" * 23, 45.0, -122.0, (2018, 1), None),
        ],
    )
    panos = dgh.parse_search_response(text)
    assert [p.third_party for p in panos] == [False, True, True]
    assert dgh.is_third_party_pano_id("TC9HEmnUNHFZac4hT1lccQ") is False


def test_parse_no_images_response_code_returns_empty():
    result = dgh.parse_search(make_response(None, code=5))
    assert result.response_code == 5 and result.panos == () and result.panos_total == 0
    assert result.unanswered is None  # an answer: the service looked and found nothing


# The bare RPC-form body the retired route answers with, and what an
# anti-abuse page served with a 200 looks like.
DECOMMISSIONED = (
    '[[5,"generic","GeoPhotoService.SingleImageSearch is decommissioned and turned down."]]'
)
CAPTCHA_HTML = "<html><body>Our systems have detected unusual traffic.</body></html>"


@pytest.mark.parametrize(
    "text, reason",
    [
        (DECOMMISSIONED, "response code 5: GeoPhotoService.SingleImageSearch is decommissioned"),
        ("/**/callbackfunc && callbackfunc( " + DECOMMISSIONED + " )", "non-JSON body"),
        (CAPTCHA_HTML, "HTML body"),
        ("", "empty body"),
        ("not json at all", "non-JSON body"),
        ("[1,2,3]", "non-JSON body"),  # JSON, but no response code where one belongs
        (json.dumps([[7, "generic", "something new"]]), "response code 7: something new"),
        (json.dumps([[0], None]), "response code 0 without a pano message"),
        (json.dumps([[0], [None, None]]), "response code 0 without a pano message"),
    ],
)
def test_a_body_that_is_not_an_answer_is_unanswered_never_no_imagery(text, reason):
    # The pilot's first requests (the retired route, 2026-10-17) all got HTTP
    # 200 and a code-5 body that is NOT "no images"; an anti-abuse page comes
    # as a 200 too. Each must come back with a reason, never as an empty
    # answer, and never raise mid-crawl.
    result = dgh.parse_search(text)
    assert result.panos == () and result.panos_total is None
    assert result.unanswered is not None and result.unanswered.startswith(reason)
    assert len(result.unanswered) <= dgh.UNANSWERED_REASON_CHARS
    assert dgh.parse_search_response(text) == []


def test_build_search_body_asks_for_official_imagery_history_at_the_walk_radius():
    body = json.loads(dgh.build_search_body(47.6, -122.3))
    assert body[1] == [[None, None, 47.6, -122.3], 50.0]
    assert body[2][10] == [[[dgh.PANO_TYPE_OFFICIAL, True, 2]]]
    assert 6 in body[3][0]  # neighbours + historical toggle


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


def test_parse_skips_malformed_date_node_keeps_good_sibling():
    # A single corrupt date entry must not sink the whole response, nor its
    # well-formed siblings — one odd row can't abort a crawl.
    text = make_response(
        main=("gMAIN", 45.0, -122.0, (2024, 1)),
        historical=[("gGOOD", 45.0, -122.0, (2015, 6)), ("gBAD", 45.0, -122.0, (2015, 6))],
    )
    data = json.loads(text)
    data[1][5][0][8][1][1] = ["oops"]  # the second history entry's date node
    panos = dgh.parse_search_response(json.dumps(data))
    assert [p.pano_id for p in panos] == ["gMAIN", "gGOOD"]
    assert panos[1].capture_date == "2015-06-01"


# ── Work-list crawl (endpoint mocked) ───────────────────────────────────────


class _FakeSession:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _patch_fetch(monkeypatch, responder):
    """Replace the unretried fetch seam with responder(url) -> (status, body)."""

    async def fake_fetch(session, url, timeout):
        out = responder(url)
        if asyncio.iscoroutine(out):
            out = await out
        return out

    monkeypatch.setattr(dgh, "_fetch_search", fake_fetch)
    monkeypatch.setattr(dgh.aiohttp, "ClientSession", lambda **kw: _FakeSession())


def _worklist(n=3, *, anchors=None, pass_no=1):
    """n rows of pass `pass_no`, keyed e0..e{n-1}/0, each at its own coordinate."""
    anchors = anchors or {}
    rows = []
    for i in range(n):
        rows.append(
            {
                "city_id": "bend--oregon--united-states",
                "walk_csv_filename": "bend_streetwalk.csv.gz",
                "network_type": "drive",
                "spacing_m": 15.0,
                "edge_id": f"e{i}",
                "sample_idx": 0,
                "edge_samples": 1,
                "pass": pass_no,
                "query_lat": 44.0 + i * 1e-3,
                "query_lon": -121.0,
                "anchor_status": "OK" if f"e{i}" in anchors else "ZERO_RESULTS",
                "anchor_pano_id": anchors.get(f"e{i}"),
                "anchor_capture_date": "2023-06-01" if f"e{i}" in anchors else None,
                "anchor_copyright": "© Google" if f"e{i}" in anchors else None,
            }
        )
    return pd.DataFrame(rows, columns=dgh.WORKLIST_COLUMNS)


def _lat_of(body):
    """The query latitude a request body carries (the fake fetch receives the body)."""
    return float(json.loads(body)[1][0][2])


# The current pano (dated) plus one historical capture, and an undated
# neighbour the search relates to it: 3 nodes, 2 dated.
BODY = make_response(
    main=("g2023", 44.0, -121.0, (2023, 6)),
    historical=[("g2007", 44.0, -121.0, (2007, 10))],
    neighbors=[("user1", 44.0, -121.0)],
)


def _run(wl, out_dir, **kw):
    kw.setdefault("stem", "bend")
    kw.setdefault("pass_no", 1)
    kw.setdefault("rate_per_minute", 0)  # pacing off; one test pins the pass-through
    kw.setdefault("fetch_tries", 1)
    kw.setdefault("fetch_backoff_factor", 0)
    # The latch's default home is the out-dir's PARENT (one per host); under
    # pytest that is the shared basetemp, so tests name it explicitly and one
    # test pins the default.
    kw.setdefault("latch_dir", str(out_dir))
    return asyncio.run(dgh.harvest_worklist_async(wl, str(out_dir), **kw))


def _read(path):
    return pd.read_csv(path, dtype={"edge_id": str, "anchor_pano_id": str, "pano_id": str})


def test_results_stay_keyed_to_the_sample_that_found_them(monkeypatch, tmp_path):
    # Two samples whose searches return the SAME two panos: four history rows,
    # each carrying its own key, never a dedup to two.
    _patch_fetch(monkeypatch, lambda url: (200, BODY))
    wl = _worklist(2, anchors={"e0": "g2023"})
    result = _run(wl, tmp_path)
    assert result["answered"] == 2 and result["api_requests"] == 2
    paths = result["paths"]
    hist = _read(paths["history"])
    assert list(hist.columns) == dgh.SAMPLE_HISTORY_COLUMNS
    assert sorted(zip(hist["edge_id"], hist["pano_id"], strict=True)) == [
        ("e0", "g2007"),
        ("e0", "g2023"),
        ("e1", "g2007"),
        ("e1", "g2023"),
    ]
    assert set(hist["capture_date"]) == {"2007-10-01", "2023-06-01"}
    assert hist.loc[hist["edge_id"] == "e0", "anchor_pano_id"].unique().tolist() == ["g2023"]
    q = _read(paths["queries"])
    assert list(q.columns) == dgh.QUERY_COLUMNS
    assert q["panos_total"].tolist() == [3, 3] and q["panos_dated"].tolist() == [2, 2]


def test_raw_store_holds_every_body_verbatim_and_reparses_to_the_history(monkeypatch, tmp_path):
    bodies = {}

    def responder(url):
        body = make_response(main=(f"p{_lat_of(url)}", 44.0, -121.0, (2019, 3)))
        bodies[url] = body
        return 200, body

    _patch_fetch(monkeypatch, responder)
    result = _run(_worklist(3), tmp_path)
    raw = [json.loads(line) for line in gzip.open(result["paths"]["raw"], "rt")]
    assert len(raw) == 3
    assert {r["body"] for r in raw} == set(bodies.values())
    assert all(r["http_status"] == 200 and r["request"] in bodies for r in raw)
    # Re-parsing the raw copy reproduces the parsed file: the raw store is the
    # irreplaceable one, so it must be sufficient on its own.
    reparsed = sorted(
        (r["edge_id"], p.pano_id, p.capture_date)
        for r in raw
        for p in dgh.parse_search_response(r["body"])
    )
    hist = _read(result["paths"]["history"])
    assert reparsed == sorted(
        zip(hist["edge_id"], hist["pano_id"], hist["capture_date"], strict=True)
    )


def test_anchor_in_history_is_true_false_or_null(monkeypatch, tmp_path):
    _patch_fetch(monkeypatch, lambda url: (200, BODY))
    wl = _worklist(3, anchors={"e0": "g2023", "e1": "gNOT_IN_HISTORY"})
    result = _run(wl, tmp_path)
    q = _read(result["paths"]["queries"]).set_index("edge_id")
    assert q.loc["e0", "anchor_in_history"] == True  # noqa: E712 (CSV round-trip)
    assert q.loc["e1", "anchor_in_history"] == False  # noqa: E712
    assert pd.isna(q.loc["e2", "anchor_in_history"])


def test_resume_skips_answered_keys_and_requeries_a_truncated_tail(monkeypatch, tmp_path):
    seen = []

    def responder(url):
        seen.append(_lat_of(url))
        return 200, BODY

    _patch_fetch(monkeypatch, responder)
    wl = _worklist(4)
    # First run answers e0 only (cap), in its own gzip member.
    with pytest.raises(dgh.HarvestIncompleteError):
        _run(wl, tmp_path, max_requests=1)
    paths = dgh.output_paths(str(tmp_path), "bend", 1)
    assert dgh.read_done_keys(paths["queries"]) == {("e0", 0)}
    # Second run answers e1, e2 in a second member -- then that member is cut
    # short, as a crash mid-flush leaves it.
    with pytest.raises(dgh.HarvestIncompleteError):
        _run(wl, tmp_path, max_requests=2)
    assert dgh.read_done_keys(paths["queries"]) == {("e0", 0), ("e1", 0), ("e2", 0)}
    with open(paths["queries"], "rb") as f:
        data = f.read()
    with open(paths["queries"], "wb") as f:
        f.write(data[:-12])
    # The cut member is not trusted at all, e1 included: a member is whole or
    # it is nothing, which is also what the repair before the next append keeps.
    assert dgh.read_done_keys(paths["queries"]) == {("e0", 0)}
    seen.clear()
    result = _run(wl, tmp_path)
    # e1 and e2 re-queried (their member was cut), e3 queried for the first
    # time; e0 never again.
    assert sorted(seen) == sorted(44.0 + i * 1e-3 for i in (1, 2, 3))
    assert result["answered"] == 3 and result["already_done"] == 1
    assert dgh.read_done_keys(paths["queries"]) == {("e0", 0), ("e1", 0), ("e2", 0), ("e3", 0)}
    # The damaged member was cut away before the append, so a PLAIN reader
    # sees every row too -- nothing is buried behind the damage.
    assert sorted(pd.read_csv(paths["queries"])["edge_id"]) == ["e0", "e1", "e2", "e3"]
    # Nothing recorded with holes: every done key has its history rows.
    hist = _read(paths["history"])
    assert set(hist["edge_id"]) == {"e0", "e1", "e2", "e3"}


def test_repair_gz_tail_keeps_whole_members_and_drops_a_cut_one(tmp_path):
    path = str(tmp_path / "x.gz")
    dgh._append_gz(path, "one\n")
    dgh._append_gz(path, "two\n")
    whole = os.path.getsize(path)
    assert dgh.repair_gz_tail(path) == 0 and os.path.getsize(path) == whole
    dgh._append_gz(path, "three\n")
    with open(path, "r+b") as f:
        f.truncate(os.path.getsize(path) - 5)
    assert dgh.repair_gz_tail(path) > 0
    assert os.path.getsize(path) == whole
    assert gzip.open(path, "rt").read() == "one\ntwo\n"
    assert dgh.repair_gz_tail(str(tmp_path / "absent.gz")) == 0


def test_the_ledger_is_written_last(monkeypatch, tmp_path):
    # A crash between the history append and the ledger append must leave the
    # key UNRECORDED, never recorded without its rows.
    order = []
    real = dgh._append_gz

    def spy(path, text):
        order.append(os.path.basename(path).split(".")[-3])
        real(path, text)

    monkeypatch.setattr(dgh, "_append_gz", spy)
    _patch_fetch(monkeypatch, lambda url: (200, BODY))
    _run(_worklist(1), tmp_path)
    assert order == ["raw", "history", "queries"]


def test_first_429_stops_the_run_and_writes_the_latch(monkeypatch, tmp_path):
    calls = []

    def responder(url):
        calls.append(url)
        return (429, "slow down") if len(calls) == 2 else (200, BODY)

    _patch_fetch(monkeypatch, responder)
    now = datetime(2026, 10, 20, 3, 0, tzinfo=UTC)
    with pytest.raises(dgh.HarvestBlockedError, match="HTTP 429"):
        _run(_worklist(5), tmp_path, now_func=lambda: now)
    assert len(calls) == 2, "no request after the refusal"
    latch = json.load(open(tmp_path / dgh.LATCH_FILENAME))
    assert latch["http_status"] == 429
    assert latch["cooldown_until"] == (now + timedelta(hours=24)).isoformat()
    assert dgh.latch_active(str(tmp_path), now=now) is not None
    assert dgh.latch_active(str(tmp_path), now=now + timedelta(hours=23)) is not None
    assert dgh.latch_active(str(tmp_path), now=now + timedelta(hours=25)) is None
    # The answered row before the refusal is on disk; the refusal's body is in
    # the raw store too (it is evidence).
    paths = dgh.output_paths(str(tmp_path), "bend", 1)
    assert dgh.read_done_keys(paths["queries"]) == {("e0", 0)}
    raw = [json.loads(line) for line in gzip.open(paths["raw"], "rt")]
    assert [r["http_status"] for r in raw] == [200, 429]


def test_403_is_a_refusal_too(monkeypatch, tmp_path):
    _patch_fetch(monkeypatch, lambda url: (403, "forbidden"))
    with pytest.raises(dgh.HarvestBlockedError, match="HTTP 403"):
        _run(_worklist(2), tmp_path)
    assert os.path.exists(tmp_path / dgh.LATCH_FILENAME)


def test_consecutive_transport_failures_trip_the_breaker_but_a_success_resets_it(
    monkeypatch, tmp_path
):
    def failing(n):
        count = {"i": 0}

        def responder(url):
            count["i"] += 1
            if count["i"] <= n:
                raise aiohttp.ClientConnectionError("reset")
            return 200, BODY

        return responder

    _patch_fetch(monkeypatch, failing(8))
    with pytest.raises(dgh.HarvestBlockedError, match="8 consecutive failed requests"):
        _run(_worklist(10), tmp_path, breaker_limit=8)
    latch = json.load(open(tmp_path / dgh.LATCH_FILENAME))
    assert "ClientConnectionError: reset" in latch["reason"]

    other = tmp_path / "other"
    _patch_fetch(monkeypatch, failing(7))
    result = _run(_worklist(10), other, breaker_limit=8)
    # 7 failures, then every row answers -- the 7 failed rows in the retry pass.
    assert result["answered"] == 10
    assert result["api_requests"] == 17
    assert not os.path.exists(other / dgh.LATCH_FILENAME)


def test_request_cap_stops_cleanly_without_a_latch_and_the_next_run_completes(
    monkeypatch, tmp_path
):
    _patch_fetch(monkeypatch, lambda url: (200, BODY))
    wl = _worklist(5)
    with pytest.raises(dgh.HarvestIncompleteError) as info:
        _run(wl, tmp_path, max_requests=3)
    assert info.value.capped and info.value.remaining == 2 and info.value.api_requests == 3
    assert not os.path.exists(tmp_path / dgh.LATCH_FILENAME)
    assert not isinstance(info.value, dgh.HarvestBlockedError)
    result = _run(wl, tmp_path)
    assert result["answered"] == 2 and result["already_done"] == 3 and result["api_requests"] == 2


def test_rows_still_failing_after_the_retry_pass_leave_the_pass_incomplete(monkeypatch, tmp_path):
    def responder(url):
        if _lat_of(url) == 44.0:
            raise aiohttp.ClientConnectionError("always")
        return 200, BODY

    _patch_fetch(monkeypatch, responder)
    with pytest.raises(dgh.HarvestIncompleteError, match="1 rows") as info:
        _run(_worklist(3), tmp_path, retry_passes=1)
    assert not info.value.capped and info.value.remaining == 1
    assert info.value.api_requests == 4  # 3 + one retry of the failing row
    paths = dgh.output_paths(str(tmp_path), "bend", 1)
    assert dgh.read_done_keys(paths["queries"]) == {("e1", 0), ("e2", 0)}


def test_one_request_in_flight(monkeypatch, tmp_path):
    in_flight = {"now": 0, "max": 0}

    async def responder(url):
        in_flight["now"] += 1
        in_flight["max"] = max(in_flight["max"], in_flight["now"])
        await asyncio.sleep(0.001)
        in_flight["now"] -= 1
        return 200, BODY

    _patch_fetch(monkeypatch, responder)
    _run(_worklist(6), tmp_path)
    assert in_flight["max"] == 1


def test_every_attempt_waits_for_the_pacer_retries_included(monkeypatch, tmp_path):
    acquired = {"n": 0, "args": None}

    class CountingLimiter:
        def __init__(self, max_per_minute, *a, **kw):
            acquired["args"] = (max_per_minute, kw.get("jitter"))

        async def acquire(self):
            acquired["n"] += 1

    monkeypatch.setattr(dgh, "AsyncRateLimiter", CountingLimiter)
    count = {"i": 0}

    def responder(url):
        count["i"] += 1
        if count["i"] == 1:
            raise aiohttp.ClientConnectionError("once")
        return 200, BODY

    _patch_fetch(monkeypatch, responder)
    result = _run(_worklist(2), tmp_path, rate_per_minute=60, jitter=0.6, fetch_tries=2)
    assert acquired["args"] == (60, 0.6)
    assert acquired["n"] == result["api_requests"] == 3, "the retry took its own slot"


def test_only_the_requested_pass_is_queried(monkeypatch, tmp_path):
    seen = []
    _patch_fetch(monkeypatch, lambda url: (seen.append(url), (200, BODY))[1])
    wl = pd.concat([_worklist(2, pass_no=1), _worklist(3, pass_no=2)], ignore_index=True)
    wl.loc[wl["pass"] == 2, "edge_id"] = ["f0", "f1", "f2"]
    result = _run(wl, tmp_path, pass_no=2)
    assert result["rows"] == 3 and len(seen) == 3
    assert os.path.exists(dgh.output_paths(str(tmp_path), "bend", 2)["queries"])
    assert not os.path.exists(dgh.output_paths(str(tmp_path), "bend", 1)["queries"])


def test_load_worklist_refuses_missing_columns_and_duplicate_keys(tmp_path):
    wl = _worklist(2)
    good = tmp_path / "w.worklist.csv.gz"
    wl.to_csv(good, index=False, compression="gzip")
    loaded = dgh.load_worklist(str(good))
    assert loaded["sample_idx"].dtype.kind == "i" and loaded["pass"].dtype.kind == "i"
    bad = tmp_path / "bad.csv.gz"
    wl.drop(columns=["anchor_pano_id"]).to_csv(bad, index=False, compression="gzip")
    with pytest.raises(ValueError, match="anchor_pano_id"):
        dgh.load_worklist(str(bad))
    dup = tmp_path / "dup.csv.gz"
    pd.concat([wl, wl]).to_csv(dup, index=False, compression="gzip")
    with pytest.raises(ValueError, match="duplicate"):
        dgh.load_worklist(str(dup))


def test_queries_ledger_carries_the_response_code_and_third_party_count(monkeypatch, tmp_path):
    body = make_response(
        main=("g2023", 44.0, -121.0, (2023, 6)),
        historical=[(TP_ID, 44.0, -121.0, (2019, 11), 10)],
    )
    _patch_fetch(monkeypatch, lambda url: (200, body))
    result = _run(_worklist(1), tmp_path)
    q = _read(result["paths"]["queries"])
    assert q.loc[0, "response_code"] == 0 and q.loc[0, "panos_total"] == 2
    assert q.loc[0, "panos_dated"] == 2 and q.loc[0, "panos_third_party"] == 1
    hist = _read(result["paths"]["history"]).set_index("pano_id")
    assert bool(hist.loc[TP_ID, "third_party"]) and not bool(hist.loc["g2023", "third_party"])
    assert bool(hist.loc["g2023", "is_current"]) and not bool(hist.loc[TP_ID, "is_current"])


def test_raw_lines_carry_the_request_body_beside_the_response(monkeypatch, tmp_path):
    _patch_fetch(monkeypatch, lambda url: (200, BODY))
    result = _run(_worklist(1), tmp_path)
    raw = [json.loads(line) for line in gzip.open(result["paths"]["raw"], "rt")]
    assert raw[0]["url"] == dgh.RPC_URL
    assert json.loads(raw[0]["request"])[1][0][2:4] == [44.0, -121.0]


def test_parse_keeps_everything_the_response_carries():
    text = make_response(
        main=("g2023", 45.0, -122.0, (2023, 6, 14)),
        historical=[("g2007", 45.0, -122.0, (2007, 10), 2, (51.5, 180.0, 88.0, 1.5))],
        pose=(50.4, 0.7, 88.1, 359.3),
        extras={
            "country_code": "US",
            "street_name": "NE 45th St",
            "upload_date": [2023, 7, 1],
            "exact_timestamp": "x/1718400000000",
        },
    )
    r = dgh.parse_search(text)
    assert (r.copyright, r.country_code, r.street_name) == ("© 2026 Google", "US", "NE 45th St")
    assert r.upload_date == "[2023, 7, 1]" and r.exact_timestamp == "x/1718400000000"
    cur, old = r.panos
    assert cur.capture_day == 14 and old.capture_day is None
    assert (cur.pano_type, old.pano_type) == (2, 2)
    assert (cur.elevation_m, cur.heading_deg, cur.roll_deg) == (50.4, 0.7, 359.3)
    assert cur.pitch_deg == pytest.approx(90 - 88.1)
    assert (old.elevation_m, old.heading_deg, old.pitch_deg, old.roll_deg) == (
        51.5,
        180.0,
        2.0,
        1.5,
    )
    # Nothing served is required: a bare response parses with every extra None.
    bare = dgh.parse_search(make_response(main=("g1", 45.0, -122.0, (2020, 1))))
    assert bare.country_code is None and bare.panos[0].heading_deg is None


def test_rebuild_from_raw_reproduces_the_crawl_and_keeps_the_last_answer_per_key(
    monkeypatch, tmp_path, capsys
):
    import scripts.reparse_gsv_history_raw as reparse

    calls = {"n": 0}

    def responder(url):
        calls["n"] += 1
        lat = _lat_of(url)
        return 200, make_response(
            main=(f"p{lat}", 44.0, -121.0, (2019, 3, 15)),
            historical=[(f"h{lat}", 44.0, -121.0, (2011, 7))],
            extras={"country_code": "US", "upload_date": [2019, 4, 1], "exact_timestamp": "x/1"},
        )

    _patch_fetch(monkeypatch, responder)
    wl = _worklist(3, anchors={"e0": "p44.0"})
    with pytest.raises(dgh.HarvestIncompleteError):
        _run(wl, tmp_path, max_requests=2)  # two members on disk, like a crawl's
    result = _run(wl, tmp_path)
    paths = result["paths"]
    history, queries = dgh.rebuild_from_raw(paths["raw"], wl, 1)

    def _text(path):
        return gzip.open(path, "rt", encoding="utf-8", newline="").read()

    # BYTE-identical once decompressed, through the crawl's own writer: a
    # served capture day the crawl wrote as `15` comes back as `15`, not
    # `15.0` (the earlier pandas round-trip hid that with check_dtype=False).
    dgh.write_csv_gz(history, str(tmp_path / "h.csv.gz"), dgh.SAMPLE_HISTORY_COLUMNS)
    dgh.write_csv_gz(queries, str(tmp_path / "q.csv.gz"), dgh.QUERY_COLUMNS)
    assert _text(tmp_path / "h.csv.gz") == _text(paths["history"])
    assert _text(tmp_path / "q.csv.gz") == _text(paths["queries"])
    assert ",15," in _text(paths["history"]) and "15.0" not in _text(paths["history"])
    assert (
        history["pano_id"].str.startswith("h").sum() == 3 and queries["country_code"].eq("US").all()
    )

    # A key answered twice in the raw store (a re-query after a cut ledger)
    # keeps its LAST response; a refusal line is not an answer.
    with gzip.open(paths["raw"], "at") as f:
        f.write(
            json.dumps(
                {"edge_id": "e0", "sample_idx": 0, "query_lat": 44.0, "query_lon": -121.0,
                 "url": dgh.RPC_URL, "request": "{}", "http_status": 200, "fetched_at": "later",
                 "body": make_response(main=("pNEW", 44.0, -121.0, (2025, 1)))}
            ) + "\n"
        )  # fmt: skip
        f.write(
            json.dumps(
                {"edge_id": "e1", "sample_idx": 0, "query_lat": 44.001, "query_lon": -121.0,
                 "url": dgh.RPC_URL, "request": "{}", "http_status": 429, "fetched_at": "later",
                 "body": "slow down"}
            ) + "\n"
        )  # fmt: skip
    history, queries = dgh.rebuild_from_raw(paths["raw"], wl, 1)
    assert set(history.loc[history["edge_id"] == "e0", "pano_id"]) == {"pNEW"}
    assert (
        queries.set_index("edge_id").loc["e1", "http_status"] == 200
    )  # the refusal did not replace it
    assert len(queries) == 3

    # The script writes the same two files and refuses to overwrite without --force.
    wl_path = tmp_path / "bend.worklist.csv.gz"
    wl.to_csv(wl_path, index=False, compression="gzip")
    out = tmp_path / "rebuilt"
    argv = ["--raw", paths["raw"], "--worklist", str(wl_path), "--out-dir", str(out)]
    assert reparse.main(argv) == 0
    assert sorted(os.listdir(out)) == ["bend.pass1.history.csv.gz", "bend.pass1.queries.csv.gz"]
    assert reparse.main(argv) == 1
    assert reparse.main(argv + ["--force"]) == 0
    assert (
        reparse.main(["--raw", "nope.gz", "--worklist", str(wl_path), "--out-dir", str(out)]) == 2
    )
    rebuilt_before = _text(out / "bend.pass1.history.csv.gz")
    assert "pNEW" in rebuilt_before

    # A raw store whose final member a crash cut short (the state SIGKILL
    # leaves, or a copy taken mid-write) rebuilds from the members before the
    # cut, and the script says how much it left out.
    size = os.path.getsize(paths["raw"])
    with open(paths["raw"], "r+b") as f:
        f.truncate(size - 20)
    cut = dgh.raw_store_cut_bytes(paths["raw"])
    assert cut > 0
    history, queries = dgh.rebuild_from_raw(paths["raw"], wl, 1)
    # The cut member held the two hand-appended lines: e0's later answer is
    # gone with it, and e0 reads as its first answer again.
    assert len(queries) == 3 and set(history.loc[history["edge_id"] == "e0", "pano_id"]) == {
        "p44.0",
        "h44.0",
    }
    assert reparse.main(argv + ["--force"]) == 0
    assert f"({cut} bytes of a cut final raw member left out)" in capsys.readouterr().out
    assert "pNEW" not in _text(out / "bend.pass1.history.csv.gz")


# ── A 200 that is not an answer (review of #446, S3) ───────────────────────


@pytest.mark.parametrize("body", [DECOMMISSIONED, CAPTCHA_HTML, "", "not json"])
def test_a_run_of_unanswered_200s_is_a_refusal_in_disguise(monkeypatch, tmp_path, body):
    # Ten rows, every 200 carrying a body that answers nothing: no key is
    # done, every body is in the raw store, every ledger row says why, and the
    # breaker trips at eight so the run latches -- a 200 of this shape is not
    # "no imagery here", and a run of them is a block.
    calls = []
    _patch_fetch(monkeypatch, lambda url: (calls.append(url), (200, body))[1])
    with pytest.raises(dgh.HarvestBlockedError, match="8 consecutive failed requests") as info:
        _run(_worklist(10), tmp_path, breaker_limit=8)
    assert len(calls) == 8
    paths = dgh.output_paths(str(tmp_path), "bend", 1)
    assert dgh.read_done_keys(paths["queries"]) == set()
    q = _read(paths["queries"])
    assert len(q) == 8 and q["unanswered"].notna().all() and (q["http_status"] == 200).all()
    reason = dgh.parse_search(body).unanswered
    assert q["unanswered"].unique().tolist() == [reason]
    assert reason in str(info.value)
    latch = json.load(open(tmp_path / dgh.LATCH_FILENAME))
    assert latch["http_status"] is None and reason in latch["reason"]
    raw = [json.loads(line) for line in gzip.open(paths["raw"], "rt")]
    assert [r["body"] for r in raw] == [body] * 8
    assert not os.path.exists(paths["history"])


def test_one_unanswered_200_is_requeried_and_its_key_is_done_once_answered(monkeypatch, tmp_path):
    count = {"i": 0}

    def responder(url):
        count["i"] += 1
        return (200, DECOMMISSIONED) if count["i"] == 2 else (200, BODY)

    _patch_fetch(monkeypatch, responder)
    result = _run(_worklist(3), tmp_path)
    # e1's first answer was not one; the retry pass asked again and it answered.
    assert result["answered"] == 3 and result["api_requests"] == 4
    paths = result["paths"]
    assert dgh.read_done_keys(paths["queries"]) == {("e0", 0), ("e1", 0), ("e2", 0)}
    q = _read(paths["queries"])
    assert len(q) == 4 and q["unanswered"].notna().sum() == 1
    assert q.loc[q["unanswered"].notna(), "edge_id"].tolist() == ["e1"]
    assert not os.path.exists(tmp_path / dgh.LATCH_FILENAME)
    # The rebuild reproduces that ledger: the unanswered row stays only where
    # the key has no answer, so here the answer replaces it.
    history, queries = dgh.rebuild_from_raw(paths["raw"], _worklist(3), 1)
    assert len(queries) == 3 and queries["unanswered"].isna().all()


def test_an_unanswered_key_with_no_answer_at_all_keeps_its_reason_in_a_rebuild(
    monkeypatch, tmp_path
):
    _patch_fetch(monkeypatch, lambda url: (200, DECOMMISSIONED if _lat_of(url) == 44.0 else BODY))
    with pytest.raises(dgh.HarvestIncompleteError, match="1 rows"):
        _run(_worklist(2), tmp_path)
    paths = dgh.output_paths(str(tmp_path), "bend", 1)
    history, queries = dgh.rebuild_from_raw(paths["raw"], _worklist(2), 1)
    q = queries.set_index("edge_id")
    assert (
        q.loc["e0", "unanswered"].startswith("response code 5")
        and q.loc["e1", "unanswered"] is None
    )
    assert set(history["edge_id"]) == {"e1"}


# ── The breaker counts REQUESTS, retries included (S13) ─────────────────────


@pytest.mark.parametrize("limit", [8, 6, 5])
def test_the_breaker_counts_requests_not_rows_and_sends_none_once_tripped(
    monkeypatch, tmp_path, limit
):
    # fetch_tries=4: under a per-ROW count eight rows would cost 32 requests
    # into a dead host before the latch. `limit` failed REQUESTS end it, and
    # the next is never sent -- also mid-row (6 and 5 are not multiples of 4:
    # without the check before each attempt the retry wrapper would send the
    # rest of the row's tries into the dead host).
    calls = []

    def responder(url):
        calls.append(url)
        raise aiohttp.ClientConnectionError("down")

    _patch_fetch(monkeypatch, responder)
    with pytest.raises(dgh.HarvestBlockedError, match=f"{limit} consecutive failed requests"):
        _run(_worklist(10), tmp_path, breaker_limit=limit, fetch_tries=4)
    assert len(calls) == limit
    assert os.path.exists(tmp_path / dgh.LATCH_FILENAME)


def test_a_success_mid_retry_resets_the_request_count(monkeypatch, tmp_path):
    count = {"i": 0}

    def responder(url):
        count["i"] += 1
        if count["i"] in (1, 2, 3, 5, 6, 7, 9, 10, 11):
            raise aiohttp.ClientConnectionError("flaky")
        return 200, BODY

    _patch_fetch(monkeypatch, responder)
    # Three failures then a success, three times over: never 8 in a row.
    result = _run(_worklist(3), tmp_path, breaker_limit=8, fetch_tries=4)
    assert result["answered"] == 3 and result["api_requests"] == 12
    assert not os.path.exists(tmp_path / dgh.LATCH_FILENAME)


# ── The real fetch seam (S6): a 429 sends nothing more ─────────────────────


class _FakeResponse:
    """What ``session.post`` yields: status, headers, text(), raise_for_status()."""

    def __init__(self, status, body="", headers=None):
        self.status = status
        self.headers = headers or {}
        self._body = body

    async def text(self):
        return self._body

    def raise_for_status(self):
        if self.status >= 400:
            info = aiohttp.RequestInfo(
                url=yarl.URL(dgh.RPC_URL),
                method="POST",
                headers=CIMultiDictProxy(CIMultiDict()),
                real_url=yarl.URL(dgh.RPC_URL),
            )
            raise aiohttp.ClientResponseError(
                request_info=info, history=(), status=self.status, message="error"
            )


class _InMemorySession:
    """An in-memory aiohttp.ClientSession: ``post`` is an async CM over a responder(body)."""

    def __init__(self, responder):
        self._responder = responder
        self.posts = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def post(self, url, **kwargs):
        self.posts.append((url, kwargs))
        response = self._responder(kwargs["data"])

        class _Ctx:
            async def __aenter__(self):
                return response

            async def __aexit__(self, *exc):
                return False

        return _Ctx()


def _fetch(session, body="{}"):
    return asyncio.run(dgh._fetch_search(session, body, aiohttp.ClientTimeout(total=5)))


@pytest.mark.parametrize("status", [429, 403])
def test_fetch_search_returns_a_refusal_unretried_with_its_body(monkeypatch, status):
    sleeps = []
    monkeypatch.setattr(dgh.asyncio, "sleep", lambda s: sleeps.append(s) or _noop())
    session = _InMemorySession(lambda body: _FakeResponse(status, "slow down"))
    assert _fetch(session) == (status, "slow down")
    assert len(session.posts) == 1 and sleeps == []
    assert session.posts[0][0] == dgh.RPC_URL
    assert session.posts[0][1]["headers"] == {"content-type": dgh.RPC_CONTENT_TYPE}


async def _noop():
    return None


def test_fetch_search_sleeps_a_bounded_retry_after_on_503_then_raises(monkeypatch):
    sleeps = []
    monkeypatch.setattr(dgh.asyncio, "sleep", lambda s: sleeps.append(s) or _noop())
    for header, expected in (
        ({"Retry-After": "7"}, [7.0]),
        ({"Retry-After": "900"}, [30.0]),
        ({}, []),
    ):
        sleeps.clear()
        session = _InMemorySession(lambda body, h=header: _FakeResponse(503, "busy", h))
        with pytest.raises(aiohttp.ClientResponseError) as info:
            _fetch(session)
        assert info.value.status == 503 and sleeps == expected


def test_fetch_search_raises_other_non_200s_and_returns_a_200_body(monkeypatch):
    with pytest.raises(aiohttp.ClientResponseError) as info:
        _fetch(_InMemorySession(lambda body: _FakeResponse(500, "oops")))
    assert info.value.status == 500
    assert _fetch(_InMemorySession(lambda body: _FakeResponse(200, BODY))) == (200, BODY)


def test_a_429_through_the_real_fetch_seam_sends_no_further_request(monkeypatch, tmp_path):
    # The mutation this pins: dropping the 429/403 short-circuit in
    # _fetch_search. Then raise_for_status turns the refusal into a retried
    # ClientResponseError -- fetch_tries attempts into a refusal, then a
    # "transport failure", then the next row. Here: one request, full stop.
    session = _InMemorySession(lambda body: _FakeResponse(429, "slow down"))
    monkeypatch.setattr(dgh.aiohttp, "ClientSession", lambda **kw: session)
    with pytest.raises(dgh.HarvestBlockedError, match="HTTP 429"):
        _run(_worklist(5), tmp_path, fetch_tries=4)
    assert len(session.posts) == 1
    assert json.load(open(tmp_path / dgh.LATCH_FILENAME))["http_status"] == 429


# ── Linear member scan, streaming readers (S4) and the 0-byte header (S5) ──


class _CountingFile:
    """A binary file that records every read size."""

    def __init__(self, path):
        self._f = open(path, "rb")
        self.reads = []

    def read(self, n=-1):
        self.reads.append(n)
        return self._f.read(n)

    def seek(self, *a):
        return self._f.seek(*a)

    def fileno(self):
        return self._f.fileno()

    def close(self):
        self._f.close()


def test_the_member_scan_is_linear_and_never_holds_the_file(monkeypatch, tmp_path):
    # 300 members of ~8 KB of incompressible text (the raw store's members are
    # ~40 KB): the quadratic form fed each byte ~150 times over; the scan
    # hands every byte to a decompressor once, plus at most a piece per
    # member, and reads in bounded chunks, never the whole file.
    import random

    rng = random.Random(442)
    path = str(tmp_path / "many.gz")
    for i in range(300):
        noise = "".join(chr(rng.randrange(0x20, 0x7E)) for _ in range(8000))
        dgh._append_gz(path, f"{i:04d} " + noise + "\n")
    size = os.path.getsize(path)
    assert size > 300 * 6000  # ~6.7 KB a member: printable noise compresses a little
    fed = {"bytes": 0, "objs": 0}
    real = dgh.zlib.decompressobj

    def counting(*a, **kw):
        d = real(*a, **kw)
        fed["objs"] += 1

        class _D:
            def __getattr__(self, name):
                return getattr(d, name)

            def decompress(self, data, *rest):
                fed["bytes"] += len(data)
                return d.decompress(data, *rest)

        return _D()

    monkeypatch.setattr(dgh.zlib, "decompressobj", counting)
    f = _CountingFile(path)
    assert dgh._complete_members_end(f) == size
    f.close()
    assert fed["objs"] == 301  # one per member, plus the one that hit EOF
    assert fed["bytes"] <= size + 300 * dgh._SCAN_PIECE
    assert max(f.reads) <= dgh._SCAN_CHUNK and all(n > 0 for n in f.reads)
    # Cut the last member: the scan ends at the member before it, and the
    # streaming reader yields exactly the complete members' text.
    with open(path, "r+b") as fh:
        fh.truncate(size - 100)
    good, dropped = dgh._scan_gz(path)
    assert 0 < good < size - 100 and dropped == size - 100 - good
    with dgh.open_complete_members(path) as (text, cut):
        lines = text.read().splitlines()
    assert cut == dropped and len(lines) == 299 and lines[-1].startswith("0298 ")


def test_repair_and_resume_read_through_the_streaming_scan(monkeypatch, tmp_path):
    # repair_gz_tail and read_done_keys must route through the scan (a file
    # object, never a bytes slice): a spy sees a file object on each call.
    seen = []
    real = dgh._complete_members_end

    def spy(fobj):
        seen.append(type(fobj).__name__)
        return real(fobj)

    monkeypatch.setattr(dgh, "_complete_members_end", spy)
    _patch_fetch(monkeypatch, lambda url: (200, BODY))
    result = _run(_worklist(2), tmp_path)
    assert dgh.read_done_keys(result["paths"]["queries"]) == {("e0", 0), ("e1", 0)}
    assert seen and all(name != "bytes" for name in seen)


def test_a_crash_in_the_first_flush_leaves_a_0_byte_file_that_gets_its_header_back(
    monkeypatch, tmp_path
):
    # repair_gz_tail cuts a file whose ONLY member was cut to 0 bytes; the
    # file exists, so "new = not exists" wrote every later row headerless,
    # DictReader took the first row as the header, no key was ever done, and
    # the same rows were re-queried on every resume (exit 83 forever).
    seen = []
    _patch_fetch(monkeypatch, lambda url: (seen.append(_lat_of(url)), (200, BODY))[1])
    wl = _worklist(4)
    with pytest.raises(dgh.HarvestIncompleteError):
        _run(wl, tmp_path, max_requests=2)
    paths = dgh.output_paths(str(tmp_path), "bend", 1)
    for key in ("queries", "history"):
        with open(paths[key], "r+b") as f:
            f.truncate(os.path.getsize(paths[key]) // 2)  # mid-first-member
    seen.clear()
    result = _run(wl, tmp_path)
    assert result["answered"] == 4 and sorted(seen) == [44.0 + i * 1e-3 for i in range(4)]
    assert dgh.read_done_keys(paths["queries"]) == {(f"e{i}", 0) for i in range(4)}
    q = pd.read_csv(paths["queries"])
    assert list(q.columns) == dgh.QUERY_COLUMNS and len(q) == 4
    assert list(_read(paths["history"]).columns) == dgh.SAMPLE_HISTORY_COLUMNS
    seen.clear()
    assert _run(wl, tmp_path)["answered"] == 0 and seen == []


# ── One latch per host (S9) ─────────────────────────────────────────────────


def test_the_latch_defaults_to_the_out_dirs_parent_so_two_work_lists_share_it(
    monkeypatch, tmp_path
):
    results = tmp_path / "results"
    assert dgh.default_latch_dir(str(results / "city-a")) == str(results)
    _patch_fetch(monkeypatch, lambda url: (429, "no"))
    with pytest.raises(dgh.HarvestBlockedError, match="HTTP 429"):
        asyncio.run(
            dgh.harvest_worklist_async(
                _worklist(2), str(results / "city-a"), stem="a", pass_no=1, rate_per_minute=0
            )
        )
    assert os.path.exists(results / dgh.LATCH_FILENAME)
    assert not os.path.exists(results / "city-a" / dgh.LATCH_FILENAME)
    # The next work list on the same host, under the same results dir, is
    # refused before its first request -- the refusal was the ADDRESS's.
    calls = []
    _patch_fetch(monkeypatch, lambda url: (calls.append(url), (200, BODY))[1])
    with pytest.raises(dgh.HarvestBlockedError, match="latch is in force"):
        asyncio.run(
            dgh.harvest_worklist_async(
                _worklist(2), str(results / "city-b"), stem="b", pass_no=1, rate_per_minute=0
            )
        )
    assert calls == []
    # An explicit --latch-dir elsewhere is honoured, and clearing it there works.
    other = tmp_path / "elsewhere"
    result = _run(_worklist(1), results / "city-c", latch_dir=str(other))
    assert result["answered"] == 1 and not os.path.exists(other / dgh.LATCH_FILENAME)


def test_a_hand_edited_naive_cooldown_is_read_as_utc(tmp_path):
    now = datetime(2026, 10, 20, 3, 0, tzinfo=UTC)
    dgh.write_latch(str(tmp_path), reason="x", http_status=429, cooldown_hours=24, now=now)
    record = json.load(open(tmp_path / dgh.LATCH_FILENAME))
    record["cooldown_until"] = "2026-10-21T03:00:00"  # no zone
    json.dump(record, open(tmp_path / dgh.LATCH_FILENAME, "w"))
    assert dgh.latch_active(str(tmp_path), now=now + timedelta(hours=23)) is not None
    assert dgh.latch_active(str(tmp_path), now=now + timedelta(hours=25)) is None


# ── Duplicated history rows after a crash between appends (S12) ─────────────


def test_history_rows_duplicated_by_a_crash_between_appends_read_as_one_answer(
    monkeypatch, tmp_path
):
    # The ledger is written last, so a crash between the history append and
    # the ledger append leaves the key un-done and its rows on disk; the
    # resume appends them again. The file is never rewritten -- the reader
    # keeps each key's LAST query only, and drops rows no ledger row stands
    # for -- and equals the rebuild from raw.
    real = dgh._append_gz
    state = {"crash": True}

    def crashing(path, text):
        if state["crash"] and path.endswith(".queries.csv.gz"):
            state["crash"] = False
            raise OSError("ENOSPC")
        real(path, text)

    monkeypatch.setattr(dgh, "_append_gz", crashing)
    _patch_fetch(monkeypatch, lambda url: (200, BODY))
    wl = _worklist(3)
    with pytest.raises(OSError):
        _run(wl, tmp_path)
    paths = dgh.output_paths(str(tmp_path), "bend", 1)
    assert not os.path.exists(paths["queries"]) and os.path.exists(paths["history"])
    result = _run(wl, tmp_path)
    assert result["answered"] == 3
    on_disk = _read(paths["history"])
    assert len(on_disk) == 12  # 3 rows x 2 panos, twice
    read = dgh.load_sample_history(paths["history"], paths["queries"])
    assert len(read) == 6 and not read.duplicated(["edge_id", "sample_idx", "pano_id"]).any()
    rebuilt, _ = dgh.rebuild_from_raw(paths["raw"], wl, 1)
    key = ["edge_id", "sample_idx", "pano_id", "harvested_at"]
    assert sorted(map(tuple, read[key].to_numpy().tolist())) == sorted(
        map(tuple, rebuilt[key].to_numpy().tolist())
    )
    # Rows the ledger does not stand for (a key stranded before any ledger
    # row) are dropped when the ledger is given; without it, the last-query
    # rule alone applies.
    with gzip.open(paths["history"], "at") as f:
        f.write(dgh._csv_text(
            dgh.history_rows(wl.iloc[0], dgh.parse_search(BODY), 1, "2027-01-01T00:00:00+00:00"),
            dgh.SAMPLE_HISTORY_COLUMNS, header=False,
        ))  # fmt: skip
    assert len(dgh.load_sample_history(paths["history"])) == 6
    assert (
        dgh.load_sample_history(paths["history"])["harvested_at"] == "2027-01-01T00:00:00+00:00"
    ).sum() == 2
    assert len(dgh.load_sample_history(paths["history"], paths["queries"])) == 6


# ── The CLI ──────────────────────────────────────────────────────────────────


def _cli(monkeypatch, tmp_path, responder, *extra):
    import scripts.harvest_gsv_history as cli

    _patch_fetch(monkeypatch, responder)
    wl = tmp_path / "bend_walk.worklist.csv.gz"
    if not wl.exists():
        _worklist(3).to_csv(wl, index=False, compression="gzip")
    out = tmp_path / "results"
    argv = [
        "--worklist", str(wl), "--pass", "1", "--out-dir", str(out),
        "--rate-per-minute", "6000", "--jitter", "0",
        *extra,
    ]  # fmt: skip
    return cli.main(argv), out


def test_cli_exit_codes_complete_incomplete_blocked(monkeypatch, tmp_path, capsys):
    code, out = _cli(monkeypatch, tmp_path, lambda url: (200, BODY), "--dry-run")
    assert code == 0 and "3 to query" in capsys.readouterr().out
    assert not os.path.exists(out / "bend_walk.pass1.queries.csv.gz")

    code, out = _cli(monkeypatch, tmp_path, lambda url: (200, BODY), "--max-requests", "2")
    assert code == 83
    code, out = _cli(monkeypatch, tmp_path, lambda url: (200, BODY))
    assert code == 0
    assert dgh.read_done_keys(str(out / "bend_walk.pass1.queries.csv.gz")) == {
        ("e0", 0),
        ("e1", 0),
        ("e2", 0),
    }

    fresh = tmp_path / "second"
    fresh.mkdir()
    code, out = _cli(monkeypatch, fresh, lambda url: (429, "no"))
    assert code == 86
    # Latched: refused before any request, until the cool-down passes or the
    # operator clears it.
    calls = []
    code, out = _cli(monkeypatch, fresh, lambda url: (calls.append(url), (200, BODY))[1])
    assert code == 86 and calls == []
    assert "cool-down until" in capsys.readouterr().err
    code, out = _cli(monkeypatch, fresh, lambda url: (200, BODY), "--clear-latch")
    assert code == 0 and not os.path.exists(fresh / dgh.LATCH_FILENAME)


def test_cli_exits_2_on_a_missing_or_malformed_worklist(monkeypatch, tmp_path, capsys):
    import scripts.harvest_gsv_history as cli

    calls = []
    _patch_fetch(monkeypatch, lambda url: (calls.append(url), (200, BODY))[1])
    base = ["--pass", "1", "--out-dir", str(tmp_path / "o")]
    assert cli.main(["--worklist", str(tmp_path / "absent.worklist.csv.gz"), *base]) == 2
    assert "no such work-list file" in capsys.readouterr().err
    bad = tmp_path / "bad.worklist.csv.gz"
    _worklist(1).drop(columns=["anchor_pano_id"]).to_csv(bad, index=False, compression="gzip")
    assert cli.main(["--worklist", str(bad), *base]) == 2
    assert "anchor_pano_id" in capsys.readouterr().err
    assert calls == []


def test_cli_refuses_a_bad_jitter_or_cap_with_argparse_exit_2(monkeypatch, tmp_path):
    import scripts.harvest_gsv_history as cli

    wl = tmp_path / "w.worklist.csv.gz"
    _worklist(1).to_csv(wl, index=False, compression="gzip")
    base = ["--worklist", str(wl), "--pass", "1", "--out-dir", str(tmp_path / "o")]
    for bad in (["--jitter", "1"], ["--max-requests", "0"], ["--pass", "3"]):
        with pytest.raises(SystemExit) as info:
            cli.main(base + bad)
        assert info.value.code == 2
