"""
The standing Panoramax growth screen (issue #316, phase 2 PR 2).

What these pin, in the order the instrument is built:

1. **The decoders**, moved here from the phase-1 study — hexagon counters are
   whole-hexagon figures repeated on every tile they touch, so they merge by MAX
   and never by SUM, while the geometry is clipped and merges by UNION.
2. **The selection rule** — a screen hexagon is bigger than most cities, so
   cities are matched by OVERLAP and the result is an UPPER BOUND. Centre
   selection would miss the very hexagon a city sits inside.
3. **The economy** — the whole catalog dedupes to a handful of z6 tiles, which
   is the only reason this can run weekly.
4. **The two refusals**. An all-404 pass is a moved endpoint, and a pass that
   finds nothing anywhere in a catalog that has found something before is a
   renamed layer. Both must refuse rather than write, because the damage in each
   case is a dated row saying "empty" about cities nobody measured.
5. **The catalog contract** — a screen is idempotent per date, spends into the
   day's ledger under the provider's own name, and publishes an artifact whose
   caveat travels with the numbers.

The study's own tests (`tests/test_panoramax_feasibility.py`) still cover the
decoders through their re-export, deliberately unchanged by the move.
"""

from __future__ import annotations

import asyncio
import gzip
import json
import os
from datetime import date

import aiohttp
import mapbox_vector_tile
import pytest

from streetscape_metadata_tracker import db, scheduler
from streetscape_metadata_tracker import panoramax_screen as ps
from streetscape_metadata_tracker.download_common import (
    HOST_BUSY_EXIT_CODES,
    HOST_EXIT_CODES,
    HOST_PANORAMAX,
    DownloadError,
    HostBlockedError,
    HostBusyError,
    lonlat_to_tile_frac,
    tiles_for_bbox,
)
from streetscape_metadata_tracker.json_summarizer import generate_provider_screen_summary

DES_MOINES = (41.5868, -93.6250)


# ── Building fake tiles ────────────────────────────────────────────────────


def encode_grid_tile(hexes, tile_x, tile_y, zoom=ps.SCREEN_ZOOM, extent=4096, layer=None):
    """
    Raw MVT bytes for the v2 `grid` layer, inverting the decode path's math.

    Each entry is (hex_id, (min_lon, min_lat, max_lon, max_lat), counters); the
    polygon written is that box, clipped by nothing, so a test can hand the same
    hexagon to two tiles and check the union.
    """
    features = []
    for hex_id, box, counters in hexes:
        min_lon, min_lat, max_lon, max_lat = box
        ring = []
        for lon, lat in (
            (min_lon, min_lat),
            (max_lon, min_lat),
            (max_lon, max_lat),
            (min_lon, max_lat),
            (min_lon, min_lat),
        ):
            fx, fy = lonlat_to_tile_frac(lon, lat, zoom)
            ring.append(((fx - tile_x) * extent, (1 - (fy - tile_y)) * extent))
        features.append(
            {
                "geometry": {"type": "Polygon", "coordinates": [ring]},
                "properties": {"id": hex_id, **counters},
            }
        )
    return mapbox_vector_tile.encode([{"name": layer or ps.SCREEN_LAYER, "features": features}])


def counters(total, pano=0, flat=0):
    return {"nb_pictures": total, "nb_360_pictures": pano, "nb_flat_pictures": flat}


class _FakeResponse:
    def __init__(self, status=200, body=b"", headers=None):
        self.status = status
        self.headers = headers or {}
        self._body = body

    async def read(self):
        return self._body

    def raise_for_status(self):
        if self.status >= 400:
            raise RuntimeError(f"HTTP {self.status}")


class _FakeSession:
    """aiohttp.ClientSession stand-in answering per-URL, in call order."""

    def __init__(self, by_url):
        self.by_url = by_url
        self.urls = []

    def get(self, url, **kwargs):
        self.urls.append(url)
        response = self.by_url.get(url, _FakeResponse(404))

        class _Ctx:
            async def __aenter__(self):
                return response

            async def __aexit__(self, *exc):
                return False

        return _Ctx()


def tile_url(x, y, zoom=ps.SCREEN_ZOOM):
    return ps.SCREEN_URL_TEMPLATE.format(z=zoom, x=x, y=y)


