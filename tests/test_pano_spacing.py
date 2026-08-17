"""
Capture-spacing study (scripts/pano_spacing_analyze.py).

Network-free tests. The critical property is that spacing is measured WITHIN a
capture sequence and never across sequences: many contributors drive the same
streets, so a pooled nearest neighbour is usually someone else's drive and
collapses the interval it claims to measure (measured 2-8x on production
censuses). A refactor that quietly pooled would still produce plausible numbers,
which is exactly why it needs pinning.

The rest is the committed-record contract from CLAUDE.md ("Notes"): the writeup's
numbers must trace to docs/experiments/pano-spacing_metrics.json, and that JSON
must be produced by committed code.
"""

import importlib.util
import json
import os
import sys

import numpy as np
import pandas as pd
import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location(
    "pano_spacing_analyze", os.path.join(PROJECT_ROOT, "scripts", "pano_spacing_analyze.py")
)
psa = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = psa
_spec.loader.exec_module(psa)

DOCS_DIR = os.path.join(PROJECT_ROOT, "docs", "experiments")
COMMITTED_JSON = os.path.join(DOCS_DIR, psa.DOCS_METRICS_NAME)


@pytest.fixture(scope="module")
def committed():
    with open(COMMITTED_JSON, encoding="utf-8") as fh:
        return json.load(fh)


# ── The estimator: within-sequence, never pooled ────────────────────────────


def _two_parallel_drives(n=6, along=10.0, apart=1.0):
    """
    Two straight drives 1 m apart, each capturing every 10 m — the shape that
    separates the two estimators. Within a drive the adjacent capture is 10 m
    away; the nearest image overall is the *other drive's*, 1 m away.
    """
    xs = np.tile(np.arange(n) * along, 2)
    ys = np.concatenate([np.zeros(n), np.full(n, apart)])
    df = pd.DataFrame({"sequence_id": ["A"] * n + ["B"] * n})
    return df, xs, ys


def test_spacing_is_measured_within_a_sequence_not_across_drives():
    df, x, y = _two_parallel_drives()
    within, idx = psa.within_sequence_spacing(df, x, y)

    # Every pano's adjacent capture is its own drive's, 10 m away.
    assert len(within) == len(df)
    assert np.allclose(within, 10.0)
    # Pooling instead would report 1 m — the contamination, ~10x off here.
    pooled = psa.nn_within_group(x, y)
    assert np.allclose(pooled, 1.0)
    # The returned index addresses the original frame, so callers can stratify.
    assert sorted(idx) == list(range(len(df)))


def test_short_sequences_are_excluded_rather_than_measured():
    """A 2-image sequence has one gap and no interior; it is not a drive."""
    df = pd.DataFrame({"sequence_id": ["A", "A", "A", "B", "B"]})
    x = np.array([0.0, 10.0, 20.0, 0.0, 3.0])
    y = np.zeros(5)
    within, idx = psa.within_sequence_spacing(df, x, y)
    assert psa.MIN_SEQUENCE_LEN == 3
    assert len(within) == 3  # only sequence A
    assert set(idx) == {0, 1, 2}
    # B's 3 m gap must not appear — it would drag the median down.
    assert 3.0 not in set(np.round(within, 6))


def test_a_single_sequence_is_unaffected_by_pooling():
    """Sanity floor: with one drive, the two estimators must agree exactly."""
    df = pd.DataFrame({"sequence_id": ["A"] * 5})
    x = np.arange(5) * 7.0
    y = np.zeros(5)
    within, _ = psa.within_sequence_spacing(df, x, y)
    assert np.allclose(np.sort(within), np.sort(psa.nn_within_group(x, y)))


# ── Measurement floor ───────────────────────────────────────────────────────


