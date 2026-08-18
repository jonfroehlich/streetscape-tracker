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


def test_histogram_shares_accounts_for_every_value():
    """The tail past the last edge counts toward >20 m, never silently dropped."""
    edges = np.array([0.0, 1.0, 2.0, 21.0, 22.0])
    h = psa.hist(np.array([0.5, 1.5, 10.0, 21.5, 99.0]), edges)
    assert h["n_above_last_edge"] == 1
    sh = psa.histogram_shares(h)
    # 21.5 is in a >20 m bin and 99.0 is past the last edge: both count.
    assert sh["beyond_20m_pct"] == pytest.approx(40.0)
    assert sh["n_total"] == 5


def test_stationary_share_uses_a_threshold_no_histogram_can_express():
    """
    The regression that made `stationary_pct` a fixed cut wearing a per-city label.

    quantization_m is ~0.40-0.47 m at these latitudes and SPACING_BINS is 0.5 m
    wide, so EVERY per-city threshold falls strictly inside the first bin: a
    histogram-derived cut selects that one bin and returns "share below 0.5 m"
    for every city. Pinned with distances that straddle the two answers.
    """
    edges = np.asarray(psa.SPACING_BINS, dtype=float)
    centers = (edges[:-1] + edges[1:]) / 2.0
    for q in (0.403, 0.441, 0.472, 0.6):
        assert list(centers[centers < q]) == [0.25], q

    # 0.30 m is below the 0.44 m unit; 0.46 m is above it but inside bin 0.
    within = np.array([0.30, 0.46, 5.0, 5.0])
    assert psa.stationary_share_pct(within, 0.441) == 25.0
    # What the histogram route would have said, for the same array.
    h = psa.hist(within)
    assert 100.0 * np.asarray(h["counts"])[centers < 0.441].sum() / h["n_total"] == 50.0

    assert psa.histogram_shares(h).keys() == {
        "under_1m_pct",
        "beyond_20m_pct",
        "peak_share_pct",
        "peak_m",
        "n_total",
    }


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


def test_writeup_stationary_shares_are_stored_scalars(committed):
    """
    The stationary spike the writeup quotes, at each city's own tile unit.

    Stored rather than recomputed from the bins BECAUSE the threshold is finer
    than one bin — see test_stationary_share_uses_a_threshold_no_histogram_can
    _express. These are the numbers the writeup's finding 4 cites.
    """
    got = {c.split("--")[0]: b["stationary_pct"] for c, b in committed["cities"].items()}
    assert got == {"budapest": 6.45, "san-francisco": 4.42, "hamtramck": 0.35}
    units = {c.split("--")[0]: b["quantization_m"] for c, b in committed["cities"].items()}
    assert units == {"budapest": 0.403, "san-francisco": 0.472, "hamtramck": 0.441}
    # Every unit sits inside the first bin, which is the whole reason for the split.
    assert all(u < psa.SPACING_BINS[1] for u in units.values())


