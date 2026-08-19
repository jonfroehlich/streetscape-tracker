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

import numpy as np
import pandas as pd
import pytest

from streetscape_metadata_tracker import census
from streetscape_metadata_tracker import download_mapillary as dm

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