def test_histogram_bins_stay_above_the_tile_quantization_floor(committed):
    """
    z14 tile coordinates snap position to ~0.4-0.6 m, so distances take discrete
    values. Binning below that resolves the tile lattice as a comb of alternating
    full and empty bins and reads as spurious multi-modality — which the first
    pass at 0.25 m bins actually did.
    """
    bin_width = float(np.diff(psa.SPACING_BINS)[0])
    assert np.allclose(np.diff(psa.SPACING_BINS), bin_width)
    for city, blk in committed["cities"].items():
        assert bin_width > blk["quantization_m"], city


def test_quantization_grows_toward_the_equator():
    """The floor is a function of latitude, which is why it is stored per city."""
    assert psa.quantization_m(0.0) > psa.quantization_m(47.6)
    assert 0.55 < psa.quantization_m(0.0) < 0.65
    assert 0.4 < psa.quantization_m(47.6) < 0.5


# ── The committed record ────────────────────────────────────────────────────


def test_committed_json_names_its_producer(committed):
    assert committed["_about"]["generated_by"] == psa.DOCS_GENERATED_BY
    assert committed["_about"]["writeup"] == "docs/experiments/pano-spacing.md"


def test_spacing_shares_accounts_for_every_value():
    """The tail past the last edge counts toward >20 m, never silently dropped."""
    edges = np.array([0.0, 1.0, 2.0, 21.0, 22.0])
    h = psa.hist(np.array([0.5, 1.5, 10.0, 21.5, 99.0]), edges)
    assert h["n_above_last_edge"] == 1
    sh = psa.spacing_shares(h, 0.45)
    # 21.5 is in a >20 m bin and 99.0 is past the last edge: both count.
    assert sh["beyond_20m_pct"] == pytest.approx(40.0)
    assert sh["n_total"] == 5


@pytest.mark.parametrize("q", ["p25", "p50", "p75"])
def test_committed_percentiles_sit_in_their_histogram_bins(committed, q):
    """Stored percentiles and stored histograms must describe the same array."""
    for city, blk in committed["cities"].items():
        h = blk["distributions"]["within_sequence_m"]
        edges = np.asarray(h["bin_edges"], dtype=float)
        counts = np.asarray(h["counts"], dtype=float)
        value = blk["within_sequence_m"][q]
        target = float(q[1:])
        idx = int(np.searchsorted(edges, value, side="right")) - 1
        below = 100.0 * counts[:idx].sum() / h["n_total"]
        above = 100.0 * counts[: idx + 1].sum() / h["n_total"]
        assert below <= target + 1e-9, (city, q)
        assert above >= target - 1e-9, (city, q)


def test_writeup_shares_recompute_from_committed_histograms(committed):
    """Every share docs/experiments/pano-spacing.md quotes, pinned to the bins."""
    sh = {
        c.split("--")[0]: psa.spacing_shares(
            b["distributions"]["within_sequence_m"], b["quantization_m"]
        )
        for c, b in committed["cities"].items()
    }
    assert {k: round(v["stationary_pct"], 1) for k, v in sh.items()} == {
        "budapest": 7.2,
        "san-francisco": 6.0,
        "hamtramck": 0.6,
    }
    assert {k: round(v["beyond_20m_pct"], 1) for k, v in sh.items()} == {
        "budapest": 12.6,
        "san-francisco": 0.6,
        "hamtramck": 0.0,
    }
    # "77% of panos fall in one 0.5 m bin at 4.9 m" — the regulated fleet.
    assert round(sh["hamtramck"]["peak_share_pct"]) == 77
    assert sh["hamtramck"]["peak_m"] == 4.75

    med = {
        c.split("--")[0]: (b["within_sequence_m"]["p50"], b["pooled_m"]["p50"])
        for c, b in committed["cities"].items()
    }
    assert med == {
        "budapest": (6.65, 1.80),
        "hamtramck": (4.92, 0.62),
        "san-francisco": (2.87, 1.42),
    }
    # The pooling error the writeup quotes as 2-8x.
    ratios = {k: w / p for k, (w, p) in med.items()}
    assert round(ratios["hamtramck"], 1) == 7.9
    assert round(ratios["budapest"], 1) == 3.7
    assert round(ratios["san-francisco"], 1) == 2.0
    # Hamtramck's IQR, quoted as tighter than any GSV area (Adrian's 0.30 m).
    hm = committed["cities"]["hamtramck--michigan--united-states"]["within_sequence_m"]
    assert round(hm["p75"] - hm["p25"], 2) == 0.16


