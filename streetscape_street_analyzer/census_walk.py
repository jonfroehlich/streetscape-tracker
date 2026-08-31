"""
The census → road-walk scorer, shared by every census provider (issue #258).

A census provider has no per-sample-point endpoint. Its "walk" is not a walk of
requests at all: it fetches one census over the city's frozen grid bbox and
joins it **locally** onto the same deterministic on-street sample points the GSV
walk queries one at a time. That join, and the status vocabulary it produces,
are identical for every such provider — only the fetch and the provider's own
column schema differ.

So this module holds the join ONCE, parameterized by a :class:`CensusWalkSpec`,
for the same reason ``census.py`` holds the grid pipeline once (see CLAUDE.md):
the contracts it enforces are invisible in a review of the second copy. Before
#258 this lived in ``collect_mapillary``, whose own comment said in so many
words that ``build_streetwalk_rows`` "is Mapillary-specific and will have to be
generalized" — this is that generalization, and the KartaView walk is the second
caller it was waiting for.

What is genuinely per-provider is small and explicit:

  * how a census row's capture date is derived (Mapillary stores epoch
    milliseconds; KartaView stores a shot date that is nulled when it is not
    strictly older than the ingest timestamp),
  * the provider's output row schema, via its own ``build_image_rows`` /
    ``build_empty_rows`` bindings of the ``census.py`` core.

Everything else — the nearest-image join, the pano-beats-flat rule, the
OK/NO_DATE/FLAT_ONLY/ZERO_RESULTS vocabulary and the sample-order restore — is
shared and stated exactly once.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import geopandas as gpd
import numpy as np
import pandas as pd

from streetscape_metadata_tracker.analysis import FLAT_ONLY, REQUEST_FAILED
from streetscape_metadata_tracker.census import census_is_pano, status_for_capture_dates

WGS84 = "EPSG:4326"

# Sample points per sjoin_nearest call. The join's peak memory is driven by the
# match set it materializes, so a big city (hundreds of thousands of samples
# against a multi-million-image census) is chunked rather than joined in one go.
# Purely a memory knob — the result is identical at any block size, since each
# sample's nearest image is decided independently.
_JOIN_CHUNK_SIZE = 50_000


@dataclass(frozen=True)
class CensusWalkSpec:
    """
    The provider-specific half of a census road walk.

    Three bindings, deliberately no more: anything else a provider might want to
    vary belongs in the census it hands in, not in the scorer. Adding a field
    here is a claim that the JOIN itself differs between providers, which is the
    thing this module exists to deny.

    Attributes:
        capture_dates_for: ``(census, positions) -> array`` of ISO date strings
            for those census rows. The one genuinely divergent piece: Mapillary
            reads ``captured_at_ms``, KartaView reads ``shot_date`` against
            ``date_added``. A date the provider's own guard rejects must come
            back as **the empty string**, which becomes NO_DATE below — never a
            dropped sample, because an undated pano still covers (issue #257).
            Both current bindings emit ``""`` (`.fillna("")`);
            ``status_for_capture_dates`` also treats None/NaN/NaT as missing,
            so a binding that returns None is scored correctly rather than
            silently publishing OK with a null date, but ``""`` is the
            convention to write against.
        build_image_rows: the provider's binding of ``census.build_image_rows``.
        build_empty_rows: the provider's binding of ``census.build_empty_rows``.
    """

    capture_dates_for: Callable[[pd.DataFrame, np.ndarray], Any]
    build_image_rows: Callable[..., pd.DataFrame]
    build_empty_rows: Callable[..., pd.DataFrame]


def nearest_images_to_samples(
    query_points: list[tuple[float, float, int, int]],
    census: pd.DataFrame,
    match_dist_m: float,
) -> tuple[np.ndarray, np.ndarray]:
    """
    For each sample location, the nearest 360° pano and the nearest flat image
    within ``match_dist_m``.

    Uses ``gpd.sjoin_nearest`` on the local UTM CRS — the same idiom the
    grid-attribution path uses to match panos to edges (street_coverage.py), so
    distances are true metres rather than degree approximations. Panos and
    flats are joined separately because they answer different questions: 360°
    coverage and any-imagery coverage (issue #116's distinction, applied to
    streets).

    The samples run through the join in ``_JOIN_CHUNK_SIZE`` blocks rather than
    one call. Now that the street channels are scheduled across the catalog, a
    dense city pairs a multi-million-image census with a few hundred thousand
    sample points, and a single join materializes the whole match set at once.
    Chunking bounds peak memory at the census plus one block's matches; it
    cannot change the result, because a sample never spans two blocks and the
    nearest-image choice is per sample.

    Provider-agnostic already — it touches only ``lon``/``lat``/``is_pano``,
    which every census schema shares — which is why it moved here whole rather
    than being parameterized.

    Args:
        query_points: ``(lat, lon, seq, _)`` tuples from
            ``road_sampling.dedupe_query_points``.
        census: the columnar census for any provider.
        match_dist_m: max sample-to-image distance in metres.

    Returns:
        ``(pano_positions, flat_positions)`` — two int arrays as long as
        ``query_points``, holding the census position of the nearest qualifying
        image per sample and ``-1`` where nothing is in range. Positions rather
        than image records: Colorado Springs pairs 360k samples with a 6.5M
        image census, and a dict of per-sample image dicts is a copy of the
        census the arrays do not need (issue #157).
    """
    n_samples = len(query_points)
    none_matched = np.full(n_samples, -1, dtype=np.int64)
    if not n_samples or not len(census):
        return none_matched, none_matched.copy()

    samples_gdf = gpd.GeoDataFrame(
        {"sample_idx": range(n_samples)},
        geometry=gpd.points_from_xy([p[1] for p in query_points], [p[0] for p in query_points]),
        crs=WGS84,
    )
    # A city's sample points span a single UTM zone in every realistic case;
    # estimate_utm_crs picks it from the sample extent.
    metric_crs = samples_gdf.estimate_utm_crs()
    samples_m = samples_gdf.to_crs(metric_crs)

    def _join(positions: np.ndarray) -> np.ndarray:
        matches = np.full(n_samples, -1, dtype=np.int64)
        if not len(positions):
            return matches
        # image_idx carries the position in the FULL census, not in this
        # subset, so the caller can index the census directly.
        # Built once, outside the chunk loop: the census is the same for every
        # block, and reprojecting it per block would dominate the runtime.
        images_gdf = gpd.GeoDataFrame(
            {"image_idx": positions},
            geometry=gpd.points_from_xy(
                census["lon"].to_numpy()[positions], census["lat"].to_numpy()[positions]
            ),
            crs=WGS84,
        ).to_crs(metric_crs)

        for start in range(0, len(samples_m), _JOIN_CHUNK_SIZE):
            block = samples_m.iloc[start : start + _JOIN_CHUNK_SIZE]
            joined = gpd.sjoin_nearest(
                block,
                images_gdf,
                how="inner",
                max_distance=match_dist_m,
                distance_col="dist_m",
            )
            # sjoin_nearest emits every tied nearest neighbour; keep the closest
            # single image per sample so each sample yields exactly one row.
            joined = joined.sort_values("dist_m").drop_duplicates("sample_idx", keep="first")
            matches[joined["sample_idx"].to_numpy()] = joined["image_idx"].to_numpy()
        return matches

    # Through census_is_pano, not census["is_pano"].to_numpy(): a provider is
    # free to declare the column nullable (KartaView's projection is decoded to
    # a plain bool today, but both OUTPUT schemas use pd.BooleanDtype()), and a
    # single null degrades .to_numpy() to an object array on which `~` raises --
    # after the whole paced census fetch is spent. Same reason the grid tail
    # reads it there.
    is_pano = census_is_pano(census)
    return _join(np.flatnonzero(is_pano)), _join(np.flatnonzero(~is_pano))


def build_streetwalk_rows(
    query_points: list[tuple[float, float, int, int]],
    census: pd.DataFrame,
    match_dist_m: float,
    query_timestamp: str,
    spec: CensusWalkSpec,
    unmeasured_mask: Callable[[np.ndarray, np.ndarray], np.ndarray] | None = None,
) -> pd.DataFrame:
    """
    Score every sample location against the census into METADATA rows.

    Exactly one row per sample, in ``query_points`` order. Status vocabulary
    matches the grid downloader exactly (issue #116):

      * ``OK`` / ``NO_DATE`` — a 360° pano is in range (NO_DATE when its
        capture date is one the provider's guard rejects), which is what 360°
        street coverage counts. Both are PRESENT: a sample covered by an
        undated pano covers, it simply ages nothing (issue #257).
      * ``FLAT_ONLY`` — no pano in range but flat/perspective imagery is. A
        presence marker with a **null capture_date**, so flat timestamps never
        enter a dated statistic; counts only toward any-imagery coverage.
      * ``ZERO_RESULTS`` — no imagery of any kind in range.
      * ``REQUEST_FAILED`` — nothing in range, but this sample sits under part
        of the bbox the fetch never measured, so "no imagery" was never
        observed here (see ``unmeasured_mask``).

    Args:
        query_points: the deterministic on-street sample points.
        census: the provider's columnar census over the frozen grid bbox.
        match_dist_m: max sample-to-image distance in metres.
        query_timestamp: the observation timestamp stamped on every row.
        spec: the provider's :class:`CensusWalkSpec`.
        unmeasured_mask: ``(lats, lons) -> bool array`` marking sample points
            under a tile or cell the fetch never got back — KartaView's
            ``_points_in_cells`` over ``failed_cells``, and the same seam
            Mapillary's ``failed_tiles`` needs for #259. None for a clean
            sweep, which pays nothing.

            Recording an unswept sample as ZERO_RESULTS publishes an absence we
            never observed into an immutable dated snapshot, and street
            coverage is a share of samples — so an unmeasured hole would read
            as measured emptiness and understate the city forever. Applied only
            to samples that matched nothing: one that found imagery within
            ``match_dist_m`` was measured by construction, whatever cell it
            sits in.
    """
    pano_positions, flat_positions = nearest_images_to_samples(query_points, census, match_dist_m)
    sample_lats = np.array([p[0] for p in query_points], dtype=np.float64)
    sample_lons = np.array([p[1] for p in query_points], dtype=np.float64)

    has_pano = pano_positions >= 0
    # A pano wins wherever there is one; a flat only speaks for samples no pano
    # reached, which is exactly what makes the row FLAT_ONLY rather than OK.
    matched = has_pano | (flat_positions >= 0)
    chosen = np.where(has_pano, pano_positions, flat_positions)

    matched_idx = np.flatnonzero(matched)
    capture_dates = np.full(len(matched_idx), None, dtype=object)
    pano_of_matched = has_pano[matched_idx]
    # Dates are asked for the PANO rows only. A flat row's timestamp is never a
    # capture date -- it stays None and the status override below makes the row
    # FLAT_ONLY -- so handing flats to the provider's date guard would be asking
    # a question whose answer is discarded.
    capture_dates[pano_of_matched] = np.asarray(
        spec.capture_dates_for(census, chosen[matched_idx][pano_of_matched])
    )
    # status_for_capture_dates is evaluated over every matched row, including
    # the flat ones whose capture_date is None — those transiently read
    # "NO_DATE" (the guard treats None as missing) and are then overridden by
    # the outer where. Kept whole-array rather than masked so the OK/NO_DATE
    # rule has exactly one statement, shared with the grid downloader; a flat
    # row's status never escapes it.
    status = np.where(~pano_of_matched, FLAT_ONLY, status_for_capture_dates(capture_dates))
    image_rows = spec.build_image_rows(
        census,
        chosen[matched_idx],
        sample_lats[matched_idx],
        sample_lons[matched_idx],
        query_timestamp,
        status,
        capture_dates,
    )

    empty_idx = np.flatnonzero(~matched)
    if not len(empty_idx):
        return image_rows
    empty_lats = sample_lats[empty_idx]
    empty_lons = sample_lons[empty_idx]
    if unmeasured_mask is None:
        empty_status = "ZERO_RESULTS"
    else:
        unknown = np.asarray(unmeasured_mask(empty_lats, empty_lons), dtype=bool)
        empty_status = np.where(unknown, REQUEST_FAILED, "ZERO_RESULTS")
    empty_rows = spec.build_empty_rows(
        empty_lats,
        empty_lons,
        query_timestamp,
        empty_status,
    )
    # Restore sample order: the two frames were built by kind, not by position.
    combined = pd.concat([image_rows, empty_rows], ignore_index=True)
    return combined.iloc[
        np.argsort(np.concatenate([matched_idx, empty_idx]), kind="stable")
    ].reset_index(drop=True)
