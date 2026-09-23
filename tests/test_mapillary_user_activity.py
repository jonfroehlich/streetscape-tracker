"""
scripts/mapillary_user_activity.py -- the per-user Graph API activity report.

No real network: the crawl takes an injected ``fetch`` primitive, and these
tests hand it an in-memory ``_FakeGraph`` that serves pages by URL
(docs/testing.md). Pacing and backoff take injected sleeps and clocks.
"""

import importlib.util
import json
import os
import sys
from datetime import date

import pytest

from streetscape_metadata_tracker import db

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_path = os.path.join(PROJECT_ROOT, "scripts", "mapillary_user_activity.py")
_spec = importlib.util.spec_from_file_location("mapillary_user_activity", _path)
mua = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = mua
_spec.loader.exec_module(mua)

# 2026-09-15T23:30:00Z -- 16:30 PDT in Spokane.
T_0915_2330 = 1789515000000
SPOKANE = (47.718, -117.464)
HOUSTON = (29.822, -95.279)


def _img(i, lat, lon, ts, seq="s1", pano=True):
    return {
        "id": str(i),
        "captured_at": ts,
        "geometry": {"type": "Point", "coordinates": [lon, lat]},
        "sequence": seq,
        "is_pano": pano,
    }


class _FakeGraph:
    """Serves a scripted list of HttpResults (or exceptions), recording calls."""

    def __init__(self, responses, clock=None):
        self.responses = list(responses)
        self.calls = []
        self.clock = clock
        self.times = []

    def __call__(self, url, params):
        self.calls.append((url, params))
        if self.clock is not None:
            self.times.append(self.clock.now())
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def _page(images, next_cursor=None):
    body = {"data": images}
    if next_cursor:
        body["paging"] = {
            "cursors": {"after": next_cursor},
            "next": f"https://graph.mapillary.com/v1.0/images?after={next_cursor}",
        }
    return mua.HttpResult(200, json.dumps(body))


class _Clock:
    def __init__(self):
        self.t = 0.0
        self.sleeps = []

    def sleep(self, s):
        self.sleeps.append(s)
        self.t += s

    def now(self):
        return self.t


def _pacer(clock=None):
    clock = clock or _Clock()
    return mua.Pacer(1.0, sleep=clock.sleep, clock=clock.now)


def _crawl(graph, max_requests=10, sleep=None, clock=None):
    return mua.crawl_user_images(
        "uwrapid",
        date(2026, 9, 15),
        date(2026, 9, 15),
        fetch=graph,
        pacer=_pacer(clock),
        max_requests=max_requests,
        sleep=sleep or (lambda s: None),
    )


def _gaps(times):
    return [b - a for a, b in zip(times, times[1:], strict=False)]


# ── Pagination ────────────────────────────────────────────────────────────


def test_follows_the_cursor_to_the_end_and_counts_exactly():
    clock = _Clock()
    graph = _FakeGraph(
        [
            _page([_img(1, *SPOKANE, T_0915_2330), _img(2, *SPOKANE, T_0915_2330)], "c1"),
            _page([_img(3, *SPOKANE, T_0915_2330)], "c2"),
            _page([_img(4, *SPOKANE, T_0915_2330)]),
        ],
        clock=clock,
    )
    result = _crawl(graph, clock=clock)
    # Every request of the crawl is paced, not just the Pacer in isolation.
    assert len(graph.times) == 3
    assert min(_gaps(graph.times)) >= 1.0
    assert [i["id"] for i in result.images] == ["1", "2", "3", "4"]
    assert result.complete is True
    assert result.requests == 3
    # First request carries the query; later ones follow paging.next verbatim.
    assert graph.calls[0][1]["creator_username"] == "uwrapid"
    assert graph.calls[0][1]["limit"] == 2000
    assert graph.calls[0][1]["end_captured_at"] == "2026-09-16T00:00:00Z"  # until inclusive
    assert graph.calls[1] == ("https://graph.mapillary.com/v1.0/images?after=c1", None)
    assert graph.calls[2][0].endswith("after=c2")


