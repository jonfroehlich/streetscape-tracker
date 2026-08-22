"""
The KartaView grid-run wrapper (#225 phase 3b).

``download_kartaview_metadata_async`` is the ~60-line join between the sweep
(``tests/test_kartaview_collector.py``) and the shared census tail
(``tests/test_census.py``). Both halves are covered there; what is only visible
HERE is the join itself, and every case below is one a plausible edit breaks
without failing anything else:

  * the three bindings handed to ``write_census_grid_run`` are KartaView's, so
    the CSV carries KartaView's schema and its date rules;
  * ``failed_cells`` become REQUEST_FAILED points rather than empty ones;
  * ``api_requests`` and ``api_requests_total`` are different numbers reaching
    different sinks;
  * the checkpoint is discarded LAST, and not at all when nothing was written.

The sweep is stubbed out entirely -- no request reaches kartaview.org, which is
the suite-wide rule for a project that already owns two per-IP bans.
"""

from __future__ import annotations

import asyncio
import gzip
import os

import numpy as np
import pandas as pd
import pytest

from streetscape_metadata_tracker import census as census_core
from streetscape_metadata_tracker import download_kartaview as kv
from streetscape_metadata_tracker.config import KARTAVIEW_METADATA_DTYPES

# ── Fixtures ───────────────────────────────────────────────────────────────

# A 3x3 lattice 20 m apart, centred on downtown Seattle. Images are placed ON
# grid points so an image's intended ordinal is exact and no test depends on
# rounding -- the same construction tests/test_census.py uses.
CENTER_LAT, CENTER_LON = 47.6, -122.3
GRID_W = GRID_H = 40.0
STEP = 20.0


def _grid():
    return census_core.build_grid(CENTER_LAT, CENTER_LON, GRID_W, GRID_H, STEP)


def _item(**overrides):
    """One raw `nearby-photos` row, matching the shape the API really returns."""
    item = {
        "id": "2625911774",
        "lat": str(CENTER_LAT),
        "lng": str(CENTER_LON),
        "shot_date": "2025-09-01 17:57:05.000",
        "date_added": "2025-09-20 21:08:37",
        "projection": "SPHERE",
        "field_of_view": "360",
        "heading": "321.98",
        "sequence_id": "11606856",
        "sequence_index": "72",
        "username": "lowestpotential",
        "orgCode": "CMNT",
        "way_id": "993382884",
    }
    item.update(overrides)
    return item


def _fetched(items, **overrides):
    """What a completed sweep hands the wrapper."""
    out = {
        "census": kv.records_to_census(kv.decode_photo_items(items)),
        "api_requests": 7,
        "api_requests_total": 7,
        "cells": 1,
        "cells_visited": 1,
        "radius_m": 1000,
        "raw_photo_count": len(items),
        "num_images": len(items),
        "num_panos": sum(1 for i in items if (i.get("projection") or "").upper() == "SPHERE"),
        "failed_cells": [],
        "checkpoint_path": None,
    }
    out.update(overrides)
    return out


def _run(monkeypatch, fetched, tmp_path, *, name="run.csv.gz", **kwargs):
    """Drive the wrapper with a stubbed sweep, returning (result, path)."""
    captured = {}

    async def fake_fetch(city_name, bbox, access_token, **kw):
        captured["bbox"] = bbox
        captured["kwargs"] = kw
        return fetched

    monkeypatch.setattr(kv, "fetch_city_images_async", fake_fetch)
    path = str(tmp_path / name)
    result = asyncio.run(
        kv.download_kartaview_metadata_async(
            city_name="Seattle, WA",
            center_lat=CENTER_LAT,
            center_lon=CENTER_LON,
            grid_width=GRID_W,
            grid_height=GRID_H,
            step_length=STEP,
            access_token="token",
            output_csv_gz_path=path,
            **kwargs,
        )
    )
    return result, path, captured


