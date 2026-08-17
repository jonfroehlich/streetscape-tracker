"""
analysis.py - Module for analyzing and displaying GSV metadata statistics.

This module provides functions and classes for analyzing Google Street View metadata and
displaying formatted statistics tables. It's designed to be used by multiple
components like json_summarizer.py, check_status_codes.py, and cli.py.
"""

import logging
from collections import Counter
from dataclasses import dataclass
from datetime import date
from typing import Any, ClassVar

import numpy as np
import pandas as pd
from tabulate import tabulate

logger = logging.getLogger(__name__)

# Official Google imagery carries exactly this copyright string; anything
# else is a third-party photographer. Substring matching is wrong because
# photographer names can contain 'Google' (e.g. '© MIB 360 - Google Virtual
# Tours Agency'). Must stay in sync with the exact match in www/js/city.js.
GOOGLE_COPYRIGHT = "© Google"

# Statuses that mean "imagery is present at this grid point". A pano the
# provider returned counts even when we could not read a usable capture date
# (status NO_DATE): it is still imagery within reach, so it counts toward both
# coverage and pano totals. Date-based stats (age, capture-year histograms)
# necessarily use only the dated subset (status == 'OK'). ZERO_RESULTS means
# "no imagery here"; everything else (REQUEST_DENIED, OVER_QUERY_LIMIT, ...) is
# an error, not an absence.
PRESENT_STATUSES = ("OK", "NO_DATE")

# Statuses that mean "some imagery is present, of any fidelity" — the broader
# any-imagery footprint (issue #116). FLAT_ONLY marks a grid point covered only
# by flat/perspective imagery (no 360-degree pano); it is Mapillary-specific
# (GSV's metadata API only returns panoramas, so GSV never emits it). It is
# deliberately kept OUT of PRESENT_STATUSES so the 360-degree coverage_rate
# stays GSV-comparable; any_imagery_coverage_rate is the metric that counts it.
FLAT_ONLY = "FLAT_ONLY"
ANY_IMAGERY_STATUSES = ("OK", "NO_DATE", FLAT_ONLY)

# Statuses that reflect a transient problem with the *request*, not a real
# answer about the grid point — so the downloader retries them, and only if
# they survive every retry does it write them back as a failure row (never as
# "no imagery"). OVER_QUERY_LIMIT is quota throttling (a normal HTTP 200 with
# this body status); UNKNOWN_ERROR is Google-documented as transient. Kept here
# next to the other status classifications so the downloader (retry decision)
# and scripts/purge_tainted_runs.py (taint scan) share one definition.
RETRYABLE_STATUSES = ("OVER_QUERY_LIMIT", "UNKNOWN_ERROR")

# Synthetic status for a grid point whose request never returned a body status
# at all (network/timeout errors, exhausted after every retry). It is written
# as a failure row so the grid stays complete (run-to-run diffs require exact
# grid-key equality), but it is neither "present" nor a provider-side denial.
REQUEST_FAILED = "REQUEST_FAILED"


def is_google_copyright(copyright_info: pd.Series) -> pd.Series:
    """
    Boolean mask: rows whose copyright marks official Google imagery.
    NaN (copyright never recorded, e.g. archival imports) compares False.
    """
    return copyright_info == GOOGLE_COPYRIGHT


# Earliest date each provider's imagery can plausibly carry. A capture date
# outside [this, the date we observed it] cannot be true, and a date that
# cannot be true is not a usable date (issue #213) — contributor photospheres
# reach us with corrupt EXIF, and on the production catalog that put 22 runs in
# 2611-2612 and 75 before Street View existed, poisoning oldest/newest/median
# for the whole city off one bad pano.
#
# The Mapillary floor is deliberately looser than its 2013 founding and matches
# download_mapillary.captured_at_to_iso_date, which applies the identical rule
# at decode: contributors upload genuinely old photographs, so the bound marks
# impossible values, not merely surprising ones.
EARLIEST_PLAUSIBLE_CAPTURE = {
    "gsv": date(2007, 1, 1),  # Street View launched 2007-05-25
    "mapillary": date(2004, 1, 1),
}
# Floor for a provider not listed above. GSV's is the stricter of the two, but
# an unknown provider is more likely to resemble a contributor-fed archive than
# a fleet, so default to the loose one and let it be tightened deliberately.
_DEFAULT_EARLIEST_PLAUSIBLE_CAPTURE = EARLIEST_PLAUSIBLE_CAPTURE["mapillary"]


