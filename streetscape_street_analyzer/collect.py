"""
CLI: road-walk street-coverage collection for a city (issue #99).

    python -m streetscape_street_analyzer.collect "Seattle, WA" \
        [--provider gsv|mapillary] \
        [--spacing 15] [--match-dist 25] [--network-type drive|all_public|...] \
        [--run-date YYYY-MM-DD] [--force] [--refresh] \
        [--connection-limit N] [--max-requests-per-minute R] \
        [--daily-budget N] [--estimate] [--data-dir DIR] [--db-path PATH]

A SECOND collection modality alongside the grid downloader. It walks the city's
frozen OSM network (issue #103), samples on-street points every ``--spacing``
metres along each edge, and finds the nearest pano at each point, yielding
**fractional** per-edge coverage. Unlike the grid downloader it scores only
on-street points, and its association to streets is by construction.

Both providers walk the SAME deterministic sample points, so their coverage
percentages are directly comparable — but they reach the imagery very
differently:

  * **gsv** issues one metadata request per sample location (a large city runs
    to a few hundred thousand), reusing the grid downloader's hardened request
    engine — rate limiter, OVER_QUERY_LIMIT retry, ``.downloading`` resume —
    via ``download_gsv.collect_points_async``.
  * **mapillary** has no per-point endpoint: it reads the z14 vector-tile
    census once — a cost set by the bbox area alone (catalog median 12 tiles,
    max 870), independent of spacing but not of city size — and joins it onto
    the sample points locally. See ``collect_mapillary``.

Each provider's requests are metered against its own ISOLATED key and ledger
channel (``GMAPS_STREETS_API_KEY``/``gsv_streets``,
``MAPILLARY_STREETS_ACCESS_TOKEN``/``mapillary_streets``, issue #141) so a road
walk can never exhaust the production grid collectors' quota.

Two dated artifacts are written next to the run (both published as ``*.gz``):
a raw sample snapshot ``..._streetwalk_sp{N}_{DATE}.csv.gz`` (METADATA schema,
one row per sampled location) and the derived per-edge coverage GeoJSON
``..._streetwalk_sp{N}_{DATE}_coverage.json.gz``.

**Which streets get walked** is set by ``--network-type``. The default
``drive`` is osmnx's motorized-public-roads filter, which excludes footways,
paths, pedestrian streets, cycleways, steps, tracks and every ``highway=service``
way (so alleys too). ``all_public`` is a strict superset that includes all of
them, letting the same city be compared across edge classes — GSV vs Mapillary
on park trails, say. Each network type is its OWN walk series: the artifacts
carry a network token (``drive`` emits none) and ``street_walks`` keys on it, so
walking a second network never overwrites the first.

Caveat on broad networks: ``--match-dist`` (default 25 m) is a plain proximity
test with no bearing check (#97/#98), so a footway mapped a few metres from its
road is scored covered by that road's pano. **Sidewalk coverage therefore reads
high.** Park trails are typically well beyond 25 m from any road, so they are
much less affected. The raw snapshot records each matched pano's coordinates, so
a tighter threshold can be re-cut offline without re-collecting.
"""

from __future__ import annotations

import argparse
import asyncio
import gzip
import json
import logging
import os
import sys
from datetime import UTC, date, datetime

from dotenv import find_dotenv, load_dotenv

from streetscape_metadata_tracker import config as cfg
from streetscape_metadata_tracker import db
from streetscape_metadata_tracker.analysis import detect_systemic_failure
from streetscape_metadata_tracker.checkpointing import (
    CENSUS_PROVIDERS,
    census_cache_probe,
    crawl_store_for,
    discard_checkpoint,
    frozen_bbox,
)
from streetscape_metadata_tracker.config import load_config
from streetscape_metadata_tracker.download_common import (
    DownloadError,
    HostUnavailableError,
    host_exit_code,
    jitter_fraction,
)
from streetscape_metadata_tracker.download_gsv import collect_points_async
from streetscape_metadata_tracker.download_mapillary import (
    DEFAULT_TILE_JITTER,
    DEFAULT_TILE_REQUESTS_PER_MINUTE,
    estimate_tile_count,
)
from streetscape_metadata_tracker.json_summarizer import generate_streetwalk_manifest
from streetscape_metadata_tracker.naming import (
    DEFAULT_NETWORK_TYPE,
    STREETWALK_NETWORK_TOKENS,
    generate_streetwalk_filename,
    streetwalk_coverage_filename,
)
from streetscape_metadata_tracker.paths import get_default_data_dir
from streetscape_metadata_tracker.walk_diff import compute_and_record_walk_diff

