#!/usr/bin/env python3
"""
Is Mapillary's `quality_score` a usable signal for ranking Sidewalk candidate
cities?

Reads the per-city rows ``mapillary_image_quality_collect.py`` produced and
writes the committed record: ``docs/experiments/mapillary-image-quality_metrics.json``,
the per-city summary CSV beside it, and two figures.

    python scripts/mapillary_image_quality_analyze.py \\
        --cities-csv experiments/mapillary-image-quality/city_quality.csv \\
        --docs-dir docs/experiments --catalog-label makelab2-prod

Four questions, each with its own block in the metrics file:

  discrimination   Does the statistic we already compute -- one median per run,
                   in `mapillary_meta.median_quality_score` -- separate cities?
                   Compared against the tail shares over the same images.
  weighting        Image-weighted vs sequence-weighted. `pano-spacing.md`
                   established that images inside a drive are one observation,
                   not thousands; if the two weightings rank cities differently
                   then the image-weighted number is describing whichever
                   contributor uploaded most.
  on_foot          Does quality track pedestrian capture? This decides whether a
                   quality ranking would rank FOR or AGAINST the imagery a
                   sidewalk deployment wants.
  organization     The same question for organizational capture, which is the
                   other signal `mapillary_meta` already carries.

The cross-city distributions all go through ``scripts/experiment_stats.describe``
so this study quotes the same ruler as every other one. Correlations are
Spearman (rank), computed here rather than pulled from scipy, which is not a
dependency of this repo: the relationships below are monotone-but-not-linear and
a Pearson r over a bounded share would understate them.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from datetime import UTC, datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from experiment_stats import describe, percentile  # noqa: E402
from experiment_style import (  # noqa: E402
    CATEGORICAL,
    GRID,
    INK,
    INK_2,
    SURFACE,
    agg_pyplot,
    style_axis,
)

TOPIC = "mapillary-image-quality"
DOCS_DIR_DEFAULT = "docs/experiments"
FIGURE_PREFIX = TOPIC + "-"
DEFAULT_CITIES_CSV = os.path.join("experiments", TOPIC, "city_quality.csv")

# A city needs both populations before an on-foot/vehicle comparison means
# anything, and "population" has to be counted in DRIVES as well as images: an
# on-foot side of 200 images is routinely one walk, and a delta built from one
# walk against one drive is an anecdote with a percentage sign on it. Both bars
# are deliberately low -- the point is to exclude the single-drive cities, not
# to restrict the study to large ones.
MIN_SIDE_PANOS = 100
MIN_SIDE_SEQUENCES = 3

# The band the image-weighted median actually occupies, used to state the
# compression as a share rather than as an adjective.
NARROW_BAND = (0.80, 0.87)

# Columns the committed per-city CSV carries, in this order: every column the
# collector wrote except `csv_filename` (a committed record should not carry one
# machine's file inventory), plus the derived org share.
#
# Deliberately the WHOLE row rather than the handful the writeup quotes, because
# this file is what a reader recomputes the study from -- `load_cities` reads it
# and `main` re-derives every block, so the committed record can regenerate its
# own metrics without the multi-GB censuses. A test pins this list against the
# collector's, so a column added there cannot quietly stop at the laptop.
SUMMARY_FIELDS = (
    "city_id",
    "run_date",
    "n_panos",
    "n_quality",
    "q_p10",
    "q_p25",
    "q_p50",
    "q_p75",
    "q_p90",
    "pct_ge_good",
    "pct_lt_poor",
    "n_sequences",
    "n_seq_mixed_foot",
    "n_seq_mixed_org",
    "seq_q_p25",
    "seq_q_p50",
    "seq_q_p75",
    "n_foot_known",
    "n_panos_on_foot",
    "pct_on_foot",
    "q_p50_on_foot",
    "q_p50_vehicle",
    "n_seq_on_foot",
    "n_seq_vehicle",
    "seq_q_p50_on_foot",
    "seq_q_p50_vehicle",
    "n_with_org",
    "n_distinct_orgs",
    "q_p50_org",
    "q_p50_no_org",
    "n_seq_org",
    "n_seq_no_org",
    "seq_q_p50_org",
    "seq_q_p50_no_org",
    "pct_with_org",
)


def docs_generated_by(docs_dir: str, cities_csv: str, label: str) -> str:
    """The exact command that reproduces the committed metrics file.

    Carries `--catalog-label` for the same reason `undated_imagery_share` does:
    which catalog was read is not recoverable from the numbers afterwards, and a
    dev laptop holds a handful of Mapillary runs against production's hundreds.
    """
    return (
        "python scripts/mapillary_image_quality_analyze.py "
        f"--cities-csv {cities_csv} --docs-dir {docs_dir} --catalog-label {label}"
    )


def _f(value: str) -> float | None:
    """CSV cell -> float, with the collector's empty cell meaning 'no sample'."""
    return float(value) if value not in ("", None) else None


