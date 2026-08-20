"""
The provider-agnostic census machinery (streetscape_metadata_tracker/census.py).

``tests/fixtures/mapillary_golden_run.csv`` already pins that Mapillary's output
is byte-identical, but it can only see ONE provider's binding. What it cannot
see is whether the module is actually parameterised or merely still
Mapillary-shaped with a schema argument bolted on -- and that is the whole
reason it was extracted, because a second census provider that has to fork this
code loses #157's three contracts (columnar, two-rule dedup, byte-stable CSV) on
the day it forks.

So these tests drive the generic layer with a DIFFERENT schema from Mapillary's
and assert the properties hold there too.
"""

import ast
import inspect
import textwrap

import numpy as np
import pandas as pd
import pytest

from streetscape_metadata_tracker import census
from streetscape_metadata_tracker import download_mapillary as dm
from streetscape_metadata_tracker.analysis import FLAT_ONLY, REQUEST_FAILED

# A deliberately un-Mapillary-like schema: different column names, a different
# count, a different order, and no `is_pano`/`captured_at_ms` at all.
OTHER_CENSUS_DTYPES = {
    "id": pd.StringDtype("pyarrow"),
    "lon": "float64",
    "lat": "float64",
    "shot_date": pd.StringDtype("pyarrow"),
    "sequence_index": "Int64",
}

OTHER_OUTPUT_DTYPES = {
    "query_lat": "float64",
    "query_lon": "float64",
    "query_timestamp": "object",
    "pano_lat": "float64",
    "pano_lon": "float64",
    "pano_id": "object",
    "capture_date": "object",
    "copyright_info": "object",
    "status": "object",
    "sequence_index": "object",
}


def _other_image_columns(picked):
    return {
        "copyright_info": ("© Somewhere " + picked["id"].astype("string"))
        .fillna("© Somewhere")
        .to_numpy(dtype=object),
        "sequence_index": picked["sequence_index"].astype(object).to_numpy(),
    }


def _record(id_, lat, lon, idx=0, shot="2020-01-01"):
    return {"id": id_, "lat": lat, "lon": lon, "shot_date": shot, "sequence_index": idx}


# ── the schema is genuinely a parameter ────────────────────────────────────


def test_a_non_mapillary_schema_round_trips_columns_and_dtypes():
    frame = census.records_to_census([_record("a", 1.0, 2.0, 7)], OTHER_CENSUS_DTYPES)
    assert list(frame.columns) == list(OTHER_CENSUS_DTYPES)
    assert frame["sequence_index"].dtype == "Int64"
    assert frame["id"].iloc[0] == "a"


def test_an_empty_census_still_has_the_schemas_columns_and_dtypes():
    """A fetch that found nothing must concat with one that did."""
    empty = census.records_to_census([], OTHER_CENSUS_DTYPES)
    assert len(empty) == 0
    assert list(empty.columns) == list(OTHER_CENSUS_DTYPES)
    full = census.records_to_census([_record("a", 1.0, 2.0)], OTHER_CENSUS_DTYPES)
    assert len(census.concat_census([empty, full], OTHER_CENSUS_DTYPES)) == 1


def test_concat_of_nothing_at_all_is_an_empty_frame_not_a_crash():
    out = census.concat_census([], OTHER_CENSUS_DTYPES)
    assert len(out) == 0
    assert list(out.columns) == list(OTHER_CENSUS_DTYPES)


def test_output_columns_come_out_in_the_schemas_order():
    """
    The schema's key order IS the CSV's column order, and a run file is an
    immutable dated snapshot -- so this is a contract, not a formatting detail.
    """
    frame = census.records_to_census(
        [_record("a", 1.0, 2.0, 7), _record("b", 3.0, 4.0, 8)], OTHER_CENSUS_DTYPES
    )
    rows = census.build_image_rows(
        frame,
        np.array([0, 1]),
        [10.0, 11.0],
        [20.0, 21.0],
        "T",
        "OK",
        ["2020-01-01", "2020-01-02"],
        dtypes=OTHER_OUTPUT_DTYPES,
        image_columns=_other_image_columns,
    )
    assert list(rows.columns) == list(OTHER_OUTPUT_DTYPES)
    assert rows["copyright_info"].tolist() == ["© Somewhere a", "© Somewhere b"]
    assert rows["pano_id"].tolist() == ["a", "b"]