@pytest.mark.parametrize(
    "next_url",
    ["https://evil.example/x", "http://graph.mapillary.com/v1.0/images?after=c1"],
)
def test_a_cursor_off_https_graph_is_refused(next_url):
    bad = mua.HttpResult(
        200,
        json.dumps({"data": [_img(1, *SPOKANE, T_0915_2330)], "paging": {"next": next_url}}),
    )
    graph = _FakeGraph([bad])
    with pytest.raises(mua.ActivityError, match="not https://graph.mapillary.com"):
        _crawl(graph)
    assert len(graph.calls) == 1


def test_an_image_served_twice_is_counted_once():
    graph = _FakeGraph(
        [
            _page([_img(1, *SPOKANE, T_0915_2330), _img(2, *SPOKANE, T_0915_2330)], "c1"),
            _page([_img(2, *SPOKANE, T_0915_2330), _img(3, *SPOKANE, T_0915_2330)]),
        ]
    )
    assert [i["id"] for i in _crawl(graph).images] == ["1", "2", "3"]


def test_a_repeated_cursor_stops_the_crawl():
    graph = _FakeGraph(
        [
            _page([_img(1, *SPOKANE, T_0915_2330)], "c1"),
            _page([_img(2, *SPOKANE, T_0915_2330)], "c1"),
            _page([_img(3, *SPOKANE, T_0915_2330)]),
        ]
    )
    with pytest.raises(mua.ActivityError, match="repeated a cursor"):
        _crawl(graph)
    assert len(graph.calls) == 2


def test_an_empty_page_ends_the_crawl_even_with_a_cursor():
    graph = _FakeGraph(
        [
            _page([_img(1, *SPOKANE, T_0915_2330)], "c1"),
            _page([], "c2"),
            _page([_img(3, *SPOKANE, T_0915_2330)]),
        ]
    )
    result = _crawl(graph)
    assert result.complete is True
    assert len(graph.calls) == 2
    assert [i["id"] for i in result.images] == ["1"]


def test_max_requests_stops_cleanly_with_the_newest_images():
    graph = _FakeGraph(
        [
            _page([_img(1, *SPOKANE, T_0915_2330)], "c1"),
            _page([_img(2, *SPOKANE, T_0915_2330)], "c2"),
            _page([_img(3, *SPOKANE, T_0915_2330)], "c3"),
        ]
    )
    result = _crawl(graph, max_requests=2)
    assert result.complete is False
    assert result.requests == 2
    assert len(graph.calls) == 2
    assert [i["id"] for i in result.images] == ["1", "2"]


# ── Retry and block detection ─────────────────────────────────────────────


def test_429_is_retried_after_retry_after_and_counts_against_the_budget():
    waits = []
    clock = _Clock()
    graph = _FakeGraph(
        [
            mua.HttpResult(429, "{}", headers={"Retry-After": "0"}),
            mua.HttpResult(429, "{}", headers={"Retry-After": "7"}),
            _page([_img(1, *SPOKANE, T_0915_2330)]),
        ],
        clock=clock,
    )

    def sleep(s):
        waits.append(s)
        clock.sleep(s)

    result = _crawl(graph, sleep=sleep, clock=clock)
    # A retry is paced like any request: even a Retry-After of 0 does not
    # let it go out sooner than the floor.
    assert min(_gaps(graph.times)) >= 1.0
    assert result.complete is True
    assert [i["id"] for i in result.images] == ["1"]
    assert waits == [0.0, 7.0]
    assert result.requests == 3  # the refused attempts were requests too


def test_retries_are_bounded():
    graph = _FakeGraph([mua.HttpResult(503, "") for _ in range(mua.MAX_TRIES)])
    with pytest.raises(mua.ActivityError, match="gave up"):
        _crawl(graph)
    assert len(graph.calls) == mua.MAX_TRIES


def test_a_transport_error_is_retried_after_one_backoff():
    waits = []
    graph = _FakeGraph(
        [mua.TransientError("ConnectionError"), _page([_img(1, *SPOKANE, T_0915_2330)])]
    )
    result = _crawl(graph, sleep=waits.append)
    assert result.complete is True
    assert result.requests == 2
    assert waits == [mua.BACKOFF_BASE_S]


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_a_redirect_is_a_block_and_is_never_retried(status):
    graph = _FakeGraph(
        [
            mua.HttpResult(status, "", headers={"Location": "https://www.mapillary.com/login"}),
            _page([_img(1, *SPOKANE, T_0915_2330)]),
        ]
    )
    with pytest.raises(mua.BlockedError):
        _crawl(graph)
    assert len(graph.calls) == 1