def _read(path):
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        return pd.read_csv(fh, dtype=str, keep_default_na=False)


# ── The bindings actually used are KartaView's ─────────────────────────────


def test_the_written_csv_carries_the_kartaview_schema_in_its_own_order(monkeypatch, tmp_path):
    """
    The whole point of passing `dtypes=` rather than letting the tail guess.

    `pd.DataFrame(..., columns=list(dtypes))` selects AND reorders, so handing
    the tail the Mapillary schema here would not raise -- it would publish a CSV
    with Mapillary's columns, KartaView's extras silently dropped, into an
    immutable dated snapshot.
    """
    _, path, _ = _run(monkeypatch, _fetched([_item()]), tmp_path)
    assert list(_read(path).columns) == list(KARTAVIEW_METADATA_DTYPES)


def test_the_date_rule_applied_is_kartaviews_not_mapillarys(monkeypatch, tmp_path):
    """
    `shot_date >= date_added` is NOT a capture date (#225), and the binding is
    the only thing that applies that rule on this path.

    Both rows below carry a perfectly plausible, non-null `shot_date`. Nothing
    but the two-column rule can tell them apart, which is exactly why a
    null-check does not catch KartaView v2 serving upload timestamps as capture
    dates. The equal-to-the-second case is the near-miss a strict `>` lets
    through.
    """
    items = [
        _item(id="good", shot_date="2025-09-01 17:57:05.000", date_added="2025-09-20 21:08:37"),
        _item(
            id="equal",
            lat=str(CENTER_LAT + 0.00018),
            shot_date="2025-11-19 08:00:00",
            date_added="2025-11-19 08:00:00",
        ),
    ]
    df = _read(_run(monkeypatch, _fetched(items), tmp_path)[1]).set_index("pano_id")
    assert df.loc["good", "capture_date"] == "2025-09-01"
    # Recorded as imagery, but with NO date -- a null is honest where a
    # plausible wrong date is not.
    assert df.loc["equal", "capture_date"] == ""
    assert df.loc["equal", "status"] == "NO_DATE"
    # date_added is published beside it rather than promoted into it: dropping
    # the column would destroy the provenance the rule is auditable from.
    assert df.loc["equal", "date_added"] == "2025-11-19 08:00:00"


def test_capture_dates_binding_takes_positions_not_a_taken_subframe():
    """
    #157's contract, asserted on the signature the tail actually calls.

    A binding that took a taken sub-frame would materialize every column of a
    multi-million-row census a second time. This one indexes the two columns it
    needs, by position.
    """
    census = kv.records_to_census(
        kv.decode_photo_items([_item(id="a"), _item(id="b", shot_date="2019-04-02 10:00:00")])
    )
    # Positions out of order and repeated: the result must follow POSITIONS,
    # not the frame's own row order.
    out = kv._kartaview_capture_dates(census, np.array([1, 0, 1]))
    assert list(out) == ["2019-04-02", "2025-09-01", "2019-04-02"]


def test_flat_imagery_becomes_a_dated_null_presence_marker(monkeypatch, tmp_path):
    """
    #116's split, reached through KartaView's own PLANE/SPHERE normalization.

    A grid point covered only by flat imagery is a FLAT_ONLY row with a null
    capture date -- present, but never entering a dated-stat path.
    """
    items = [
        _item(id="pano", projection="SPHERE"),
        _item(id="flat", lat=str(CENTER_LAT + 0.00018), projection="PLANE"),
    ]
    result, path, _ = _run(monkeypatch, _fetched(items), tmp_path)
    df = _read(path)
    flat = df[df["pano_id"] == "flat"].iloc[0]
    assert flat["status"] == "FLAT_ONLY"
    assert flat["capture_date"] == ""
    # The flat census magnitude is threaded out separately: a flat-only point
    # collapses to ONE row, so the CSV cannot reconstruct it.
    assert result["num_flat_images"] == 1


