"""
What a Panoramax screen CELL is, measured rather than written down (#406).

The published `provider_screen.json.gz` described a screen cell as the literal
"H3 resolution 6 (~36 km²)"; on 2026-10-01 every one of the 261,913 hexagon ids
the z6 layer served decoded to resolution 7 (~5.2 km²)
(`docs/experiments/panoramax-world-screen.md`). These pin, in order:

1. **The decode** — `h3_cell_resolution` reads bits 52-55 of the 64-bit H3
   index, and refuses an id whose mode, reserved bits, base cell or digit run
   say it is not an H3 cell. Every id below was checked against the reference
   `h3` library (v4.5.0, `is_valid_cell` / `get_resolution`), which is
   deliberately NOT a dependency.
2. **The pass** — a screen pass returns the histogram of distinct ids it read,
   and WARNS (never refuses) on anything but the expected resolution.
3. **The catalog and the artifact** — the histogram is stored per screen date,
   and the artifact's `cell` is derived from it, with a `cell_warning` present
   only when the pass saw something unexpected; the caveat states the real
   reason the figures are upper bounds.

Kept apart from `test_panoramax_screen.py` so two branches editing that file's
refusal tests do not collide with these.
"""

from __future__ import annotations

import asyncio
import gzip
import json
import logging
import os
from datetime import UTC, date, datetime

import aiohttp
import pytest

from streetscape_metadata_tracker import db, scheduler
from streetscape_metadata_tracker import panoramax_screen as ps
from streetscape_metadata_tracker.json_summarizer import (
    _SCREEN_CAVEAT,
    generate_provider_screen_summary,
)
from tests.test_panoramax_screen import (
    DES_MOINES,
    _AsyncCM,
    _cfg,
    _FakeResponse,
    _FakeSession,
    counters,
    encode_grid_tile,
    register,
    tile_url,
)

# ── 1. The decode ──────────────────────────────────────────────────────────

# (id, resolution), each verified with h3 4.5.0: is_valid_cell True and
# get_resolution equal to the number given.
KNOWN_CELLS = [
    ("8001fffffffffff", 0),  # base cell 0, the first res-0 cell
    ("85283473fffffff", 5),  # the H3 documentation's own example
    ("85283083fffffff", 5),  # res-5 parent of the San Francisco cell below
    ("86283082fffffff", 6),  # its res-6 parent
    ("870800000ffffff", 7),  # a res-7 PENTAGON (h3.get_pentagons(7)[0])
    ("871fb4662ffffff", 7),  # Paris, latlng_to_cell(48.8566, 2.3522, 7)
    ("87260d876ffffff", 7),  # Des Moines, latlng_to_cell(41.59, -93.62, 7)
    ("872830828ffffff", 7),  # San Francisco's res-7 parent
    ("8728dc65dffffff", 7),  # the example in PR #409's analysis script
    ("87be0e35cffffff", 7),  # Sydney, latlng_to_cell(-33.87, 151.21, 7)
    ("8828308281fffff", 8),
    ("8928308280fffff", 9),  # latlng_to_cell(37.7752702151959, -122.418307270836, 9)
    ("8f2830828052d25", 15),  # the finest resolution, no unused digits at all
]


@pytest.mark.parametrize(("hex_id", "resolution"), KNOWN_CELLS)
def test_a_known_H3_cell_decodes_to_its_published_resolution(hex_id, resolution):
    assert ps.h3_cell_resolution(hex_id) == resolution


# Each is h3.is_valid_cell False (or unparseable) in the reference library, and
# each is one field of a real res-7 cell (872830828ffffff) broken.
NOT_CELLS = [
    ("11928308280fffff", "a directed EDGE (mode 2) -- its resolution bits still read 9"),
    ("229283082803ffff", "a VERTEX (mode 4)"),
    # The edge and vertex above also set the mode-dependent bits, so these two
    # break ONLY the mode -- without them a missing mode check goes unseen.
    ("72830828ffffff", "mode 0, every other field a valid res-7 cell's"),
    ("1072830828ffffff", "mode 2, every other field a valid res-7 cell's"),
    ("8872830828ffffff", "the reserved high bit set"),
    ("972830828ffffff", "a mode-dependent reserved bit set"),
    ("87f430828ffffff", "base cell 122, one past the last"),
    ("8728308287fffff", "an unused digit (7) INSIDE the resolution"),
    ("8728308280fffff", "a used digit PAST the resolution"),
    ("h1", "not hexadecimal at all"),
    ("", "empty"),
    ("-872830828ffffff", "negative"),
]


@pytest.mark.parametrize(("hex_id", "why"), NOT_CELLS)
def test_an_id_that_is_not_an_H3_cell_decodes_to_None_not_to_a_resolution(hex_id, why):
    """Four bits read off ANY hex string come out 0-15, so the shift alone would
    'measure' a resolution for a layer that had switched id scheme entirely."""
    assert ps.h3_cell_resolution(hex_id) is None, why