def plausible_capture_mask(
    capture_dates: pd.Series, now: pd.Timestamp, provider: str = "gsv"
) -> pd.Series:
    """
    Boolean mask: capture dates that could actually be true.

    Args:
        capture_dates: datetime64 Series of capture dates (NaT allowed).
        now: the moment the imagery was observed — a run's run_date for stored
            stats, wall-clock for a live summary. Nothing can be captured after
            the query that saw it, so this is the upper bound; it is inclusive,
            because a pano captured on the run date is ordinary (and GSV's
            month-precision dates are pinned to the 1st, so they can only ever
            round *down* toward the past).
        provider: imagery provider, selecting the earliest plausible date.

    Returns:
        Boolean Series aligned to the input. NaT compares False: an unreadable
        date is no more usable than an impossible one.
    """
    earliest = EARLIEST_PLAUSIBLE_CAPTURE.get(provider, _DEFAULT_EARLIEST_PLAUSIBLE_CAPTURE)
    return capture_dates.between(pd.Timestamp(earliest), now)


def _dated_unique(df: pd.DataFrame) -> pd.DataFrame:
    """One row per dated pano (status OK, deduped), capture_date as datetime64."""
    dated = df[df["status"] == "OK"].drop_duplicates(subset=["pano_id"]).copy()
    if not pd.api.types.is_datetime64_any_dtype(dated["capture_date"]):
        # coerce, not raise: an unparseable date reaches the same fate as an
        # implausible one (dropped by the mask) rather than killing a whole run
        dated["capture_date"] = pd.to_datetime(dated["capture_date"], errors="coerce")
    return dated


def dated_unique_panos(df: pd.DataFrame, now: pd.Timestamp, provider: str = "gsv") -> pd.DataFrame:
    """
    The subset of a run every date-derived statistic is computed over.

    One pano per pano_id (a pano snapped from several grid points must
    contribute its age once), status OK (a NO_DATE pano carries no usable
    date), and a capture date that passes plausible_capture_mask. Age stats,
    the capture-year histogram and the daily histogram all read this one
    frame, so they can never disagree about which panos count.

    Args:
        df: run DataFrame, or a copyright-filtered subset of one.
        now: observation date; also the upper plausibility bound.
        provider: imagery provider (selects the earliest plausible date).

    Returns:
        A new DataFrame with capture_date coerced to datetime64.
    """
    dated = _dated_unique(df)
    return dated[plausible_capture_mask(dated["capture_date"], now, provider)]


def implausible_capture_date_count(
    df: pd.DataFrame, now: pd.Timestamp, provider: str = "gsv"
) -> int:
    """
    How many of a run's dated panos carry a date that cannot be true.

    Not used by the stats themselves — dated_unique_panos simply drops them —
    but a repair pass needs to know which runs on disk were affected at all,
    and asking here keeps that question answered by the same rule rather than
    by a second copy of it (scripts/recompute_run_stats.py).

    Counts ONLY dates that are present and impossible. plausible_capture_mask
    also rejects NaT, but a pano whose date was absent or unreadable never
    claimed a date to begin with — it is dropped from the dated stats for a
    different reason, and folding it in here would inflate the operator-facing
    "N runs hold an impossible capture date" report and send --regenerate-json
    at runs whose published JSON is fine.
    """
    dated = _dated_unique(df)
    dates = dated["capture_date"]
    return int((dates.notna() & ~plausible_capture_mask(dates, now, provider)).sum())


@dataclass
class DistanceStats:
    """Statistics about distances between query points and panoramas."""

    min_meters: float
    max_meters: float
    avg_meters: float
    median_meters: float
    stdev_meters: float

    # Class variable mapping internal field names to human-readable labels
    FIELD_LABELS: ClassVar[dict[str, str]] = {
        "min_meters": "Min Distance (m)",
        "max_meters": "Max Distance (m)",
        "avg_meters": "Avg Distance (m)",
        "median_meters": "Median Distance (m)",
        "stdev_meters": "Std Dev Distance (m)",
    }

    def to_rows(self) -> list[list[str]]:
        """Convert stats to formatted rows for tabulation."""
        return [
            [self.FIELD_LABELS[field], f"{getattr(self, field):.2f}"]
            for field in self.FIELD_LABELS.keys()
        ]