def test_html_on_200_is_a_block():
    graph = _FakeGraph([mua.HttpResult(200, "<html>login</html>", content_type="text/html")])
    with pytest.raises(mua.BlockedError):
        _crawl(graph)


def test_a_bad_token_is_a_plain_error_not_a_block():
    body = json.dumps({"error": {"message": "Invalid OAuth access token"}})
    graph = _FakeGraph([mua.HttpResult(401, body)])
    with pytest.raises(mua.ActivityError, match="Invalid OAuth") as exc:
        _crawl(graph)
    assert not isinstance(exc.value, mua.BlockedError)


def test_pacer_never_goes_faster_than_the_floor():
    clock = _Clock()
    pacer = mua.Pacer(1.0, sleep=clock.sleep, clock=clock.now)
    starts = []
    for _ in range(20):
        pacer.wait()
        starts.append(clock.now())
    gaps = [b - a for a, b in zip(starts, starts[1:], strict=False)]
    assert min(gaps) >= 1.0
    assert max(gaps) <= 1.0 * (1 + mua.JITTER_FRACTION)
    assert clock.sleeps  # it really did sleep


# ── Grouping ──────────────────────────────────────────────────────────────


def test_solar_day_keeps_an_american_afternoon_on_its_own_day():
    # 23:30Z on 09-15 is 16:30 local in Spokane: still the 15th.
    assert mua.solar_day(T_0915_2330, SPOKANE[1]) == date(2026, 9, 15)
    # 01:00Z on 09-16 is also 09-15 afternoon there -- the UTC date would say 16th.
    assert mua.solar_day(T_0915_2330 + 90 * 60 * 1000, SPOKANE[1]) == date(2026, 9, 15)


def test_cells_are_about_cell_km_wide_in_both_directions():
    lat = 47.7
    r0, c0 = mua.cell_of(lat, -117.0, 10.0)
    # ~9 km north / east of a cell's south-west corner stays in the same cell;
    # 11 km does not. Walk from the cell's own corner so the check is exact.
    dlat = 10.0 / mua.KM_PER_DEG_LAT
    south = r0 * dlat + 1e-9
    import math

    dlon = 10.0 / (mua.KM_PER_DEG_LAT * math.cos(math.radians((r0 + 0.5) * dlat)))
    west = c0 * dlon + 1e-9
    assert mua.cell_of(south + 0.9 * dlat, west + 0.9 * dlon, 10.0) == (r0, c0)
    # Next row up (its columns are re-widened for its own latitude, so only
    # the row index is comparable across rows).
    assert mua.cell_of(south + 1.1 * dlat, west, 10.0)[0] == r0 + 1
    assert mua.cell_of(south, west + 1.1 * dlon, 10.0) == (r0, c0 + 1)
    # An independent check of the widening: two points 9.5 km apart east-west
    # (measured geodesically, not with the formula under test) share a cell
    # only if columns really are ~10 km wide at this latitude. Without the
    # cos widening a column would be 10 km of EQUATORIAL longitude -- ~6.7 km
    # here -- and they would not.
    import geopy.distance

    origin = (south + 0.5 * dlat, west + 0.01 * dlon)
    east = geopy.distance.distance(kilometers=9.5).destination(origin, 90)
    assert mua.cell_of(*origin, 10.0) == mua.cell_of(east.latitude, east.longitude, 10.0)


def test_group_stats():
    imgs = [
        _img(1, 47.710, -117.460, T_0915_2330, seq="a", pano=True),
        _img(2, 47.720, -117.470, T_0915_2330 + 60_000, seq="b", pano=False),
        _img(3, 47.715, -117.465, T_0915_2330 - 60_000, seq="a", pano=True),
        _img(4, *HOUSTON, T_0915_2330, seq="h"),
        {"id": "5", "captured_at": T_0915_2330, "geometry": None},  # unplaceable
    ]
    groups = mua.group_images(imgs, 10.0)
    assert len(groups) == 2
    spokane = groups[0]  # largest first within a day
    assert spokane["images"] == 3
    assert spokane["sequences"] == 2
    assert spokane["pano_share"] == pytest.approx(2 / 3, abs=1e-4)
    assert spokane["lat"] == pytest.approx(47.715)
    assert spokane["lon"] == pytest.approx(-117.465)
    assert spokane["first_capture_utc"] == "2026-09-15T23:29:00Z"
    assert spokane["last_capture_utc"] == "2026-09-15T23:31:00Z"
    assert groups[1]["images"] == 1