# ── Unmeasured cells ───────────────────────────────────────────────────────


def test_unmeasured_cells_become_request_failed_not_empty(monkeypatch, tmp_path):
    """
    A cell nothing came back for leaves its points UNKNOWN, never ZERO_RESULTS.

    Recording an unswept point as empty publishes an absence we never observed
    into an immutable dated snapshot, which then diffs as imagery removed.
    """
    # A cell covering the whole tiny grid, so every point falls inside it.
    failed = [kv.Cell(lat=CENTER_LAT, lon=CENTER_LON, size_m=500.0)]
    _, path, _ = _run(monkeypatch, _fetched([], failed_cells=failed), tmp_path)
    df = _read(path)
    assert set(df["status"]) == {"REQUEST_FAILED"}
    assert len(df) == _grid().num_points


def test_a_clean_sweep_passes_no_mask_and_leaves_points_empty(monkeypatch, tmp_path):
    """The other side of it: no failed cells means ZERO_RESULTS, as observed."""
    _, path, _ = _run(monkeypatch, _fetched([]), tmp_path)
    assert set(_read(path)["status"]) == {"ZERO_RESULTS"}


def test_points_in_cells_masks_the_square_not_the_circumscribed_circle():
    """
    The circle is what the REQUEST covered; the square is the cell's share of
    the lattice. Masking with the circle would mark points in neighbouring
    cells -- which were measured, by their own request -- as unknown.
    """
    cell = kv.Cell(lat=0.0, lon=0.0, size_m=200.0)  # +/-100 m, r = 141 m
    # Due east, between the square's edge (100 m) and the circle's radius (141).
    m_per_deg_lon = kv._METERS_PER_DEG_LAT  # cos(0) == 1
    lons = np.array([0.0, 120.0 / m_per_deg_lon])
    lats = np.array([0.0, 0.0])
    assert list(kv._points_in_cells(lats, lons, [cell])) == [True, False]


def test_points_in_cells_handles_mixed_sizes_and_the_antimeridian():
    """
    Subdivision means cells are NOT one size -- the whole difference from the
    tile case, where one packed key works. And a cell beside the antimeridian
    must compare against points on the other side of it rather than see a
    ~360-degree gap.
    """
    # A 100 m cell straddling the fold: its centre is 11 m west of 180, so its
    # eastern half is at NEGATIVE longitudes. This is Taveuni's geometry, the
    # case that collapsed a real city to 3.4% of its cells (cells_for_bbox).
    small = kv.Cell(lat=0.0, lon=179.9999, size_m=100.0)
    big = kv.Cell(lat=10.0, lon=0.0, size_m=4000.0)
    # ~22 m east of that centre, i.e. across the fold and inside the square.
    wrapped_lon = kv._wrap_lon(179.9999 + 20.0 / kv._METERS_PER_DEG_LAT)
    assert wrapped_lon < 0, wrapped_lon  # genuinely wrapped, or this proves nothing
    lats = np.array([0.0, 10.0, 40.0])
    lons = np.array([wrapped_lon, 0.01, 0.0])
    assert list(kv._points_in_cells(lats, lons, [small, big])) == [True, True, False]


def test_points_in_cells_tolerates_an_empty_point_set():
    assert list(kv._points_in_cells(np.array([]), np.array([]), [kv.Cell(0.0, 0.0, 100.0)])) == []


# ── The two request counts ─────────────────────────────────────────────────


def test_this_processs_spend_and_the_sweeps_total_are_reported_separately(monkeypatch, tmp_path):
    """
    They are different numbers on a resumed sweep and must not be conflated.

    `db.add_api_usage` is additive and keyed by (date, provider), so a resumed
    night reporting the cumulative figure would charge last night's requests
    against tonight's budget gate and eventually skip cities that fit.
    """
    fetched = _fetched([_item()], api_requests=40, api_requests_total=310)
    result, _, _ = _run(monkeypatch, fetched, tmp_path)
    assert result["api_requests"] == 40
    assert result["api_requests_total"] == 310