@dataclass
class CoverageStats:
    """
    Statistics about imagery coverage of the sampled grid points.

    coverage_rate is grid-point coverage: points with >= 1 pano / total
    points (issue #90). Unique-pano counts are a separate metric — the
    field name num_points_with_unique_pano_ids is kept for per-run JSON
    key stability but holds the count of unique panoramas.
    """

    num_points_with_panos: int
    num_points_with_unique_pano_ids: int
    num_points_with_errors: int
    num_points_without_panos: int
    coverage_rate: float
    # Any-imagery footprint (issue #116): points covered by a pano OR by
    # flat-only imagery (status FLAT_ONLY), and the corresponding rate. For
    # GSV — which never emits FLAT_ONLY — these equal num_points_with_panos /
    # coverage_rate exactly, so the fields are universal but only widen the
    # number for Mapillary.
    num_points_with_any_imagery: int
    any_imagery_coverage_rate: float
    pano_distance_stats: DistanceStats | None = None

    # A percent-formatted rate field vs a plain count, so to_rows knows to
    # append '%'. Kept as a set so new rate fields only touch one place.
    _RATE_FIELDS: ClassVar[frozenset[str]] = frozenset(
        {"coverage_rate", "any_imagery_coverage_rate"}
    )

    FIELD_LABELS: ClassVar[dict[str, str]] = {
        "num_points_with_panos": "Points with Panoramas",
        "num_points_with_unique_pano_ids": "Unique Panoramas",
        "num_points_without_panos": "Points without Panoramas",
        "num_points_with_errors": "Points with Errors",
        "coverage_rate": "Points with Panos / Total Points (360°)",
        "num_points_with_any_imagery": "Points with Any Imagery",
        "any_imagery_coverage_rate": "Any-Imagery Coverage",
    }

    def to_rows(self) -> list[list[str]]:
        """Convert stats to formatted rows for tabulation."""
        rows = [
            [
                self.FIELD_LABELS[field],
                f"{getattr(self, field):.2f}%"
                if field in self._RATE_FIELDS
                else str(getattr(self, field)),
            ]
            for field in self.FIELD_LABELS.keys()
        ]

        if self.pano_distance_stats:
            rows.extend(self.pano_distance_stats.to_rows())

        return rows

    def format_table(self) -> str:
        """Create a formatted table representation."""
        return tabulate(
            self.to_rows(),
            headers=["Metric", "Value"],
            tablefmt="simple",
            numalign="right",
            stralign="left",
        )


@dataclass
class AgeStats:
    """Statistics about panorama ages."""

    count: int
    oldest_pano_date: str | None
    newest_pano_date: str | None
    avg_pano_age_years: float | None
    median_pano_age_years: float | None
    stdev_pano_age_years: float | None
    age_percentiles_years: dict[str, float] | None

    FIELD_LABELS: ClassVar[dict[str, str]] = {
        "count": "Total Panoramas",
        "oldest_pano_date": "Oldest Panorama",
        "newest_pano_date": "Newest Panorama",
        "avg_pano_age_years": "Average Age (years)",
        "median_pano_age_years": "Median Age (years)",
        "stdev_pano_age_years": "Std Dev Age (years)",
    }

    def format_field_value(self, field: Any) -> str:
        """Helper to safely format field values that might be None."""
        value = getattr(self, field)

        if value is None:
            return "N/A"
        if field == "count":
            return f"{value:,}"
        if isinstance(value, (float, int)):
            return f"{value:.1f}"
        return str(value)

    def to_rows(self) -> list[list[str]]:
        """Convert stats to formatted rows for tabulation."""
        return [
            [self.FIELD_LABELS[field], self.format_field_value(field)]
            for field in self.FIELD_LABELS.keys()
        ]

    def format_table(self, title: str = "Age Statistics") -> str:
        """Create a formatted table representation."""
        return f"\n{title}\n" + tabulate(
            self.to_rows(),
            headers=["Metric", "Value"],
            tablefmt="simple",
            numalign="right",
            stralign="left",
        )


def calculate_age_stats(df: pd.DataFrame, now: pd.Timestamp) -> AgeStats:
    """Helper function to calculate age statistics for panoramas."""
    if len(df) == 0:
        return AgeStats(
            count=0,
            oldest_pano_date=None,
            newest_pano_date=None,
            avg_pano_age_years=None,
            median_pano_age_years=None,
            stdev_pano_age_years=None,
            age_percentiles_years=None,
        )

    # Convert capture_date to datetime if necessary
    if not pd.api.types.is_datetime64_any_dtype(df["capture_date"]):
        df["capture_date"] = pd.to_datetime(df["capture_date"])

    valid_dates_mask = df["capture_date"].notna()
    df_with_dates = df[valid_dates_mask]

    ages = (now - df_with_dates["capture_date"]).dt.total_seconds() / (365.25 * 24 * 3600)

    return AgeStats(
        count=len(df),
        oldest_pano_date=df_with_dates["capture_date"].min().isoformat()
        if len(df_with_dates) > 0
        else None,
        newest_pano_date=df_with_dates["capture_date"].max().isoformat()
        if len(df_with_dates) > 0
        else None,
        avg_pano_age_years=float(ages.mean()) if len(ages) > 0 else None,
        median_pano_age_years=float(ages.median()) if len(ages) > 0 else None,
        # std() with a single sample returns NaN (ddof=1), which is not valid JSON
        stdev_pano_age_years=float(ages.std()) if len(ages) > 1 else None,
        age_percentiles_years={
            "p10": float(ages.quantile(0.1)) if len(ages) > 0 else None,
            "p25": float(ages.quantile(0.25)) if len(ages) > 0 else None,
            "p75": float(ages.quantile(0.75)) if len(ages) > 0 else None,
            "p90": float(ages.quantile(0.9)) if len(ages) > 0 else None,
        },
    )