def test_a_binding_that_skips_a_declared_column_is_refused_not_published():
    """
    The all-null column is the failure this raises to prevent. `columns=` on
    the DataFrame constructor selects and reorders, so a column the binding
    forgot is created full of nulls and SHIPS -- into an immutable dated
    snapshot, silently. #225 publishes `date_added` as its own column precisely
    so a null capture_date keeps its provenance; an all-null one destroys it.
    """
    frame = census.records_to_census([_record("a", 1.0, 2.0, 7)], OTHER_CENSUS_DTYPES)
    with pytest.raises(ValueError, match="missing.*sequence_index"):
        census.build_image_rows(
            frame,
            np.array([0]),
            [10.0],
            [20.0],
            "T",
            "OK",
            ["2020-01-01"],
            dtypes=OTHER_OUTPUT_DTYPES,
            # copyright_info only -- sequence_index quietly dropped.
            image_columns=lambda picked: {"copyright_info": np.array(["© Somewhere"])},
        )


def test_a_mistyped_binding_key_is_refused_rather_than_dropped():
    """A typo is the realistic form of the above: the intended column is null
    and the misspelling leaves no trace at all."""
    frame = census.records_to_census([_record("a", 1.0, 2.0, 7)], OTHER_CENSUS_DTYPES)
    with pytest.raises(ValueError, match="unexpected.*sequence_indexx"):
        census.build_image_rows(
            frame,
            np.array([0]),
            [10.0],
            [20.0],
            "T",
            "OK",
            ["2020-01-01"],
            dtypes=OTHER_OUTPUT_DTYPES,
            image_columns=lambda picked: {
                "copyright_info": np.array(["© Somewhere"]),
                "sequence_indexx": np.array([7], dtype=object),
            },
        )


def test_a_binding_cannot_silently_overwrite_a_core_column():
    """
    The provider's dict is splatted LAST, so without this check a binding
    returning `pano_lat` would win over the census's own coordinate -- and the
    row would look perfectly well-formed.
    """
    frame = census.records_to_census([_record("a", 1.0, 2.0, 7)], OTHER_CENSUS_DTYPES)
    with pytest.raises(ValueError, match="pano_lat.*core"):
        census.build_image_rows(
            frame,
            np.array([0]),
            [10.0],
            [20.0],
            "T",
            "OK",
            ["2020-01-01"],
            dtypes=OTHER_OUTPUT_DTYPES,
            image_columns=lambda picked: {
                **_other_image_columns(picked),
                "pano_lat": np.array([999.0]),
            },
        )


def test_empty_rows_null_every_column_the_provider_did_not_fill():
    out = census.build_empty_rows(
        [1.0, 2.0], [3.0, 4.0], "T", "ZERO_RESULTS", dtypes=OTHER_OUTPUT_DTYPES
    )
    assert list(out.columns) == list(OTHER_OUTPUT_DTYPES)
    assert out["status"].tolist() == ["ZERO_RESULTS"] * 2
    for column in OTHER_OUTPUT_DTYPES:
        if column not in census.QUERY_COLUMNS:
            assert out[column].isna().all(), column


def test_query_columns_names_exactly_what_build_empty_rows_fills():
    """
    If these fall out of step, build_empty_rows either nulls a column it just
    set or leaves a provider column unset -- both silent.
    """
    out = census.build_empty_rows([1.0], [2.0], "T", "ZERO_RESULTS", dtypes=OTHER_OUTPUT_DTYPES)
    filled = {c for c in OTHER_OUTPUT_DTYPES if not out[c].isna().all()}
    assert filled == set(census.QUERY_COLUMNS)


# ── the two-rule dedup, at the layer it now lives in ───────────────────────


def test_dedup_takes_the_last_copys_values_but_the_first_ones_position():
    """
    A dict is two rules. ``drop_duplicates(keep="last")`` satisfies only the
    first and moves the surviving row, which reorders essentially every real
    city -- so it is asserted here, generically, and not only through
    Mapillary's golden fixture (whose one duplicate sits in the single
    arrangement where the two orderings coincide).
    """
    first = census.records_to_census(
        [_record("B", 1.0, 1.0), _record("A", 2.0, 2.0)], OTHER_CENSUS_DTYPES
    )
    second = census.records_to_census([_record("B", 9.0, 9.0)], OTHER_CENSUS_DTYPES)
    out = census.dedupe_census(census.concat_census([first, second], OTHER_CENSUS_DTYPES))

    assert list(out["id"]) == ["B", "A"]  # FIRST appearance order
    assert out.loc[0, "lat"] == 9.0  # LAST copy's values
    assert list(out.index) == [0, 1]  # re-indexed from 0

    naive = pd.concat([first, second], ignore_index=True).drop_duplicates(subset="id", keep="last")
    assert list(naive["id"]) == ["A", "B"]  # the shortcut this test rejects