from .collect_mapillary import collect_mapillary_street_samples_async
from .download_street_network import fetch_street_edges
from .road_sampling import dedupe_query_points, generate_samples
from .street_coverage import (
    DEFAULT_MATCH_DIST_M,
    build_streetwalk_geojson,
    compute_streetwalk_coverage,
)

logger = logging.getLogger(__name__)

# Imagery provider → its ISOLATED street budget channel. The provider is what
# lands in street_walks/the artifacts (the imagery series); the channel is the
# api_usage ledger + credential the collection is metered against, kept
# separate from the grid collectors' so a road walk can never exhaust their
# quota (issue #141).
STREET_BUDGET_CHANNELS = {
    "gsv": "gsv_streets",
    "mapillary": "mapillary_streets",
}
DEFAULT_PROVIDER = "gsv"
DEFAULT_SPACING_M = 15

# Defaults mirror config/scheduler.toml's [providers.gsv_streets] / [download]
# so a manual run paces like the scheduled one. The gsv_streets key has its own
# ~30k/min GSV metadata quota; 24000 is ~80% client-side headroom. Mapillary has
# its own, far smaller pacing flag (a per-IP tile limit, issue #198), which is
# why this GSV figure must never be applied to it.
DEFAULT_MAX_REQUESTS_PER_MINUTE = 24_000


def _cached_census_marker(city, provider: str, args) -> dict | None:
    """
    Is a reusable census already on hand for this city? Marker only (issue #290).

    Two callers, and they must agree: ``--estimate`` (which prints the cost) and
    the ``--daily-budget`` pre-flight (which refuses to start when the cost does
    not fit). A walk whose census is free has to read as free in both, or the
    guard aborts exactly the collections the cache was built to make cheap.

    Deliberately conservative in three ways. It answers None for gsv, which has
    no census to share; None under ``--refetch-census``, so the flag prices the
    fetch it is about to force; and it reads the MARKER without validating the
    parts, because a planning pass must not become a disk sweep. That last makes
    a hit a strong hint rather than a promise -- the collector's own loader may
    still reject the entry and refetch, spending requests this pass priced at
    zero. That is the safe direction: the ledger records what is actually spent,
    and the alternative (pricing a free walk at full cost) is the failure this
    exists to prevent.
    """
    if provider not in CENSUS_PROVIDERS or args.refetch_census:
        return None
    return census_cache_probe(provider, city.city_id, frozen_bbox(city))