def load_cities(path: str) -> list[dict]:
    with open(path, encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh))
    out = []
    for row in rows:
        city = {
            "city_id": row["city_id"],
            "run_date": row["run_date"],
            "n_panos": int(row["n_panos"]),
            "n_quality": int(row["n_quality"]),
            "n_sequences": int(row["n_sequences"]),
            "n_seq_mixed_foot": int(row["n_seq_mixed_foot"]),
            "n_seq_mixed_org": int(row["n_seq_mixed_org"]),
            "n_foot_known": int(row["n_foot_known"]),
            "n_panos_on_foot": int(row["n_panos_on_foot"]),
            "n_seq_on_foot": int(row["n_seq_on_foot"]),
            "n_seq_vehicle": int(row["n_seq_vehicle"]),
            "n_with_org": int(row["n_with_org"]),
            "n_distinct_orgs": int(row["n_distinct_orgs"]),
            "n_seq_org": int(row["n_seq_org"]),
            "n_seq_no_org": int(row["n_seq_no_org"]),
        }
        for key in (
            "q_p10",
            "q_p25",
            "q_p50",
            "q_p75",
            "q_p90",
            "pct_ge_good",
            "pct_lt_poor",
            "seq_q_p25",
            "seq_q_p50",
            "seq_q_p75",
            "pct_on_foot",
            "q_p50_on_foot",
            "q_p50_vehicle",
            "seq_q_p50_on_foot",
            "seq_q_p50_vehicle",
            "q_p50_org",
            "q_p50_no_org",
            "seq_q_p50_org",
            "seq_q_p50_no_org",
        ):
            city[key] = _f(row[key])
        # Derived here rather than in the collector: it is a ratio of two counts
        # the collector already wrote, so deriving it keeps one source of truth.
        city["pct_with_org"] = (
            round(100.0 * city["n_with_org"] / city["n_panos"], 4) if city["n_panos"] else None
        )
        out.append(city)
    return out


def _ranks(values: list[float]) -> list[float]:
    """Average ranks, so ties do not fabricate an ordering."""
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        shared = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            ranks[order[k]] = shared
        i = j + 1
    return ranks


def spearman(xs: list[float], ys: list[float]) -> float | None:
    """Pearson r over average ranks. None for a sample too small to have one."""
    if len(xs) < 3 or len(xs) != len(ys):
        return None
    rx, ry = _ranks(xs), _ranks(ys)
    n = len(rx)
    mx, my = sum(rx) / n, sum(ry) / n
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry, strict=True))
    dx = sum((a - mx) ** 2 for a in rx) ** 0.5
    dy = sum((b - my) ** 2 for b in ry) ** 0.5
    if dx == 0 or dy == 0:
        return None
    return round(num / (dx * dy), 4)


def _iqr(values: list[float]) -> float:
    return round(percentile(values, 75) - percentile(values, 25), 4)


def band_cities(cities: list[dict]) -> list[dict]:
    """The cities whose medians fall in NARROW_BAND -- the ones the published
    statistic calls interchangeable. Shared by the metrics and the figure so
    the picture and the JSON cannot describe different populations."""
    return [
        c
        for c in cities
        if c["q_p50"] is not None
        and NARROW_BAND[0] <= c["q_p50"] <= NARROW_BAND[1]
        and c["pct_ge_good"] is not None
    ]