def test_pedestrian_capture_is_denser_than_vehicle_in_the_same_city(committed):
    """The study's stratification claim, pinned per city rather than in aggregate."""
    for city in ("budapest--budapest--hungary", "san-francisco--california--united-states"):
        strata = committed["cities"][city]["strata_m"]
        assert strata["on_foot"]["p50"] < strata["vehicle"]["p50"], city
    bud = committed["cities"]["budapest--budapest--hungary"]["strata_m"]
    assert round(bud["vehicle"]["p50"] / bud["on_foot"]["p50"], 1) == 3.5

    # The writeup's full strata table. Pinned because its first version was
    # transcribed from figure labels that were histogram-derived at the time,
    # putting six of eight cells off by up to 0.04 m against these exact
    # percentiles - which is precisely the drift this contract exists to catch.
    sf = committed["cities"]["san-francisco--california--united-states"]["strata_m"]
    assert {k: bud[k]["p50"] for k in ("vehicle", "on_foot", "organization", "individual")} == {
        "vehicle": 7.05,
        "on_foot": 2.02,
        "organization": 7.59,
        "individual": 6.50,
    }
    assert {k: sf[k]["p50"] for k in ("vehicle", "on_foot", "organization", "individual")} == {
        "vehicle": 3.33,
        "on_foot": 1.89,
        "organization": 2.36,
        "individual": 2.87,
    }


# ── Producer contract ───────────────────────────────────────────────────────


def test_docs_dir_refuses_a_partial_city_set(tmp_path):
    """A committed record built from a subset would under-report the spread."""
    with pytest.raises(SystemExit, match="requires --city all"):
        psa.main(["--docs-dir", str(tmp_path), "--city", "budapest"])


def test_load_census_refuses_a_pre_extras_run(tmp_path):
    """
    Runs before 2026-07-23 carry no sequence_id. Falling back to the pooled
    estimator there would publish a number wrong by 2-8x, so refuse instead.
    """
    path = tmp_path / "oldcity_width_1_height_1_step_20_mapillary_2026-07-06.csv.gz"
    pd.DataFrame(
        {
            "query_lat": [1.0],
            "query_lon": [2.0],
            "pano_lat": [1.0],
            "pano_lon": [2.0],
            "pano_id": ["x"],
            "capture_date": ["2026-01-01"],
            "copyright_info": ["c"],
            "status": ["OK"],
        }
    ).to_csv(path, index=False, compression="gzip")
    with pytest.raises(SystemExit, match="predates the Mapillary extras"):
        psa.load_census(str(path))


def test_figures_from_metrics_needs_only_the_committed_json(tmp_path):
    """
    The census CSVs are gitignored and hundreds of MB; the figures must
    regenerate from what is in git alone.
    """
    import shutil

    shutil.copy(COMMITTED_JSON, tmp_path / psa.DOCS_METRICS_NAME)
    assert (
        psa.main(
            [
                "--figures-from-metrics",
                str(tmp_path / psa.DOCS_METRICS_NAME),
                "--gsv-metrics",
                os.path.join(DOCS_DIR, "grid-density_metrics.json"),
            ]
        )
        == 0
    )
    written = sorted(p.name for p in (tmp_path / "figures").iterdir())
    assert written == [
        psa.DOCS_FIGURE_PREFIX + n + ".png"
        for n in sorted(
            ["provider_comparison", "within_sequence_shape", "pooled_vs_sequence", "capture_setup"]
        )
    ]