def calculate_coverage_stats(df: pd.DataFrame) -> CoverageStats:
    """
    Calculate grid-point coverage and pano-distance statistics.

    coverage_rate is the percentage of sampled grid points with at least
    one pano — "what fraction of the sampled area is within reach of
    imagery?" (issue #90). Unlike unique-panos / points (a density proxy
    that shrinks as the sampling step shrinks), this is roughly
    step-independent and matches the definition behind the originally
    published site and the GeoIndustry 2025 paper.

    A run may hold several rows per grid point (Mapillary stores one row
    per pano), so points are counted as distinct (query_lat, query_lon)
    pairs. Coordinates within a run come from a single grid-generation
    pass, so exact equality is safe.
    """
    point_cols = ["query_lat", "query_lon"]
    num_total_points = len(df[point_cols].drop_duplicates())

    # A grid point is covered if it holds >= 1 present pano (OK or NO_DATE):
    # a dateless pano is still imagery within reach (see PRESENT_STATUSES).
    present_rows = df[df["status"].isin(PRESENT_STATUSES)]
    num_points_with_panos = len(present_rows[point_cols].drop_duplicates())
    logger.debug(f"Grid points with panoramas: {num_points_with_panos}")

    # Any-imagery coverage additionally counts points covered only by flat
    # imagery (status FLAT_ONLY, issue #116). A FLAT_ONLY row only ever exists
    # at a point with no pano, so this is a strict superset of the 360-degree
    # covered points. GSV runs carry no FLAT_ONLY rows, so this collapses to
    # num_points_with_panos there.
    any_imagery_rows = df[df["status"].isin(ANY_IMAGERY_STATUSES)]
    num_points_with_any_imagery = len(any_imagery_rows[point_cols].drop_duplicates())

    # ZERO_RESULTS rows exist only at grid points with no pano, so they never
    # overlap the covered points; whatever remains saw only errors
    # (REQUEST_DENIED, OVER_QUERY_LIMIT, ...). Subtract the ANY-imagery covered
    # points (pano + flat-only), not just the pano points — a FLAT_ONLY point
    # is a real answer, not an error (issue #116).
    num_points_without_panos = len(
        df.loc[df["status"] == "ZERO_RESULTS", point_cols].drop_duplicates()
    )
    num_points_with_errors = (
        num_total_points - num_points_with_any_imagery - num_points_without_panos
    )

    successful_df_no_duplicates = present_rows.drop_duplicates(subset=["pano_id"]).copy()
    num_unique_panos = len(successful_df_no_duplicates)
    logger.debug(f"Unique panoramas: {num_unique_panos}")

    distance_stats = None
    if num_unique_panos > 0:
        distances = (
            np.sqrt(
                (successful_df_no_duplicates["query_lat"] - successful_df_no_duplicates["pano_lat"])
                ** 2
                + (
                    successful_df_no_duplicates["query_lon"]
                    - successful_df_no_duplicates["pano_lon"]
                )
                ** 2
            )
            * 111000
        )  # Approximate conversion to meters

        successful_df_no_duplicates.loc[:, "distance_to_query"] = distances
        logger.debug(f"Distance: {distances}")
        logger.debug(
            f"Distance is NA: {successful_df_no_duplicates['distance_to_query'].isna().all()}"
        )
        logger.debug(f"Distance std: {successful_df_no_duplicates['distance_to_query'].std()}")

        if not successful_df_no_duplicates["distance_to_query"].isna().all():
            # If we have only one point, std will be NaN
            std_val = successful_df_no_duplicates["distance_to_query"].std()
            distance_stats = DistanceStats(
                min_meters=float(successful_df_no_duplicates["distance_to_query"].min()),
                max_meters=float(successful_df_no_duplicates["distance_to_query"].max()),
                avg_meters=float(successful_df_no_duplicates["distance_to_query"].mean()),
                median_meters=float(successful_df_no_duplicates["distance_to_query"].median()),
                stdev_meters=0.0
                if pd.isna(std_val)
                else float(std_val),  # Use 0.0 for single points
            )

    return CoverageStats(
        num_points_with_panos=num_points_with_panos,
        num_points_with_unique_pano_ids=num_unique_panos,
        num_points_without_panos=num_points_without_panos,
        num_points_with_errors=num_points_with_errors,
        coverage_rate=(num_points_with_panos / num_total_points) * 100
        if num_total_points > 0
        else 0,
        num_points_with_any_imagery=num_points_with_any_imagery,
        any_imagery_coverage_rate=(num_points_with_any_imagery / num_total_points) * 100
        if num_total_points > 0
        else 0,
        pano_distance_stats=distance_stats,
    )