def register(conn, name, *, lat, lon, width=2000, height=2000, enabled=True):
    return db.register_city(
        conn,
        city_name=name,
        state_name=None,
        state_code=None,
        country_name="United States",
        country_code="US",
        center_lat=lat,
        center_lon=lon,
        grid_width_m=width,
        grid_height_m=height,
        step_m=20,
        enabled=enabled,
    )


# ── 1. The decoders, and what merging one hexagon twice must mean ──────────


def test_a_hexagon_seen_in_two_tiles_keeps_ONE_count_and_BOTH_halves_extent():
    """
    The measured contract: a counter is the WHOLE hexagon's, repeated verbatim
    in every tile the hexagon touches, while the geometry is clipped to the
    tile. So merging must take the counter ONCE and union the boxes — summing
    would double every hexagon on a seam, and a city sitting under such a
    hexagon would report twice the imagery that exists.
    """
    # 90°W is an exact z6 seam, so these two boxes are the same hexagon's two
    # clipped halves, one per tile.
    west = ps.hexes_from_tile(
        encode_grid_tile([("h1", (-90.5, 41.0, -90.0, 41.5), counters(100, 90, 10))], 15, 23),
        15,
        23,
        ps.SCREEN_ZOOM,
    )
    east = ps.hexes_from_tile(
        encode_grid_tile([("h1", (-90.0, 41.0, -89.5, 41.5), counters(100, 90, 10))], 16, 23),
        16,
        23,
        ps.SCREEN_ZOOM,
    )
    merged = ps.merge_hexes(dict(west), east)

    assert merged["h1"]["nb_pictures"] == 100, "counters merge by max, never by sum"
    # Tolerance is one MVT pixel: the geometry is quantized to 1/4096 of a tile,
    # about 0.0014° at z6.
    assert merged["h1"]["min_lon"] == pytest.approx(-90.5, abs=2e-3)
    assert merged["h1"]["max_lon"] == pytest.approx(-89.5, abs=2e-3)


def test_a_seam_hexagon_reporting_zero_on_one_side_still_counts_its_pictures():
    """
    The merge takes the MAX rather than the first sighting, and this is the case
    that choice exists for. If the provider ever served per-piece counts, the
    piece a city's tiles happened to see first could be the empty one — and
    first-seen would then write a conclusive ZERO for a city that holds imagery,
    the one failure this instrument cannot tolerate. Max degrades to a lower
    bound instead.
    """
    empty_side = ps.hexes_from_tile(
        encode_grid_tile([("h1", (-90.5, 41.0, -90.0, 41.5), counters(0))], 15, 23),
        15,
        23,
        ps.SCREEN_ZOOM,
    )
    full_side = ps.hexes_from_tile(
        encode_grid_tile([("h1", (-90.0, 41.0, -89.5, 41.5), counters(42, 40, 2))], 16, 23),
        16,
        23,
        ps.SCREEN_ZOOM,
    )
    assert ps.merge_hexes(dict(empty_side), full_side)["h1"]["nb_pictures"] == 42


def test_an_empty_tile_and_a_tile_without_the_layer_are_both_answers():
    """An area the provider knows nothing about answers 200 with no `grid`
    layer, and a 404 decodes from empty bytes. Neither is an error, and both
    must decode to "no hexagons" rather than raising."""
    assert ps.hexes_from_tile(b"", 15, 23, ps.SCREEN_ZOOM) == {}
    other_layer = encode_grid_tile(
        [("h1", (-94.0, 41.0, -93.5, 41.5), counters(1))], 15, 23, layer="pictures"
    )
    assert ps.hexes_from_tile(other_layer, 15, 23, ps.SCREEN_ZOOM) == {}


# ── 2. Selection: overlap, because the hexagon is bigger than the city ─────


