"""
Issue #226's capture-date-precision experiment
(scripts/capture_date_precision_analyze.py + docs/experiments/).

Network-free, and deliberately two kinds of test in one file:

  * the shape classifier's own contract, which is what every count in the
    committed metrics file is built out of; and
  * every number docs/experiments/capture-date-precision.md quotes, recomputed
    from the committed JSON rather than restated. The docs/experiments rule is
    that a quoted number is traceable to committed data produced by committed
    code — a writeup whose figures live only in its own prose is the
    single-copy failure the convention exists to prevent (#106).

The classifier tests matter more than they look: `classify_shape` decides what
"month precision" MEANS for the sweep, so a change there silently redefines the
headline count rather than breaking anything.
"""

import importlib.util
import json
import os
import subprocess
import sys

import pandas as pd
import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SCRIPT = os.path.join(PROJECT_ROOT, "scripts", "capture_date_precision_analyze.py")
_spec = importlib.util.spec_from_file_location("capture_date_precision_analyze", _SCRIPT)
cdp = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = cdp
_spec.loader.exec_module(cdp)

COMMITTED_JSON = os.path.join(PROJECT_ROOT, "docs", "experiments", f"{cdp.TOPIC}_metrics.json")
WRITEUP = os.path.join(PROJECT_ROOT, "docs", "experiments", f"{cdp.TOPIC}.md")


@pytest.fixture(scope="module")
def committed():
    with open(COMMITTED_JSON, encoding="utf-8") as fh:
        return json.load(fh)


# --- the classifier every committed count is built from --------------------


@pytest.mark.parametrize(
    "value, shape",
    [
        ("2022-09-15", "day"),
        ("2022-09", "month"),
        ("2019", "year"),
        # Shape-valid, calendar-invalid. Classified by SHAPE on purpose: the
        # sweep asks which formats a reader must accept, not which values are
        # real, and folding these into "other" would understate the month
        # population by exactly the corrupt rows.
        ("2022-13", "month"),
        ("", "absent"),
        (None, "absent"),
        (float("nan"), "absent"),
        # The literal string a legacy writer emitted for "no date". pandas'
        # default NA list already catches it on read; folded here too so a
        # caller that reads raw strings agrees with one that does not.
        ("None", "absent"),
        ("nan", "absent"),
        ("20220915", "other"),
        ("2022/09/15", "other"),
        ("2022-09-15T12:00:00Z", "other"),
        ("not-a-date", "other"),
    ],
)
def test_classify_shape(value, shape):
    assert cdp.classify_shape(value) == shape


def test_reduced_precision_is_exactly_what_a_strict_reader_loses():
    """The sweep's headline population is defined by the parser, not by taste.

    REDUCED_PRECISION_SHAPES is the set of shapes that the old strict
    '%Y-%m-%d' turned into NaT while ISO8601 reads them — so it is checked
    against both parsers rather than asserted as a list.
    """
    samples = {"day": "2022-09-15", "month": "2022-09", "year": "2019"}
    for shape, value in samples.items():
        strict = pd.to_datetime(pd.Series([value]), format="%Y-%m-%d", errors="coerce")
        iso = pd.to_datetime(pd.Series([value]), format="ISO8601", errors="coerce")
        lost_by_strict = bool(strict.isna().iloc[0]) and not bool(iso.isna().iloc[0])
        assert lost_by_strict == (shape in cdp.REDUCED_PRECISION_SHAPES), shape


# --- the committed record ---------------------------------------------------


def test_committed_json_names_its_producer(committed):
    assert committed["generated_by"] == cdp.DOCS_GENERATED_BY
    # ...and the constant renders the canonical invocation, so the stamp names
    # a run the repo can make rather than a string copied onto the file.
    assert cdp.docs_generated_by("docs/experiments") == cdp.DOCS_GENERATED_BY
    assert cdp.docs_generated_by("/tmp/scratch").endswith("--docs-dir /tmp/scratch")


def test_committed_json_carries_all_three_measurements(committed):
    for key in cdp.MEASUREMENTS:
        assert key in committed, key


def test_docs_dir_refuses_a_partial_measurement_set(tmp_path):
    """A subset run must not overwrite the committed record with a partial file."""
    result = subprocess.run(
        [sys.executable, _SCRIPT, "--measure", "sweep", "--docs-dir", str(tmp_path)],
        capture_output=True,
        text=True,
        cwd=PROJECT_ROOT,
        timeout=60,
    )
    assert result.returncode == 2  # argparse usage error, before any CSV is read
    assert not list(tmp_path.iterdir())