def run_collect(args: argparse.Namespace) -> int:
    data_dir = args.data_dir
    provider = args.provider
    budget_channel = STREET_BUDGET_CHANNELS[provider]
    db_path = args.db_path or db.get_default_db_path(data_dir)
    if not os.path.exists(db_path):
        logger.error("Catalog DB not found at %s", db_path)
        return 1

    conn = db.connect(db_path)
    try:
        city = db.resolve_city(conn, args.city)
        if city is None:
            logger.error("City not found in catalog: %s", args.city)
            return 1

        run_date = date.fromisoformat(args.run_date) if args.run_date else date.today()

        # Frozen network → edges (registers the #103 network row via conn).
        # A cached network costs nothing; a cold one goes to Overpass, which
        # meters by IP — so a busy or blocked host exits with that host's code
        # rather than an anonymous 1 (issue #208).
        try:
            edges = fetch_street_edges(
                city, data_dir, refresh=args.refresh, network_type=args.network_type, conn=conn
            )
        except HostUnavailableError as e:
            logger.error("Street network unavailable: %s", e)
            return host_exit_code(e)
        samples = generate_samples(edges, args.spacing)
        query_points = dedupe_query_points(samples)
        logger.info(
            "%s: %d edges → %d samples → %d unique locations (spacing %.1fm, %s)",
            city.city_id,
            len(edges),
            len(samples),
            len(query_points),
            args.spacing,
            provider,
        )

        if args.estimate:
            # Dry run: no key, no API calls — just report the work + cost.
            # Only GSV bills per sample location; Mapillary reads a tile census
            # whose size is set by the city's area, not by the sample count.
            cached = _cached_census_marker(city, provider, args)
            if cached is not None:
                cost = (
                    f"0 Mapillary tile requests (cached census fetched by "
                    f"{cached.get('fetched_by')}, crawl started "
                    f"{cached.get('crawl_started_at')})"
                )
            else:
                cost = (
                    f"{len(query_points)} unique GSV queries"
                    if provider == "gsv"
                    else f"~{estimate_tile_count(city.center_lat, city.center_lon, city.grid_width_m, city.grid_height_m, city.step_m)} Mapillary tile requests (independent of spacing)"
                )
            print(
                f"{city.city_id} [{args.network_type}]: {len(edges)} edges, "
                f"{len(samples)} samples, {cost} "
                f"(spacing={args.spacing}m). No requests issued (--estimate)."
            )
            return 0

        if len(query_points) == 0:
            logger.error("No on-street sample points generated; nothing to collect.")
            return 1

        # Keyed on the BUDGET CHANNEL, not the provider: a walk and a grid run
        # of one city sweep the identical frozen bbox, so a path derived from
        # geometry alone would let them resume each other's census -- with the
        # spend landing in the wrong api_usage ledger, under the wrong
        # credential. Built after --estimate returns, so an estimate still
        # creates nothing and needs no token.
        #
        # AND ON THE NETWORK TYPE, which the channel does NOT separate: 'drive'
        # and 'all_public' are different series over the SAME frozen bbox in the
        # SAME street channel (which is why generate_streetwalk_filename carries
        # the network token). Without it, a walk that dies after its census but
        # before register_street_walk leaves a checkpoint the other network
        # type's walk re-finalizes for zero requests, writing the first crawl's
        # api_requests_total into the second's row. Both walks read the same
        # tiles, so the census is identical and nothing downstream would show it.
        #
        # THE CACHE PATH IS THE MIRROR IMAGE (issue #290) and is built from the
        # PROVIDER, with neither the channel nor the network type in it. The
        # paragraph above ends "both walks read the same tiles, so the census is
        # identical and nothing downstream would show it" -- that identity is a
        # HAZARD for a checkpoint, whose spend belongs to exactly one crawl, and
        # the whole POINT for a cache, which holds a finished observation any
        # consumer may republish. The grid run writes the entry minutes earlier
        # on a paired night; this walk and the other --network-type both read it
        # for zero requests.
        #
        # Both, and the "is this a census provider" test, come from ONE
        # derivation (crawl_store_for), the same one the grid run uses, so the
        # writer and this reader key the identical lattice. (None, None) for gsv.
        checkpoint_path, census_cache = crawl_store_for(
            provider,
            city,
            budget_channel,
            variant=args.network_type,
            reuse=not args.refetch_census,
            # The snapshot date being written: an entry observed after it is
            # refused rather than published into a snapshot from its past.
            run_date=run_date,
        )

        # The provider and network-type tokens are what keep same-night walks
        # apart. Both providers walk the SAME sample points and the scheduler
        # runs them on one run_date; and one frozen bbox yields both a 'drive'
        # network and a much larger 'all_public' one. Without either token the
        # second collection would find the first's snapshot already on disk and
        # skip as a silent no-op reported as success.
        stem = generate_streetwalk_filename(
            city.city_id,
            city.grid_width_m,
            city.grid_height_m,
            city.step_m,
            args.spacing,
            run_date,
            provider=provider,
            network_type=args.network_type,
        )
        csv_name = stem + ".csv.gz"
        coverage_name = streetwalk_coverage_filename(csv_name)
        out_csv = os.path.join(data_dir, csv_name)
        out_coverage = os.path.join(data_dir, coverage_name)

        # Immutable-per-date snapshot: refuse to overwrite unless --force, which
        # clears the prior artifacts so this date can be re-collected.
        if os.path.exists(out_csv):
            if not args.force:
                logger.info(
                    "Streetwalk snapshot already exists for %s [%s/%s] on %s; skipping "
                    "(use --force to re-collect, or a different --run-date).",
                    city.city_id,
                    provider,
                    args.network_type,
                    run_date,
                )
                return 0
            logger.warning("--force: removing existing snapshot %s", out_csv)
            os.remove(out_csv)
            for stale in (out_csv[: -len(".gz")] + ".downloading", out_csv + ".rejected"):
                if os.path.exists(stale):
                    os.remove(stale)

        # Pre-flight budget guard against the isolated street ledger. Only GSV
        # spends per sample location; a Mapillary walk costs its tile census.
        #
        # A cached census costs nothing, so the guard must not abort on it
        # (#290). Without this, the cheapest possible walk -- one whose census
        # the grid run already paid for -- is exactly the one a nearly-exhausted
        # street budget refuses, and the reuse never happens on the nights it
        # helps most. The probe reads the marker only, so it is honest about
        # "cost" and deliberately not about "will validate"; an entry the
        # collector then rejects costs a re-fetch that was not budgeted, which
        # is the safe direction (the ledger still records what was spent).
        estimated_requests = (
            len(query_points)
            if provider == "gsv"
            else 0
            if _cached_census_marker(city, provider, args) is not None
            else estimate_tile_count(
                city.center_lat,
                city.center_lon,
                city.grid_width_m,
                city.grid_height_m,
                city.step_m,
            )
        )
        if args.daily_budget is not None:
            already = db.get_api_usage(conn, run_date, provider=budget_channel)
            if already + estimated_requests > args.daily_budget:
                logger.error(
                    "%s daily budget %d would be exceeded: %d already spent "
                    "+ %d estimated requests. Aborting.",
                    budget_channel,
                    args.daily_budget,
                    already,
                    estimated_requests,
                )
                return 1

        # Load .env so the street credential is picked up the same way the grid
        # CLI loads its own (cli.py). Done after the --estimate return so a dry
        # run needs no key at all.
        load_dotenv()
        cfg.warn_if_credentials_world_readable(find_dotenv(usecwd=True))
        config = load_config(budget_channel)

        try:
            if provider == "gsv":
                dict_results = asyncio.run(
                    collect_points_async(
                        query_points,
                        config["api_key"],
                        out_csv,
                        city_label=city.display_name,
                        batch_size=args.batch_size,
                        connection_limit=args.connection_limit,
                        request_timeout=args.timeout,
                        max_retries=args.max_retries,
                        max_requests_per_minute=args.max_requests_per_minute,
                    )
                )
            else:
                dict_results = asyncio.run(
                    collect_mapillary_street_samples_async(
                        query_points,
                        city,
                        config["access_token"],
                        out_csv,
                        match_dist_m=args.match_dist,
                        connection_limit=args.connection_limit,
                        request_timeout=args.timeout,
                        max_requests_per_minute=args.mapillary_max_requests_per_minute,
                        jitter=args.mapillary_jitter,
                        checkpoint_path=checkpoint_path,
                        checkpoint_channel=budget_channel,
                        checkpoint_variant=args.network_type,
                        census_cache=census_cache,
                    )
                )
        except Exception as e:
            # Failed crawls still spent real requests; record them so a later
            # budget check doesn't overspend the street channel. This runs for
            # a host-unavailable failure too (HostUnavailableError IS a
            # DownloadError) — a Mapillary walk blocked partway through the
            # tile census has spent real requests, and a busy-lock failure has
            # spent none, so the same accounting is correct for both.
            #
            # `except Exception`, not DownloadError, and cli.py's register_run
            # arm says why in the same words: a Mapillary walk whose POST-FETCH
            # tail dies (ENOSPC on the gzip write, a read-back failure) raises
            # OSError/ValueError with the spend attached by the collector — and
            # because its checkpoint survives and the resume re-finalizes for ~0
            # new requests, a spend missed here would never land in ANY ledger
            # row (PR #251 review, and #256 for this path). Narrower than that,
            # the tail's requests were simply lost with the process and a re-run
            # bought them again, so nothing went unrecorded.
            spent = getattr(e, "api_requests", 0)
            if spent:
                db.add_api_usage(conn, run_date, spent, provider=budget_channel)
                logger.warning(
                    "Recorded %d %s requests spent by the failed crawl", spent, budget_channel
                )
            if isinstance(e, DownloadError):
                logger.error("Collection failed: %s", e)
            else:
                # Not a provider condition: log the traceback, since this is the
                # only record of it (run_collect's caller has no handler).
                logger.exception("Collection failed after the fetch: %s", e)
            if isinstance(e, HostUnavailableError):
                return host_exit_code(e)
            return 1

        df = dict_results["df"]
        db.add_api_usage(conn, run_date, dict_results["api_requests"], provider=budget_channel)

        # Reject a crawl dominated by credential/quota denials (cf. the grid
        # pipeline): it says nothing about coverage and must not be cataloged.
        failure_reason = detect_systemic_failure(df)
        if failure_reason:
            rejected_path = f"{out_csv}.rejected"
            os.replace(out_csv, rejected_path)
            logger.error(
                "Streetwalk run rejected, not cataloged: %s. Raw responses kept at %s",
                failure_reason,
                rejected_path,
            )
            return 1

        covered = compute_streetwalk_coverage(
            edges,
            samples,
            df,
            run_date.isoformat(),
            provider=provider,
            match_dist_m=args.match_dist,
        )
        geojson = build_streetwalk_geojson(
            covered,
            city_id=city.city_id,
            provider=provider,
            run_date=run_date.isoformat(),
            spacing_m=args.spacing,
            match_dist_m=args.match_dist,
            source_csv=csv_name,
            network_type=args.network_type,
        )
        with gzip.open(out_coverage, "wt", encoding="utf-8") as fh:
            json.dump(geojson, fh)

        totals = geojson["properties"]["metadata"]["totals"]
        walk_id = db.register_street_walk(
            conn,
            city_id=city.city_id,
            run_date=run_date,
            csv_filename=csv_name,
            provider=provider,
            coverage_filename=coverage_name,
            network_type=args.network_type,
            spacing_m=args.spacing,
            match_dist_m=args.match_dist,
            sample_points=len(samples),
            edges_total=totals["edges"],
            edges_fully_covered=totals["edges_fully_covered"],
            mean_edge_coverage=totals["mean_edge_coverage"],
            coverage_pct_by_length=totals["coverage_pct_by_length"],
            coverage_pct_by_length_any=totals["coverage_pct_by_length_any"],
            coverage_by_highway=json.dumps(
                geojson["properties"]["metadata"]["coverage_by_highway"]
            ),
            # Absolute street length (v12), fraction-weighted like the
            # percentages beside it. All indexed, not `.get()`-guarded: this
            # artifact was just built above by the CURRENT
            # summarize_streetwalk_coverage, which emits all four
            # unconditionally — a pre-#116 frame lacking coverage_fraction_any
            # still yields a length, because that function synthesizes the
            # column from coverage_fraction before summing. The salvage and
            # backfill paths DO guard the any-imagery length, for the reason
            # that does not apply here: they read artifacts off disk, and ones
            # written between #99 and the any-imagery split carry length_km
            # without length_km_covered_any.
            length_km=totals["length_km"],
            length_km_covered=totals["length_km_covered"],
            length_km_covered_any=totals["length_km_covered_any"],
            median_covered_age_years=totals["median_covered_age_years"],
            # The CRAWL's cost across resumes, not this process's: the row
            # describes the walk. add_api_usage above is fed the per-process
            # figure because it is additive and keyed by (date, provider) --
            # see the same split in cli.py's register_run (#239, #256).
            api_requests=dict_results.get("api_requests_total", dict_results["api_requests"]),
            # Which channel actually paid for this census and when it observed
            # the provider (issue #290). A walk on a paired night costs 0, and
            # these are what make that 0 legible rather than alarming; NULL for
            # gsv, which has no census to share.
            census_fetched_by=dict_results.get("census_fetched_by"),
            census_fetched_at=dict_results.get("census_fetched_at"),
            started_at=dict_results.get("started_at"),
            finished_at=dict_results.get("finished_at") or datetime.now(UTC).isoformat(),
        )

        # Only now is the checkpoint spent: the street_walks row is committed,
        # so the crawl's cost is durable and every remaining failure is cheap (a
        # lost diff or manifest rebuilds from artifacts already on disk).
        # Discarding before this line would make a register failure cost the
        # whole census again -- the placement cli.py already reasons through,
        # and the walk path had no discard at all until #256.
        if dict_results.get("checkpoint_path"):
            discard_checkpoint(dict_results["checkpoint_path"])

        # Diff against the previous walk of this series (issue #101) — before
        # the manifest refresh below so the manifest immediately advertises
        # the change block. A no-op on every first walk. Never fail a fully
        # paid-for, already-cataloged crawl over its diff: the pair can be
        # re-diffed offline.
        change = None
        try:
            change = compute_and_record_walk_diff(
                conn,
                data_dir=data_dir,
                city_id=city.city_id,
                walk_id=walk_id,
                run_date=run_date,
                provider=provider,
                network_type=args.network_type,
                spacing_m=float(args.spacing),
                match_dist_m=float(args.match_dist),
                fc_new=geojson,
            )
        except Exception:
            logger.exception("Walk diff failed; continuing (walk is cataloged)")

        # Refresh the sidecar manifest so the city page can actually find this
        # artifact (issue #155). The collector is a manual CLI outside the
        # scheduler, and the manifest is only otherwise rebuilt by a nightly
        # run-due or an explicit regenerate-aggregate — without this a freshly
        # collected walk would publish but stay invisible until the next one of
        # those. Catalog-driven and cheap (one small query, no artifact reads).
        manifest = generate_streetwalk_manifest(conn, data_dir)

        logger.info(
            "Wrote %s and %s (manifest: %d walks)",
            out_csv,
            out_coverage,
            len(manifest["walks"]),
        )
        # The any-imagery number only says something new for Mapillary; for GSV
        # it is the 360° number by construction, so don't print it twice.
        any_note = (
            ""
            if totals["coverage_pct_by_length_any"] == totals["coverage_pct_by_length"]
            else f", {totals['coverage_pct_by_length_any']}% including flat imagery"
        )
        unit = "GSV queries" if provider == "gsv" else "Mapillary tile requests"
        print(
            f"{city.city_id} [streetwalk {provider}/{args.network_type} {run_date}]: "
            f"{len(samples)} samples over {totals['edges']} edges "
            f"({dict_results['api_requests']} {unit}); "
            f"mean edge coverage {totals['mean_edge_coverage']:.3f}, "
            f"{totals['coverage_pct_by_length']}% of street-km covered"
            f"{any_note} "
            f"({totals['edges_fully_covered']}/{totals['edges']} edges fully covered)"
        )
        if change is not None:
            delta = change["coverage_pct_by_length_delta"]
            delta_note = f"{delta:+.1f} pp by length" if delta is not None else "delta n/a"
            print(
                f"  since {change['from']}: {delta_note}, "
                f"{change['edges_gained_coverage']} edges gained / "
                f"{change['edges_lost_coverage']} lost coverage, "
                f"{change['nearest_pano_date_changed']} pano-date changes"
            )
        return 0
    finally:
        conn.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Road-walk street-coverage collection (issue #99)."
    )
    parser.add_argument("city", help="City query or catalog slug (e.g. 'Seattle, WA')")
    parser.add_argument(
        "--provider",
        choices=sorted(STREET_BUDGET_CHANNELS),
        default=DEFAULT_PROVIDER,
        help=(
            "Imagery provider to walk (default: gsv). Each is metered against "
            "its own isolated street budget channel (gsv_streets / "
            "mapillary_streets); gsv costs one request per sample point, "
            "mapillary reads a tile census whose cost depends on the city's "
            "bbox area (catalog median 12 tiles, max 870) but not on spacing. "
            "Use --estimate to price a city before spending."
        ),
    )
    parser.add_argument(
        "--spacing",
        # Integer metres only: the artifact filename encodes spacing as an
        # integer `sp{N}` token (naming.generate_streetwalk_filename), so a
        # fractional spacing would silently truncate (12.5 -> "sp12") and
        # misrepresent the run. Reject it at the CLI instead.
        type=int,
        default=DEFAULT_SPACING_M,
        help=f"Along-edge sample spacing in whole metres (default: {DEFAULT_SPACING_M})",
    )
    parser.add_argument(
        "--match-dist",
        type=float,
        default=DEFAULT_MATCH_DIST_M,
        help=f"Max sample-to-pano distance in metres (default: {DEFAULT_MATCH_DIST_M})",
    )
    parser.add_argument(
        "--network-type",
        default=DEFAULT_NETWORK_TYPE,
        choices=sorted(STREETWALK_NETWORK_TOKENS),
        help=(
            "OSM network to walk (default: drive — motorized public roads only). "
            "'all_public' adds alleys, footways, park paths, pedestrian streets, "
            "cycleways and steps. Each network type is its own walk series: the "
            "artifacts carry a network token and the catalog keys on it, so a "
            "broad walk never overwrites a drive walk"
        ),
    )
    parser.add_argument(
        "--run-date",
        default=None,
        help="Collection date YYYY-MM-DD (default: today)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-collect this run-date, removing any existing snapshot first",
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="Re-download (re-freeze) the OSM network instead of using the cache",
    )
    parser.add_argument(
        "--estimate",
        action="store_true",
        help="Report edge/sample/query counts and exit — no API key or requests needed",
    )
    parser.add_argument(
        "--refetch-census",
        action="store_true",
        help="""Ask Mapillary again instead of reusing the census the grid run
             (or the other --network-type) already fetched for this city and
             bbox (issue #290). DELIBERATELY SEPARATE FROM --force: --force
             clears this run date's artifacts, and a walk whose tail died after
             writing its CSV is re-run with --force and must re-finalize for
             zero requests rather than re-pay a census it already bought.""",
    )
    parser.add_argument("--batch-size", type=int, default=100)
    parser.add_argument("--connection-limit", type=int, default=50)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--max-retries", type=int, default=3)
    parser.add_argument(
        "--max-requests-per-minute",
        type=int,
        default=DEFAULT_MAX_REQUESTS_PER_MINUTE,
        help=(
            "Client-side pacing cap for the gsv_streets key "
            f"(default: {DEFAULT_MAX_REQUESTS_PER_MINUTE}); <= 0 disables pacing"
        ),
    )
    parser.add_argument(
        "--mapillary-max-requests-per-minute",
        type=int,
        default=DEFAULT_TILE_REQUESTS_PER_MINUTE,
        help=(
            "Client-side pacing cap for Mapillary vector-tile requests "
            f"(default: {DEFAULT_TILE_REQUESTS_PER_MINUTE}); <= 0 disables "
            "pacing. Separate from the gsv_streets cap because Mapillary's "
            "tile CDN rate-limits per IP, so exceeding it blocks every "
            "Mapillary channel on this host at once (issue #198)"
        ),
    )
    parser.add_argument(
        "--mapillary-jitter",
        type=jitter_fraction,
        default=DEFAULT_TILE_JITTER,
        help=(
            "Randomize the gap between Mapillary tile requests: a floor of "
            "(1 - this) x the mean gap plus an exponential tail scaled to this, "
            "so the mean rate is unchanged and this is the gaps' coefficient of "
            f"variation (default: {DEFAULT_TILE_JITTER}; 0 restores an exact "
            "cadence). Rate and daily volume were both falsified as the per-IP "
            "block trigger, so the metronomic request pattern is the axis under "
            "test (issue #292)"
        ),
    )
    parser.add_argument(
        "--daily-budget",
        type=int,
        default=None,
        help=(
            "Today's FULL ceiling for this provider's street budget channel "
            "(gsv_streets / mapillary_streets). Abort when the ledger's spend "
            "so far plus this collection's estimated requests would exceed it — "
            "pass the whole daily budget, not what is left of it"
        ),
    )
    parser.add_argument("--data-dir", default=get_default_data_dir(), help="Data directory")
    parser.add_argument(
        "--db-path",
        default=None,
        help="Catalog DB path (default: the DB inside --data-dir)",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
    )
    return parser


def main(argv: list | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    return run_collect(args)


if __name__ == "__main__":
    sys.exit(main())