def test_groups_within_a_day_are_largest_first():
    # Insertion order (Houston first, one image) differs from size order.
    imgs = [_img(1, *HOUSTON, T_0915_2330)] + [_img(i, *SPOKANE, T_0915_2330) for i in range(2, 5)]
    groups = mua.group_images(imgs, 10.0)
    assert [g["images"] for g in groups] == [3, 1]


def test_groups_split_by_day_newest_first():
    day_ms = 86_400_000
    imgs = [_img(1, *SPOKANE, T_0915_2330 - 20 * day_ms), _img(2, *SPOKANE, T_0915_2330)]
    groups = mua.group_images(imgs, 10.0)
    assert [g["date"] for g in groups] == ["2026-09-15", "2026-08-26"]


# ── Catalog match ─────────────────────────────────────────────────────────


def _register_spokane(conn, run_date=None, newest=None):
    city_id = db.register_city(
        conn,
        city_name="Spokane",
        state_name="Washington",
        state_code="WA",
        country_name="United States",
        country_code="US",
        center_lat=47.672788,
        center_lon=-117.45392875,
        grid_width_m=22539,
        grid_height_m=19125,
        step_m=20,
    )
    if run_date:
        db.register_run(
            conn,
            city_id=city_id,
            run_date=run_date,
            csv_filename="spokane_mapillary.csv.gz",
            provider="mapillary",
            newest_capture_date=newest,
        )
    conn.commit()
    return city_id


def _catalog_cities(data_dir):
    ro = mua.open_catalog_readonly(os.path.join(data_dir, "streetscape_tracker.db"))
    try:
        return mua.load_catalog_cities(ro)
    finally:
        ro.close()


def _group(lat, lon, last="2026-09-15T23:31:00Z"):
    return {"lat": lat, "lon": lon, "last_capture_utc": last}


def test_catalog_match_inside_after_the_last_run(conn, data_dir):
    city_id = _register_spokane(conn, date(2026, 9, 13), "2026-04-03")
    cities = _catalog_cities(data_dir)
    m = mua.match_group(_group(*SPOKANE), cities)
    assert m["city_id"] == city_id
    assert m["last_mapillary_run"] == "2026-09-13"
    assert m["newest_capture_seen"] == "2026-04-03"
    assert m["after_last_run"] is True
    assert m["newer_than_seen"] is True


def test_captured_before_the_run_but_newer_than_it_saw(conn, data_dir):
    # The 2026-08-26 Spokane batch: before the 09-13 run, yet the run's newest
    # capture was 04-03 -- so the run did not see it.
    _register_spokane(conn, date(2026, 9, 13), "2026-04-03")
    m = mua.match_group(_group(47.717, -117.485, "2026-08-26T20:00:00Z"), _catalog_cities(data_dir))
    assert m["after_last_run"] is False
    assert m["newer_than_seen"] is True


def test_imagery_the_run_already_saw_is_not_flagged(conn, data_dir):
    _register_spokane(conn, date(2026, 9, 13), "2026-09-01")
    m = mua.match_group(_group(47.717, -117.485, "2026-08-26T20:00:00Z"), _catalog_cities(data_dir))
    assert m["after_last_run"] is False
    assert m["newer_than_seen"] is False


def test_overlapping_cities_the_smallest_wins(conn, data_dir):
    big = _register_spokane(conn)
    small = db.register_city(
        conn,
        city_name="Spokane Valley",
        state_name="Washington",
        state_code="WA",
        country_name="United States",
        country_code="US",
        center_lat=SPOKANE[0],
        center_lon=SPOKANE[1],
        grid_width_m=2000,
        grid_height_m=2000,
        step_m=20,
    )
    conn.commit()
    # The big city is registered first AND sorts first by city_id
    # ("spokane--..." < "spokane-valley--..."), so only the area sort can put
    # the small one ahead.
    m = mua.match_group(_group(*SPOKANE), _catalog_cities(data_dir))
    assert m["city_id"] == small
    assert m["also_in"] == [big]