def _band_medians(band: list[dict]) -> list[float]:
    return [c["q_p50"] for c in band]


def measure_discrimination(cities: list[dict]) -> dict:
    """How much do the candidate statistics separate cities from each other?

    The comparison the study exists to make. All three are computed over the
    SAME images in the same cities, so the difference in spread is a property of
    the statistic and not of the sample.
    """
    medians = [c["q_p50"] for c in cities if c["q_p50"] is not None]
    good = [c["pct_ge_good"] for c in cities if c["pct_ge_good"] is not None]
    poor = [c["pct_lt_poor"] for c in cities if c["pct_lt_poor"] is not None]
    band = band_cities(cities)
    return {
        "image_weighted_median": {
            **describe(medians),
            "iqr": _iqr(medians),
            "narrow_band": list(NARROW_BAND),
            "cities_in_narrow_band": len(band),
            "pct_cities_in_narrow_band": round(100.0 * len(band) / len(medians), 2)
            if medians
            else None,
        },
        "pct_images_ge_good": {**describe(good), "iqr": _iqr(good)},
        "pct_images_lt_poor": {**describe(poor), "iqr": _iqr(poor)},
        # The claim the study actually rests on, isolated so it can be checked:
        # take only the cities the median calls interchangeable and ask how far
        # apart the tail share puts them. A wide spread here means the median is
        # discarding a real ordering rather than reporting that none exists.
        "within_narrow_band": {
            "cities": len(band),
            "pct_images_ge_good": describe([c["pct_ge_good"] for c in band]),
            "median_spread": round(
                max(m for m in _band_medians(band)) - min(_band_medians(band)), 4
            )
            if band
            else None,
        },
        "note": (
            "The median is what mapillary_meta already stores and the only one "
            "of the three published anywhere. Compare its IQR against the range "
            "the tail shares occupy before reading a city ranking off it, and "
            "read `within_narrow_band` before concluding the cities it bunches "
            "together are genuinely alike."
        ),
    }


def measure_weighting(cities: list[dict]) -> dict:
    """Image-weighted vs sequence-weighted quality: do they rank cities alike?"""
    both = [c for c in cities if c["q_p50"] is not None and c["seq_q_p50"] is not None]
    deltas = [round(c["q_p50"] - c["seq_q_p50"], 4) for c in both]
    image_ranks = _ranks([c["q_p50"] for c in both])
    seq_ranks = _ranks([c["seq_q_p50"] for c in both])
    displacement = [abs(a - b) for a, b in zip(image_ranks, seq_ranks, strict=True)]
    moved = [
        {
            "city_id": c["city_id"],
            "n_panos": c["n_panos"],
            "n_sequences": c["n_sequences"],
            "q_p50": c["q_p50"],
            "seq_q_p50": c["seq_q_p50"],
            "rank_displacement": round(d, 1),
        }
        for c, d in sorted(zip(both, displacement, strict=True), key=lambda t: -t[1])[:5]
    ]
    return {
        "cities": len(both),
        "sequences_per_city": describe([float(c["n_sequences"]) for c in both], digits=1),
        "panos_per_sequence": describe(
            [c["n_panos"] / c["n_sequences"] for c in both if c["n_sequences"]], digits=2
        ),
        "sequence_weighted_median": describe([c["seq_q_p50"] for c in both]),
        "image_minus_sequence": describe(deltas),
        "spearman_image_vs_sequence": spearman(
            [c["q_p50"] for c in both], [c["seq_q_p50"] for c in both]
        ),
        # The assumption every per-class number in this study rests on, counted
        # rather than assumed: assigning a drive to the on-foot or the
        # organizational class is only meaningful if the drive HAS one class.
        # Measured over the whole corpus so it is a finding about Mapillary's
        # data model, not a spot check.
        "sequences_with_mixed_on_foot": sum(c["n_seq_mixed_foot"] for c in both),
        "sequences_with_mixed_organization": sum(c["n_seq_mixed_org"] for c in both),
        "rank_displacement": describe([float(d) for d in displacement], digits=1),
        "largest_rank_displacements": moved,
        "note": (
            "A sequence is one contributor's drive, so its images are one "
            "observation sampled every few metres, not thousands of independent "
            "ones (pano-spacing.md). The sequence-weighted median takes one "
            "value per drive."
        ),
    }