def test_a_city_INSIDE_one_big_hexagon_is_selected_by_overlap_but_not_by_centre():
    """
    A res-6 hexagon is ~36 km² and the median tracked city is 19.5 km², so the
    normal case is a city sitting wholly INSIDE one hexagon whose centre is
    nowhere near it. Centre selection returns nothing there — which would call
    every such city empty, conclusively and wrongly.
    """
    big = {
        "h1": {
            "min_lon": -94.0,
            "max_lon": -93.0,
            "min_lat": 41.0,
            "max_lat": 42.0,
            "nb_pictures": 500,
            "nb_360_pictures": 400,
            "nb_flat_pictures": 100,
        }
    }
    city_bbox = (-93.65, 41.58, -93.61, 41.60)
    assert ps.hexes_overlapping_bbox(big, city_bbox), "overlap must find the containing hexagon"
    assert ps.hexes_in_bbox(big, city_bbox) == [], "its centre is outside the city, by design"


def test_the_row_sums_only_the_hexagons_that_touch_the_city():
    target = ps.ScreenTarget("dm", "Des Moines", "United States", (-93.7, 41.5, -93.5, 41.7))
    by_tile = {
        (0, 0): {
            "near": {
                "min_lon": -93.8,
                "max_lon": -93.4,
                "min_lat": 41.4,
                "max_lat": 41.8,
                "nb_pictures": 300,
                "nb_360_pictures": 250,
                "nb_flat_pictures": 50,
            },
            "far": {
                "min_lon": -80.0,
                "max_lon": -79.0,
                "min_lat": 41.4,
                "max_lat": 41.8,
                "nb_pictures": 9_000,
                "nb_360_pictures": 9_000,
                "nb_flat_pictures": 0,
            },
        }
    }
    row = ps.screen_row(target, by_tile, [(0, 0)])
    assert row["pictures_upper_bound"] == 300
    assert row["pictures_360_upper_bound"] == 250
    assert row["pictures_flat_upper_bound"] == 50
    assert row["cells"] == 1


# ── 3. The economy, which is the whole reason this can be weekly ───────────


def test_neighbouring_cities_share_z6_tiles_so_the_plan_is_smaller_than_the_catalog():
    """
    113 requests screen 1,144 cities because a z6 tile spans ~5.6° of longitude
    and neighbours fall inside one. If the plan ever stopped deduping, the cost
    would become per-city and the instrument would stop being weekly.
    """
    targets = [
        ps.ScreenTarget(
            f"c{i}", f"C{i}", "United States", (-93.70 + i * 0.01, 41.5, -93.65 + i * 0.01, 41.55)
        )
        for i in range(8)
    ]
    tiles, per_city = ps.plan_screen(targets)
    assert len(tiles) == 1 < len(targets)
    assert all(per_city[t.city_id] == tiles for t in targets)


def test_the_tiles_are_enumerated_from_the_GROWN_bbox():
    """
    A hexagon overlapping the city can be carried by the NEXT z6 tile, and it
    arrives CLIPPED — so without the margin the union never reconstructs it and a
    city near a seam is screened against a hexagon nobody fetched. 90°W is an
    exact z6 seam, so a city just east of it needs the tile to its west.
    """
    bbox = (-89.99, 41.50, -89.95, 41.55)
    bare = set(tiles_for_bbox(*bbox, ps.SCREEN_ZOOM))
    grown = set(ps.screen_tiles_for_city(bbox))
    assert len(bare) == 1
    assert grown > bare, "the seam's other tile must be fetched, or the margin is imaginary"


# ── 4. The refusals ────────────────────────────────────────────────────────


def test_every_tile_404ing_is_a_moved_endpoint_and_refuses():
    """
    On this host an empty area answers 200 with no layer — phase 1 saw zero 404s
    in 3,321 requests, including 20 cities holding nothing. So an all-404 pass is
    a renamed URL, and finalizing it would stamp the whole catalog with a zero on
    the day the endpoint changed.
    """
    with pytest.raises(DownloadError, match="moved or been renamed"):
        ps._refuse_if_endpoint_moved([(1, 1), (1, 2)], empty_tiles=2)


def test_a_single_404_is_a_hole_and_not_an_endpoint_change():
    ps._refuse_if_endpoint_moved([(1, 1)], empty_tiles=1)  # one tile: no evidence
    ps._refuse_if_endpoint_moved([(1, 1), (1, 2)], empty_tiles=1)  # not all of them