def test_a_null_id_does_not_overwrite_the_last_real_image():
    """
    factorize's default NA sentinel is -1, and `last_position[-1] = ...` writes
    to the END of the array -- the slot belonging to the last unique id. So a
    single id-less record did not merely get dropped, it REPLACED a real image:
    [A, B, <NA>] deduped to [A, <NA>], with B's row carrying the null record's
    coordinates. In an immutable dated snapshot diff.py reads that as one pano
    removed and another added at a different grid point.

    The dict this reproduces would have kept all three (None is a fine dict
    key), so that is the behaviour asserted here. Mapillary's decoder skips a
    feature with no id and can never reach this; a census provider that does
    not filter its ids can, so the seam has to be safe on its own.
    """
    frame = census.records_to_census(
        [
            _record("A", 1.0, 1.0),
            _record("A", 2.0, 2.0),
            _record("B", 3.0, 3.0),
            _record(None, 99.0, 99.0),
        ],
        OTHER_CENSUS_DTYPES,
    )
    out = census.dedupe_census(frame)
    assert out["id"].tolist()[:2] == ["A", "B"]
    assert pd.isna(out["id"].iloc[2])
    # B survives with its OWN coordinates rather than the null row's.
    assert out.loc[1, ["lat", "lon"]].tolist() == [3.0, 3.0]
    # ...and A still takes the last copy's values at the first position.
    assert out.loc[0, ["lat", "lon"]].tolist() == [2.0, 2.0]


def test_dedup_of_a_clean_census_returns_it_untouched():
    frame = census.records_to_census(
        [_record("a", 1.0, 2.0), _record("b", 3.0, 4.0)], OTHER_CENSUS_DTYPES
    )
    assert census.dedupe_census(frame) is frame  # no copy of a 19M-row census


# ── one dirty value must not cost a whole fetch ────────────────────────────


@pytest.mark.parametrize("bad", [10**25, 42.5, "not a number"])
def test_a_single_unusable_value_is_nulled_rather_than_failing_the_batch(bad, caplog):
    """
    The cast raises during decode, i.e. before any capture-date guard runs, so
    letting it escape would score the whole tile/circle as failed and discard
    every other image in it.
    """
    records = [_record("a", 1.0, 2.0, 1), _record("b", 3.0, 4.0, bad), _record("c", 5.0, 6.0, 3)]
    frame = census.records_to_census(records, OTHER_CENSUS_DTYPES)
    assert len(frame) == 3
    assert frame["sequence_index"].tolist()[0] == 1
    assert pd.isna(frame["sequence_index"].iloc[1])
    assert frame["sequence_index"].tolist()[2] == 3
    assert "coercing the bad" in caplog.text


# ── the Mapillary binding delegates rather than forking ────────────────────


def test_mapillary_still_produces_its_own_columns_through_the_generic_core():
    frame = dm.records_to_census(
        [
            {
                "id": "1",
                "lon": -122.3,
                "lat": 47.6,
                "captured_at_ms": 1_600_000_000_000,
                "creator_id": 42,
                "is_pano": True,
                "organization_id": None,
                "quality_score": 0.5,
                "on_foot": False,
                "compass_angle": 12.0,
                "sequence_id": "s1",
            }
        ]
    )
    rows = dm.build_image_rows(frame, np.array([0]), [47.6], [-122.3], "T", "OK", ["2020-09-13"])
    assert list(rows.columns) == list(dm.MAPILLARY_METADATA_DTYPES)
    assert rows["copyright_info"].iloc[0] == "© Mapillary contributor 42"
    assert rows["sequence_id"].iloc[0] == "s1"


def test_a_missing_creator_id_stays_missing_rather_than_rendering_as_NA_text():
    frame = dm.records_to_census(
        [
            {
                "id": "1",
                "lon": -122.3,
                "lat": 47.6,
                "captured_at_ms": None,
                "creator_id": None,
                "is_pano": False,
                "organization_id": None,
                "quality_score": None,
                "on_foot": None,
                "compass_angle": None,
                "sequence_id": None,
            }
        ]
    )
    rows = dm.build_image_rows(frame, np.array([0]), [47.6], [-122.3], "T", "NO_DATE", [""])
    assert rows["copyright_info"].iloc[0] == "© Mapillary"
    assert pd.isna(rows["creator_id"].iloc[0])