def paired_cities(cities: list[dict], a: str, b: str, n_a: str, n_b: str, seq_a: str, seq_b: str):
    """Cities where BOTH sides of a within-city comparison are real populations.

    Real means images AND drives: a side that is one sequence is one
    observation, however many images it holds, which is `pano-spacing.md`'s rule
    applied to a subgroup. The first pass of this study filtered on images only
    and the on-foot headline was built partly on single-walk cities.
    """
    out = []
    for c in cities:
        if c[a] is None or c[b] is None:
            continue
        if min(c[n_a], c[n_b]) < MIN_SIDE_PANOS:
            continue
        if min(c[seq_a], c[seq_b]) < MIN_SIDE_SEQUENCES:
            continue
        out.append(c)
    return out


def _paired_block(paired: list[dict], a: str, b: str, direction: str) -> dict:
    """The delta distribution for one paired comparison, image- or drive-weighted."""
    deltas = [round(c[a] - c[b], 4) for c in paired]
    n_hit = sum(1 for d in deltas if (d < 0 if direction == "lower" else d > 0))
    return {
        "delta": describe(deltas),
        f"cities_{direction}": n_hit,
        f"pct_cities_{direction}": round(100.0 * n_hit / len(paired), 2) if paired else None,
    }


def measure_on_foot(cities: list[dict]) -> dict:
    """Does quality track pedestrian capture -- the imagery Sidewalk wants?"""
    with_share = [c for c in cities if c["pct_on_foot"] is not None and c["q_p50"] is not None]
    # n_foot_known counts BOTH classes, so the vehicle side is the remainder.
    for c in cities:
        c["_n_vehicle"] = c["n_foot_known"] - c["n_panos_on_foot"]
    paired = paired_cities(
        cities,
        "q_p50_on_foot",
        "q_p50_vehicle",
        "n_panos_on_foot",
        "_n_vehicle",
        "n_seq_on_foot",
        "n_seq_vehicle",
    )
    return {
        "pct_on_foot_across_cities": describe([c["pct_on_foot"] for c in with_share], digits=2),
        "cities_with_any_on_foot": sum(1 for c in cities if c["n_panos_on_foot"] > 0),
        "spearman_pct_on_foot_vs_median_quality": spearman(
            [c["pct_on_foot"] for c in with_share], [c["q_p50"] for c in with_share]
        ),
        "paired_cities": len(paired),
        "min_side_panos": MIN_SIDE_PANOS,
        "min_side_sequences": MIN_SIDE_SEQUENCES,
        "sequences_per_side": {
            "on_foot": describe([float(c["n_seq_on_foot"]) for c in paired], digits=1),
            "vehicle": describe([float(c["n_seq_vehicle"]) for c in paired], digits=1),
        },
        "image_weighted": _paired_block(paired, "q_p50_on_foot", "q_p50_vehicle", "lower"),
        "sequence_weighted": _paired_block(
            [c for c in paired if c["seq_q_p50_on_foot"] is not None],
            "seq_q_p50_on_foot",
            "seq_q_p50_vehicle",
            "lower",
        ),
        "note": (
            "A within-city paired comparison, so it is not confounded by which "
            "cities happen to have pedestrian capture. A city counts only when "
            f"BOTH sides carry >= {MIN_SIDE_PANOS} scored panos across "
            f">= {MIN_SIDE_SEQUENCES} distinct drives. Both weightings are "
            "reported: the image-weighted delta is what a labeller meets, the "
            "sequence-weighted one is what survives if a single long walk is "
            "not allowed to be the whole finding."
        ),
    }