@dataclass
class DuplicateStats:
    """Statistics about panorama duplications."""

    total_unique_panos: int
    total_pano_references: int
    duplicate_reference_count: int
    most_referenced_count: int
    panos_with_multiple_refs: int
    average_references_per_pano: float

    FIELD_LABELS: ClassVar[dict[str, str]] = {
        "total_unique_panos": "Total Unique Panoramas",
        "total_pano_references": "Total References",
        "duplicate_reference_count": "Duplicate References",
        "most_referenced_count": "Most Referenced Count",
        "panos_with_multiple_refs": "Panoramas with Multiple Refs",
        "average_references_per_pano": "Average References per Pano",
    }

    def to_rows(self) -> list[list[str]]:
        """Convert stats to formatted rows for tabulation."""
        return [
            [
                self.FIELD_LABELS[field],
                f"{getattr(self, field):.2f}"
                if field == "average_references_per_pano"
                else str(getattr(self, field)),
            ]
            for field in self.FIELD_LABELS.keys()
        ]

    def format_table(self) -> str:
        """Create a formatted table representation."""
        return tabulate(
            self.to_rows(),
            headers=["Metric", "Value"],
            tablefmt="simple",
            numalign="right",
            stralign="left",
        )


@dataclass
class YearlyDistribution:
    """Distribution of panoramas by year."""

    counts: dict[int, int]

    def to_rows(self) -> list[list[str]]:
        """Convert distribution to formatted rows for tabulation."""
        total_panos = sum(self.counts.values())
        rows = []

        for year in sorted(self.counts.keys()):
            count = self.counts[year]
            percentage = (count / total_panos) * 100
            rows.append([str(year), str(count), f"{percentage:.2f}%"])

        # Add total row
        rows.append(["TOTAL", str(total_panos), "100.00%"])
        return rows

    def format_table(self, title: str = "Yearly Distribution") -> str:
        """Create a formatted table representation."""
        return f"\n{title}\n" + tabulate(
            self.to_rows(),
            headers=["Year", "Count", "Percentage"],
            tablefmt="simple",
            floatfmt=".2f",
            numalign="right",
            stralign="left",
        )


@dataclass
class DailyDistribution:
    """Distribution of panoramas by day."""

    counts: dict[str, int]  # Maps ISO date strings (YYYY-MM-DD) to counts

    def to_rows(self) -> list[list[str]]:
        """Convert distribution to formatted rows for tabulation."""
        total_panos = sum(self.counts.values())
        rows = []

        # `day`, not `date`: the module imports datetime.date at the top
        for day in sorted(self.counts.keys()):
            count = self.counts[day]
            percentage = (count / total_panos) * 100
            rows.append([day, str(count), f"{percentage:.2f}%"])

        # Add total row
        rows.append(["TOTAL", str(total_panos), "100.00%"])
        return rows

    def format_table(self, title: str = "Daily Distribution") -> str:
        """Create a formatted table representation."""
        return f"\n{title}\n" + tabulate(
            self.to_rows(),
            headers=["Date", "Count", "Percentage"],
            tablefmt="simple",
            floatfmt=".2f",
            numalign="right",
            stralign="left",
        )


@dataclass
class PhotographerStats:
    """Statistics about photographer contributions."""

    photographer_counts: dict[str, int]  # Maps photographer name to unique pano count
    top_n: int = 5  # Number of top photographers to show

    def to_rows(self) -> list[list[str]]:
        """Convert stats to formatted rows for tabulation."""
        total_panos = sum(self.photographer_counts.values())
        rows = []

        # Sort by count and take top N
        sorted_photographers = sorted(
            self.photographer_counts.items(), key=lambda x: x[1], reverse=True
        )[: self.top_n]

        for photographer, count in sorted_photographers:
            percentage = (count / total_panos) * 100
            rows.append([photographer, f"{count:,}", f"{percentage:.2f}%"])

        return rows

    def format_table(self, title: str = "Top Photographers by Unique Panoramas") -> str:
        """Create a formatted table representation."""
        return f"\n{title}\n" + tabulate(
            self.to_rows(),
            headers=["Photographer", "Unique Panos", "Percentage"],
            tablefmt="simple",
            floatfmt=".2f",
            numalign="right",
            stralign="left",
        )