def test_a_tile_that_cannot_be_read_ends_the_pass_rather_than_writing_a_hole(monkeypatch):
    """
    A census tolerates 2% failed tiles because a tile is a patch of ONE city.
    A screen tile is every city under ~5.6° of longitude, so the same tolerance
    would write conclusive zeroes for cities nobody measured. There is nothing to
    salvage — refuse the pass.
    """

    async def boom(*args, **kwargs):
        raise aiohttp.ClientError("connection reset")

    monkeypatch.setattr(ps, "_fetch_tile", boom)
    with pytest.raises(DownloadError, match="unread tile"):
        asyncio.run(
            ps.screen_targets_async(
                [ps.ScreenTarget("dm", "Des Moines", "US", (-93.7, 41.5, -93.5, 41.7))]
            )
        )


def test_a_refusal_propagates_as_a_host_condition_rather_than_an_empty_screen(monkeypatch):
    async def refused(*args, **kwargs):
        raise HostBlockedError("refused", host=HOST_PANORAMAX)

    monkeypatch.setattr(ps, "_fetch_tile", refused)
    with pytest.raises(HostBlockedError):
        asyncio.run(
            ps.screen_targets_async(
                [ps.ScreenTarget("dm", "Des Moines", "US", (-93.7, 41.5, -93.5, 41.7))]
            )
        )


# ── 5. A whole pass, through the fake session ──────────────────────────────


def _one_tile_session(target, hexes):
    (x, y) = ps.screen_tiles_for_city(target.bbox)[0]
    return _FakeSession({tile_url(x, y): _FakeResponse(200, encode_grid_tile(hexes, x, y))})


def test_a_pass_counts_ATTEMPTS_and_returns_one_row_per_city(monkeypatch):
    target = ps.ScreenTarget("dm", "Des Moines", "United States", (-93.70, 41.55, -93.60, 41.62))
    (x, y) = ps.screen_tiles_for_city(target.bbox)[0]
    session = _one_tile_session(
        target, [("h1", (-94.0, 41.0, -93.0, 42.0), counters(548_451, 545_000, 3_451))]
    )
    monkeypatch.setattr(aiohttp, "ClientSession", lambda **kw: _AsyncCM(session))

    result = asyncio.run(ps.screen_targets_async([target]))
    assert result["api_requests"] == 1
    assert result["tiles"] == 1
    row = result["rows"][0]
    assert row["city_id"] == "dm"
    assert row["pictures_upper_bound"] == 548_451
    assert session.urls == [tile_url(x, y)]


class _AsyncCM:
    def __init__(self, session):
        self.session = session

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, *exc):
        return False


# ── 6. The catalog contract ────────────────────────────────────────────────


def test_a_second_screen_on_one_day_REPLACES_rather_than_duplicating(conn):
    register(conn, "Des Moines", lat=DES_MOINES[0], lon=DES_MOINES[1])
    rows = [
        {
            "city_id": "des-moines-united-states",
            "cells": 1,
            "pictures_upper_bound": 10,
            "pictures_360_upper_bound": 8,
            "pictures_flat_upper_bound": 2,
        }
    ]
    city_id = db.get_all_cities(conn)[0].city_id
    rows[0]["city_id"] = city_id
    today = date(2026, 9, 9)
    db.record_provider_screen(conn, provider="panoramax", screen_date=today, rows=rows)
    rows[0]["pictures_upper_bound"] = 20
    db.record_provider_screen(conn, provider="panoramax", screen_date=today, rows=rows)

    stored = db.get_latest_provider_screen(conn, "panoramax")
    assert len(stored) == 1
    assert stored[0]["pictures_upper_bound"] == 20


def test_first_positive_is_the_earliest_NON_ZERO_screen_not_the_earliest_screen(conn):
    register(conn, "Des Moines", lat=DES_MOINES[0], lon=DES_MOINES[1])
    city_id = db.get_all_cities(conn)[0].city_id

    def write(day, count):
        db.record_provider_screen(
            conn,
            provider="panoramax",
            screen_date=day,
            rows=[
                {
                    "city_id": city_id,
                    "cells": 1,
                    "pictures_upper_bound": count,
                    "pictures_360_upper_bound": count,
                    "pictures_flat_upper_bound": 0,
                }
            ],
        )

    write(date(2026, 9, 1), 0)
    write(date(2026, 9, 8), 12)
    write(date(2026, 9, 15), 30)

    assert db.get_provider_screen_firsts(conn, "panoramax") == {city_id: "2026-09-08"}
    series = db.get_provider_screen_series(conn, "panoramax")
    assert [row["screen_date"] for row in series] == ["2026-09-01", "2026-09-08", "2026-09-15"]
    assert [row["cities_positive"] for row in series] == [0, 1, 1]


