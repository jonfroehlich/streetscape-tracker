"""
Panoramax road-walk sample collection (issue #331).

The THIRD census provider bound to :mod:`census_walk`, and the one that made
that module's claim testable: its docstring says a census provider's walk is one
fetch over the frozen bbox joined **locally** onto the same deterministic sample
points, and that "only the fetch and the provider's own column schema differ."
This module is that sentence, for Panoramax — three bindings and a fetch, with
no join, no status rule and no ordering logic of its own.

Why the arm exists at all: grid coverage and street coverage are different
denominators and never substitute for each other (Seattle: 54.3% grid vs 98.4%
street), and `scheduler assess-city` answers a deployment question from street
coverage precisely because grid points land on water, rail, parkland and roofs.
Until this arm, Panoramax was the one collectable provider that could answer
only the non-comparable question.

Three things a reader coming from ``collect_mapillary`` will otherwise assume
wrongly:

  * **The tiles are z15, not z14**, because the v1 ``pictures`` layer does not
    exist below it — so the same bbox costs ~4x Mapillary's tile count. Nothing
    here names a zoom; ``download_panoramax.tiles_for_bbox`` owns it, and the
    shared helper takes zoom as a REQUIRED argument so no caller can inherit
    another provider's default.
  * **There is no credential.** ``fetch_city_images_async`` takes no token
    parameter, and ``config.load_config("panoramax_streets")`` returns
    ``access_token=None`` rather than raising. A missing key cannot be the
    reason this walk fails.
  * **Pacing is the lowest tile rate in the repo** (30/min). Panoramax documents
    no rate limit and returns no ``X-RateLimit-*``/``Retry-After`` header, so the
    figure is a conservative default rather than a measured ceiling — see
    CLAUDE.md's provider-access rule before changing it. It is half the
    ``download_mapillary`` module's exported 60, but be careful quoting that as
    the margin: both Mapillary channels RUN at 40 (#292), so the real gap to a
    host that does document a limit is 25%, not 50%.

Cost tracks bbox AREA, not sample count or ``--spacing`` (pinned by a test), and
on a paired night it is ZERO: the census cache keys on (provider, city, bbox)
with no channel, variant or date in it (issue #290), so a Panoramax grid run's
tiles minutes earlier are this walk's census for no requests at all — and a
second ``--network-type`` is free on top of that.

The output is a METADATA-schema snapshot, one row per unique sample location,
exactly like the GSV, Mapillary and KartaView collectors' — so everything
downstream (``compute_streetwalk_coverage``, the coverage GeoJSON, the catalog
row, the manifest, the frontend) is shared, not duplicated.
"""

from __future__ import annotations

import gzip
import logging
import os
from datetime import UTC, datetime
from typing import Any

import pandas as pd

from streetscape_metadata_tracker.census import census_is_pano
from streetscape_metadata_tracker.checkpointing import CensusCache, observation_timestamp
from streetscape_metadata_tracker.download_panoramax import (
    DEFAULT_TILE_JITTER,
    DEFAULT_TILE_REQUESTS_PER_MINUTE,
    _panoramax_capture_dates,
    _points_in_tiles,
    build_empty_rows,
    build_image_rows,
    fetch_city_images_async,
    grid_bbox,
)
from streetscape_metadata_tracker.fileutils import load_city_csv_file
from streetscape_street_analyzer.census_walk import CensusWalkSpec
from streetscape_street_analyzer.census_walk import (
    build_streetwalk_rows as census_walk_rows,
)

logger = logging.getLogger(__name__)


# Panoramax's binding of the shared census→walk scorer.
#
# The date binding is REUSED from the grid run rather than rewritten here, which
# is the point of #323 having landed first: `_panoramax_capture_dates` already
# holds this provider's rule — an ISO8601-pinned parse (mixed precisions in one
# response otherwise null each other, #226 from the other direction) with a
# plausibility floor that drops the genuine 1970 epoch sentinel — and it already
# returns "" for a rejected date, which is the empty string CensusWalkSpec asks
# for and which becomes NO_DATE. An undated pano still covers; it ages nothing.
#
# A second implementation here would be a second date rule, and the grid and
# street artifacts for one city would disagree about the same picture.
PANORAMAX_WALK = CensusWalkSpec(
    capture_dates_for=_panoramax_capture_dates,
    build_image_rows=build_image_rows,
    build_empty_rows=build_empty_rows,
)