@dataclass
class GSVAnalysisResults:
    """Complete set of GSV metadata analysis results."""

    duplicate_stats: DuplicateStats
    age_stats: AgeStats
    coverage_stats: CoverageStats
    yearly_distribution: YearlyDistribution
    daily_distribution: DailyDistribution
    photographer_stats: PhotographerStats

    def print_summary(self, title: str = "GSV Analysis Summary") -> None:
        """Print a comprehensive summary of the analysis results."""
        print(f"\n{title}")
        print("=" * 40)

        print("\nCoverage Statistics")
        print(self.coverage_stats.format_table())

        print("\nDuplicate Statistics")
        print(self.duplicate_stats.format_table())

        print(self.age_stats.format_table())

        print("\nPhotographer Statistics")
        print(self.photographer_stats.format_table())

        print("\nYearly and Daily Distributions")
        print(self.yearly_distribution.format_table())
        print(self.daily_distribution.format_table())


def calculate_daily_distribution(df: pd.DataFrame) -> DailyDistribution:
    """
    Calculate distribution of panoramas by date (YYYY-MM-DD).

    Args:
        df: DataFrame containing panorama data with capture_date column

    Returns:
        DailyDistribution object containing date-wise counts
    """
    if len(df) == 0:
        return DailyDistribution(counts={})

    # Convert capture_date to datetime if necessary
    if not pd.api.types.is_datetime64_any_dtype(df["capture_date"]):
        df["capture_date"] = pd.to_datetime(df["capture_date"])

    # Use ISO format for dates (YYYY-MM-DD)
    date_counts = (
        df["capture_date"].apply(lambda x: x.date().isoformat()).value_counts().sort_index()
    )

    # Convert counts to integers while maintaining ISO date strings as keys
    return DailyDistribution(counts={date: int(count) for date, count in date_counts.items()})


def calculate_photographer_stats(df: pd.DataFrame) -> PhotographerStats:
    """
    Calculate photographer contribution statistics.

    Args:
        df: DataFrame containing GSV metadata with 'copyright_info' and 'pano_id' columns

    Returns:
        PhotographerStats containing photographer contribution analysis
    """
    # Filter for present panoramas (OK or NO_DATE) and drop duplicates; a
    # dateless pano still carries its contributor/copyright attribution.
    present_panos = df[df["status"].isin(PRESENT_STATUSES)].drop_duplicates(subset=["pano_id"])

    # Count unique pano_ids per photographer
    photographer_counts = present_panos["copyright_info"].value_counts().to_dict()

    return PhotographerStats(photographer_counts=photographer_counts)


def calculate_pano_stats(
    df: pd.DataFrame, now: pd.Timestamp, google_only: bool = False, provider: str = "gsv"
) -> GSVAnalysisResults:
    """
    Calculate comprehensive panorama statistics from a DataFrame.

    Args:
        df: DataFrame containing GSV metadata
        now: Timestamp to use for age calculations
        google_only: When True, restrict to official Google imagery
            (exact '© Google' copyright; see is_google_copyright)
        provider: imagery provider, selecting the earliest plausible capture
            date (see dated_unique_panos)

    Returns:
        GSVAnalysisResults containing all calculated statistics

    Example:
        >>> df = pd.read_csv('gsv_metadata.csv')
        >>> now = pd.Timestamp.now()
        >>> results = calculate_pano_stats(df, now)
        >>> results.print_summary("Analysis Results")
    """
    filtered_df = df[is_google_copyright(df["copyright_info"])] if google_only else df

    # Present panoramas (OK or NO_DATE) count toward pano totals; date-based
    # stats below use only the dated (OK) subset.
    present_panos = filtered_df[filtered_df["status"].isin(PRESENT_STATUSES)].copy()

    # Calculate duplicate statistics over every present pano
    pano_id_counts = present_panos["pano_id"].value_counts()
    duplicate_stats = DuplicateStats(
        total_unique_panos=len(pano_id_counts),
        total_pano_references=len(present_panos),
        duplicate_reference_count=len(present_panos) - len(pano_id_counts),
        most_referenced_count=int(pano_id_counts.max()) if not pano_id_counts.empty else 0,
        panos_with_multiple_refs=int((pano_id_counts > 1).sum()),
        average_references_per_pano=float(len(present_panos) / len(pano_id_counts))
        if len(pano_id_counts) > 0
        else 0,
    )

    # For most stats, we want to focus only on unique pano ids or we risk
    # skewing our statistics for duplicate pano ids that are referenced multiple times
    # from different query points

    # Age / capture-year / daily stats need a real — and possible — date
    dated_unique = dated_unique_panos(filtered_df, now, provider)
    age_stats = calculate_age_stats(dated_unique, now)

    # Coverage describes the sampled grid, not the copyright subset, so it
    # is always computed over the full frame (the google_only filter would
    # drop the ZERO_RESULTS rows that anchor the denominator)
    coverage_stats = calculate_coverage_stats(df)

    # Calculate distributions and photographer stats
    yearly_dist = calculate_yearly_distribution(dated_unique)
    daily_dist = calculate_daily_distribution(dated_unique)
    photographer_stats = calculate_photographer_stats(present_panos)

    return GSVAnalysisResults(
        duplicate_stats=duplicate_stats,
        age_stats=age_stats,
        coverage_stats=coverage_stats,
        yearly_distribution=yearly_dist,
        daily_distribution=daily_dist,
        photographer_stats=photographer_stats,
    )


