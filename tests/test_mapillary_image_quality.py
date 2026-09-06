"""Sampling invariants for the Mapillary `quality_score` study.

The study reduces ~500 censuses of tens of millions of image rows to one row per
city, and every hazard it has is a *denominator* or *weighting* hazard: which
rows count, which population a percentile is taken over, and whether one
contributor's drive can carry a whole city. These tests pin those separations
rather than the arithmetic, which numpy already owns.
"""

import csv
import gzip
import json

import numpy as np
import pytest

from scripts import experiment_stats
from scripts import mapillary_image_quality_analyze as mqa
from scripts import mapillary_image_quality_collect as mqc


def _write_census(path, rows, header=None):
    """A minimal Mapillary run CSV. `header=None` gives the enriched schema."""
    header = header or [
        "query_lat",
        "status",
        "quality_score",
        "on_foot",
        "organization_id",
        "sequence_id",
    ]
    with gzip.open(path, "wt", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(header)
        writer.writerows(rows)


def _row(status, quality="", on_foot="", org="", seq="", lat="47.6"):
    return [lat, status, quality, on_foot, org, seq]


def test_only_pano_rows_enter_the_denominator(tmp_path):
    """FLAT_ONLY and ZERO_RESULTS rows carry the extra columns too (the
    collector writes all seven on every row), so a share taken over the whole
    file describes the GRID, not the imagery — and for Mapillary a census file
    is mostly the other two statuses. Four panos, four non-panos, and every
    non-pano deliberately carries a quality that would move the median if it
    counted."""
    p = tmp_path / "census.csv.gz"
    _write_census(
        p,
        [
            _row("OK", 0.80, "false", "", "s1"),
            _row("OK", 0.82, "false", "", "s1"),
            _row("NO_DATE", 0.84, "false", "", "s1"),
            _row("OK", 0.86, "false", "", "s1"),
            _row("FLAT_ONLY", 0.10, "false", "", "s9"),
            _row("FLAT_ONLY", 0.10, "false", "", "s9"),
            _row("ZERO_RESULTS", 0.10, "", "", ""),
            _row("ZERO_RESULTS", 0.10, "", "", ""),
        ],
    )
    row = mqc.measure_run(str(p), "testville", "2026-09-01", "census.csv.gz")
    assert row["n_panos"] == 4, "NO_DATE is a pano; FLAT_ONLY and ZERO_RESULTS are not"
    assert row["n_quality"] == 4
    assert row["q_p50"] == 0.83, "the 0.10 rows would drag this to ~0.45 if they counted"


def test_a_legacy_run_is_skipped_rather_than_counted_as_zero(tmp_path):
    """Mapillary CSVs written before 2026-07-24 have no quality_score column at
    all. Folding them in as absent scores would report the schema change as a
    collapse in imagery quality, and averaging over them would understate every
    city collected before that date. `_has_quality_column` is the gate, and it
    reads one line rather than failing mid-parse."""
    legacy = tmp_path / "legacy.csv.gz"
    _write_census(
        legacy,
        [["47.6", "OK", "", "", ""]],
        header=["query_lat", "status", "pano_id", "capture_date", "copyright_info"],
    )
    enriched = tmp_path / "enriched.csv.gz"
    _write_census(enriched, [_row("OK", 0.8, "false", "", "s1")])

    assert mqc._has_quality_column(str(legacy)) is False
    assert mqc._has_quality_column(str(enriched)) is True


def test_the_collector_and_experiment_stats_use_one_percentile_ruler():
    """The per-city percentiles are numpy's (the samples are millions of values
    per city) and the cross-city ones go through experiment_stats. Two rulers
    would mean the writeup's p90 of p50s was computed by a different definition
    than the p50s themselves — the exact drift experiment_stats exists to
    prevent. Checked on an even-sized sample, where a lower-index pick and a
    real median differ."""
    sample = [0.10, 0.20, 0.30, 0.40, 0.50, 0.95]
    for pct in (10, 25, 50, 75, 90):
        assert mqc._percentiles(np.array(sample), (pct,))[0] == pytest.approx(
            round(experiment_stats.percentile(sample, pct), 4)
        )


def test_sequence_weighting_is_what_stops_one_drive_carrying_a_city(tmp_path):
    """`pano-spacing.md`'s rule, arriving as a weighting: images inside a drive
    are one contributor's camera sampled every few metres, not thousands of
    independent observations. Here one long poor drive outnumbers three short
    good ones 30:3, so the image-weighted median reports the city as poor while
    the sequence-weighted median — one value per drive — reports it as good.
    Both are recorded precisely because they disagree."""
    p = tmp_path / "census.csv.gz"
    rows = [_row("OK", 0.40, "false", "", "long") for _ in range(30)]
    rows += [_row("OK", 0.95, "false", "", f"short{i}") for i in range(3)]
    _write_census(p, rows)

    row = mqc.measure_run(str(p), "onedrive", "2026-09-01", "census.csv.gz")
    assert row["n_sequences"] == 4
    assert row["q_p50"] == 0.40, "image-weighted: the long drive is 30 of 33 rows"
    assert row["seq_q_p50"] == pytest.approx(0.95), "sequence-weighted: 3 of 4 drives are good"


def test_images_without_a_sequence_are_dropped_from_the_sequence_cut_only(tmp_path):
    """An image with no sequence_id has no drive to belong to. Giving each one
    its own synthetic sequence would make the sequence-weighted median converge
    on the image-weighted one — erasing the comparison the cut exists to make —
    so they are dropped there and kept everywhere else."""
    p = tmp_path / "census.csv.gz"
    rows = [_row("OK", 0.30, "false", "", "") for _ in range(9)]
    rows += [_row("OK", 0.90, "false", "", "s1")]
    _write_census(p, rows)

    row = mqc.measure_run(str(p), "nosequence", "2026-09-01", "census.csv.gz")
    assert row["n_panos"] == 10, "sequence-less images still count as imagery"
    assert row["q_p50"] == 0.30
    assert row["n_sequences"] == 1
    assert row["seq_q_p50"] == pytest.approx(0.90)


def _paired_city(**overrides):
    base = {
        "city_id": "paired",
        "run_date": "2026-09-01",
        "n_panos": 10_000,
        "n_quality": 10_000,
        "n_sequences": 50,
        "n_foot_known": 10_000,
        "n_panos_on_foot": 5_000,
        "n_seq_on_foot": 25,
        "n_seq_vehicle": 25,
        "n_with_org": 0,
        "n_distinct_orgs": 0,
        "n_seq_org": 0,
        "n_seq_no_org": 50,
        "q_p50": 0.80,
        "pct_on_foot": 50.0,
        "q_p50_on_foot": 0.70,
        "q_p50_vehicle": 0.85,
        "seq_q_p50_on_foot": 0.72,
        "seq_q_p50_vehicle": 0.84,
        "seq_q_p50": 0.80,
        "pct_with_org": 0.0,
        "q_p50_org": None,
        "q_p50_no_org": 0.80,
        "seq_q_p50_org": None,
        "seq_q_p50_no_org": 0.80,
    }
    return {**base, **overrides}


def test_a_drives_class_is_counted_not_assumed(tmp_path):
    """Every per-class number here assigns a CLASS to a DRIVE, which is only
    meaningful if `on_foot` is a property of the drive. Measured, it is: zero
    mixed sequences across the corpus. So the counter has to actually detect a
    mixed drive when there is one, or the reassuring zero means nothing — and a
    partially-labelled drive must NOT read as mixed, since an unknown is not a
    second class."""
    p = tmp_path / "census.csv.gz"
    _write_census(
        p,
        [
            _row("OK", 0.80, "true", "", "mixed"),
            _row("OK", 0.82, "false", "", "mixed"),
            _row("OK", 0.84, "true", "", "partial"),
            _row("OK", 0.86, "", "", "partial"),
            _row("OK", 0.88, "false", "", "clean"),
            _row("OK", 0.90, "false", "", "clean"),
        ],
    )
    row = mqc.measure_run(str(p), "mixedville", "2026-09-01", "census.csv.gz")
    assert row["n_sequences"] == 3
    assert row["n_seq_mixed_foot"] == 1, "'mixed' carries both classes"
    assert row["n_seq_on_foot"] == 2, "'mixed' takes the MAX, so it counts as on-foot"
    assert row["n_seq_vehicle"] == 1


def test_a_one_sided_city_is_excluded_from_the_paired_on_foot_comparison():
    """The on-foot finding is a WITHIN-city paired comparison, so it cannot be
    explained by which cities happen to have pedestrian capture. That only holds
    while both populations are real, and 'real' has to be counted in DRIVES as
    well as images: a city whose entire on-foot side is one long walk would
    otherwise contribute a delta built from a single observation. The study's
    first pass filtered on images alone and let exactly those cities in."""
    ok = _paired_city()
    too_few_panos = _paired_city(city_id="thin", n_panos_on_foot=50, pct_on_foot=0.5)
    one_walk = _paired_city(city_id="onewalk", n_seq_on_foot=1)

    result = mqa.measure_on_foot([ok, too_few_panos, one_walk])
    assert result["paired_cities"] == 1, "images alone let 'onewalk' through"
    assert result["image_weighted"]["delta"]["p50"] == pytest.approx(-0.15)
    assert result["image_weighted"]["cities_lower"] == 1
    assert result["sequence_weighted"]["delta"]["p50"] == pytest.approx(-0.12)


def test_both_weightings_of_the_on_foot_delta_are_reported():
    """A drive-weighted delta can disagree with an image-weighted one — that is
    the whole reason `pano-spacing.md` insists on the distinction — so reporting
    only the one that happens to be larger would be choosing the answer. Here
    the image-weighted delta says on-foot is worse and the drive-weighted one
    says it is better, and the record has to carry both."""
    contrary = _paired_city(
        q_p50_on_foot=0.70,
        q_p50_vehicle=0.85,
        seq_q_p50_on_foot=0.88,
        seq_q_p50_vehicle=0.84,
    )
    result = mqa.measure_on_foot([contrary])
    assert result["image_weighted"]["cities_lower"] == 1
    assert result["sequence_weighted"]["cities_lower"] == 0


def test_spearman_reads_a_monotone_relationship_a_pearson_r_would_understate():
    """The relationships here are monotone but not linear (a bounded share
    against a bounded score), which is why the study reports rank correlation.
    Ties take average ranks so a flat stretch cannot fabricate an ordering."""
    xs = [1.0, 2.0, 3.0, 4.0, 5.0]
    assert mqa.spearman(xs, [1.0, 4.0, 9.0, 16.0, 25.0]) == 1.0
    assert mqa.spearman(xs, [25.0, 16.0, 9.0, 4.0, 1.0]) == -1.0
    assert mqa.spearman(xs, [1.0, 1.0, 1.0, 1.0, 1.0]) is None, "no ordering to report"
    assert mqa.spearman([1.0, 2.0], [1.0, 2.0]) is None, "too small to have one"


def test_the_committed_record_names_a_command_the_repo_can_run():
    """The generated_by stamp is the study's reproducibility claim (the
    grid-density precedent), and it has to name the flags that determine the
    numbers — `--catalog-label` above all, since a dev catalog holding three
    Mapillary runs and production's hundreds give different answers to the same
    question and nothing in the output distinguishes them."""
    stamp = mqa.docs_generated_by("docs/experiments", "experiments/x/city_quality.csv", "prod")
    assert stamp.startswith("python scripts/mapillary_image_quality_analyze.py")
    assert "--catalog-label prod" in stamp
    assert "--cities-csv experiments/x/city_quality.csv" in stamp


def test_every_collected_column_reaches_the_committed_record():
    """The committed per-city CSV is what a reader recomputes the study from,
    so it carries the collector's whole row rather than the handful the writeup
    quotes. Without this, a column added to the collector would be measured on
    production, land in a gitignored directory, and never reach the repo — which
    is #106's failure mode reappearing one file later."""
    assert set(mqa.SUMMARY_FIELDS) == (set(mqc.FIELDNAMES) - {"csv_filename"}) | {"pct_with_org"}


def test_the_committed_metrics_file_matches_its_own_summary_csv():
    """The writeup quotes percentiles from the metrics JSON while a reader
    checking the work reads the per-city CSV beside it. Recompute the headline
    distributions from the CSV and require they agree — #106's failure was a
    writeup whose distribution existed nowhere a reader could recompute it."""
    with open("docs/experiments/mapillary-image-quality_metrics.json", encoding="utf-8") as fh:
        metrics = json.load(fh)
    cities = mqa.load_cities("docs/experiments/mapillary-image-quality_cities.csv")

    assert metrics["corpus"]["cities"] == len(cities)
    recomputed = mqa.measure_discrimination(cities)["image_weighted_median"]
    for key in ("n", "p25", "p50", "p75", "iqr", "cities_in_narrow_band"):
        assert recomputed[key] == metrics["discrimination"]["image_weighted_median"][key]