def test_a_city_positive_at_the_FIRST_screen_publishes_the_archives_start_beside_it(conn, data_dir):
    """
    A city already positive when we started looking has a first-positive date
    equal to the first screen — which records when WE arrived, not when the
    imagery did. Without the archive's own start date published beside it, every
    such city reads as an arrival the week the instrument shipped.
    """
    register(conn, "Des Moines", lat=DES_MOINES[0], lon=DES_MOINES[1])
    city_id = db.get_all_cities(conn)[0].city_id
    db.record_provider_screen(
        conn,
        provider="panoramax",
        screen_date=date(2026, 9, 9),
        rows=[
            {
                "city_id": city_id,
                "cells": 2,
                "pictures_upper_bound": 500,
                "pictures_360_upper_bound": 450,
                "pictures_flat_upper_bound": 50,
            }
        ],
    )
    doc = generate_provider_screen_summary(conn, data_dir)
    provider = doc["providers"]["panoramax"]
    assert provider["first_screen_date"] == "2026-09-09"
    assert provider["cities"][0]["first_positive_date"] == "2026-09-09"
    assert "upper bound" in provider["caveat"].lower()

    with gzip.open(os.path.join(data_dir, "provider_screen.json.gz"), "rt") as fh:
        assert json.load(fh)["schema_version"] == 1


def test_a_never_positive_city_carries_NO_first_positive_key(conn, data_dir):
    """Absent, not null — the driving-plan artifact's convention, so a consumer
    can write `if (rec.first_positive_date)` and be right."""
    register(conn, "Nowhere", lat=45.0, lon=-100.0)
    city_id = db.get_all_cities(conn)[0].city_id
    db.record_provider_screen(
        conn,
        provider="panoramax",
        screen_date=date(2026, 9, 9),
        rows=[
            {
                "city_id": city_id,
                "cells": 1,
                "pictures_upper_bound": 0,
                "pictures_360_upper_bound": 0,
                "pictures_flat_upper_bound": 0,
            }
        ],
    )
    doc = generate_provider_screen_summary(conn, data_dir)
    assert "first_positive_date" not in doc["providers"]["panoramax"]["cities"][0]


def test_an_empty_table_still_writes_the_file(conn, data_dir):
    doc = generate_provider_screen_summary(conn, data_dir)
    assert doc["providers"] == {}
    assert os.path.exists(os.path.join(data_dir, "provider_screen.json.gz"))


# ── 7. The command ─────────────────────────────────────────────────────────


def _cfg(data_dir, **overrides):
    return scheduler.SchedulerConfig(
        data_dir=data_dir,
        db_path=os.path.join(data_dir, "streetscape_tracker.db"),
        log_dir=os.path.join(data_dir, "logs"),
        **overrides,
    )


def test_an_unknown_provider_exits_64_and_screens_nothing(data_dir, conn):
    assert (
        scheduler.cmd_screen_provider(_cfg(data_dir), "panoramx", dry_run=True)
        == scheduler.USAGE_EXIT_CODE
    )


def test_limit_without_measure_exits_64_because_a_partial_screen_corrupts_the_series(
    data_dir, conn
):
    """
    The series counts cities per screen date. Screening 20 cities today and
    1,144 next week would put two different observations on one axis, and the
    published trend would read as a collapse.
    """
    assert (
        scheduler.cmd_screen_provider(_cfg(data_dir), "panoramax", limit=20)
        == scheduler.USAGE_EXIT_CODE
    )


def test_measure_without_a_limit_exits_64_because_the_whole_positive_set_is_28_hours(
    data_dir, conn
):
    assert (
        scheduler.cmd_screen_provider(_cfg(data_dir), "panoramax", measure=True)
        == scheduler.USAGE_EXIT_CODE
    )