def calculate_yearly_distribution(df: pd.DataFrame) -> YearlyDistribution:
    """Calculate distribution of panoramas by year."""
    if len(df) == 0:
        return YearlyDistribution(counts={})

    # Convert capture_date to datetime if necessary
    if not pd.api.types.is_datetime64_any_dtype(df["capture_date"]):
        df["capture_date"] = pd.to_datetime(df["capture_date"])

    # Extract year and count occurrences
    year_counts = df["capture_date"].dt.year.value_counts().sort_index()

    return YearlyDistribution(counts={int(year): int(count) for year, count in year_counts.items()})


def calculate_run_stats(df: pd.DataFrame, run_date, provider: str = "gsv") -> dict[str, Any]:
    """
    Compute the per-run summary stats stored in the runs catalog table.

    Ages are computed relative to run_date (not wall-clock now), so the
    stored stats are deterministic and comparable across runs.

    The three capture-date columns (oldest/newest/median age) describe
    official '© Google' imagery for gsv and every pano for other providers;
    see the comment at the age_source assignment for why.

    Args:
        df: loaded run DataFrame (load_city_csv_file format)
        run_date: datetime.date of the collection run
        provider: imagery provider. The Google-copyright breakdown only
            makes sense for GSV runs; other providers store NULL for
            unique_google_panos (their unique_panos already counts only
            provider imagery). GSV runs whose copyright_info is entirely
            null (archival imports that never captured it) also store
            NULL — the Google subset is unknown, not zero.

    Returns:
        Dict matching db.register_run keyword arguments (stats subset).
    """
    now = pd.Timestamp(run_date)

    status_ok = int((df["status"] == "OK").sum())
    status_no_date = int((df["status"] == "NO_DATE").sum())
    status_zero = int((df["status"] == "ZERO_RESULTS").sum())
    # FLAT_ONLY (issue #116) is a real answer ("flat imagery here, no pano"),
    # not an error — so it gets its own bucket and is excluded from the
    # status_other error catch-all, mirroring how NO_DATE was split out in v3.
    status_flat_only = int((df["status"] == FLAT_ONLY).sum())
    status_other = int(len(df) - status_ok - status_no_date - status_zero - status_flat_only)

    # Pano totals count every present pano (OK or NO_DATE); age stats use only
    # the dated subset, since NO_DATE panos carry no usable capture date.
    present = df[df["status"].isin(PRESENT_STATUSES)]
    unique = present.drop_duplicates(subset=["pano_id"])
    # Whether this run recorded copyright at all. Archival imports (issue #93)
    # never did, so their Google subset is unknown rather than empty — one
    # condition, decided once, because it governs both the Google pano count
    # and whether the age columns below can be restricted to Google imagery.
    copyright_recorded = not (len(unique) > 0 and unique["copyright_info"].isna().all())
    unique_google_panos = None
    if provider == "gsv" and copyright_recorded:
        unique_google_panos = int(is_google_copyright(unique["copyright_info"]).sum())

    # For gsv the age columns describe OFFICIAL GOOGLE imagery, not every pano
    # (issue #213). Two reasons, and the second is the load-bearing one:
    # third-party photospheres carry corrupt EXIF that a min/max cannot survive,
    # and the site has always displayed the Google-filtered figures — the
    # aggregate's `latest` block reads the per-run JSON's google_panos stats —
    # so an all-panos column published under the same name as the map's "median
    # age" was two different numbers wearing one label. A run with no copyright
    # recorded keeps every pano, mirroring the frontend's
    # `google_panos_age_stats ?? all_panos_age_stats` fallback; other providers
    # have no copyright concept and are unaffected.
    age_source = df
    if provider == "gsv" and copyright_recorded:
        age_source = df[is_google_copyright(df["copyright_info"])]
    age_stats = calculate_age_stats(dated_unique_panos(age_source, now, provider), now)
    coverage = calculate_coverage_stats(df)

    return {
        "total_points": len(df),
        "status_ok": status_ok,
        "status_no_date": status_no_date,
        "status_zero_results": status_zero,
        "status_flat_only": status_flat_only,
        "status_other": status_other,
        "unique_panos": len(unique),
        "unique_google_panos": unique_google_panos,
        "coverage_rate_pct": coverage.coverage_rate,
        "any_imagery_coverage_rate_pct": coverage.any_imagery_coverage_rate,
        "oldest_capture_date": age_stats.oldest_pano_date,
        "newest_capture_date": age_stats.newest_pano_date,
        "median_pano_age_years": age_stats.median_pano_age_years,
    }