def test_the_area_table_is_the_published_H3_one():
    """Pinned to https://h3geo.org/docs/core-library/restable, the source the
    artifact cites: resolution 6 is ~36.13 km² and 7 is ~5.16 km²."""
    assert ps.H3_AVERAGE_HEX_AREA_KM2[6] == 36.129062164
    assert ps.H3_AVERAGE_HEX_AREA_KM2[7] == 5.161293360
    assert sorted(ps.H3_AVERAGE_HEX_AREA_KM2) == list(range(16))
    areas = [ps.H3_AVERAGE_HEX_AREA_KM2[r] for r in range(16)]
    # Each resolution is ~1/7 the area of the one above (aperture 7; res 0->1 is 7.15).
    assert all(6.9 < a / b < 7.2 for a, b in zip(areas, areas[1:], strict=False))


def test_the_histogram_counts_DISTINCT_ids_and_keeps_the_unrecognised():
    """A seam hexagon arrives once per tile and is still one hexagon; an id that
    is not an H3 cell is counted under None rather than dropped."""
    ids = ["872830828ffffff", "872830828ffffff", "871fb4662ffffff", "86283082fffffff", "h1"]
    assert ps.cell_resolution_counts(ids) == {7: 2, 6: 1, None: 1}


def test_only_the_single_expected_resolution_is_unremarkable():
    assert ps.EXPECTED_SCREEN_H3_RESOLUTION == 7
    assert ps.unexpected_cell_resolutions({7: 261_913}) is None
    assert ps.unexpected_cell_resolutions({}) is None  # the layer guard owns "nothing"
    assert (
        ps.unexpected_cell_resolutions({6: 3, 7: 10})
        == "mixed H3 resolutions (6: 3 hexagons, 7: 10 hexagons); expected only 7"
    )
    assert ps.unexpected_cell_resolutions({6: 1}) == (
        "H3 resolution (6: 1 hexagon); expected only 7"
    )
    assert "not an H3 cell: 2 hexagons" in ps.unexpected_cell_resolutions({7: 5, None: 2})
    assert ps.unexpected_cell_resolutions({None: 4}).startswith("no H3 cell ids")


# ── 2. The pass ────────────────────────────────────────────────────────────


def _pass(monkeypatch, hexes):
    """One screen pass over Des Moines' single z6 tile, carrying `hexes`."""
    target = ps.ScreenTarget("dm", "Des Moines", "United States", (-93.70, 41.55, -93.60, 41.62))
    (x, y) = ps.screen_tiles_for_city(target.bbox)[0]
    session = _FakeSession({tile_url(x, y): _FakeResponse(200, encode_grid_tile(hexes, x, y))})
    monkeypatch.setattr(aiohttp, "ClientSession", lambda **kw: _AsyncCM(session))
    return asyncio.run(ps.screen_targets_async([target]))


def test_a_pass_returns_the_resolution_of_every_hexagon_it_READ(monkeypatch, caplog):
    """Every decoded hexagon counts, not only the ones a city selected: the
    histogram describes the layer, and the far hexagon is part of it."""
    with caplog.at_level(logging.WARNING, logger=ps.__name__):
        result = _pass(
            monkeypatch,
            [
                ("87260d876ffffff", (-93.70, 41.55, -93.60, 41.62), counters(5, 5, 0)),
                ("872830828ffffff", (-90.5, 40.0, -90.4, 40.1), counters(1, 0, 1)),
            ],
        )
    assert result["cell_resolutions"] == {7: 2}
    assert result["rows"][0]["cells"] == 1
    assert not [r for r in caplog.records if "resolution" in r.getMessage()]


def test_a_MIXED_pass_is_warned_about_and_still_screens(monkeypatch, caplog):
    """Warned, never refused: overlap selection and whole-hexagon counting keep
    every figure an upper bound at any resolution, so refusing would cost a
    week of an un-backfillable series to protect a number the artifact can
    simply describe."""
    with caplog.at_level(logging.WARNING, logger=ps.__name__):
        result = _pass(
            monkeypatch,
            [
                ("87260d876ffffff", (-93.70, 41.55, -93.60, 41.62), counters(5, 5, 0)),
                ("86283082fffffff", (-93.65, 41.56, -93.62, 41.60), counters(9, 0, 9)),
            ],
        )
    assert result["cell_resolutions"] == {7: 1, 6: 1}
    assert result["rows"][0]["pictures_upper_bound"] == 14
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("mixed H3 resolutions (6: 1 hexagon, 7: 1 hexagon)" in m for m in warnings)


# ── 3. The catalog and the artifact ────────────────────────────────────────


