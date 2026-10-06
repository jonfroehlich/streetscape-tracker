"""The #425 measurement script (scripts/csv_float_parse_analyze.py).

The script's one trap is the key count: a 9-decimal key built from an
``np.float64`` takes numpy's rounding path and can land a half-way value on
the other side, so it must count keys from Python floats, as the scorer's own
``zip`` over a Series yields them. The rest pins provenance: the committed
record names the command that wrote it, and the writeup quotes that record.
"""

import gzip
import json
import re
from pathlib import Path

import pandas as pd
import pytest

from scripts import csv_float_parse_analyze as cfa

# A longitude from a real walk CSV that pandas' default C parser returns one
# ULP low, moving its 9-decimal key (the same literal tests/test_fileutils.py
# pins the loader with).
BOUNDARY_LON_TEXT = "-122.26811722250001"
# Another real Seattle longitude the default parser misreads across the
# half-way point -- but whose numpy-rounded key does NOT move, so a key count
# taken from np.float64 rather than Python floats misses it.
NUMPY_BLIND_LON_TEXT = "-122.37477129150001"

_DOCS = Path(__file__).resolve().parent.parent / "docs" / "experiments"


def _walk_csv(tmp_path):
    path = tmp_path / "walk.csv.gz"
    text = (
        "query_lat,query_lon\n"
        f"47.67595068398769,{BOUNDARY_LON_TEXT}\n"
        f"47.6,{NUMPY_BLIND_LON_TEXT}\n"
        "47.6,-122.3\n"
    )
    with gzip.open(path, "wt", encoding="utf-8", newline="") as fh:
        fh.write(text)
    return str(path)


def test_a_coordinate_one_ulp_across_the_half_way_point_is_counted_as_a_key_shift(tmp_path):
    """Two longitudes the default parser misreads by one ULP across the 9-decimal
    half-way point: two coordinates off, two keys shifted, an error of one ULP.
    The second is counted only from Python-float keys (numpy rounding misses it)."""
    out = cfa.measure_walk_csv(_walk_csv(tmp_path))
    assert out["rows"] == 3
    assert out["lat_off"] == 0
    assert out["lon_off"] == 2
    assert out["keys_shifted"] == 2
    assert out["max_lon_err_ulp"] == 1.0
    assert out["max_lon_err_deg"] == pytest.approx(1.42e-14, rel=0.01)


def test_the_round_trip_premise_is_checked_not_assumed(tmp_path, monkeypatch):
    """If the 'exact' read ever stops being float() of the text, the script
    refuses rather than reporting a comparison against the wrong reference."""
    real = pd.read_csv

    def lossy(*args, **kwargs):
        df = real(*args, **kwargs)
        if kwargs.get("float_precision") == "round_trip":
            df["query_lon"] = df["query_lon"].round(6)
        return df

    monkeypatch.setattr(cfa.pd, "read_csv", lossy)
    with pytest.raises(AssertionError, match="not float"):
        cfa.measure_walk_csv(_walk_csv(tmp_path))


def test_loader_parser_restores_pandas_and_forces_the_option(tmp_path):
    """The grid and scoring measurements read through the REAL loader with the
    parser forced; the patch must not outlive the block."""
    path = _walk_csv(tmp_path)
    real = pd.read_csv
    with cfa.loader_parser(None):
        default = pd.read_csv(path, float_precision="round_trip")["query_lon"].tolist()[0]
    assert pd.read_csv is real
    assert default != float(BOUNDARY_LON_TEXT)


def test_generated_by_names_the_run_that_wrote_the_file():
    """A fixed constant would let a scratch run claim the canonical provenance;
    the canonical invocation must render DOCS_GENERATED_BY exactly, and it has
    to be a command the script's own parser accepts."""
    assert cfa.docs_generated_by("data", "docs/experiments", 4) == cfa.DOCS_GENERATED_BY
    scratch = cfa.docs_generated_by("/tmp/d", "/tmp/scratch", 0)
    assert "/tmp/scratch" in scratch and "--grid-sample 0" in scratch
    argv = cfa.DOCS_GENERATED_BY.split()[1:]
    args = cfa.build_parser().parse_args(argv)
    assert (args.data_dir, args.docs_dir, args.grid_sample) == ("data", "docs/experiments", 4)


def test_the_committed_record_still_matches_the_writeup():
    """The record and the prose are two copies of one measurement; every
    headline figure the writeup quotes is checked back against the JSON."""
    record = json.loads((_DOCS / cfa.DOCS_METRICS_NAME).read_text())
    prose = (_DOCS / "csv-float-parse.md").read_text()
    flat = prose.replace(",", "")
    assert record["_about"]["generated_by"] == cfa.DOCS_GENERATED_BY

    walks = {
        (w["city_id"].split("--")[0], w["provider"], w["network_type"]): w
        for w in record["observations"]["walks"]
    }
    seattle = walks[("seattle", "gsv", "drive")]
    assert re.search(rf"\b{seattle['keys_shifted']} of {seattle['rows']}\b", flat)
    corvallis = walks[("corvallis", "gsv", "all_public")]
    assert re.search(rf"\b{corvallis['keys_shifted']} of {corvallis['rows']}\b", flat)
    for side, key in (("default", "edges_fully_covered"), ("round_trip", "edges_fully_covered")):
        assert str(seattle["scoring"][side][key]) in flat
    for side in ("default", "round_trip"):
        assert f"{seattle['scoring'][side]['length_km_covered']}" in flat

    summary = record["summary"]
    for dist in ("lat_off_share", "lon_off_share"):
        for end in ("min", "max"):
            assert f"{100 * summary[dist][end]:.1f} %" in prose, (dist, end)
    assert f"{summary['keys_shifted_total']} " in flat
    for end in ("min", "max"):
        assert f"{summary['grid_load_slowdown'][end]:.2f}" in flat
    assert str(summary["grid_rows"]) in flat