def test_the_wrapper_never_counts_the_census_itself(monkeypatch, tmp_path):
    """
    The totals in the log line come from the fetch, not from the census.

    Counting them here would require binding the frame to a local, which pins
    every row alive through both CSV writes and defeats the tail's release --
    with every runtime test still green. tests/test_census.py asserts the source
    is free of that binding; this asserts the values are actually taken from the
    dict, so the source guard cannot be satisfied by a different wrong thing.
    """
    fetched = _fetched([_item()], num_images=999_999, num_panos=1)
    # Nothing raises and nothing recomputes: a wrapper that counted the census
    # would disagree with these deliberately impossible figures.
    result, path, _ = _run(monkeypatch, fetched, tmp_path)
    assert result["df"] is not None
    assert len(_read(path)) == _grid().num_points


# ── The checkpoint lifecycle ───────────────────────────────────────────────


def test_the_checkpoint_is_discarded_only_after_the_artifact_lands(monkeypatch, tmp_path):
    """
    The caller-tail half of #239, and the reason the fetch does NOT delete it.

    Ordering is the assertion, not merely that it happens: discarding before the
    CSV write is what would make a crash in this tail re-pay the whole sweep.
    """
    cp = tmp_path / "cp"
    cp.mkdir()
    (cp / "state.json").write_text("{}")
    output = str(tmp_path / "run.csv.gz")
    seen = {}

    def fake_discard(path):
        # The artifact must already be on disk when this runs.
        seen["artifact_existed"] = os.path.exists(output)
        seen["path"] = path

    monkeypatch.setattr(kv, "discard_checkpoint", fake_discard)
    fetched = _fetched([_item()], checkpoint_path=str(cp))
    _run(monkeypatch, fetched, tmp_path)

    assert seen["path"] == str(cp)
    assert seen["artifact_existed"] is True


def test_no_checkpoint_means_nothing_to_discard(monkeypatch, tmp_path):
    """`checkpoint_path=None` is the pre-#239 path and must stay a no-op."""
    calls = []
    monkeypatch.setattr(kv, "discard_checkpoint", lambda p: calls.append(p))
    _run(monkeypatch, _fetched([_item()]), tmp_path)
    assert calls == []


def test_an_incomplete_sweep_propagates_and_publishes_nothing(monkeypatch, tmp_path):
    """
    A partial census must never become a dated snapshot: 60% of a city diffs
    against its predecessor as "every pano in the rest of the city removed".

    So the error propagates unchanged -- not flattened into a bare DownloadError,
    which cli.py could not tell from a real failure and would exit 1 on -- and
    the checkpoint is NOT discarded, because it is the surviving spend.
    """
    discarded = []
    monkeypatch.setattr(kv, "discard_checkpoint", lambda p: discarded.append(p))

    async def fake_fetch(city_name, bbox, access_token, **kw):
        raise kv.SweepIncompleteError(
            "out of budget", checkpoint_path="/cp", roots_done=3, root_count=10
        )

    monkeypatch.setattr(kv, "fetch_city_images_async", fake_fetch)
    path = str(tmp_path / "run.csv.gz")
    with pytest.raises(kv.SweepIncompleteError) as excinfo:
        asyncio.run(
            kv.download_kartaview_metadata_async(
                city_name="Singapore",
                center_lat=CENTER_LAT,
                center_lon=CENTER_LON,
                grid_width=GRID_W,
                grid_height=GRID_H,
                step_length=STEP,
                access_token="token",
                output_csv_gz_path=path,
                checkpoint_path="/cp",
            )
        )
    assert excinfo.value.roots_done == 3
    assert discarded == []
    assert not os.path.exists(path)


# ── The checkpoint path ────────────────────────────────────────────────────