def test_a_dry_run_prices_the_pass_and_issues_no_request(data_dir, conn, capsys, monkeypatch):
    register(conn, "Des Moines", lat=DES_MOINES[0], lon=DES_MOINES[1])
    conn.commit()

    def forbidden(*args, **kwargs):
        raise AssertionError("a dry run must not fetch")

    monkeypatch.setattr(ps, "screen_targets", forbidden)
    assert scheduler.cmd_screen_provider(_cfg(data_dir), "panoramax", dry_run=True) == 0
    assert "distinct z6 tiles" in capsys.readouterr().out


def _screened(rows):
    return {"rows": rows, "tiles": len(rows), "api_requests": len(rows), "empty_tiles": 0}


def test_a_screen_writes_the_rows_the_ledger_and_the_artifact(data_dir, conn, monkeypatch):
    register(conn, "Des Moines", lat=DES_MOINES[0], lon=DES_MOINES[1])
    conn.commit()
    city_id = db.get_all_cities(conn)[0].city_id

    monkeypatch.setattr(
        ps,
        "screen_targets",
        lambda targets, **kw: {
            "rows": [
                {
                    "city_id": city_id,
                    "display_name": "Des Moines",
                    "country_name": "United States",
                    "tiles": 1,
                    "cells": 2,
                    "pictures_upper_bound": 548_451,
                    "pictures_360_upper_bound": 545_000,
                    "pictures_flat_upper_bound": 3_451,
                }
            ],
            "tiles": 113,
            "api_requests": 113,
            "empty_tiles": 0,
        },
    )
    assert scheduler.cmd_screen_provider(_cfg(data_dir), "panoramax", publish=False) == 0

    stored = db.get_latest_provider_screen(conn, "panoramax")
    assert [row["pictures_upper_bound"] for row in stored] == [548_451]
    # The screen's requests reach the SAME (date, provider) row a collection
    # writes: same host, same IP, same day, so a budget gate that could not see
    # them would under-count our real load by exactly what nobody added.
    assert db.get_api_usage(conn, date.today(), provider="panoramax") == 113
    assert os.path.exists(os.path.join(data_dir, "provider_screen.json.gz"))


def test_a_catalog_wide_zero_is_REFUSED_when_cities_have_screened_positive_before(
    data_dir, conn, monkeypatch
):
    """
    The quiet failure the all-404 guard cannot see: a 200 whose `grid` layer has
    been renamed decodes to nothing, and every city would be written a
    conclusive zero it was never measured at. Refused BEFORE the write, because
    the write is the damage.
    """
    register(conn, "Des Moines", lat=DES_MOINES[0], lon=DES_MOINES[1])
    conn.commit()
    city_id = db.get_all_cities(conn)[0].city_id
    db.record_provider_screen(
        conn,
        provider="panoramax",
        screen_date=date(2026, 9, 1),
        rows=[
            {
                "city_id": city_id,
                "cells": 2,
                "pictures_upper_bound": 548_451,
                "pictures_360_upper_bound": 545_000,
                "pictures_flat_upper_bound": 3_451,
            }
        ],
    )

    empty_row = {
        "city_id": city_id,
        "display_name": "Des Moines",
        "country_name": "United States",
        "tiles": 1,
        "cells": 0,
        "pictures_upper_bound": 0,
        "pictures_360_upper_bound": 0,
        "pictures_flat_upper_bound": 0,
    }
    monkeypatch.setattr(ps, "screen_targets", lambda targets, **kw: _screened([empty_row]))

    assert scheduler.cmd_screen_provider(_cfg(data_dir), "panoramax", publish=False) == 1
    assert [row["screen_date"] for row in db.get_latest_provider_screen(conn, "panoramax")] == [
        "2026-09-01"
    ], "the refused pass must leave the previous screen standing"

    # ...and the operator can still record a collapse they have verified.
    assert (
        scheduler.cmd_screen_provider(
            _cfg(data_dir), "panoramax", publish=False, allow_collapse=True
        )
        == 0
    )
    assert db.get_latest_provider_screen(conn, "panoramax")[0]["pictures_upper_bound"] == 0