def measure_organization(cities: list[dict]) -> dict:
    """The same paired treatment for organizational vs individual capture.

    Worth measuring rather than assuming: `mapillary_meta` already carries
    pct_with_org, and the obvious reading -- an organization means a fleet means
    a good camera -- is a hypothesis, not a fact about the data.
    """
    with_share = [c for c in cities if c["pct_with_org"] is not None and c["q_p50"] is not None]
    for c in cities:
        c["_n_individual"] = c["n_panos"] - c["n_with_org"]
    paired = paired_cities(
        cities,
        "q_p50_org",
        "q_p50_no_org",
        "n_with_org",
        "_n_individual",
        "n_seq_org",
        "n_seq_no_org",
    )
    return {
        "pct_with_org_across_cities": describe([c["pct_with_org"] for c in with_share], digits=2),
        "distinct_orgs_across_cities": describe(
            [float(c["n_distinct_orgs"]) for c in cities], digits=1
        ),
        "spearman_pct_with_org_vs_median_quality": spearman(
            [c["pct_with_org"] for c in with_share], [c["q_p50"] for c in with_share]
        ),
        "paired_cities": len(paired),
        "image_weighted": _paired_block(paired, "q_p50_org", "q_p50_no_org", "higher"),
        "sequence_weighted": _paired_block(
            [c for c in paired if c["seq_q_p50_org"] is not None],
            "seq_q_p50_org",
            "seq_q_p50_no_org",
            "higher",
        ),
    }