def test_a_null_newest_capture_is_newer_than_seen(conn, data_dir):
    _register_spokane(conn, date(2026, 9, 13), None)
    m = mua.match_group(_group(47.717, -117.485, "2026-08-26T20:00:00Z"), _catalog_cities(data_dir))
    assert m["newest_capture_seen"] is None
    assert m["after_last_run"] is False
    assert m["newer_than_seen"] is True


def test_the_latest_mapillary_run_is_the_one_matched(conn, data_dir):
    city_id = _register_spokane(conn, date(2026, 6, 1), "2026-01-01")
    db.register_run(
        conn,
        city_id=city_id,
        run_date=date(2026, 9, 13),
        csv_filename="spokane_mapillary_2.csv.gz",
        provider="mapillary",
        newest_capture_date="2026-04-03",
    )
    db.register_run(  # a NEWER gsv run must not be read as the mapillary one
        conn, city_id=city_id, run_date=date(2026, 9, 20), csv_filename="spokane_gsv.csv.gz"
    )
    conn.commit()
    m = mua.match_group(_group(*SPOKANE), _catalog_cities(data_dir))
    assert (m["last_mapillary_run"], m["newest_capture_seen"]) == ("2026-09-13", "2026-04-03")


@pytest.mark.parametrize("version", [0, 99])
def test_an_unreadable_catalog_schema_exits_64(tmp_path, capsys, version):
    import sqlite3

    path = tmp_path / "odd.db"
    c = sqlite3.connect(path)
    c.execute(f"PRAGMA user_version = {version}")
    c.close()
    graph = _FakeGraph([_page([_img(1, *SPOKANE, T_0915_2330)])])
    assert mua.run(_args(tmp_path, "--db", str(path)), fetch=graph, pacer=_pacer()) == 64
    assert f"schema version {version}" in capsys.readouterr().err


def test_catalog_match_outside_every_city(conn, data_dir):
    _register_spokane(conn, date(2026, 9, 13), "2026-04-03")
    assert mua.match_group(_group(*HOUSTON), _catalog_cities(data_dir)) == {"city_id": None}
    # Just past the frozen bbox's northern edge (47.672788 + ~9.57 km + half-step).
    assert mua.match_group(_group(47.76, -117.454), _catalog_cities(data_dir))["city_id"] is None


def test_a_city_never_collected_is_flagged(conn, data_dir):
    _register_spokane(conn)
    m = mua.match_group(_group(*SPOKANE), _catalog_cities(data_dir))
    assert m["never_collected"] is True
    assert m["after_last_run"] is False


def test_disabled_cities_are_not_matched(conn, data_dir):
    city_id = _register_spokane(conn, date(2026, 9, 13), "2026-04-03")
    db.set_city_enabled(conn, city_id, False)
    conn.commit()
    assert mua.match_group(_group(*SPOKANE), _catalog_cities(data_dir)) == {"city_id": None}


def test_catalog_is_opened_read_only(conn, data_dir):
    _register_spokane(conn)
    ro = mua.open_catalog_readonly(os.path.join(data_dir, "streetscape_tracker.db"))
    try:
        with pytest.raises(Exception, match="readonly"):
            ro.execute("DELETE FROM cities")
    finally:
        ro.close()


# ── End to end through run() ──────────────────────────────────────────────


def _args(tmp_path, *extra):
    return mua.parse_args(["uwrapid", "--since", "2026-09-15", "--until", "2026-09-15", *extra])


def test_missing_db_is_reported_and_skipped(tmp_path, capsys):
    graph = _FakeGraph([_page([_img(1, *SPOKANE, T_0915_2330)])])
    missing = str(tmp_path / "nope.db")
    args = _args(tmp_path, "--db", missing)
    rc = mua.run(args, fetch=graph, pacer=_pacer())
    out = capsys.readouterr().out
    assert rc == mua.EXIT_OK
    assert f"No catalog at {missing}" in out
    assert not os.path.exists(missing)  # never created