def build_streetwalk_rows(
    query_points: list[tuple[float, float, int, int]],
    census: pd.DataFrame,
    match_dist_m: float,
    query_timestamp: str,
    unmeasured_mask=None,
    unmeasured_desc=None,
) -> pd.DataFrame:
    """
    Score Panoramax census pictures against the walk's sample points.

    A thin binding of :func:`census_walk.build_streetwalk_rows`; the contract
    and the status vocabulary are documented there. Kept as a named function on
    this module rather than a bare partial so the collector below resolves it as
    a module global, which is what lets a test substitute it to simulate a tail
    failure after the census is already paid for.
    """
    return census_walk_rows(
        query_points,
        census,
        match_dist_m,
        query_timestamp,
        PANORAMAX_WALK,
        unmeasured_mask=unmeasured_mask,
        unmeasured_desc=unmeasured_desc,
    )


async def collect_panoramax_street_samples_async(
    query_points: list[tuple[float, float, int, int]],
    city,
    output_csv_gz_path: str,
    match_dist_m: float,
    connection_limit: int = 5,
    request_timeout: float = 30,
    max_requests_per_minute: int = DEFAULT_TILE_REQUESTS_PER_MINUTE,
    jitter: float = DEFAULT_TILE_JITTER,
    checkpoint_path: str | None = None,
    checkpoint_channel: str | None = None,
    checkpoint_variant: str | None = None,
    census_cache: CensusCache | None = None,
) -> dict[str, Any]:
    """
    Collect Panoramax street samples for a city and write the snapshot csv.gz.

    Returns the same contract as ``download_gsv.collect_points_async`` (``df``,
    ``filename_with_path``, ``api_requests``, ``started_at``, ``finished_at``)
    plus ``num_flat_images``, so ``collect.py`` treats every provider identically
    after the download step — and ``api_requests_total`` (the census's spend
    across resumes, for the ``street_walks`` row) and ``checkpoint_path`` (the
    caller's to discard once that row is committed).

    ``api_requests`` is THIS PROCESS's spend and ``api_requests_total`` is the
    crawl's across every resume. They are different numbers and the distinction
    is load-bearing: ``db.add_api_usage`` is additive and keyed by (date,
    provider), so a resumed walk reporting the whole crawl would charge last
    night's tiles against tonight's budget gate.

    **No ``access_token`` parameter, and that is not an omission.** Panoramax
    reads are unauthenticated, so there is no credential to thread; the
    ``panoramax_streets`` channel is declared credential-free in
    ``config.CHANNEL_ENV_VARS`` and its ``load_config`` returns
    ``access_token=None``. Accepting one here would invite a caller to believe
    a token was being honoured.

    The census is fetched over the city's **frozen grid bbox** — the same
    footprint the Panoramax grid run tiles — so a street walk can never reach
    imagery outside the area the city is defined to cover, and the two share one
    cache entry (issue #290). ``census_cache.reuse=False``
    (``--refetch-census``) opts out. When the census IS reused, every row's
    ``query_timestamp`` records when the provider was observed rather than when
    this process started, and the return carries
    ``census_fetched_by``/``census_fetched_at`` for the ``street_walks`` row.
    """
    started_at = datetime.now(UTC).isoformat()
    if not output_csv_gz_path.endswith(".csv.gz"):
        raise ValueError(f"output_csv_gz_path must end in .csv.gz, got: {output_csv_gz_path}")
    os.makedirs(os.path.dirname(os.path.abspath(output_csv_gz_path)), exist_ok=True)

    bbox = grid_bbox(
        city.center_lat, city.center_lon, city.grid_width_m, city.grid_height_m, city.step_m
    )
    fetched = await fetch_city_images_async(
        city.display_name,
        bbox,
        connection_limit=connection_limit,
        request_timeout=request_timeout,
        max_requests_per_minute=max_requests_per_minute,
        jitter=jitter,
        checkpoint_path=checkpoint_path,
        checkpoint_channel=checkpoint_channel,
        checkpoint_variant=checkpoint_variant,
        census_cache=census_cache,
    )
    # A reused census is stamped with when the provider was observed, a fresh
    # one with this process's clock; see checkpointing.observation_timestamp.
    query_timestamp = observation_timestamp(fetched, started_at)
    failed_tiles = fetched.get("failed_tiles") or []
    # THE TAIL IS WRAPPED BECAUSE THE CHECKPOINT CHANGES WHAT A CRASH HERE COSTS
    # (#239/#256, and #323 for this provider's checkpoint). Without one, a
    # failure below lost the spend with the process and the caller recorded
    # whatever the exception carried. With one, the checkpoint survives complete
    # and the NEXT invocation re-finalizes it for ZERO requests — so a tail
    # failure that carried no spend would land this census's tiles in no
    # api_usage row, EVER.
    try:
        # pop, not [] — `fetched` is a live local until this function returns, so
        # indexing it would keep the whole census resident past the `del` below,
        # through the join, the row build and the CSV write. It binds hard here:
        # a Panoramax leader city is 500,000+ pictures (issue #157).
        census = fetched.pop("census")
        num_flat_images = int((~census_is_pano(census)).sum())
        logger.info(
            "%s: %d Panoramax pictures (%d panos, %d flat) from %d tiles → scoring %d sample points",
            city.city_id,
            len(census),
            len(census) - num_flat_images,
            num_flat_images,
            fetched.get("tiles") or 0,
            len(query_points),
        )

        df = build_streetwalk_rows(
            query_points,
            census,
            match_dist_m,
            query_timestamp,
            # A tile nothing came back for leaves its samples UNKNOWN rather
            # than empty, exactly as this provider's GRID run does and as the
            # Mapillary (#259) and KartaView (#258) walks do; a clean fetch
            # passes None and pays nothing. Publishing an unswept sample as
            # ZERO_RESULTS records an absence nobody observed into an immutable
            # dated snapshot, and no later reader can tell it from a measured
            # one.
            #
            # Panoramax makes the distinction sharper than either sibling: a 404
            # is an EMPTY TILE here, not a failure, so a tile that is genuinely
            # missing imagery never reaches this list — everything in it is
            # ground the fetch really did not see.
            unmeasured_mask=(
                (lambda lats, lons: _points_in_tiles(lats, lons, failed_tiles))
                if failed_tiles
                else None
            ),
            unmeasured_desc=f"{len(failed_tiles)} undownloaded tile(s)",
        )
        del census
        # Straight into the gzip handle: to_csv() with no path builds the whole CSV
        # as a str and then a second copy as bytes, which at a big city's sample
        # count is pure duplication of a file being written out anyway (issue #157).
        with gzip.open(output_csv_gz_path, "wt", encoding="utf-8", newline="") as f:
            df.to_csv(f, index=False)

        # Read back through the shared loader so dtypes match the GSV path exactly.
        df = load_city_csv_file(output_csv_gz_path)
    except BaseException as e:
        e.api_requests = fetched["api_requests"]
        e.api_requests_total = fetched["api_requests_total"]
        raise
    return {
        "df": df,
        "filename_with_path": output_csv_gz_path,
        # This process's spend, for the additive daily ledger; the cumulative
        # figure below is for the street_walks row (#239's rule, #290's census).
        "api_requests": fetched["api_requests"],
        "api_requests_total": fetched["api_requests_total"],
        "checkpoint_path": fetched.get("checkpoint_path"),
        "census_fetched_by": fetched.get("census_fetched_by"),
        "census_fetched_at": fetched.get("census_fetched_at"),
        "census_reused": bool(fetched.get("census_reused")),
        "num_flat_images": num_flat_images,
        "started_at": started_at,
        "finished_at": datetime.now(UTC).isoformat(),
    }


__all__ = [
    "PANORAMAX_WALK",
    "build_streetwalk_rows",
    "collect_panoramax_street_samples_async",
]