def test_writeup_shares_recompute_from_committed_histograms(committed):
    """Every bin-aligned share docs/experiments/pano-spacing.md quotes."""
    sh = {
        c.split("--")[0]: psa.histogram_shares(b["distributions"]["within_sequence_m"])
        for c, b in committed["cities"].items()
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


def test_hamtramck_org_and_individual_medians_are_identical(committed):
    """
    The one strata cell the transcription drift survived in.

    The writeup claimed these two "differ by 0.03 m"; the record says they are
    the same to the stored precision and separate only in the tails. No
    percentile pair in that block differs by 0.03, so the number came from
    neither - which is what the committed-record contract exists to catch.
    """
    ham = committed["cities"]["hamtramck--michigan--united-states"]["strata_m"]
    assert ham["organization"]["p50"] == ham["individual"]["p50"] == 4.92
    assert (ham["individual"]["p90"], ham["organization"]["p90"]) == (5.14, 4.94)
    deltas = {
        q: round(abs(ham["organization"][q] - ham["individual"][q]), 2)
        for q in ("p5", "p10", "p25", "p50", "p75", "p90", "p95", "p99")
    }
    assert 0.03 not in deltas.values(), deltas

    # An empty stratum is a real, committed case: Hamtramck's fleet is all
    # vehicle, so on_foot carries a count and no percentiles.
    assert ham["on_foot"] == {"n": 0}


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
    pytest.importorskip("matplotlib")
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
    # Names alone would pass on a blank canvas — e.g. a stratum filter that
    # excluded every row, or an exception swallowed into empty axes.
    assert all(p.stat().st_size > 10_000 for p in (tmp_path / "figures").iterdir())


# ── Input selection: the glob is not the contract, naming.py is ─────────────


def _run_name(city_id: str, run_date: str, provider: str = "mapillary") -> str:
    """
    Build an artifact name through the generator, never by hand.

    CLAUDE.md, "Filename parsing is a contract": hand-built names in tests are
    how the streetwalk provider-token collision reached production.
    """
    from datetime import date

    from streetscape_metadata_tracker import naming

    return (
        naming.generate_run_filename(
            city_id, 2953, 3265, 20, date.fromisoformat(run_date), provider=provider
        )
        + ".csv.gz"
    )


def test_only_grid_runs_are_accepted_as_input(tmp_path):
    """
    `*_mapillary_*.csv.gz` also matches streetwalk snapshots and diff files.

    Neither carries sequence_id, so before this they reached load_census and
    failed with a pre-2026-07-23 vintage diagnosis — for a 2026-08 artifact.
    """
    run = _run_name("hamtramck--michigan--united-states", "2026-08-12")
    assert psa.is_mapillary_run(str(tmp_path / run))
    assert psa.city_id_from_run(run) == "hamtramck--michigan--united-states"

    stem = run[: -len(".csv.gz")]
    assert not psa.is_mapillary_run(stem.replace("_mapillary_", "_mapillary_streetwalk_sp15_"))
    assert not psa.is_mapillary_run(
        "hamtramck--michigan--united-states_diff_mapillary_2026-07-01_to_2026-08-12.csv.gz"
    )
    # A GSV run is a run, but not this study's input.
    assert not psa.is_mapillary_run(
        _run_name("hamtramck--michigan--united-states", "2026-08-12", "gsv")
    )


def test_two_run_dates_for_one_city_are_refused(tmp_path):
    """
    Everything downstream keys on city, so the later glob entry would silently
    win and a paid-for run would vanish from the committed record.
    """
    for d in ("2026-07-12", "2026-08-12"):
        (tmp_path / _run_name("hamtramck--michigan--united-states", d)).write_bytes(b"")
    with pytest.raises(SystemExit, match="more than one run per city"):
        psa.main(["--in-dir", str(tmp_path), "--out-dir", str(tmp_path)])


def test_docs_dir_refuses_an_incomplete_study_set(tmp_path):
    """
    `--city all` only means "do not filter the glob" — it says nothing about
    what is on disk. A half-populated in-dir used to write a committed record
    covering fewer cities and exit 0, which is the under-reporting the sibling
    --city guard claims to prevent.
    """
    (tmp_path / _run_name("budapest--budapest--hungary", "2026-08-17")).write_bytes(b"")
    with pytest.raises(SystemExit, match="needs every study city"):
        psa.main(
            ["--in-dir", str(tmp_path), "--out-dir", str(tmp_path), "--docs-dir", str(tmp_path)]
        )


def test_out_dir_cannot_clobber_the_committed_record(tmp_path, monkeypatch):
    """
    The working combined file must not share the committed record's filename.

    `--out-dir docs/experiments` is a plausible invocation and the out-dir copy
    carries no `_about` block, so a shared name would replace the provenance the
    producer contract requires with a file that has none.
    """
    assert psa.WORKING_METRICS_NAME != psa.DOCS_METRICS_NAME

    in_dir, out_dir = tmp_path / "in", tmp_path / "out"
    in_dir.mkdir()
    out_dir.mkdir()
    (in_dir / _run_name("budapest--budapest--hungary", "2026-08-17")).write_bytes(b"")
    sentinel = out_dir / psa.DOCS_METRICS_NAME
    sentinel.write_text(json.dumps({"_about": {"generated_by": "committed"}}))

    monkeypatch.setattr(psa, "analyze_city", lambda p: {"city": psa.city_id_from_run(p)})
    assert psa.main(["--in-dir", str(in_dir), "--out-dir", str(out_dir)]) == 0

    assert json.loads(sentinel.read_text())["_about"]["generated_by"] == "committed"
    assert (out_dir / psa.WORKING_METRICS_NAME).exists()


# ── Capture-setup strata: unknown mode is not "vehicle" ────────────────────


def _synthetic_census(path, on_foot):
    """One 4-image sequence plus an uncovered grid point, written as a run CSV."""
    n = len(on_foot)
    lat, lon = 42.39, -83.05
    rows = {
        "query_lat": [lat + i * 1e-4 for i in range(n)] + [lat + 9e-4],
        "query_lon": [lon] * n + [lon],
        "pano_lat": [lat + i * 1e-4 for i in range(n)] + [None],
        "pano_lon": [lon] * n + [None],
        "pano_id": [str(10**18 + i) for i in range(n)] + [None],
        "capture_date": ["2026-01-01"] * n + [None],
        "copyright_info": ["c"] * n + [None],
        "status": ["OK"] * n + ["ZERO_RESULTS"],
        "organization_id": ["77"] * n + [None],
        "sequence_id": ["A"] * n + [None],
        "is_pano": [True] * n + [None],
        "on_foot": list(on_foot) + [None],
        "quality_score": [0.5] * n + [None],
    }
    pd.DataFrame(rows).to_csv(path, index=False, compression="gzip")
    return path


def test_unknown_capture_mode_is_excluded_from_both_strata(tmp_path):
    """
    `on_foot` is nullable and null means the tile omitted the field — UNKNOWN,
    not "vehicle". Folding unknowns into the vehicle stratum would contaminate
    the vehicle median the pedestrian-vs-vehicle finding rests on, invisibly:
    nothing in the record would say the denominator had moved.
    """
    pytest.importorskip("geopandas")
    path = _synthetic_census(
        tmp_path / _run_name("hamtramck--michigan--united-states", "2026-08-12"),
        [True, None, False, False],
    )
    r = psa.analyze_city(str(path))

    assert r["strata_m"]["on_foot"]["n"] == 1
    assert r["strata_m"]["vehicle"]["n"] == 2  # the unknown is in NEITHER
    assert r["n_foot_known"] == 3
    assert r["share_on_foot_pct"] == round(100.0 / 3, 2)


def test_grid_coverage_is_recomputed_from_the_run_csv(tmp_path):
    """
    The Implications section quotes grid coverage; publishing it here is what
    makes it traceable to committed code rather than to the local catalog.
    """
    pytest.importorskip("geopandas")
    path = _synthetic_census(
        tmp_path / _run_name("hamtramck--michigan--united-states", "2026-08-12"),
        [False, False, False, False],
    )
    # 5 distinct grid points, 4 carrying a 360 pano.
    assert psa.analyze_city(str(path))["grid_coverage_pct"] == 80.0


def test_a_run_with_no_long_enough_sequence_is_refused(tmp_path):
    """
    The estimator is undefined, and emitting empty percentile blocks would defer
    the failure to a KeyError deep in make_figures — after writing a record.
    """
    pytest.importorskip("geopandas")
    path = tmp_path / _run_name("hamtramck--michigan--united-states", "2026-08-12")
    _synthetic_census(path, [False, False, False, False])
    df = pd.read_csv(path)
    df["sequence_id"] = ["A", "B", "C", "D", None]  # every sequence below MIN_SEQUENCE_LEN
    df.to_csv(path, index=False, compression="gzip")
    with pytest.raises(SystemExit, match="no sequence reaches"):
        psa.analyze_city(str(path))


# ── Shared figure styling ──────────────────────────────────────────────────


def test_shared_axis_style_keeps_the_two_studies_ygrid_apart():
    """
    The two experiments' `_style_axis` were near-copies that differed by ONE
    line: grid-density draws horizontal rules, pano-spacing does not. Sharing
    the function is only safe while that difference stays a parameter — merging
    them outright would silently restyle a committed figure in whichever study
    lost the argument.
    """
    pytest.importorskip("matplotlib")
    sys.path.insert(0, os.path.join(PROJECT_ROOT, "scripts"))
    import experiment_style

    plt = experiment_style.agg_pyplot()
    fig, ax = plt.subplots()
    try:
        experiment_style.style_axis(ax, ygrid=False)
        assert not any(line.get_visible() for line in ax.get_ygridlines())
        experiment_style.style_axis(ax, ygrid=True)
        assert all(line.get_visible() for line in ax.get_ygridlines())
        # The parts both studies share, applied either way.
        assert ax.get_facecolor() == plt.matplotlib.colors.to_rgba(experiment_style.SURFACE)
        assert not ax.spines["top"].get_visible()
    finally:
        plt.close(fig)


def test_both_studies_draw_from_one_palette():
    """The two writeups' figures are meant to read as one system."""
    sys.path.insert(0, os.path.join(PROJECT_ROOT, "scripts"))
    import experiment_style

    assert psa.CITY_COLORS == list(experiment_style.CATEGORICAL)
    assert psa.PROVIDER_COLORS["gsv"] == experiment_style.CATEGORICAL[0]
