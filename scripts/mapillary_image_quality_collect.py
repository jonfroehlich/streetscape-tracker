#!/usr/bin/env python3
"""
Collection half of the Mapillary `quality_score` study: reduce every city's
latest Mapillary census to one row of distribution statistics.

Mapillary publishes a per-image ``quality_score`` -- "predicted visual quality
of the image in the range [0.0, 1.0]" -- on the same z14 tiles the downloader
already fetches, so we have carried it, free, on every pano row since
2026-07-24. The per-run JSON keeps exactly one number from it
(``mapillary_meta.median_quality_score``), and the question this study exists to
answer is whether that signal can rank candidate cities for a Project Sidewalk
deployment.

    python scripts/mapillary_image_quality_collect.py --out-dir experiments/mapillary-image-quality

This is the expensive half and it runs where the run CSVs are (production):
~500 censuses, several GB of gzip, tens of millions of image rows. It reads five
columns, in chunks, and writes ONE row per city -- a few hundred rows, small
enough to travel to a laptop and be committed beside the writeup. The analysis
half (``mapillary_image_quality_analyze.py``) never touches a census CSV.

WHAT A ROW COUNTS
-----------------
Only ``status`` OK or NO_DATE, which for Mapillary is the 360-degree pano
census -- the same population ``json_summarizer.compute_mapillary_meta``
summarizes, and the population a Sidewalk deployment can actually use. The
extra columns are written on FLAT_ONLY and ZERO_RESULTS rows too (they carry
the flat image's metadata, or nothing), so a share taken over every row would
describe the grid rather than the imagery. That is #226's lesson in the
capture-date study, arriving here as a denominator.

Note that a Mapillary run CSV is a CENSUS: each of these rows is one IMAGE, not
one grid point (issue #289). ``n_panos`` here is an image count and is not
comparable to a GSV run's point count.

TWO WEIGHTINGS, BOTH RECORDED
-----------------------------
``pano-spacing.md`` established that any per-image Mapillary statistic has to
group by ``sequence_id`` before it means anything: images inside one drive are
not independent observations, they are one contributor's camera sampled every
few metres. Quality is a property of that camera and that drive, so a city's
image-weighted quality distribution is really a distribution over its LARGEST
sequences. Both weightings are therefore computed per city:

  image-weighted     percentiles over every pano row (what a naive read gives)
  sequence-weighted  percentiles over per-sequence MEDIAN quality, one value
                     per drive (what treats a drive as the observation)

The writeup reports the gap. Neither is "correct" on its own -- an image-weighted
number answers "what will a labeller see?", a sequence-weighted one answers "how
many distinct capture efforts are good?" -- but quoting one without knowing the
other is how a ranking gets built on a handful of drives.

PERCENTILES
-----------
Per-city percentiles are numpy's default linear interpolation, which is the same
definition as ``scripts/experiment_stats.percentile`` -- pinned by a test, since
the studies are meant to quote one ruler. numpy rather than the stdlib helper
only because the samples here are millions of values per city; the CROSS-CITY
distributions the writeup actually quotes go through ``experiment_stats`` in the
analysis half.

LEGACY RUNS ARE SKIPPED, NOT ZEROED
-----------------------------------
A Mapillary CSV written before 2026-07-24 has no ``quality_score`` column at
all. Those runs are counted in ``runs_legacy_schema`` and excluded, never
folded in as an absent or zero score: "not measured" and "measured zero" are
different findings and a study that cannot tell them apart is not measuring.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import os
import sys
from datetime import UTC, datetime

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from streetscape_metadata_tracker import db  # noqa: E402
from streetscape_metadata_tracker.paths import get_default_data_dir  # noqa: E402

TOPIC = "mapillary-image-quality"
DEFAULT_OUT_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "experiments", TOPIC
)

# The pano census, i.e. what compute_mapillary_meta summarizes. FLAT_ONLY and
# ZERO_RESULTS rows are excluded on purpose -- see WHAT A ROW COUNTS above.
PANO_STATUSES = ("OK", "NO_DATE")

# Read only what the statistics need. A Mapillary census carries 16 columns and
# the widest of them (copyright_info) is pure text we never look at, so naming
# five here is most of the difference between a 20-minute pass and an hour's.
NEEDED_COLUMNS = ("status", "quality_score", "on_foot", "organization_id", "sequence_id")

# Rows per chunk. Bounded memory is the point: the largest census in the catalog
# is ~2.7M pano rows and this script runs beside the nightly batch.
CHUNK_ROWS = 500_000

# The two tail shares the study reports. The median compresses every city into
# ~0.80-0.87 (measured); these are where the cities actually separate, so they
# are the headline rather than a supplementary cut.
GOOD_THRESHOLD = 0.90
POOR_THRESHOLD = 0.60

# One row per city. Declared here rather than built ad hoc so the committed CSV
# has a pinned column order a test can assert against.
FIELDNAMES = (
    "city_id",
    "run_date",
    "csv_filename",
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
)


def latest_mapillary_runs(conn) -> list[tuple[str, str, str]]:
    """(city_id, run_date, csv_filename) for each city's newest Mapillary run.

    One run per city, never the whole series: a city collected six times would
    otherwise contribute six near-identical rows and quietly weight the
    cross-city distribution by collection frequency, which is a property of the
    scheduler rather than of the imagery.
    """
    rows = conn.execute(
        """SELECT r.city_id, r.run_date, r.csv_filename
             FROM runs r
             JOIN (SELECT city_id, MAX(run_date) AS run_date
                     FROM runs WHERE provider = 'mapillary'
                    GROUP BY city_id) latest
               ON latest.city_id = r.city_id AND latest.run_date = r.run_date
            WHERE r.provider = 'mapillary'
            ORDER BY r.city_id"""
    ).fetchall()
    # MAX(run_date) can tie when a city was collected twice on one date under
    # different geometry; take the first deterministically rather than emitting
    # the city twice.
    seen: set[str] = set()
    out = []
    for city_id, run_date, csv_filename in rows:
        if city_id in seen:
            continue
        seen.add(city_id)
        out.append((city_id, run_date, csv_filename))
    return out


def _has_quality_column(path: str) -> bool:
    """Does this CSV predate the 2026-07-24 enriched Mapillary schema?

    Reads the header line only. A pandas `usecols` miss would raise mid-parse
    after the file was already half-decompressed, and the answer is one line in.
    """
    with gzip.open(path, "rt", encoding="utf-8", newline="") as fh:
        header = fh.readline()
    return "quality_score" in next(csv.reader([header]), [])


def _pct(numerator: int, denominator: int) -> float | None:
    return round(100.0 * numerator / denominator, 4) if denominator else None


def _percentiles(values: np.ndarray, points: tuple[int, ...]) -> list[float | None]:
    """numpy's linear interpolation, or Nones for an empty sample.

    Same definition as experiment_stats.percentile (pinned by a test). Empty ->
    None rather than nan, because this lands in a CSV and in JSON, where nan is
    not representable and None reads as "no sample" in both.
    """
    if values.size == 0:
        return [None] * len(points)
    return [round(float(v), 4) for v in np.percentile(values, points)]


def _median_or_none(values: np.ndarray) -> float | None:
    return round(float(np.median(values)), 4) if values.size else None


class CityAccumulator:
    """Streams one city's pano rows into the statistics its row needs.

    Holds per-image quality and a factorized sequence code, so the largest city
    in the catalog costs tens of MB rather than the hundreds a string-keyed
    frame would. Everything else is a running count.
    """

    def __init__(self) -> None:
        self._quality: list[np.ndarray] = []
        self._seq_codes: list[np.ndarray] = []
        self._foot: list[np.ndarray] = []  # 1 on foot, 0 vehicle, -1 unknown
        self._has_org: list[np.ndarray] = []
        self._seq_ids: dict[str, int] = {}
        self._org_ids: set[str] = set()
        self.n_panos = 0

    def add(self, chunk: pd.DataFrame) -> None:
        panos = chunk[chunk["status"].isin(PANO_STATUSES)]
        if panos.empty:
            return
        self.n_panos += len(panos)

        quality = pd.to_numeric(panos["quality_score"], errors="coerce").to_numpy(dtype="float64")

        seq = panos["sequence_id"].astype("object")
        codes = np.empty(len(panos), dtype="int64")
        for i, value in enumerate(seq):
            if value is None or (isinstance(value, float) and np.isnan(value)):
                codes[i] = -1
                continue
            key = str(value)
            code = self._seq_ids.get(key)
            if code is None:
                code = len(self._seq_ids)
                self._seq_ids[key] = code
            codes[i] = code

        # on_foot arrives as a nullable bool from the Mapillary dtypes, but a
        # CSV read without them gives object/str, so normalize through pandas
        # rather than trusting either.
        foot_raw = panos["on_foot"]
        foot = np.full(len(panos), -1, dtype="int8")
        truthy = foot_raw.astype("string").str.lower()
        foot[truthy.isin(["true", "1", "1.0"]).to_numpy()] = 1
        foot[truthy.isin(["false", "0", "0.0"]).to_numpy()] = 0

        org = panos["organization_id"].astype("string")
        has_org = org.notna().to_numpy()
        self._org_ids.update(org.dropna().unique().tolist())

        self._quality.append(quality)
        self._seq_codes.append(codes)
        self._foot.append(foot)
        self._has_org.append(has_org)

    def finish(self, city_id: str, run_date: str, csv_filename: str) -> dict:
        quality = np.concatenate(self._quality) if self._quality else np.empty(0, dtype="float64")
        seq_codes = (
            np.concatenate(self._seq_codes) if self._seq_codes else np.empty(0, dtype="int64")
        )
        foot = np.concatenate(self._foot) if self._foot else np.empty(0, dtype="int8")
        has_org = np.concatenate(self._has_org) if self._has_org else np.empty(0, dtype="bool")

        scored = np.isfinite(quality)
        q = quality[scored]
        p10, p25, p50, p75, p90 = _percentiles(q, (10, 25, 50, 75, 90))

        # Sequence-weighted: one median per drive, then the distribution over
        # drives. Images with no sequence_id (code -1) are dropped from this cut
        # only -- they have no drive to belong to, and inventing one per image
        # would make the sequence-weighted number converge on the image-weighted
        # one, which is precisely the comparison being made.
        #
        # The per-class counts come off the same groupby rather than a second
        # pass, and they are what makes the on-foot comparison a comparison: an
        # "on-foot population" of 200 images can be ONE walk, and a delta built
        # from one walk on each side is an anecdote with a percentage sign.
        #
        # Assigning a class to a drive assumes on_foot and organization_id are
        # drive-level, not image-level. That is an assumption about a vendor's
        # data, so it is COUNTED rather than asserted: `n_seq_mixed_foot` and
        # `n_seq_mixed_org` report sequences carrying more than one known value,
        # and the study reports the total. Where one does occur, the MAX wins,
        # so a mixed drive counts as on-foot/organizational rather than being
        # dropped -- the conservative direction for a study asking whether
        # pedestrian capture scores worse.
        seq_medians = np.empty(0, dtype="float64")
        seq_foot = np.empty(0, dtype="int8")
        seq_org = np.empty(0, dtype="bool")
        n_sequences = n_mixed_foot = n_mixed_org = 0
        usable = scored & (seq_codes >= 0)
        if usable.any():
            # foot as float with NaN for unknown, so nunique() counts distinct
            # KNOWN classes -- a drive with some rows unlabelled is not mixed.
            foot_known = foot[usable].astype("float64")
            foot_known[foot[usable] < 0] = np.nan
            frame = pd.DataFrame(
                {
                    "seq": seq_codes[usable],
                    "q": quality[usable],
                    "foot": foot_known,
                    "org": has_org[usable],
                }
            )
            grouped = frame.groupby("seq", sort=False).agg(
                q=("q", "median"),
                foot=("foot", "max"),
                org=("org", "max"),
                foot_classes=("foot", "nunique"),
                org_classes=("org", "nunique"),
            )
            seq_medians = grouped["q"].to_numpy(dtype="float64")
            # A drive with no labelled row at all has a NaN max; -1 is this
            # module's "unknown", and it must not fall into either class.
            seq_foot = grouped["foot"].fillna(-1).to_numpy(dtype="int8")
            seq_org = grouped["org"].to_numpy(dtype="bool")
            n_sequences = int(len(grouped))
            n_mixed_foot = int((grouped["foot_classes"] > 1).sum())
            n_mixed_org = int((grouped["org_classes"] > 1).sum())
        sq25, sq50, sq75 = _percentiles(seq_medians, (25, 50, 75))

        n_foot_known = int((foot >= 0).sum())
        n_on_foot = int((foot == 1).sum())
        n_with_org = int(has_org.sum())

        return {
            "city_id": city_id,
            "run_date": run_date,
            "csv_filename": csv_filename,
            "n_panos": self.n_panos,
            "n_quality": int(scored.sum()),
            "q_p10": p10,
            "q_p25": p25,
            "q_p50": p50,
            "q_p75": p75,
            "q_p90": p90,
            "pct_ge_good": _pct(int((q >= GOOD_THRESHOLD).sum()), q.size),
            "pct_lt_poor": _pct(int((q < POOR_THRESHOLD).sum()), q.size),
            "n_sequences": n_sequences,
            "n_seq_mixed_foot": n_mixed_foot,
            "n_seq_mixed_org": n_mixed_org,
            "seq_q_p25": sq25,
            "seq_q_p50": sq50,
            "seq_q_p75": sq75,
            "n_foot_known": n_foot_known,
            "n_panos_on_foot": n_on_foot,
            "pct_on_foot": _pct(n_on_foot, n_foot_known),
            "q_p50_on_foot": _median_or_none(quality[scored & (foot == 1)]),
            "q_p50_vehicle": _median_or_none(quality[scored & (foot == 0)]),
            "n_seq_on_foot": int((seq_foot == 1).sum()),
            "n_seq_vehicle": int((seq_foot == 0).sum()),
            "seq_q_p50_on_foot": _median_or_none(seq_medians[seq_foot == 1]),
            "seq_q_p50_vehicle": _median_or_none(seq_medians[seq_foot == 0]),
            "n_with_org": n_with_org,
            "n_distinct_orgs": len(self._org_ids),
            "q_p50_org": _median_or_none(quality[scored & has_org]),
            "q_p50_no_org": _median_or_none(quality[scored & ~has_org]),
            "n_seq_org": int(seq_org.sum()),
            "n_seq_no_org": int((~seq_org).sum()),
            "seq_q_p50_org": _median_or_none(seq_medians[seq_org]),
            "seq_q_p50_no_org": _median_or_none(seq_medians[~seq_org]),
        }


def measure_run(path: str, city_id: str, run_date: str, csv_filename: str) -> dict:
    """One city's row, streamed in bounded chunks."""
    acc = CityAccumulator()
    reader = pd.read_csv(
        path,
        usecols=lambda c: c in NEEDED_COLUMNS,
        chunksize=CHUNK_ROWS,
        low_memory=False,
    )
    for chunk in reader:
        acc.add(chunk)
    return acc.finish(city_id, run_date, csv_filename)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    parser.add_argument("--data-dir", default=None, help="data dir holding the catalog and CSVs")
    parser.add_argument(
        "--out-dir", default=DEFAULT_OUT_DIR, help="where to write city_quality.csv"
    )
    parser.add_argument(
        "--catalog-label",
        default="unspecified",
        help="which catalog this read, e.g. 'makelab2-prod' (recorded, not a path)",
    )
    parser.add_argument("--limit", type=int, default=None, help="stop after N cities (smoke run)")
    args = parser.parse_args()

    data_dir = args.data_dir or get_default_data_dir()
    conn = db.connect(db.get_default_db_path(data_dir))
    try:
        runs = latest_mapillary_runs(conn)
    finally:
        conn.close()
    if args.limit is not None:
        runs = runs[: args.limit]

    os.makedirs(args.out_dir, exist_ok=True)
    out_path = os.path.join(args.out_dir, "city_quality.csv")

    rows, missing, legacy, empty = [], [], [], []
    for i, (city_id, run_date, csv_filename) in enumerate(runs, start=1):
        path = os.path.join(data_dir, csv_filename)
        if not os.path.exists(path):
            missing.append(city_id)
            continue
        if not _has_quality_column(path):
            legacy.append(city_id)
            continue
        row = measure_run(path, city_id, run_date, csv_filename)
        if row["n_quality"] == 0:
            # A run with the column but no scored pano -- a city Mapillary has
            # only flats in, or none at all. Recorded as its own population, not
            # as a zero-quality city.
            empty.append(city_id)
            continue
        rows.append(row)
        print(
            f"[{i}/{len(runs)}] {city_id} {run_date}: {row['n_panos']:,} panos, "
            f"p50 {row['q_p50']}, >={GOOD_THRESHOLD} {row['pct_ge_good']}%, "
            f"{row['n_sequences']:,} sequences",
            flush=True,
        )

    with open(out_path, "w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(FIELDNAMES))
        writer.writeheader()
        writer.writerows(rows)

    manifest = {
        "experiment": TOPIC,
        "catalog_label": args.catalog_label,
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "runs_considered": len(runs),
        "runs_measured": len(rows),
        "runs_legacy_schema": len(legacy),
        "runs_missing_csv": len(missing),
        "runs_no_scored_pano": len(empty),
        "pano_statuses": list(PANO_STATUSES),
        "good_threshold": GOOD_THRESHOLD,
        "poor_threshold": POOR_THRESHOLD,
    }
    with open(os.path.join(args.out_dir, "manifest.json"), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2)
        fh.write("\n")

    print(
        f"\n{len(rows)} cities measured, {len(legacy)} skipped (pre-2026-07-24 schema), "
        f"{len(missing)} skipped (missing CSV), {len(empty)} with no scored pano"
    )
    print(f"Wrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