# Response statuses meaning the request itself failed (credentials, quota, or
# an unrecoverable network error), as opposed to "no imagery here"
# (ZERO_RESULTS). A run dominated by these carries no information about the
# city, so detect_systemic_failure rejects it before it can become a diff
# baseline. REQUEST_FAILED is included so a night of pervasive network failure
# is caught the same way a quota/credential failure is.
SYSTEMIC_FAILURE_STATUSES = ("REQUEST_DENIED", "OVER_QUERY_LIMIT", REQUEST_FAILED)


def detect_systemic_failure(df: pd.DataFrame, threshold: float = 0.95) -> str | None:
    """
    Detect a run whose responses are dominated by credential/quota denials.

    Such a run contains no information about the city; registering it would
    poison the run series (it becomes the diff baseline) and, in the
    unattended scheduler, quietly catalog garbage night after night.
    Callers should abort the run instead of registering it.

    A run that is 100% ZERO_RESULTS is NOT flagged — "no imagery anywhere"
    is a valid answer for remote areas.

    Args:
        df: loaded run DataFrame (must have a 'status' column)
        threshold: fraction of denied responses at or above which the run
            is rejected

    Returns:
        A human-readable reason string when the run should be rejected,
        None when the run looks like a genuine collection.

    Examples:
        >>> import pandas as pd
        >>> detect_systemic_failure(pd.DataFrame({'status': ['REQUEST_DENIED'] * 100}))
        '100 of 100 responses (100%) were denied (REQUEST_DENIED=100) — check the API credential/quota'
        >>> detect_systemic_failure(pd.DataFrame({'status': ['OK', 'ZERO_RESULTS']})) is None
        True
    """
    if len(df) == 0:
        return None
    counts = df["status"].value_counts()
    denied = int(sum(counts.get(s, 0) for s in SYSTEMIC_FAILURE_STATUSES))
    fraction = denied / len(df)
    if fraction < threshold:
        return None
    breakdown = ", ".join(f"{s}={int(counts[s])}" for s in SYSTEMIC_FAILURE_STATUSES if s in counts)
    return (
        f"{denied:,} of {len(df):,} responses ({fraction:.0%}) were "
        f"denied ({breakdown}) — check the API credential/quota"
    )


def analyze_gsv_status(df: pd.DataFrame) -> dict[str, Any]:
    """
    Analyze GSV metadata status codes in a DataFrame.

    Args:
        df: DataFrame containing GSV metadata

    Returns:
        Dictionary containing status analysis including counts and percentages

    Raises:
        ValueError: If no status column is found in the DataFrame
    """
    # Get the status column
    status_cols = [col for col in df.columns if "status" in col.lower()]
    if not status_cols:
        raise ValueError("No status column found in DataFrame")
    status_col = status_cols[0]

    # Calculate status statistics
    status_counts = Counter(df[status_col])
    total_records = len(df)

    return {
        "total_records": total_records,
        "status_counts": dict(status_counts),
        "status_percentages": {
            status: (count / total_records * 100) for status, count in status_counts.items()
        },
    }


def print_df_summary(
    df: pd.DataFrame, now: pd.Timestamp | None = None, provider: str = "gsv"
) -> None:
    """
    Print a comprehensive summary of download results.

    Args:
        df: DataFrame containing the downloaded metadata
        now: Optional timestamp for age calculations (defaults to current time)
        provider: imagery provider; the Google-only breakdown is printed
            only for GSV runs (other providers' rows are all provider panos)
    """
    # Use provided timestamp or current time
    timestamp = now if now is not None else pd.Timestamp.now()

    all_stats = calculate_pano_stats(df, timestamp, provider=provider)

    print("\nAll Panoramas")
    print("=" * 40)
    all_stats.print_summary()

    if provider == "gsv":
        google_stats = calculate_pano_stats(df, timestamp, google_only=True, provider=provider)
        print("\nGoogle Panoramas Only")
        print("=" * 40)
        google_stats.print_summary()