def make_figures(cities: list[dict], fig_dir: str) -> list[str]:
    """Two figures: what the median hides, and what on-foot capture costs."""
    plt = agg_pyplot()
    os.makedirs(fig_dir, exist_ok=True)
    written = []

    # 1. The discrimination claim, drawn as the argument rather than as two
    # marginal distributions. The right panel is deliberately NOT every city:
    # plotting all of them shows a zero-heavy pile that says little, because
    # most cities in the catalog have almost no high-scoring imagery. The
    # question is whether the median discards an ordering, so the right panel
    # takes exactly the cities the median calls interchangeable and shows how
    # far apart the tail share puts them.
    medians = [c["q_p50"] for c in cities if c["q_p50"] is not None]
    band = band_cities(cities)
    band_good = [c["pct_ge_good"] for c in band]

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(8.6, 3.7), facecolor=SURFACE)
    style_axis(ax1, ygrid=True)
    style_axis(ax2, ygrid=True)

    ax1.axvspan(*NARROW_BAND, color=CATEGORICAL[1], alpha=0.16, zorder=0)
    ax1.hist(medians, bins=40, range=(0.0, 1.0), color=CATEGORICAL[0], zorder=2)
    ax1.set_xlim(0, 1)
    ax1.set_xlabel("City median quality_score", color=INK_2, fontsize=10)
    ax1.set_ylabel("Cities", color=INK_2, fontsize=10)
    ax1.set_title(
        f"What mapillary_meta stores today (n={len(medians)})",
        color=INK,
        fontsize=10.5,
        loc="left",
        pad=8,
    )
    ax1.annotate(
        f"{len(band)} cities inside a\n{NARROW_BAND[1] - NARROW_BAND[0]:.2f}-wide band",
        xy=(NARROW_BAND[0] - 0.03, 0.82),
        xycoords=("data", "axes fraction"),
        ha="right",
        color=INK_2,
        fontsize=9,
    )

    ax2.hist(band_good, bins=27, range=(0.0, 27.0), color=CATEGORICAL[1])
    ax2.set_xlim(0, 27)
    ax2.set_xlabel("% of a city's panos scoring ≥ 0.9", color=INK_2, fontsize=10)
    ax2.set_title(
        f"Those same {len(band)} cities, counted at the tail",
        color=INK,
        fontsize=10.5,
        loc="left",
        pad=8,
    )
    if band_good:
        ax2.annotate(
            f"spread {min(band_good):.1f}%–{max(band_good):.1f}%",
            xy=(0.97, 0.88),
            xycoords="axes fraction",
            ha="right",
            color=INK_2,
            fontsize=9,
        )
    fig.suptitle(
        "The cities the median calls identical are not identical",
        color=INK,
        fontsize=11.5,
        x=0.01,
        ha="left",
    )
    fig.tight_layout(rect=(0, 0, 1, 0.93))
    p = os.path.join(fig_dir, FIGURE_PREFIX + "median_vs_tail.png")
    fig.savefig(p, dpi=150, facecolor=SURFACE)
    plt.close(fig)
    written.append(p)

    # 2. The on-foot relationship, as a within-city paired plot rather than a
    # scatter of city medians against city on-foot share: the paired form is the
    # one that cannot be explained by which cities have pedestrian capture.
    for c in cities:
        c["_n_vehicle"] = c["n_foot_known"] - c["n_panos_on_foot"]
    paired = paired_cities(
        cities,
        "q_p50_on_foot",
        "q_p50_vehicle",
        "n_panos_on_foot",
        "_n_vehicle",
        "n_seq_on_foot",
        "n_seq_vehicle",
    )
    paired.sort(key=lambda c: c["q_p50_vehicle"])

    fig, ax = plt.subplots(figsize=(7.6, 4.4), facecolor=SURFACE)
    style_axis(ax)
    ax.xaxis.grid(True, color=GRID, linewidth=0.8)
    for i, c in enumerate(paired):
        ax.plot(
            [c["q_p50_vehicle"], c["q_p50_on_foot"]],
            [i, i],
            color=GRID,
            linewidth=1.4,
            zorder=1,
            solid_capstyle="round",
        )
    ax.scatter(
        [c["q_p50_vehicle"] for c in paired],
        range(len(paired)),
        s=26,
        color=CATEGORICAL[0],
        zorder=2,
        label="vehicle capture",
    )
    ax.scatter(
        [c["q_p50_on_foot"] for c in paired],
        range(len(paired)),
        s=26,
        color=CATEGORICAL[1],
        zorder=2,
        label="on-foot capture",
    )
    ax.set_yticks([])
    ax.set_ylabel(f"{len(paired)} cities with both populations", color=INK_2, fontsize=10)
    ax.set_xlabel("Median quality_score within the city", color=INK_2, fontsize=10)
    # Upper left: rows are sorted by the vehicle median, so the bottom-right
    # corner holds the lowest-scoring cities' points and a legend there sits on
    # top of them.
    ax.legend(frameon=False, fontsize=9, labelcolor=INK_2, loc="upper left")
    ax.set_title(
        "Within a city, Mapillary scores on-foot capture below vehicle capture",
        color=INK,
        fontsize=11,
        loc="left",
        pad=14,
    )
    ax.annotate(
        "one row per city · line joins the two medians over the same census",
        xy=(0, 1.0),
        xycoords="axes fraction",
        xytext=(0, 5),
        textcoords="offset points",
        color=INK_2,
        fontsize=8.5,
    )
    fig.tight_layout()
    p = os.path.join(fig_dir, FIGURE_PREFIX + "on_foot_penalty.png")
    fig.savefig(p, dpi=150, facecolor=SURFACE)
    plt.close(fig)
    written.append(p)
    return written