def _row(city_id, upper=10):
    return {
        "city_id": city_id,
        "cells": 1,
        "pictures_upper_bound": upper,
        "pictures_360_upper_bound": upper,
        "pictures_flat_upper_bound": 0,
    }


def _instrument(conn, data_dir):
    doc = generate_provider_screen_summary(conn, data_dir)
    return doc["providers"]["panoramax"]["instrument"], doc


def test_the_published_cell_is_DERIVED_from_the_measured_resolution(conn, data_dir):
    register(conn, "Des Moines", lat=DES_MOINES[0], lon=DES_MOINES[1])
    city_id = db.get_all_cities(conn)[0].city_id
    db.record_provider_screen(
        conn,
        provider="panoramax",
        screen_date=date(2026, 10, 8),
        rows=[_row(city_id)],
        cell_resolutions={7: 261_913},
    )
    instrument, doc = _instrument(conn, data_dir)
    assert instrument["cell"] == "H3 resolution 7 (~5.16 km² average)"
    assert instrument["cell_resolutions"] == [
        {"resolution": 7, "hexagons": 261_913, "average_area_km2": 5.161293360}
    ]
    assert instrument["cell_area_source"] == "https://h3geo.org/docs/core-library/restable"
    assert "cell_warning" not in instrument  # absent, not null
    assert doc["providers"]["panoramax"]["series"][0]["cell_resolutions"] == [
        {"resolution": 7, "hexagons": 261_913, "average_area_km2": 5.161293360}
    ]
    # Additive within v1: `cell` is still a string, and nothing else moved.
    with gzip.open(os.path.join(data_dir, "provider_screen.json.gz"), "rt") as fh:
        assert json.load(fh)["schema_version"] == 1


def test_a_MIXED_or_unrecognised_screen_says_so_in_the_artifact(conn, data_dir):
    register(conn, "Des Moines", lat=DES_MOINES[0], lon=DES_MOINES[1])
    city_id = db.get_all_cities(conn)[0].city_id
    db.record_provider_screen(
        conn,
        provider="panoramax",
        screen_date=date(2026, 10, 8),
        rows=[_row(city_id)],
        cell_resolutions={7: 900, 6: 12, None: 3},
    )
    instrument, _ = _instrument(conn, data_dir)
    assert instrument["cell"] == (
        "Mixed: H3 resolution 7 (~5.16 km² average) for 900 hexagons; "
        "H3 resolution 6 (~36.1 km² average) for 12 hexagons; "
        "3 hexagons whose ids are not H3 cells"
    )
    assert [c["resolution"] for c in instrument["cell_resolutions"]] == [7, 6, None]
    assert instrument["cell_resolutions"][2]["average_area_km2"] is None
    assert "not comparable" in instrument["cell_warning"]
    assert "6: 12 hexagons" in instrument["cell_warning"]
    assert "not an H3 cell: 3 hexagons" in instrument["cell_warning"]


def test_a_screen_from_BEFORE_the_measurement_says_not_recorded_rather_than_guessing(
    conn, data_dir
):
    """No cells row for the date: the artifact says the resolution was not
    recorded. It does not fall back to any constant -- a constant is how the
    published description came to be wrong."""
    register(conn, "Des Moines", lat=DES_MOINES[0], lon=DES_MOINES[1])
    city_id = db.get_all_cities(conn)[0].city_id
    db.record_provider_screen(
        conn, provider="panoramax", screen_date=date(2026, 9, 9), rows=[_row(city_id)]
    )
    instrument, doc = _instrument(conn, data_dir)
    assert instrument["cell"] == (
        "H3 hexagons; resolution not recorded for the screen of 2026-09-09 "
        "(it predates per-pass measurement)"
    )
    assert instrument["cell_resolutions"] is None
    assert "cell_area_source" not in instrument
    assert "cell_warning" not in instrument
    assert doc["providers"]["panoramax"]["series"][0]["cell_resolutions"] is None


def test_the_instrument_describes_the_LATEST_screen_and_the_series_each_date(conn, data_dir):
    register(conn, "Des Moines", lat=DES_MOINES[0], lon=DES_MOINES[1])
    city_id = db.get_all_cities(conn)[0].city_id
    db.record_provider_screen(
        conn,
        provider="panoramax",
        screen_date=date(2026, 10, 1),
        rows=[_row(city_id)],
        cell_resolutions={6: 40},
    )
    db.record_provider_screen(
        conn,
        provider="panoramax",
        screen_date=date(2026, 10, 8),
        rows=[_row(city_id)],
        cell_resolutions={7: 280},
    )
    instrument, doc = _instrument(conn, data_dir)
    assert instrument["cell"].startswith("H3 resolution 7 ")
    assert "cell_warning" not in instrument
    series = doc["providers"]["panoramax"]["series"]
    assert [[c["resolution"] for c in p["cell_resolutions"]] for p in series] == [[6], [7]]