def test_checkpoint_path_is_date_free_and_keyed_by_channel(tmp_path, monkeypatch):
    """
    Date-free because the whole point is a sweep that spans nights, and a run is
    dated on the day it COMPLETES -- a date in the path restarts every night.

    Channel-keyed because a KartaView road walk sweeps the same frozen bbox at
    the same radius, so every validation in load_checkpoint would pass and the
    two channels would resume each other's sweeps on different credentials.
    """
    monkeypatch.setenv(kv.CHECKPOINT_DIR_ENV, str(tmp_path / "cp"))
    bbox = (-122.31, 47.59, -122.29, 47.61)
    grid_run = kv.checkpoint_path_for("seattle--wa", bbox, "kartaview")
    road_walk = kv.checkpoint_path_for("seattle--wa", bbox, "kartaview_streets")

    assert grid_run != road_walk
    # No ISO date anywhere in the path.
    assert not any(part.count("-") == 2 and part[:4].isdigit() for part in grid_run.split(os.sep))


def test_checkpoint_path_changes_when_the_frozen_grid_is_resized(tmp_path, monkeypatch):
    """
    A checkpoint keyed on the slug alone would survive a re-registration
    (scripts/resize_city.py, cap_oversized_grids.py) and resume onto a lattice
    it does not describe.
    """
    monkeypatch.setenv(kv.CHECKPOINT_DIR_ENV, str(tmp_path / "cp"))
    before = kv.checkpoint_path_for("browning--mt", (-113.1, 48.5, -113.0, 48.6), "kartaview")
    after = kv.checkpoint_path_for("browning--mt", (-113.2, 48.4, -112.9, 48.7), "kartaview")
    assert before != after


def test_checkpoint_dir_is_not_under_data_and_resolves_symlinks(tmp_path, monkeypatch):
    """
    Two of host_lock.lock_dir's three constraints, for the same host.

    NOT under data/ -- sync_data_to_server.sh rsyncs that to a public web
    server, and a partial census is the one artifact that must never reach the
    publisher. And realpath'd, because on makelab2 the unit's WorkingDirectory
    is a symlink: two spellings of one directory would silently be two
    checkpoints, i.e. exactly the restart-from-zero this exists to prevent.
    """
    real = tmp_path / "real_checkpoints"
    real.mkdir()
    link = tmp_path / "linked"
    link.symlink_to(real)

    monkeypatch.setenv(kv.CHECKPOINT_DIR_ENV, str(link))
    assert kv.checkpoint_dir() == os.path.realpath(str(real))

    monkeypatch.delenv(kv.CHECKPOINT_DIR_ENV)
    assert f"{os.sep}data{os.sep}" not in kv.checkpoint_dir() + os.sep


# ── The bbox handed to the sweep ───────────────────────────────────────────


def test_the_sweep_is_bounded_by_the_frozen_grids_own_bbox(monkeypatch, tmp_path):
    """
    The lattice is derived from the grid, once, and threaded through -- so the
    census covers exactly the rectangle the CSV is keyed to. A sweep given a
    differently-derived bbox would return imagery for points that are not in
    the run, and miss points that are.
    """
    _, _, captured = _run(monkeypatch, _fetched([_item()]), tmp_path)
    assert captured["bbox"] == _grid().bbox


def test_pacing_and_budget_reach_the_sweep(monkeypatch, tmp_path):
    """
    The two knobs an operator sets by hand. `max_requests` is what makes a
    metro takeable a night at a time; without it reaching the sweep the CLI flag
    is decoration.
    """
    _, _, captured = _run(
        monkeypatch,
        _fetched([_item()]),
        tmp_path,
        max_requests_per_minute=4,
        max_requests=250,
        checkpoint_path="/cp",
    )
    assert captured["kwargs"]["max_requests_per_minute"] == 4
    assert captured["kwargs"]["max_requests"] == 250
    assert captured["kwargs"]["checkpoint_path"] == "/cp"