def test_a_first_ever_screen_finding_nothing_is_recorded_rather_than_refused(
    data_dir, conn, monkeypatch
):
    """
    The guard fires on a COLLAPSE, never on an honest empty catalog: 730 of
    1,144 cities screening zero is the measured normal case, and a first pass
    over a catalog with no Panoramax coverage at all must still record its
    zeroes — that is the observation.
    """
    register(conn, "Nowhere", lat=45.0, lon=-100.0)
    conn.commit()
    city_id = db.get_all_cities(conn)[0].city_id
    monkeypatch.setattr(
        ps,
        "screen_targets",
        lambda targets, **kw: _screened(
            [
                {
                    "city_id": city_id,
                    "display_name": "Nowhere",
                    "country_name": "United States",
                    "tiles": 1,
                    "cells": 0,
                    "pictures_upper_bound": 0,
                    "pictures_360_upper_bound": 0,
                    "pictures_flat_upper_bound": 0,
                }
            ]
        ),
    )
    assert scheduler.cmd_screen_provider(_cfg(data_dir), "panoramax", publish=False) == 0
    assert len(db.get_latest_provider_screen(conn, "panoramax")) == 1


def test_a_refusal_reports_the_HOST_exit_code_and_still_charges_what_it_spent(
    data_dir, conn, monkeypatch
):
    register(conn, "Des Moines", lat=DES_MOINES[0], lon=DES_MOINES[1])
    conn.commit()

    def refused(targets, **kwargs):
        # The attribute is set by hand HERE only because the fetch is stubbed
        # out; that production actually computes it is pinned separately, over
        # the real loop, by
        # test_a_pass_refused_MIDWAY_charges_the_requests_it_actually_sent.
        error = HostBlockedError("refused", host=HOST_PANORAMAX)
        error.api_requests = 7
        raise error

    monkeypatch.setattr(ps, "screen_targets", refused)
    rc = scheduler.cmd_screen_provider(_cfg(data_dir), "panoramax", publish=False)
    assert rc == HOST_EXIT_CODES[HOST_PANORAMAX] == 84
    # A refused pass still sent what it sent, and a ledger that forgot those
    # attempts would let the next process walk back into the same host.
    assert db.get_api_usage(conn, date.today(), provider="panoramax") == 7
    assert db.get_latest_provider_screen(conn, "panoramax") == []


def test_a_busy_host_lock_reports_85_and_writes_nothing(data_dir, conn, monkeypatch):
    register(conn, "Des Moines", lat=DES_MOINES[0], lon=DES_MOINES[1])
    conn.commit()

    def busy(targets, **kwargs):
        raise HostBusyError("busy", host=HOST_PANORAMAX)

    monkeypatch.setattr(ps, "screen_targets", busy)
    rc = scheduler.cmd_screen_provider(_cfg(data_dir), "panoramax", publish=False)
    assert rc == HOST_BUSY_EXIT_CODES[HOST_PANORAMAX] == 85
    assert db.get_latest_provider_screen(conn, "panoramax") == []


def test_the_screen_paces_at_the_channels_configured_rate_once_that_block_exists(data_dir):
    """
    One host, one IP: lowering the collection channel's pace during a block must
    lower the screen's too, or the config change leaves the hole it was closing.
    Until `[providers.panoramax]` is wired (PR 3 of #316), the collector's own
    defaults stand — which is the same 30/min.
    """
    assert scheduler._screen_pacing(_cfg(data_dir), "panoramax") == (
        ps.DEFAULT_TILE_REQUESTS_PER_MINUTE,
        ps.DEFAULT_TILE_JITTER,
    )
    configured = _cfg(
        data_dir,
        providers={"panoramax": scheduler.ProviderConfig(max_requests_per_minute=12, jitter=0.3)},
    )
    assert scheduler._screen_pacing(configured, "panoramax") == (12, 0.3)