def test_a_same_day_rerun_REPLACES_the_dates_cells_and_an_unmeasured_write_keeps_them(conn):
    """A morning pass that saw resolution 6 must not survive an afternoon pass
    that saw only 7 -- an upsert keyed on resolution would keep it. And a write
    that measured nothing must not erase a measurement."""
    register(conn, "Des Moines", lat=DES_MOINES[0], lon=DES_MOINES[1])
    city_id = db.get_all_cities(conn)[0].city_id
    day = date(2026, 10, 8)
    db.record_provider_screen(
        conn,
        provider="panoramax",
        screen_date=day,
        rows=[_row(city_id)],
        cell_resolutions={6: 3, 7: 10},
    )
    db.record_provider_screen(
        conn, provider="panoramax", screen_date=day, rows=[_row(city_id)], cell_resolutions={7: 11}
    )
    assert db.get_provider_screen_cells(conn, "panoramax") == {"2026-10-08": {7: 11}}
    db.record_provider_screen(conn, provider="panoramax", screen_date=day, rows=[_row(city_id)])
    assert db.get_provider_screen_cells(conn, "panoramax") == {"2026-10-08": {7: 11}}


def _screen_returning(monkeypatch, city_id, cell_resolutions):
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
                    "pictures_upper_bound": 7,
                    "pictures_360_upper_bound": 7,
                    "pictures_flat_upper_bound": 0,
                }
            ],
            "tiles": 1,
            "api_requests": 1,
            "empty_tiles": 0,
            "cell_resolutions": cell_resolutions,
        },
    )


def test_the_command_RECORDS_the_passs_histogram_and_publishes_it(
    data_dir, conn, monkeypatch, capsys, frozen_utc_clock
):
    """The pass-through from `screen_targets` to the catalog to the artifact --
    the default (no cells) would also produce a valid artifact, so only the
    measured value arriving end to end proves the wire is connected."""
    frozen_utc_clock(datetime(2026, 10, 8, 15, tzinfo=UTC))
    register(conn, "Des Moines", lat=DES_MOINES[0], lon=DES_MOINES[1])
    conn.commit()
    city_id = db.get_all_cities(conn)[0].city_id
    _screen_returning(monkeypatch, city_id, {7: 1234})

    assert scheduler.cmd_screen_provider(_cfg(data_dir), "panoramax", publish=False) == 0
    assert db.get_provider_screen_cells(conn, "panoramax") == {"2026-10-08": {7: 1234}}
    with gzip.open(os.path.join(data_dir, "provider_screen.json.gz"), "rt") as fh:
        instrument = json.load(fh)["providers"]["panoramax"]["instrument"]
    assert instrument["cell"] == "H3 resolution 7 (~5.16 km² average)"
    assert "WARNING" not in capsys.readouterr().out


def test_the_command_WARNS_the_operator_on_a_mixed_pass_and_still_records(
    data_dir, conn, monkeypatch, capsys, frozen_utc_clock
):
    frozen_utc_clock(datetime(2026, 10, 8, 15, tzinfo=UTC))
    register(conn, "Des Moines", lat=DES_MOINES[0], lon=DES_MOINES[1])
    conn.commit()
    city_id = db.get_all_cities(conn)[0].city_id
    _screen_returning(monkeypatch, city_id, {6: 2, 7: 1234})

    assert scheduler.cmd_screen_provider(_cfg(data_dir), "panoramax", publish=False) == 0
    out = capsys.readouterr().out
    assert "WARNING: screen hexagons were mixed H3 resolutions (6: 2 hexagons" in out
    assert [
        row["pictures_upper_bound"] for row in db.get_latest_provider_screen(conn, "panoramax")
    ] == [7]
    with gzip.open(os.path.join(data_dir, "provider_screen.json.gz"), "rt") as fh:
        assert "cell_warning" in json.load(fh)["providers"]["panoramax"]["instrument"]


# ── The caveat ─────────────────────────────────────────────────────────────


def test_the_caveat_states_the_MECHANISM_not_a_size_comparison():
    """The old caveat said the cells were LARGER than the city -- true only for
    the res-6 figure nobody had read off an id. The bound rests on overlap
    selection and whole-cell counting, which hold at any resolution, and the
    zero is conclusive about the bounding box."""
    text = _SCREEN_CAVEAT
    assert text.startswith("Upper bounds, not counts.")
    assert "overlaps the city's bounding box" in text
    assert "counts each such cell whole" in text
    assert "straddling the box's edge" in text
    assert "rectangle around the city, not its boundary" in text
    assert "never under-count the box" in text
    assert "A zero is conclusive" in text
    assert "LARGER" not in text and "larger than" not in text.lower()