def test_the_mapillary_names_are_bindings_of_the_generic_ones():
    """
    dedupe_census and status_for_capture_dates are re-exported, not re-written:
    a fork of either loses #157's contract on the day it forks.
    """
    assert dm.dedupe_census is census.dedupe_census
    assert dm.status_for_capture_dates is census.status_for_capture_dates


# ── the grid run: a census becomes a run CSV ───────────────────────────────
#
# write_census_grid_run is the ~150-line back half every census provider needs:
# grid assignment, the pano / flat-only / empty three-frame build, the
# sequential gzip write. It carries #157's memory shape and #116's flat-only
# rule, so a forked copy loses both silently. Same posture as the tests above --
# drive it with a schema that is NOT Mapillary's.

# `is_pano` is the one column the tail requires BY NAME, and that is a contract
# rather than Mapillary-shapedness: it is the shared 360-degree boolean (issue
# #116) that each census provider normalizes its own flag into (KartaView:
# projection == "SPHERE"). Everything else here still differs from Mapillary's
# schema in name, count and order.
OTHER_GRID_CENSUS_DTYPES = {**OTHER_CENSUS_DTYPES, "is_pano": "boolean"}

TS = "2026-08-20T00:00:00+00:00"


def _grid_record(id_, lat, lon, *, is_pano=True, idx=0, shot="2020-01-01"):
    return {**_record(id_, lat, lon, idx, shot), "is_pano": is_pano}


def _other_capture_dates(census_frame, positions):
    """This provider's date binding: it indexes its OWN column, by position."""
    picked = census_frame["shot_date"].to_numpy()[positions]
    return np.array(["" if pd.isna(v) else str(v) for v in picked], dtype=object)


def _tiny_grid():
    """3x3 points, 20 m apart. Images are placed ON grid points, so an image's
    intended ordinal is exact and the test never depends on rounding."""
    return census.build_grid(47.6, -122.3, 40, 40, 20)


def _run_grid(tmp_path, records, *, grid=None, capture_dates_for=None, **kwargs):
    grid = grid if grid is not None else _tiny_grid()
    fetched = {
        "census": census.records_to_census(records, OTHER_GRID_CENSUS_DTYPES),
        "api_requests": 1,
    }
    written = census.write_census_grid_run(
        fetched,
        grid,
        str(tmp_path / "run.csv.gz"),
        TS,
        capture_dates_for=capture_dates_for or _other_capture_dates,
        image_columns=_other_image_columns,
        dtypes=OTHER_OUTPUT_DTYPES,
        **kwargs,
    )
    return grid, fetched, written


def test_the_grid_run_writes_panos_then_flat_only_then_the_empty_fill(tmp_path):
    grid = _tiny_grid()
    _, _, written = _run_grid(
        tmp_path,
        [
            _grid_record("p0", grid.lats[0], grid.lons[0]),
            _grid_record("f4", grid.lats[4], grid.lons[4], is_pano=False),
        ],
        grid=grid,
    )
    df = written["df"]

    # Column order is the provider's dtypes key order, not this module's.
    assert list(df.columns) == list(OTHER_OUTPUT_DTYPES)
    # Every grid point is accounted for exactly once.
    assert len(df) == grid.num_points == 9
    assert written["num_flat_images"] == 1
    assert written["num_flat_only_points"] == 1
    assert written["num_empty_points"] == 7
    # ...in the three-frame write order the fixture pins for Mapillary.
    assert list(df["status"]) == ["OK", FLAT_ONLY] + ["ZERO_RESULTS"] * 7
    assert df["pano_id"].iloc[0] == "p0"
    assert df["pano_id"].iloc[1] == "f4"
    # The query columns are the GRID point, not the image's own position.
    assert df["query_lat"].iloc[0] == pytest.approx(grid.lats[0])
    assert df["query_lon"].iloc[0] == pytest.approx(grid.lons[0])


def test_a_flat_only_point_carries_a_null_capture_date(tmp_path):
    """
    Issue #116: a FLAT_ONLY row is a coverage-presence marker. A real date there
    would put contributor flat timestamps into every dated statistic, which key
    on status == 'OK'.
    """
    grid = _tiny_grid()
    _, _, written = _run_grid(
        tmp_path,
        [_grid_record("f0", grid.lats[0], grid.lons[0], is_pano=False, shot="2021-05-05")],
        grid=grid,
    )
    flat = written["df"][written["df"]["status"] == FLAT_ONLY]
    assert len(flat) == 1
    assert pd.isna(flat["capture_date"].iloc[0])
    # ...but the image itself is still identified, so coverage is attributable.
    assert flat["pano_id"].iloc[0] == "f0"