def test_measure_needs_a_positive_screen_first_and_never_writes_to_the_catalog(
    data_dir, conn, monkeypatch
):
    register(conn, "Des Moines", lat=DES_MOINES[0], lon=DES_MOINES[1])
    conn.commit()
    city_id = db.get_all_cities(conn)[0].city_id

    assert scheduler.cmd_screen_provider(_cfg(data_dir), "panoramax", measure=True, limit=1) == 1, (
        "nothing has screened positive yet"
    )

    db.record_provider_screen(
        conn,
        provider="panoramax",
        screen_date=date(2026, 9, 9),
        rows=[
            {
                "city_id": city_id,
                "cells": 2,
                "pictures_upper_bound": 548_451,
                "pictures_360_upper_bound": 545_000,
                "pictures_flat_upper_bound": 3_451,
            }
        ],
    )
    monkeypatch.setattr(
        ps,
        "measure_targets",
        lambda targets, **kw: {
            "rows": [
                {
                    "city_id": city_id,
                    "display_name": "Des Moines",
                    "tiles": 240,
                    "cells": 1_000,
                    "pictures": 141_914,
                    "pictures_360": 135_501,
                    "pictures_flat": 6_413,
                }
            ],
            "tiles": 240,
            "api_requests": 240,
            "empty_tiles": 0,
        },
    )
    assert scheduler.cmd_screen_provider(_cfg(data_dir), "panoramax", measure=True, limit=1) == 0
    stored = db.get_latest_provider_screen(conn, "panoramax")
    assert [row["pictures_upper_bound"] for row in stored] == [548_451], (
        "the measure prints; folding its exact counts into the upper-bound series "
        "would make one column mean two instruments"
    )


def test_regenerate_aggregate_rebuilds_the_screen_artifact_too(data_dir, conn, monkeypatch):
    """
    `regenerate-aggregate` is the prescribed recovery from a stale or missing
    published set, so it has to cover EVERY published file — including one the
    nightly tail deliberately does not touch. Without this the recovery command
    would quietly leave the screen artifact behind whenever it was the file that
    went missing.
    """
    register(conn, "Des Moines", lat=DES_MOINES[0], lon=DES_MOINES[1])
    conn.commit()
    city_id = db.get_all_cities(conn)[0].city_id
    db.record_provider_screen(
        conn,
        provider="panoramax",
        screen_date=date(2026, 9, 9),
        rows=[
            {
                "city_id": city_id,
                "cells": 2,
                "pictures_upper_bound": 548_451,
                "pictures_360_upper_bound": 545_000,
                "pictures_flat_upper_bound": 3_451,
            }
        ],
    )
    summary, complete = scheduler._regenerate_published_json(conn, _cfg(data_dir))
    assert complete
    assert "provider_screen.json.gz" in summary
    assert os.path.exists(os.path.join(data_dir, "provider_screen.json.gz"))


def test_a_pass_refused_MIDWAY_charges_the_requests_it_actually_sent(monkeypatch):
    """
    Through the real fetch loop, not a hand-set attribute — which is the trap
    the collector's moved-endpoint guard fell into (#323): a test that supplies
    the value production computes exercises the one path production never takes.
    Here the block arrives on the third tile and the error must carry **3**, not
    2: the refused request was itself sent, and `_fetch_tile` counts an attempt
    before it reads the status, which is the #198 contract.
    """
    calls = {"n": 0}

    async def refuse_on_the_third(session, url, timeout, limiter, on_request, on_empty):
        calls["n"] += 1
        on_request()
        if calls["n"] == 3:
            raise HostBlockedError("refused", host=HOST_PANORAMAX)
        return b""

    monkeypatch.setattr(ps, "_fetch_tile", refuse_on_the_third)
    monkeypatch.setattr(
        ps, "plan_screen", lambda targets: ([(1, 1), (1, 2), (1, 3), (1, 4)], {"c": [(1, 1)]})
    )
    with pytest.raises(HostBlockedError) as excinfo:
        asyncio.run(ps.screen_targets_async([ps.ScreenTarget("c", "C", "US", (0, 0, 1, 1))]))
    assert excinfo.value.api_requests == 3, "the refused request was sent too"


def test_an_all_404_pass_charges_the_whole_lattice_it_paid_for(monkeypatch):
    """The moved-endpoint refusal is the screen's most expensive failure — it
    comes after every tile was requested — so the ledger must see all of it."""

    async def always_404(session, url, timeout, limiter, on_request, on_empty):
        on_request()
        on_empty()
        return b""

    monkeypatch.setattr(ps, "_fetch_tile", always_404)
    monkeypatch.setattr(ps, "plan_screen", lambda targets: ([(1, 1), (1, 2)], {"c": [(1, 1)]}))
    with pytest.raises(DownloadError) as excinfo:
        asyncio.run(ps.screen_targets_async([ps.ScreenTarget("c", "C", "US", (0, 0, 1, 1))]))
    assert excinfo.value.api_requests == 2