def test_run_writes_geojson_with_catalog_fields(conn, data_dir, tmp_path, capsys):
    _register_spokane(conn, date(2026, 9, 13), "2026-04-03")
    graph = _FakeGraph([_page([_img(1, *SPOKANE, T_0915_2330), _img(2, *HOUSTON, T_0915_2330)])])
    gj = tmp_path / "out.geojson"
    args = _args(
        tmp_path, "--db", os.path.join(data_dir, "streetscape_tracker.db"), "--geojson", str(gj)
    )
    assert mua.run(args, fetch=graph, pacer=_pacer()) == mua.EXIT_OK
    fc = json.loads(gj.read_text())
    assert fc["type"] == "FeatureCollection"
    by_city = {f["properties"].get("city_id"): f for f in fc["features"]}
    sp = by_city["spokane--washington--united-states"]
    assert sp["geometry"]["coordinates"] == [SPOKANE[1], SPOKANE[0]]
    assert sp["properties"]["after_last_run"] is True
    assert sp["properties"]["complete"] is True
    assert None in by_city  # Houston, untracked here
    assert "AFTER LAST RUN" in capsys.readouterr().out


def test_truncated_run_exits_83_and_says_lower_bounds(tmp_path, capsys):
    graph = _FakeGraph([_page([_img(1, *SPOKANE, T_0915_2330)], "c1")])
    args = _args(tmp_path, "--db", str(tmp_path / "none.db"), "--max-requests", "1")
    gj = tmp_path / "t.geojson"
    args.geojson = str(gj)
    assert mua.run(args, fetch=graph, pacer=_pacer()) == mua.INCOMPLETE_EXIT
    captured = capsys.readouterr()
    assert "LOWER BOUND" in captured.out
    assert json.loads(gj.read_text())["features"][0]["properties"]["complete"] is False


def test_a_block_after_page_one_still_reports_what_was_fetched(tmp_path, capsys):
    graph = _FakeGraph(
        [
            _page([_img(1, *SPOKANE, T_0915_2330), _img(2, *SPOKANE, T_0915_2330)], "c1"),
            mua.HttpResult(302, ""),
        ]
    )
    gj = tmp_path / "b.geojson"
    args = _args(tmp_path, "--db", str(tmp_path / "none.db"), "--geojson", str(gj))
    assert mua.run(args, fetch=graph, pacer=_pacer()) == mua.BLOCKED_EXIT
    out = capsys.readouterr().out
    assert "BLOCKED after 1 page(s)" in out
    assert "LOWER BOUND" in out
    props = json.loads(gj.read_text())["features"][0]["properties"]
    assert props["images"] == 2
    assert props["complete"] is False


def test_unplaceable_images_are_counted(tmp_path, capsys):
    graph = _FakeGraph(
        [
            _page(
                [
                    _img(1, *SPOKANE, T_0915_2330),
                    {"id": "2", "captured_at": T_0915_2330, "geometry": None},
                    {"id": "3", "geometry": {"type": "Point", "coordinates": [-117.4, 47.7]}},
                ]
            )
        ]
    )
    path = str(tmp_path / "m.json")
    args = _args(tmp_path, "--db", str(tmp_path / "none.db"), "--metrics-json", path)
    mua.run(args, fetch=graph, pacer=_pacer())
    assert json.loads(open(path).read())["windows"][0]["skipped_unplaceable"] == 2
    assert "2 images had no geometry or time" in capsys.readouterr().out


def test_block_exits_75(tmp_path):
    graph = _FakeGraph([mua.HttpResult(302, "")])
    args = _args(tmp_path, "--db", str(tmp_path / "none.db"))
    assert mua.run(args, fetch=graph, pacer=_pacer()) == mua.BLOCKED_EXIT


def test_metrics_upsert_replaces_the_same_window(tmp_path):
    path = str(tmp_path / "m.json")
    for n in (1, 2):
        graph = _FakeGraph([_page([_img(i, *SPOKANE, T_0915_2330) for i in range(n)])])
        args = _args(tmp_path, "--db", str(tmp_path / "none.db"), "--metrics-json", path)
        mua.run(args, fetch=graph, pacer=_pacer())
    doc = json.loads(open(path).read())
    assert len(doc["windows"]) == 1
    assert doc["windows"][0]["images"] == 2
    assert doc["windows"][0]["generated_by"].startswith(
        "scripts/mapillary_user_activity.py uwrapid"
    )