def test_a_point_holding_both_a_pano_and_a_flat_is_not_flat_only(tmp_path):
    grid = _tiny_grid()
    _, _, written = _run_grid(
        tmp_path,
        [
            _grid_record("flat", grid.lats[3], grid.lons[3], is_pano=False),
            _grid_record("pano", grid.lats[3], grid.lons[3]),
        ],
        grid=grid,
    )
    df = written["df"]
    assert written["num_flat_only_points"] == 0
    # The flat image still counts toward the census magnitude of flat imagery.
    assert written["num_flat_images"] == 1
    assert list(df["status"]) == ["OK"] + ["ZERO_RESULTS"] * 8
    assert df["pano_id"].iloc[0] == "pano"


def test_the_first_flat_at_a_point_is_its_representative(tmp_path):
    """
    Mirrors the dict.setdefault the vectorized form replaced: earliest in census
    order wins. np.unique returns first occurrences but SORTED by value, so the
    positions have to be re-sorted -- dropping that re-sort silently changes
    which image represents a point, and therefore the row's coordinates.
    """
    grid = _tiny_grid()
    _, _, written = _run_grid(
        tmp_path,
        [
            _grid_record("second_point", grid.lats[5], grid.lons[5], is_pano=False),
            _grid_record("first_here", grid.lats[1], grid.lons[1], is_pano=False),
            _grid_record("later_here", grid.lats[1], grid.lons[1], is_pano=False),
        ],
        grid=grid,
    )
    df = written["df"]
    flat_ids = list(df[df["status"] == FLAT_ONLY]["pano_id"])
    # Census order, not ordinal order: the point seen first is written first.
    assert flat_ids == ["second_point", "first_here"]
    assert written["num_flat_images"] == 3
    assert written["num_flat_only_points"] == 2


def test_points_under_an_unmeasured_area_are_request_failed_not_empty(tmp_path):
    """
    Issue #168: an uncovered point whose area never downloaded is UNKNOWN. Left
    as ZERO_RESULTS it is indistinguishable from genuine no-imagery, which
    understates coverage and pollutes the next run-to-run diff.
    """
    grid = _tiny_grid()

    # Everything strictly north of the centre row is "unmeasured".
    def mask(lats, lons):
        return np.asarray(lats) > grid.center_lat

    _, _, written = _run_grid(
        tmp_path, [], grid=grid, unmeasured_mask=mask, unmeasured_desc="1 bad cell"
    )
    df = written["df"]
    assert written["num_empty_points"] == 9
    assert written["num_unmeasured_points"] == 3
    assert set(df["status"]) == {"ZERO_RESULTS", REQUEST_FAILED}
    assert (df["status"] == REQUEST_FAILED).sum() == 3
    # ...and only north of centre.
    assert (df[df["status"] == REQUEST_FAILED]["query_lat"] > grid.center_lat).all()


def test_a_clean_fetch_pays_nothing_for_the_unmeasured_check(tmp_path):
    _, _, written = _run_grid(tmp_path, [], unmeasured_mask=None)
    assert written["num_unmeasured_points"] == 0
    assert set(written["df"]["status"]) == {"ZERO_RESULTS"}


def test_images_outside_the_grid_are_dropped(tmp_path):
    grid = _tiny_grid()
    _, _, written = _run_grid(
        tmp_path,
        [_grid_record("far", grid.center_lat + 1.0, grid.center_lon + 1.0)],
        grid=grid,
    )
    assert written["num_flat_images"] == 0
    assert set(written["df"]["status"]) == {"ZERO_RESULTS"}


def test_the_tail_takes_ownership_of_the_census(tmp_path):
    """
    The census is POPPED out of the fetch result, so this function is its sole
    owner and can release it before the CSV writes. A caller that kept its own
    name would pin the whole census (19M rows at Detroit) alive through both
    writes and defeat the release entirely -- issue #157's actual failure mode.
    """
    grid = _tiny_grid()
    _, fetched, _ = _run_grid(tmp_path, [_grid_record("p", grid.lats[0], grid.lons[0])], grid=grid)
    assert "census" not in fetched
    # The rest of the fetch result is untouched -- the caller still needs it.
    assert fetched["api_requests"] == 1