def test_sweep_counts_are_internally_consistent(committed):
    sweep = committed["sweep"]
    assert sum(sweep["rows_by_shape"].values()) == sweep["rows_total"]
    assert sum(sweep["files_by_shape_set"].values()) == sweep["files_scanned"] - len(
        sweep["files_unreadable"]
    )
    # Every file listed as carrying reduced precision must actually show a
    # month or year count, and vice versa — the list and the histogram are two
    # views of one pass and cannot disagree.
    for entry in sweep["files_with_reduced_precision"]:
        assert any(entry["shape_counts"].get(s) for s in cdp.REDUCED_PRECISION_SHAPES)
        assert sum(entry["shape_counts"].values()) == entry["rows"]


def test_writeup_figures_recompute_from_the_committed_json(committed):
    """Every figure capture-date-precision.md quotes, RECOMPUTED from the JSON.

    Not "the writeup mentions a 9 somewhere": each share below is derived here
    from the committed counts and then looked up in the prose, so a number that
    drifts in either file fails. That is the docs/experiments rule -- a quoted
    figure is traceable to committed data produced by committed code, never
    restated from a transcript (#106's cautionary tale).
    """
    sweep = committed["sweep"]
    text = open(WRITEUP, encoding="utf-8").read()
    rows = sweep["rows_by_shape"]
    dated = rows["day"] + rows.get("month", 0) + rows.get("year", 0)

    # Corpus size and the shape histogram
    assert f"{sweep['files_scanned']:,}" in text  # 1,170 run files, not 1,177
    assert f"{sweep['rows_total']:,}" in text
    for shape in ("absent", "day", "month"):
        assert f"{rows[shape]:,}" in text
        assert f"{rows[shape] / sweep['rows_total'] * 100:.2f}%" in text
    # ...and the share OF DATED rows, which is the honest denominator: 76.89%
    # of cells are ZERO_RESULTS fill, so a share over all rows describes the
    # sampling grid rather than the imagery.
    for shape in ("day", "month"):
        assert f"{rows[shape] / dated * 100:.2f}%" in text

    # The by-file split, which is the claim that no file mixes precisions
    for population, n in sweep["files_by_shape_set"].items():
        assert f"{n:,}" in text, population
    assert sweep["files_mixing_precisions"] == []
    affected = sweep["files_with_reduced_precision"]
    assert f"| month only | **{len(affected)}** |" in text
    for entry in affected:
        # Pure month precision: no day-shaped value anywhere in these files,
        # which is why a format-free parse happens to work on them today.
        assert not entry["shape_counts"].get("day")
        assert f"{entry['rows']:,}" in text
    assert rows.get("year", 0) == 0  # a year-precision file would need its own line

    # Finding 3: the day precision is standardize_capture_date's pinning, not
    # the provider's. Mapillary is the control at ~1/30.
    for provider, block in sweep["by_provider"].items():
        day = block["rows_by_shape"]["day"]
        assert f"{block['day_rows_on_first'] / day * 100:.2f}%" in text, provider
        assert f"{block['files']:,}" in text

    # Finding 6: the size of the --regenerate-json repair population
    iso = committed["asymmetry"]["by_reader"]["iso8601"]
    moved_any = sum(n for verdict, n in iso.items() if verdict != "unchanged")
    assert f"**{moved_any} of {committed['asymmetry']['runs_in_catalog']:,} gsv runs" in text
    assert f"{moved_any / committed['asymmetry']['runs_in_catalog'] * 100:.1f}%" in text


def test_committed_parse_benchmark_shows_no_iso8601_penalty(committed):
    """The #157 objection, retired by the committed numbers rather than by prose."""
    seconds = committed["parse"]["seconds_best_of_3"]
    assert seconds["iso8601"] <= seconds["strict_ymd"] * 1.25
    # Both orderings measured, or the trap reads as absent: inference keeps one
    # precision and NaTs the other, whichever came first.
    for ordering in ("day_first", "month_first"):
        loss = committed["parse"]["inference_loss"][ordering]
        assert None in loss["inferred"]
        assert None not in loss["iso8601"]


def test_committed_asymmetry_is_one_sided_under_the_strict_reader(committed):
    """The tell: a repair pass that can only ever CLEAR a date column.

    This is the finding the writeup exists to make reusable, so it is asserted
    on the committed counts rather than described.
    """
    by_reader = committed["asymmetry"]["by_reader"]
    assert by_reader["strict_ymd"].get("null_to_value", 0) == 0
    restored = by_reader["iso8601"].get("null_to_value", 0)
    assert restored > 0
    assert len(committed["asymmetry"]["runs_restored_by_iso8601"]) == restored