# ── The production fetch primitive ────────────────────────────────────────


class _FakeResponse:
    status_code = 302
    text = ""
    headers = {"Content-Type": "text/html", "Location": "https://www.mapillary.com/login"}


class _FakeSession:
    def __init__(self):
        self.headers = {}
        self.gets = []

    def get(self, url, **kwargs):
        self.gets.append((url, kwargs))
        return _FakeResponse()


def test_the_requests_fetch_never_follows_redirects_and_keeps_the_token_in_a_header(
    monkeypatch,
):
    import requests

    session = _FakeSession()
    monkeypatch.setattr(requests, "Session", lambda: session)
    fetch = mua.make_requests_fetch("MLY|secret|token")
    params = mua.first_page_params("uwrapid", date(2026, 9, 15), date(2026, 9, 15))
    res = fetch(mua.GRAPH_IMAGES_URL, params)

    assert session.headers["Authorization"] == "OAuth MLY|secret|token"
    ((url, kwargs),) = session.gets
    assert kwargs["allow_redirects"] is False
    assert kwargs["timeout"] == mua.REQUEST_TIMEOUT_S
    assert "secret" not in url
    assert "secret" not in json.dumps(kwargs["params"])
    # The 302 comes back as a 302, so fetch_page can call it a block.
    assert res.status == 302
    assert res.headers["Location"].endswith("/login")


def test_the_requests_fetch_turns_transport_failures_into_transient_errors(monkeypatch):
    import requests

    class _Dead(_FakeSession):
        def get(self, url, **kwargs):
            raise requests.ConnectionError("refused")

    monkeypatch.setattr(requests, "Session", _Dead)
    with pytest.raises(mua.TransientError):
        mua.make_requests_fetch("t")(mua.GRAPH_IMAGES_URL, None)


# ── Usage errors ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "argv",
    [
        ["bad user!"],
        ["uwrapid", "--since", "2026-09-20", "--until", "2026-09-01"],
        ["uwrapid", "--since", "yesterday"],
        ["uwrapid", "--cell-km", "0"],
        ["uwrapid", "--max-requests", "0"],
        ["uwrapid", "--min-interval", "0.2"],
    ],
)
def test_usage_errors_exit_64(argv):
    with pytest.raises(SystemExit) as exc:
        mua.parse_args(argv)
    assert exc.value.code == mua.USAGE_EXIT


def test_since_defaults_to_thirty_days_before_until():
    args = mua.parse_args(["uwrapid", "--until", "2025-01-31"], today=date(2026, 9, 23))
    assert (args.since, args.until) == (date(2025, 1, 1), date(2025, 1, 31))
    args = mua.parse_args(["uwrapid"], today=date(2026, 9, 23))
    assert (args.since, args.until) == (date(2026, 8, 24), date(2026, 9, 23))


def test_portable_path_never_records_a_home_directory():
    inside = os.path.join(mua.PROJECT_ROOT, "data", "streetscape_tracker.db")
    assert mua.portable_path(inside) == os.path.join("data", "streetscape_tracker.db")
    assert mua.portable_path("/Users/someone/elsewhere/prod.db") == "prod.db"
    assert mua.portable_path(None) is None


def test_normalize_metrics_rewrites_only_catalog_paths(tmp_path):
    path = tmp_path / "m.json"
    rec = {
        "username": "u",
        "since": "a",
        "until": "b",
        "catalog_path": "/Users/x/y.db",
        "images": 5,
    }
    path.write_text(json.dumps({"windows": [rec]}))
    assert mua.normalize_metrics(str(path)) == 1
    (w,) = json.loads(path.read_text())["windows"]
    assert w == {**rec, "catalog_path": "y.db"}
    assert mua.normalize_metrics(str(path)) == 0  # idempotent


def test_refuses_a_collection_host(monkeypatch):
    monkeypatch.setattr(mua.socket, "gethostname", lambda: "makelab2")
    with pytest.raises(SystemExit) as exc:
        mua.refuse_on_collection_host(False)
    assert exc.value.code == mua.USAGE_EXIT
    mua.refuse_on_collection_host(True)  # the explicit override