def test_the_date_binding_is_handed_the_whole_census_and_positions(tmp_path):
    """
    capture_dates_for takes (census, positions), NOT a pre-taken sub-frame, so a
    provider indexes only the one or two columns it needs. A seam that took a
    taken frame would materialize every column of the census a second time.
    """
    grid = _tiny_grid()
    seen = {}

    def spy(census_frame, positions):
        seen["rows"] = len(census_frame)
        seen["positions"] = np.asarray(positions).tolist()
        return _other_capture_dates(census_frame, positions)

    _run_grid(
        tmp_path,
        [
            _grid_record("p0", grid.lats[0], grid.lons[0]),
            _grid_record("flat", grid.lats[1], grid.lons[1], is_pano=False),
            _grid_record("p2", grid.lats[2], grid.lons[2]),
        ],
        grid=grid,
        capture_dates_for=spy,
    )
    assert seen["rows"] == 3  # the WHOLE census, not the 2 panos
    assert seen["positions"] == [0, 2]  # ...selected by position


def test_a_missing_date_is_no_date_rather_than_a_dropped_row(tmp_path):
    grid = _tiny_grid()
    _, _, written = _run_grid(
        tmp_path,
        [
            _grid_record("dated", grid.lats[0], grid.lons[0], shot="2019-03-03"),
            _grid_record("undated", grid.lats[1], grid.lons[1], shot=None),
        ],
        grid=grid,
    )
    df = written["df"]
    assert list(df["status"])[:2] == ["OK", "NO_DATE"]
    assert pd.isna(df["capture_date"].iloc[1])


def test_build_grid_ordinals_round_trip_to_the_coordinate_arrays(tmp_path):
    """
    ordinals() is arithmetic on a regular lattice, standing in for the
    {(i, j): position} dict that cost ~4.5 GB at Cairo scale (issue #157).
    """
    grid = _tiny_grid()
    assert grid.num_points == len(grid.lats) == len(grid.lons) == 9
    # The generation order is row-major from the lowest (i, j).
    assert grid.ordinals(grid.i_min, grid.j_min) == 0
    assert grid.ordinals(grid.i_min + 2, grid.j_min + 2) == 8
    # Vectorized over arrays, which is how the tail calls it.
    got = grid.ordinals(
        np.array([grid.i_min, grid.i_min + 1]), np.array([grid.j_min + 1, grid.j_min])
    )
    assert got.tolist() == [1, 3]
    # The bbox the fetch is bounded by contains every point it will assign to.
    min_lon, min_lat, max_lon, max_lat = grid.bbox
    assert (grid.lats >= min_lat).all() and (grid.lats <= max_lat).all()
    assert (grid.lons >= min_lon).all() and (grid.lons <= max_lon).all()


# Every grid collector that hands its census to write_census_grid_run. Add a
# provider's entry point here when it is wired up -- the check below is cheap
# and the bug it catches is invisible at runtime.
GRID_COLLECTORS = [dm.download_mapillary_metadata_async]


@pytest.mark.parametrize("collector", GRID_COLLECTORS, ids=lambda f: f.__name__)
def test_no_grid_collector_binds_the_census_to_a_local(collector):
    """
    The other half of the ownership contract, and the half no runtime test can
    see. write_census_grid_run pops the census so it can drop it before the CSV
    writes, but a caller that ALSO holds ``census = fetched["census"]`` pins the
    whole thing (19M rows at Detroit) alive through both writes -- the release
    buys nothing, peak memory returns to its pre-#157 level, and every test in
    this file still passes. So the caller's discipline is asserted on its source.

    Reading `fetched["census"]` inline (for a log line, say) is fine and stays
    allowed; what is banned is giving it a NAME that outlives the call.
    """
    tree = ast.parse(textwrap.dedent(inspect.getsource(collector)))
    for node in ast.walk(tree):
        targets = []
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign | ast.NamedExpr):
            targets = [node.target]
        if not targets or not isinstance(node.value, ast.Subscript):
            continue
        key = node.value.slice
        if isinstance(key, ast.Constant) and key.value == "census":
            name = getattr(targets[0], "id", "<expr>")
            raise AssertionError(
                f"{collector.__name__} binds the census to a local ({name!r}); "
                "read it inline instead, or write_census_grid_run's release is "
                "defeated and #157's memory shape silently regresses"
            )