def write_summary_csv(cities: list[dict], path: str) -> None:
    """The per-city rows, committed beside the writeup.

    Small enough to commit (one row per city) and it is the evidence every
    quoted percentile is computed from -- #106's failure mode was a writeup
    whose distribution lived only in a gitignored directory on one laptop.
    """
    with open(path, "w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(SUMMARY_FIELDS), extrasaction="ignore")
        writer.writeheader()
        for city in sorted(cities, key=lambda c: c["city_id"]):
            writer.writerow(city)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    parser.add_argument("--cities-csv", default=DEFAULT_CITIES_CSV, help="collector output")
    parser.add_argument("--docs-dir", default=None, help=f"write {TOPIC}_metrics.json here")
    parser.add_argument(
        "--catalog-label",
        default="unspecified",
        help="which catalog the collector read, e.g. 'makelab2-prod' (recorded, not a path)",
    )
    parser.add_argument("--no-figures", action="store_true", help="skip the figures")
    args = parser.parse_args()

    cities = load_cities(args.cities_csv)
    if not cities:
        print(f"No rows in {args.cities_csv}", file=sys.stderr)
        return 1

    docs_dir = args.docs_dir or DOCS_DIR_DEFAULT
    metrics = {
        "_about": {
            "experiment": TOPIC,
            "writeup": f"docs/experiments/{TOPIC}.md",
            "generated_by": docs_generated_by(docs_dir, args.cities_csv, args.catalog_label),
            "collected_by": (
                "python scripts/mapillary_image_quality_collect.py "
                f"--out-dir experiments/{TOPIC} --catalog-label {args.catalog_label}"
            ),
            "catalog_label": args.catalog_label,
            "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "note": (
                "Mapillary's per-image quality_score -- 'predicted visual quality of the "
                "image in the range [0.0, 1.0]' -- read over each city's LATEST Mapillary "
                "census, 360-degree panos only (status OK or NO_DATE). One run per city, "
                "so the distribution is not weighted by how often the scheduler happened "
                "to collect a city. Runs written before the 2026-07-24 enriched schema "
                "carry no quality_score column and are excluded rather than counted as "
                "zero; the collector's manifest records how many. No data_dir is recorded "
                "-- catalog_label names which catalog was read instead."
            ),
        },
        "corpus": {
            "cities": len(cities),
            "panos": sum(c["n_panos"] for c in cities),
            "panos_scored": sum(c["n_quality"] for c in cities),
            "sequences": sum(c["n_sequences"] for c in cities),
            "panos_per_city": describe([float(c["n_panos"]) for c in cities], digits=1),
            "run_date_range": [
                min(c["run_date"] for c in cities),
                max(c["run_date"] for c in cities),
            ],
        },
        "discrimination": measure_discrimination(cities),
        "weighting": measure_weighting(cities),
        "on_foot": measure_on_foot(cities),
        "organization": measure_organization(cities),
    }

    disc = metrics["discrimination"]
    print(
        f"{len(cities)} cities, {metrics['corpus']['panos']:,} scored panos "
        f"({metrics['corpus']['sequences']:,} sequences)"
    )
    print(
        f"  city median quality: p25 {disc['image_weighted_median']['p25']} "
        f"p50 {disc['image_weighted_median']['p50']} "
        f"p75 {disc['image_weighted_median']['p75']} "
        f"(IQR {disc['image_weighted_median']['iqr']}, "
        f"{disc['image_weighted_median']['pct_cities_in_narrow_band']}% inside "
        f"{NARROW_BAND[0]}-{NARROW_BAND[1]})"
    )
    print(
        f"  % panos >= 0.9:      p25 {disc['pct_images_ge_good']['p25']} "
        f"p50 {disc['pct_images_ge_good']['p50']} "
        f"p75 {disc['pct_images_ge_good']['p75']} "
        f"(IQR {disc['pct_images_ge_good']['iqr']})"
    )
    foot = metrics["on_foot"]
    print(
        f"  on-foot vs vehicle:  {foot['paired_cities']} paired cities "
        f"(>= {MIN_SIDE_PANOS} panos and >= {MIN_SIDE_SEQUENCES} drives per side)"
    )
    for weighting in ("image_weighted", "sequence_weighted"):
        block = foot[weighting]
        print(
            f"    {weighting:<18} median delta {block['delta'].get('p50')}, "
            f"{block['pct_cities_lower']}% of cities score on-foot lower"
        )
    print(
        "    spearman(on-foot share, city median quality) = "
        f"{foot['spearman_pct_on_foot_vs_median_quality']} -- the cross-city view"
    )

    if args.docs_dir:
        os.makedirs(args.docs_dir, exist_ok=True)
        out = os.path.join(args.docs_dir, f"{TOPIC}_metrics.json")
        with open(out, "w", encoding="utf-8") as fh:
            json.dump(metrics, fh, indent=2, sort_keys=False)
            fh.write("\n")
        print(f"Wrote {out}")
        summary = os.path.join(args.docs_dir, f"{TOPIC}_cities.csv")
        write_summary_csv(cities, summary)
        print(f"Wrote {summary}")
        if not args.no_figures:
            for p in make_figures(cities, os.path.join(args.docs_dir, "figures")):
                print(f"Wrote {p}")
    else:
        print(json.dumps(metrics, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
